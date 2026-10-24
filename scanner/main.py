"""One scan: snapshot buzz -> find building buzz -> enrich -> alert -> evaluate past alerts."""
import json
import os
import statistics
from datetime import datetime, timezone, timedelta

import requests

from . import config, sources, extras, review
from .storage import Store

NOW = datetime.now(timezone.utc)
EXTRA_STATUS = {}
REVIEW_STATUS = {}


def is_etf(name):
    return any(w.lower() in (name or "").lower() for w in config.ETF_WORDS)


def smoothed(now, base):
    return (now + 2) / (base + 2)


# ---------------- 1. snapshot ----------------
def collect(store):
    main = sources.apewisdom(config.APEWISDOM_FILTERS_MAIN, config.APEWISDOM_MAIN_PAGES)
    subs = {}
    for f in config.APEWISDOM_SUB_FILTERS:
        rows = sources.apewisdom(f, 1)
        if rows:
            subs[f] = {r["ticker"]: r for r in rows}
    snap = {"ts": NOW.isoformat(timespec="minutes"), "main": {r["ticker"]: r for r in main}}
    compact = {"ts": snap["ts"],
               "main": {r["ticker"]: [r["mentions"], r["mentions_24h_ago"]] for r in main if r["mentions"] >= 2},
               "subs": {f: {t: [r["mentions"], r["mentions_24h_ago"]] for t, r in d.items() if r["mentions"] >= 1}
                        for f, d in subs.items()}}
    store.append_snapshot(compact)
    return snap, subs


# ---------------- 2. buzz metrics ----------------
def buzz_rows(snap, subs, history):
    """history: list of older snapshots (dicts) sorted by ts."""
    out = {}
    tickers = set(snap["main"]) | set((subs.get("pennystocks") or {}).keys())
    three_h_ago = NOW - timedelta(hours=3)
    week_ago = NOW - timedelta(days=7)
    novelty_from = NOW - timedelta(days=config.NOVELTY_DAYS)
    hist_days = sorted({h["ts"][:10] for h in history})
    long_history = bool(history) and datetime.fromisoformat(history[0]["ts"]) <= NOW - timedelta(days=config.NOVELTY_MIN_HISTORY_DAYS)
    today = NOW.date().isoformat()
    for t in tickers:
        if not t or t in config.LARGE_CAPS:
            continue
        r = snap["main"].get(t) or (subs.get("pennystocks") or {}).get(t)
        name = r.get("name", "")
        if is_etf(name):
            continue
        m_now = r["mentions"]
        m_24 = r["mentions_24h_ago"]
        # history-based: value ~3h ago and 7-day baseline of the rolling-24h count
        past3, base_vals, day_peak, old_max = None, [], {}, 0
        for h in history:
            ts = datetime.fromisoformat(h["ts"])
            mv = h["main"].get(t)
            if mv is None:
                mv = (h["subs"].get("pennystocks") or {}).get(t)
            v = mv[0] if mv else 0
            if ts <= three_h_ago:
                past3 = v
            if week_ago <= ts <= NOW - timedelta(hours=24):
                base_vals.append(v)
            if novelty_from <= ts <= NOW - timedelta(hours=24):
                old_max = max(old_max, v)
            day_peak[h["ts"][:10]] = max(day_peak.get(h["ts"][:10], 0), v)
        day_peak[today] = max(day_peak.get(today, 0), m_now)
        days = (hist_days + [today]) if (not hist_days or hist_days[-1] != today) else hist_days
        last_days = days[-config.RISING_DAYS:]
        seq = [day_peak.get(d, 0) for d in last_days]
        rising = (len(seq) == config.RISING_DAYS and all(a < b for a, b in zip(seq, seq[1:]))
                  and seq[-1] >= config.PENNY_MIN_MENTIONS)
        novel = long_history and old_max < 2
        baseline = statistics.median(base_vals) if len(base_vals) >= 6 else m_24
        spread = sorted(f for f, d in subs.items() if (d.get(t) or {}).get("mentions", 0) >= 1)
        p = (subs.get("pennystocks") or {}).get(t)
        main_gate = (t in snap["main"] and m_now >= config.MAIN_MIN_MENTIONS and m_now - m_24 >= config.MAIN_MIN_ADD
                     and smoothed(m_now, m_24) >= config.MIN_SMOOTHED_GROWTH)
        penny_gate = bool(p and p["mentions"] >= config.PENNY_MIN_MENTIONS
                          and p["mentions"] - p["mentions_24h_ago"] >= config.PENNY_MIN_ADD
                          and smoothed(p["mentions"], p["mentions_24h_ago"]) >= config.MIN_SMOOTHED_GROWTH)
        out[t] = {"ticker": t, "name": name, "m_now": m_now, "m_24h_ago": m_24, "m_3h_ago": past3,
                  "baseline7d": baseline, "vel3h": (m_now - past3) if past3 is not None else None,
                  "accel": round(smoothed(m_now, baseline), 2), "spread": spread,
                  "gate": main_gate or penny_gate, "word": t in config.WORD_TICKERS,
                  "rising": rising, "rise_seq": seq if rising else None, "novel": novel}
    return out


def score(b, rd, st_trend, st, fund, x=None):
    """x: extra signals dict (price, watchers, borrow, xueqiu, youtube, promoted)."""
    x = x or {}
    pts, why = 0, []
    if b["accel"] >= 3:
        pts += 2; why.append(f"האצה x{b['accel']} מול הבסיס")
    elif b["accel"] >= 2:
        pts += 1; why.append(f"האצה x{b['accel']}")
    if b.get("vel3h") is not None and b["vel3h"] >= 3:
        pts += 1; why.append(f"+{b['vel3h']} אזכורים ב-3 שעות")
    n_spread = len(b["spread"]) + (len(rd.get("subs6", [])) if rd else 0)
    if n_spread >= 3:
        pts += 2; why.append(f"פיזור ב-{n_spread} פורומים")
    elif n_spread >= 2:
        pts += 1; why.append(f"פיזור ב-{n_spread} פורומים")
    if rd and rd.get("authors6", 0) >= 4:
        pts += 1; why.append(f"{rd['authors6']} כותבים שונים ברדיט ב-6 שעות")
    if b.get("rising"):
        pts += 1; why.append("שיח עולה {} ימים ברציפות ({})".format(len(b["rise_seq"]), "→".join(map(str, b["rise_seq"]))))
    if b.get("novel"):
        pts += 1; why.append(f"חדשה בשיח (לא דוברה {config.NOVELTY_DAYS} יום)")
    if rd and rd.get("m24", 0) >= config.ONE_AUTHOR_MIN_MENTIONS and rd.get("authors24", 99) <= config.ONE_AUTHOR_MAX_AUTHORS:
        pts -= 1; why.append(f"⚠️ {rd['m24']} אזכורים ברדיט מ-{rd['authors24']} כותבים בלבד")
    if b["ticker"] in st_trend:
        pts += 1; why.append("בטרנד של סטוקטוויטס")
    if st and st.get("msgs_6h", 0) >= 10:
        pts += 1; why.append(f"{st['msgs_6h']} הודעות בסטוקטוויטס ב-6 שעות")
    if fund:
        if (fund.get("short_float_pct") or 0) >= 20:
            pts += 1; why.append(f"שורט {fund['short_float_pct']:.0f}%")
        if fund.get("float_m") and fund["float_m"] < 50:
            pts += 1; why.append(f"פלואט {fund['float_m']:.1f}M")
    rv = (x.get("price") or {}).get("rel_vol")
    if rv and rv >= 4:
        pts += 2; why.append(f"נפח פי {rv:.1f} מהממוצע")
    elif rv and rv >= 2:
        pts += 1; why.append(f"נפח פי {rv:.1f} מהממוצע")
    wa, wp = x.get("watch_added"), x.get("watch_pct")
    if wa and wp is not None and wa >= 50 and wp >= 10:
        pts += 2; why.append(f"+{wa} עוקבים בסטוקטוויטס ב-24ש ({wp:+.0f}%)")
    elif wa and wp is not None and wa >= 30 and wp >= 3:
        pts += 1; why.append(f"+{wa} עוקבים בסטוקטוויטס ב-24ש ({wp:+.0f}%)")
    br = x.get("borrow")
    if br:
        if br["fee"] >= 20:
            pts += 1; why.append(f"עמלת השאלה {br['fee']:.0f}%")
        if (br.get("fee_5d") and br["fee"] >= 10 and br["fee"] >= 2 * br["fee_5d"]) or \
           (br.get("available_5d") and br["available_5d"] > 1000 and br["available"] <= 0.5 * br["available_5d"]):
            pts += 1; why.append("השאלה מתייקרת / מניות לשורט אוזלות")
    if x.get("xueqiu"):
        pts += 1; why.append(f"חמה בסין (Xueqiu #{x['xueqiu']})")
    yt = x.get("youtube")
    if yt and yt.get("channels", 0) >= 2:
        pts += 1; why.append(f"{yt['videos']} סרטוני YouTube מ-{yt['channels']} ערוצים ב-12ש")
    sec = x.get("sec") or {}
    if sec.get("catalysts"):
        pts += 1; why.append("דיווח טרי ב-SEC: " + ", ".join(sec["catalysts"][:2]))
    rs_age = x.get("rsplit_age")
    if rs_age is not None and rs_age <= config.RSPLIT_PENALTY_DAYS:
        pts -= 1; why.append(f"⚠️ איחוד מניות / שינוי יחס ADS לפני {rs_age} ימים")
    if x.get("promoted"):
        pts -= 3; why.append("⚠️ קידום בתשלום (stockpromoters)")
    if b["word"]:
        pts -= 2; why.append("חשד להתראת שווא (מילה)")
    return pts, why


# ---------------- 3. alerts ----------------
def send_telegram(text):
    if not (config.TELEGRAM_BOT_TOKEN and config.TELEGRAM_CHAT_ID):
        return False
    try:
        r = requests.post(f"https://api.telegram.org/bot{config.TELEGRAM_BOT_TOKEN}/sendMessage",
                          json={"chat_id": config.TELEGRAM_CHAT_ID, "text": text,
                                "disable_web_page_preview": True}, timeout=20)
        if r.status_code != 200:
            sources.ERRORS.append(f"Telegram HTTP {r.status_code}: {r.text[:120]}")
        return r.status_code == 200
    except Exception as e:
        sources.ERRORS.append(f"Telegram error: {e}")
        return False


def track_record(alerts):
    done, seen = [], set()
    for a in alerts:  # one entry per ticker per day (a re-alert is not a new trade)
        k = (a["ticker"], a["ts"][:10])
        if a.get("outcome", {}).get("days", 0) >= 1 and k not in seen:
            seen.add(k); done.append(a)
    if not done:
        return "עדיין אין היסטוריה של התראות"
    hit30 = sum(1 for a in done if (a["outcome"].get("max_close_pct") or -99) >= 30)
    med = statistics.median([a["outcome"].get("last_pct") or 0 for a in done])
    return f"עד כה: {len(done)} התראות, {hit30} עלו 30%+ בסגירה ממחיר ההתראה, חציון {med:+.1f}%"


def format_alert(c, record):
    p = c["price"]
    lines = [f"🔔 באז מתהווה: ${c['ticker']} (ציון {c['score']})",
             f"מחיר {p['price']} ({p['move_pct']:+.1f}% מול סגירה {p['prev_close']})",
             f"אזכורים 24ש: {c['m_24h_ago']} → {c['m_now']}" + (f" | 3ש: {c['m_3h_ago']} → {c['m_now']}" if c.get("m_3h_ago") is not None else "")]
    f = c.get("fund") or {}
    if f:
        lines.append("שווי ${:.0f}M | פלואט {} | שורט {} | ימי כיסוי {}".format(
            f.get("market_cap_m") or 0,
            f"{f['float_m']:.1f}M" if f.get("float_m") else "?",
            f"{f['short_float_pct']:.0f}%" if f.get("short_float_pct") is not None else "?",
            f.get("days_to_cover") if f.get("days_to_cover") is not None else "?"))
    rd_ = (p or {}).get("readiness") or {}
    if rd_:
        lines.append(f"מוכנות מהמסחר: {rd_['points']} ({'; '.join(rd_['why']) or 'אין סימנים'})")
        for r in rd_.get("risk", []):
            lines.append(f"⚠️ {r}")
    br = c.get("borrow") or {}
    if br:
        lines.append(f"השאלה: עמלה {br['fee']:.1f}%, זמינות {br['available']:,} מניות")
    lines.append("למה: " + "; ".join(c["why"]))
    if c.get("reddit", {}).get("sample"):
        lines.append(c["reddit"]["sample"])
    if c.get("dilution") is None:
        lines.append("⚠️ לא נבדקו דיווחי הנפקה (אין CIK)")
    inv = review.invalidation_level(p)
    lines.append(f"ביטול: ירידה מתחת ל-{inv}" + (" (סגירה קודמת)" if inv == round(p["prev_close"], 4) else
                                                 f" ({config.INVALIDATION_FALLBACK_PCT}% מתחת למחיר ההתראה)"))
    lines.append("אשלח הודעת המשך ב-+30%, ב-+60% או בירידה מתחת לרמת הביטול.")
    lines.append(record)
    lines.append("מידע לסריקה, לא המלצת השקעה.")
    return "\n".join(lines)


# ---------------- 4. evaluate past alerts ----------------
def evaluate(alerts):
    for a in alerts:
        o = a.setdefault("outcome", {})
        if o.get("final"):
            continue
        bars = sources.daily_bars(a["ticker"], "1mo")
        d0 = a["ts"][:10]
        after = [b for b in bars if b["date"] >= d0]
        if not after:
            continue
        ep = a["price"]["price"]
        o["days"] = len(after)
        o["last_close"] = after[-1]["close"]
        o["last_pct"] = round((after[-1]["close"] / ep - 1) * 100, 1)
        o["max_close_pct"] = round((max(b["close"] for b in after[:5]) / ep - 1) * 100, 1)
        o["max_high_pct"] = round((max((b["high"] or 0) for b in after[:5]) / ep - 1) * 100, 1)
        o["d1_pct"] = round((after[0]["close"] / ep - 1) * 100, 1)
        if len(after) >= 5:
            o["final"] = True


# ---------------- main ----------------
def run():
    store = Store(config.DATA_DIR)
    known = set(sources.cik_map(store.path("cik.json")).keys())
    snap, subs = collect(store)
    if not snap["main"]:
        sources.ERRORS.append("ApeWisdom all-stocks empty – scan skipped")
    history = store.load_history(days=8)
    buzz = buzz_rows(snap, subs, history)

    rd_counts, rd_ok = sources.reddit_counts(known) if known else ({}, False)
    st_trend = sources.stocktwits_trending()
    xq = extras.xueqiu_hot_us()
    promoted = extras.promoted_tickers(known) if known else set()
    yt = extras.youtube_mentions(known) if known else {}
    watch_hist = store.load_json("st_watch.json", {})
    for t, v in st_trend.items():  # build watcher history for trending names at no extra cost
        if v.get("watchlist_count"):
            extras.watchers_growth(watch_hist, t, v["watchlist_count"])
    profiles = store.load_json("profiles.json", {})
    borrow_hist = store.load_json("borrow_hist.json", {})
    EXTRA_STATUS.update(xq=xq, promoted=promoted)

    # candidates: passed the mention gate, or strong reddit-only spread
    cands = [b for b in buzz.values() if b["gate"]]
    for t, r in rd_counts.items():
        if t not in buzz and r["authors6"] >= 4 and len(r["subs6"]) >= 2 and t not in config.WORD_TICKERS:
            cands.append({"ticker": t, "name": "", "m_now": r["m24"], "m_24h_ago": 0, "m_3h_ago": None,
                          "baseline7d": 0, "vel3h": None, "accel": 0, "spread": [], "gate": True, "word": False})
    # skip tickers already known to be too large (cache refreshed weekly) so the slots go to small caps
    caps = store.load_json("caps.json", {})
    def known_large(t):
        c = caps.get(t)
        return bool(c and c.get("cap") and c["cap"] > config.MAX_MARKET_CAP_M
                    and (NOW - datetime.fromisoformat(c["ts"])).days < 7)
    skipped_large = [b["ticker"] for b in cands if known_large(b["ticker"])]
    cands = [b for b in cands if not known_large(b["ticker"])]
    cands.sort(key=lambda b: (b["accel"], b["m_now"]), reverse=True)
    cands = cands[:config.MAX_CANDIDATES_TO_ENRICH]

    state = store.load_json("state.json", {})
    # one-time hello message once Telegram secrets are set, so the connection can be verified
    if config.TELEGRAM_BOT_TOKEN and not state.get("_telegram_ok"):
        if send_telegram("✅ small-cap-buzz מחובר. מכאן יגיעו התראות על באז מתהווה במניות קטנות."):
            state["_telegram_ok"] = NOW.isoformat(timespec="minutes")
    alerts = store.load_alerts()
    # cleanup from the first run, when market cap was missing and large caps slipped through
    bad = {a["ticker"] for a in alerts if not (a.get("fund") or {}).get("market_cap_m")}
    alerts = [a for a in alerts if a["ticker"] not in bad or (a.get("fund") or {}).get("market_cap_m")]
    for t in bad:
        state.pop(t, None)
    record = track_record(alerts)
    report = []
    for b in cands:
        t = b["ticker"]
        price = sources.price_snapshot(t)
        fund = sources.fundamentals(t)
        if fund and fund.get("market_cap_m"):
            caps[t] = {"cap": fund["market_cap_m"], "ts": NOW.isoformat(timespec="minutes")}
        rd = rd_counts.get(t)
        block = []
        if not price:
            block.append("אין מחיר")
        else:
            if price["move_pct"] >= config.MAX_MOVE_PCT:
                block.append(f"כבר זזה {price['move_pct']:+.0f}%")
            if price["price"] < config.MIN_PRICE:
                block.append("מחיר נמוך מדי")
        mc = (fund or {}).get("market_cap_m")
        if not mc:
            block.append("שווי לא ידוע")
        elif mc > config.MAX_MARKET_CAP_M:
            block.append(f"שווי ${mc:.0f}M")
        elif mc < config.MIN_MARKET_CAP_M:
            block.append(f"שווי ${mc:.1f}M (נמוך מדי או נתון שגוי)")
        rf = ((price or {}).get("readiness") or {}).get("f") or {}
        if rf.get("ret_3d") is not None and rf["ret_3d"] * 100 >= config.MAX_RUN_3D_PCT:
            block.append(f"כבר רצה {rf['ret_3d']*100:+.0f}% ב-3 ימים")
        elif rf.get("ret_10d") is not None and rf["ret_10d"] * 100 >= config.MAX_RUN_10D_PCT:
            block.append(f"כבר רצה {rf['ret_10d']*100:+.0f}% ב-10 ימים")
        x = {"price": price, "xueqiu": xq.get(t), "youtube": yt.get(t), "promoted": t in promoted}
        rs_ages = [int(rf["days_since_rsplit"])] if rf.get("days_since_rsplit") is not None else []
        st, prof = None, None
        if not block:  # extra calls only for names that can still alert
            st = sources.stocktwits_stream(t)
            x["watch_added"], x["watch_pct"] = extras.watchers_growth(watch_hist, t, (st or {}).get("watchlist"))
            x["borrow"] = extras.borrow(t, borrow_hist)
            prof = extras.sec_profile(t, profiles, store.path("cik.json"))
            x["sec"] = sources.sec_filings(t, store.path("cik.json"))
            if x["sec"] and x["sec"].get("rsplit"):
                rs_ages.append((NOW.date() - datetime.fromisoformat(x["sec"]["rsplit"]).date()).days)
        x["rsplit_age"] = min(rs_ages) if rs_ages else None
        if x["rsplit_age"] is not None and x["rsplit_age"] <= config.RSPLIT_BLOCK_DAYS:
            block.append(f"איחוד מניות טרי ({x['rsplit_age']} ימים) – חשד לעיוות")
        pts, why = score(b, rd, st_trend, st, fund, x)
        c = dict(b, price=price, fund=fund, stocktwits=st, reddit=rd or {}, score=pts, why=why,
                 borrow=x.get("borrow"), profile=prof)
        if prof and prof.get("asia"):
            c["why"] = why = why + [f"חברה מ{prof['asia']}"]
        if x["promoted"] and config.BLOCK_ON_PROMOTION:
            block.append("קידום בתשלום")
        sec = x.get("sec")
        c["dilution"] = None if sec is None else sec["dilution"]
        c["sec"] = sec
        if sec and sec["dilution"] and config.BLOCK_ON_DILUTION:
            block.append("דיווח הנפקה: " + ", ".join(sec["dilution"][:3]))
        c["blocked"] = block
        report.append(c)

        if block or pts < config.ALERT_MIN_SCORE:
            continue
        last = state.get(t)
        if last:
            age_h = (NOW - datetime.fromisoformat(last["ts"])).total_seconds() / 3600
            if age_h < config.REALERT_HOURS and pts < last["score"] + config.REALERT_SCORE_JUMP:
                continue
        text = format_alert(c, record)
        sent = send_telegram(text)
        alert = {"id": f"{NOW:%Y%m%d%H%M}-{t}", "ts": NOW.isoformat(timespec="minutes"), "ticker": t,
                 "score": pts, "why": why, "price": price, "fund": fund, "m_now": b["m_now"],
                 "m_24h_ago": b["m_24h_ago"], "spread": b["spread"], "reddit": rd or {},
                 "stocktwits": st, "borrow": x.get("borrow"), "xueqiu": x.get("xueqiu"),
                 "youtube": x.get("youtube"), "profile": prof, "sec": x.get("sec"),
                 "forum": {"rising": b.get("rise_seq"), "novel": b.get("novel")},
                 "invalidation": review.invalidation_level(price),
                 "telegram_sent": sent, "outcome": {}}
        alerts.append(alert)
        state[t] = {"ts": alert["ts"], "score": pts}

    run_ts = NOW.isoformat(timespec="minutes")
    alerted_now = {a["ticker"] for a in alerts if a["ts"] == run_ts}
    followed, weekly = [], None
    try:
        cand_log = review.log_candidates(store, report, NOW, alerted_now)
        review.evaluate_candidates(store, cand_log, NOW)
        followed = review.follow_up(alerts, NOW, send_telegram)
        weekly = review.maybe_send_weekly(state, cand_log, alerts, NOW, send_telegram)
    except Exception as e:  # the learning loop must never break the scan
        sources.ERRORS.append(f"review: {type(e).__name__}: {e}")
    REVIEW_STATUS.update(followed=followed, weekly=bool(weekly))
    store.save_json("caps.json", caps)
    store.save_json("st_watch.json", watch_hist)
    store.save_json("profiles.json", profiles)
    store.save_json("borrow_hist.json", borrow_hist)
    evaluate(alerts)
    store.save_alerts(alerts)
    store.save_json("state.json", state)
    store.save_json("last_run.json", {"ts": NOW.isoformat(timespec="minutes"), "reddit_ok": rd_ok,
                                      "stocktwits_trending": len(st_trend), "apewisdom_main": len(snap["main"]),
                                      "apewisdom_subs": sorted(subs), "candidates": len(report),
                                      "errors": sources.ERRORS[-30:]})
    store.prune(config.KEEP_SNAPSHOT_DAYS)
    write_summary(report, alerts, rd_ok, snap, subs, record, skipped_large)


def write_summary(report, alerts, rd_ok, snap, subs, record, skipped_large=()):
    lines = [f"## סריקת באז {NOW:%Y-%m-%d %H:%M} UTC", "",
             f"ApeWisdom: {len(snap['main'])} טיקרים, פורומים: {', '.join(sorted(subs)) or 'אין'} | "
             f"Reddit: {dict(api='API פעיל', rss='RSS פעיל (פוסטים בלבד)', failed='נחסם', off='כבוי').get(sources.REDDIT_MODE, sources.REDDIT_MODE)} | Telegram: {'פעיל' if config.TELEGRAM_BOT_TOKEN else 'לא מוגדר'}",
             f"Xueqiu (סין): {extras.STATUS.get('xueqiu') or str(len(EXTRA_STATUS.get('xq', {}))) + ' מניות חמות'} | "
             f"קידום בתשלום: {extras.STATUS.get('promoters') or str(len(EXTRA_STATUS.get('promoted', ()))) + ' מניות'} | "
             f"השאלות IBKR: {extras.STATUS.get('ibkr', 'לא נבדק')} | YouTube: {'פעיל' if config.YOUTUBE_API_KEY else 'לא מוגדר'}",
             f"שגיאות בהרצה: {len(sources.ERRORS)}", "", f"**{record}**", "",
             "| טיקר | ציון | מוכנות | אזכורים 24ש | מחיר | תזוזה | שווי | שורט | סיבות | חסימה |",
             "|---|---|---|---|---|---|---|---|---|---|"]
    for c in sorted(report, key=lambda x: -x["score"]):
        p, f = c.get("price") or {}, c.get("fund") or {}
        rdy = (p.get("readiness") or {})
        rtxt = (f"{rdy['points']} ({', '.join(rdy['why'][:3]) or 'אין סימנים'})" if rdy else "?")
        lines.append(f"| {c['ticker']} | {c['score']} | {rtxt} | {c['m_24h_ago']}→{c['m_now']} | {p.get('price','?')} | "
                     f"{p.get('move_pct','?')}% | {round(f['market_cap_m']) if f.get('market_cap_m') else '?'} | {f.get('short_float_pct') or '?'} | "
                     f"{'; '.join(c['why'])} | {'; '.join(c['blocked']) or '✅'} |")
    if skipped_large:
        lines += ["", f"דולגו (שווי גבוה, ממטמון): {', '.join(skipped_large)}"]
    new = [a for a in alerts if a["ts"] == NOW.isoformat(timespec="minutes")]
    lines += ["", f"התראות בהרצה הזו: {', '.join(a['ticker'] for a in new) or 'אין'}"]
    if REVIEW_STATUS.get("followed"):
        lines.append(f"הודעות המשך: {', '.join(REVIEW_STATUS['followed'])}")
    if REVIEW_STATUS.get("weekly"):
        lines.append("נשלח הדוח השבועי")
    if sources.ERRORS:
        lines += ["", "<details><summary>שגיאות</summary>", ""] + [f"- {e}" for e in sources.ERRORS[-30:]] + ["</details>"]
    md = "\n".join(lines)
    path = os.getenv("GITHUB_STEP_SUMMARY")
    if path:
        with open(path, "a") as fh:
            fh.write(md + "\n")
    print(md)


if __name__ == "__main__":
    run()
