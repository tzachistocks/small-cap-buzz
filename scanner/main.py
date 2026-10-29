"""One scan: snapshot buzz -> find building buzz -> enrich -> alert -> evaluate past alerts."""
import json
import os
import statistics
import time
from datetime import datetime, timezone, timedelta

import requests

from . import config, sources, extras, review, news, tg_groups, digest
from .storage import Store

NOW = datetime.now(timezone.utc)
EXTRA_STATUS = {}
REVIEW_STATUS = {}
# where candidates drop out, per run (saved in last_run.json and shown in the run summary)
FUNNEL = {"buzz_tickers": 0, "mention_gate": 0, "reddit_path": 0, "telegram_path": 0, "news_path": 0, "skipped_large_cache": 0,
          "shadow_gate": 0, "to_check": 0, "checked": 0, "not_checked_time": 0, "blocked": {}, "below_score": 0, "cooldown": 0,
          "shadow_would_alert": 0, "sent": 0, "send_failed": 0}


def news_path_candidates(wire_map, already):
    """Tickers with a fresh STRONG press release (not an offering) that are not candidates yet.
    Newest first, at most NEWS_PATH_MAX. They enter with no forum signal; the score decides the rest."""
    cutoff = (NOW - timedelta(hours=config.NEWS_PATH_HOURS)).isoformat(timespec="minutes")
    found = []
    for t, items in wire_map.items():
        if t in already or t in config.LARGE_CAPS or t in config.WORD_TICKERS:
            continue
        fresh = [it for it in items if it["ts"] >= cutoff]
        strong = [it for it in fresh if news.classify(it["title"])[0] == "strong"]
        if not strong or any(news.classify(it["title"])[1] == "offering" for it in fresh):
            continue
        best = max(strong, key=lambda it: it["ts"])
        found.append({"ticker": t, "name": "", "m_now": 0, "m_24h_ago": 0, "m_3h_ago": None, "baseline7d": 0,
                      "vel3h": None, "accel": 0, "spread": [], "gate": True, "word": False, "path": "news",
                      "news_title": best["title"], "news_ts": best["ts"]})
    found.sort(key=lambda b: b["news_ts"], reverse=True)
    return found[:config.NEWS_PATH_MAX]


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
        shadow_gate = config.SHADOW_GATE and not (main_gate or penny_gate) and (
            (t in snap["main"] and m_now >= config.SHADOW_MAIN_MIN_MENTIONS and m_now - m_24 >= config.SHADOW_MAIN_MIN_ADD
             and smoothed(m_now, m_24) >= config.SHADOW_MIN_SMOOTHED_GROWTH)
            or bool(p and p["mentions"] >= config.SHADOW_PENNY_MIN_MENTIONS
                    and p["mentions"] - p["mentions_24h_ago"] >= config.SHADOW_PENNY_MIN_ADD
                    and smoothed(p["mentions"], p["mentions_24h_ago"]) >= config.SHADOW_MIN_SMOOTHED_GROWTH))
        out[t] = {"ticker": t, "name": name, "m_now": m_now, "m_24h_ago": m_24, "m_3h_ago": past3,
                  "baseline7d": baseline, "vel3h": (m_now - past3) if past3 is not None else None,
                  "accel": round(smoothed(m_now, baseline), 2), "spread": spread,
                  "gate": main_gate or penny_gate, "shadow_gate": shadow_gate, "word": t in config.WORD_TICKERS,
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
    tg = x.get("telegram")
    if tg and tg["authors24"] >= config.TG_MIN_AUTHORS and tg["chats24"] >= config.TG_MIN_CHATS:
        pts += 1; why.append(f"בטלגרם: {tg['m24']} אזכורים מ-{tg['authors24']} כותבים ב-{tg['chats24']} קבוצות")
    elif tg and tg["m24"] >= 3:
        why.append(f"טלגרם רגיל: {tg['m24']} אזכורים ב-{tg['chats24']} קבוצות")
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
    fl = sec.get("filings") or []
    best = (next((f for f in fl if f["class"] == "negative"), None) or next((f for f in fl if f["class"] == "strong"), None)
            or (fl[0] if fl else None))
    if best and best["class"] == "strong":
        pts += 1; why.append(f"SEC חזק: {best['label']} ({best['date']})")
    elif best and best["class"] == "negative":
        pts -= 1; why.append(f"⚠️ SEC שלילי: {best['label']} ({best['date']})")
    elif best:
        why.append(f"SEC רגיל: {best['label']} ({best['date']})")
    nw = x.get("news")
    mc = (fund or {}).get("market_cap_m")
    ratio = (nw["amount_m"] / mc) if (nw and nw.get("amount_m") and mc) else None
    size_txt = f" | ${nw['amount_m']:,.1f}M ≈ {ratio*100:.0f}% מהשווי" if ratio is not None else ""
    if nw and nw["class"] == "strong" and ratio is not None and ratio < config.PR_SMALL_RATIO:
        why.append(f"PR רגיל – סכום זניח ({news.et_time(nw['ts'])}): {nw['title'][:60]}{size_txt}")
    elif nw and nw["class"] == "strong":
        pts += 1; why.append(f"PR חזק ({news.et_time(nw['ts'])}): {nw['title'][:70]}{size_txt}")
        if ratio is not None and ratio >= config.PR_BIG_RATIO:
            pts += 1; why.append(f"עסקה גדולה ביחס לשווי ({ratio*100:.0f}%)")
    elif nw and nw["class"] == "negative":
        pts -= 2; why.append(f"⚠️ PR שלילי ({news.et_time(nw['ts'])}): {nw['title'][:70]}{size_txt}")
    elif nw and nw["class"] == "weak":
        why.append(f"PR רגיל ({news.et_time(nw['ts'])}): {nw['title'][:70]}")
    rs_age = x.get("rsplit_age")
    if rs_age is not None and rs_age <= config.RSPLIT_PENALTY_DAYS:
        pts -= 1; why.append(f"⚠️ איחוד מניות / שינוי יחס ADS לפני {rs_age} ימים")
    if x.get("promoted"):
        pts -= 3; why.append("⚠️ קידום בתשלום (stockpromoters)")
    if b["word"]:
        pts -= 2; why.append("חשד להתראת שווא (מילה)")
    return pts, why


# ---------------- 3. alerts ----------------
TG_LOG = []  # (ok, first line, chat description) for every message this run


def scrub_errors():
    """The repo is public: errors are committed (last_run.json) and shown in the run summary.
    A network error message can contain a full URL, e.g. .../bot<TOKEN>/sendMessage or ?key=<API KEY>."""
    secrets = [s for s in (config.TELEGRAM_BOT_TOKEN, config.TELEGRAM_CHAT_ID, config.TELEGRAM_URGENT_CHAT_ID,
                           config.REDDIT_CLIENT_ID, config.REDDIT_SECRET, config.YOUTUBE_API_KEY,
                           config.TG_API_HASH, config.TG_SESSION, *tg_groups.chat_list_raw(),
                           os.getenv("SEC_USER_AGENT", "").strip()) if s and len(s) >= 5]
    for i, e in enumerate(sources.ERRORS):
        e = str(e)
        for s in secrets:
            e = e.replace(s, "***")
        sources.ERRORS[i] = e


def send_telegram(text, urgent=False):
    """Every message goes to the main chat; urgent ones also go to the (unmuted) urgent chat if it is set."""
    if not config.TELEGRAM_BOT_TOKEN:
        return False
    ok = _tg_post(config.TELEGRAM_CHAT_ID, text) if config.TELEGRAM_CHAT_ID else False
    if not config.TELEGRAM_CHAT_ID:
        sources.ERRORS.append("Telegram: חסר ה-secret TELEGRAM_CHAT_ID")
    if urgent and config.TELEGRAM_URGENT_CHAT_ID:
        ok = _tg_post(config.TELEGRAM_URGENT_CHAT_ID, text) or ok
    return ok


def _tg_post(chat_id, text):
    first = (text or "").split("\n", 1)[0][:60]
    try:
        r = requests.post(f"https://api.telegram.org/bot{config.TELEGRAM_BOT_TOKEN}/sendMessage",
                          json={"chat_id": chat_id, "text": text,
                                "disable_web_page_preview": True}, timeout=20)
        if r.status_code != 200:
            sources.ERRORS.append(f"Telegram HTTP {r.status_code}: {r.text[:120]}")
            TG_LOG.append((False, first, ""))
            return False
        chat = ((r.json() or {}).get("result") or {}).get("chat") or {}
        who = " ".join(str(x) for x in (chat.get("type"), chat.get("title") or chat.get("username")
                                         or chat.get("first_name"), chat.get("id")) if x)
        TG_LOG.append((True, first, who))
        return True
    except Exception as e:
        sources.ERRORS.append(f"Telegram error: {e}")
        TG_LOG.append((False, first, ""))
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


def urgent_check(c, b, x):
    """Strong candidate: high score, still early, a forum-run signal, fuel, and nothing suspicious.
    Returns (is_urgent, reasons)."""
    p, f, br = c.get("price") or {}, c.get("fund") or {}, x.get("borrow") or {}
    if c.get("blocked") or c["score"] < config.URGENT_MIN_SCORE:
        return False, []
    if (p.get("move_pct") or 0) > config.URGENT_MAX_MOVE_PCT:
        return False, []
    rd = c.get("reddit") or {}
    n_spread = len(b.get("spread") or []) + len(rd.get("subs6") or [])
    forum = []
    if b.get("rising"):
        forum.append("השיח עולה כמה ימים")
    if b.get("novel"):
        forum.append("חדשה בשיח")
    if n_spread >= 3:
        forum.append(f"מתפשטת ב-{n_spread} פורומים")
    fuel = []
    if f.get("float_m") and f["float_m"] < config.URGENT_MAX_FLOAT_M:
        fuel.append(f"פלואט {f['float_m']:.1f}M")
    if (br.get("fee") or 0) >= config.URGENT_MIN_BORROW_FEE:
        fuel.append(f"עמלת השאלה {br['fee']:.0f}%")
    if (f.get("short_float_pct") or 0) >= config.URGENT_MIN_SHORT_PCT:
        fuel.append(f"שורט {f['short_float_pct']:.0f}%")
    clean = (not x.get("promoted") and not b.get("word") and c.get("dilution") == []
             and (x.get("rsplit_age") is None or x["rsplit_age"] > config.RSPLIT_PENALTY_DAYS)
             and not any("כותבים בלבד" in w or "שלילי" in w for w in c.get("why") or []))
    if forum and fuel and clean:
        return True, forum + fuel
    return False, []


def format_alert(c, record):
    p = c["price"]
    head = (f"🔥 מועמדת חזקה: ${c['ticker']} (ציון {c['score']})" if c.get("urgent")
            else f"📰 קטליזטור טרי: ${c['ticker']} (ציון {c['score']})" if c.get("news_title")
            else f"🔔 באז מתהווה: ${c['ticker']} (ציון {c['score']})")
    lines = [head]
    if c.get("urgent"):
        lines.append("למה חזקה: " + "; ".join(c["urgent_why"]) + f" | עלתה רק {p['move_pct']:+.1f}% | אין הנפקה ב-30 יום")
        lines.append("(לפי הכללים שלנו, לא ודאות. הקבוצה הזו נמדדת בנפרד בדוח השבועי.)")
    lines += [
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
    tg_counts, tg_ok = tg_groups.telegram_counts(known) if known else ({}, False)
    st_trend = sources.stocktwits_trending()
    xq = extras.xueqiu_hot_us()
    promoted = extras.promoted_tickers(known) if known else set()
    yt = extras.youtube_mentions(known) if known else {}
    wire_map = news.wire_news(store, known) if known else {}
    watch_hist = store.load_json("st_watch.json", {})
    for t, v in st_trend.items():  # build watcher history for trending names at no extra cost
        if v.get("watchlist_count"):
            extras.watchers_growth(watch_hist, t, v["watchlist_count"])
    profiles = store.load_json("profiles.json", {})
    borrow_hist = store.load_json("borrow_hist.json", {})
    EXTRA_STATUS.update(xq=xq, promoted=promoted)

    # candidates: passed the mention gate, or strong reddit-only spread, or a fresh strong press release
    cands = [dict(b, path="buzz") for b in buzz.values() if b["gate"]]
    FUNNEL["buzz_tickers"] = len(buzz)
    FUNNEL["mention_gate"] = len(cands)
    gated = {b["ticker"] for b in cands}
    for t, r in rd_counts.items():
        # also for tickers ApeWisdom saw but that failed its gate: Reddit is a second, independent chance
        if t not in gated and r["authors6"] >= 4 and len(r["subs6"]) >= 2 and t not in config.WORD_TICKERS:
            if t in buzz:  # keep its ApeWisdom numbers
                cands.append(dict(buzz[t], gate=True, path="reddit"))
            else:
                cands.append({"ticker": t, "name": "", "m_now": r["m24"], "m_24h_ago": 0, "m_3h_ago": None,
                              "baseline7d": 0, "vel3h": None, "accel": 0, "spread": [], "gate": True, "word": False,
                              "path": "reddit"})
    FUNNEL["reddit_path"] = len(cands) - FUNNEL["mention_gate"]
    gated = {b["ticker"] for b in cands}
    for t, r in tg_counts.items():
        if (t not in gated and r["authors6"] >= config.TG_PATH_MIN_AUTHORS6 and r["chats6"] >= config.TG_PATH_MIN_CHATS6
                and t not in config.WORD_TICKERS):
            base = buzz.get(t) or {"ticker": t, "name": "", "m_now": 0, "m_24h_ago": 0, "m_3h_ago": None,
                                   "baseline7d": 0, "vel3h": None, "accel": 0, "spread": [], "word": False}
            cands.append(dict(base, gate=True, path="telegram"))
    FUNNEL["telegram_path"] = len(cands) - FUNNEL["mention_gate"] - FUNNEL["reddit_path"]
    news_all = news_path_candidates(wire_map, set()) if config.NEWS_PATH else []
    have = {b["ticker"]: b for b in cands}
    for n in news_all:  # already a candidate by buzz/reddit/telegram: it still gets the news-path alert bar
        if n["ticker"] in have:
            have[n["ticker"]].update(news_title=n["news_title"], news_ts=n["news_ts"])
    news_cands = [n for n in news_all if n["ticker"] not in have]
    FUNNEL["news_path"] = len(news_cands)
    in_cands = {b["ticker"] for b in cands} | {b["ticker"] for b in news_cands}
    shadow_cands = [dict(b, path="shadow") for b in buzz.values()
                    if b.get("shadow_gate") and b["ticker"] not in in_cands and not b["word"]]
    FUNNEL["shadow_gate"] = len(shadow_cands)
    # skip tickers already known to be too large (cached caps) so the run time goes to small caps
    caps = store.load_json("caps.json", {})
    def known_large(t):
        c = caps.get(t)
        if not (c and c.get("cap") and c["cap"] > config.MAX_MARKET_CAP_M):
            return False
        days = config.LARGE_CAP_CACHE_DAYS_FAR if c["cap"] >= 4 * config.MAX_MARKET_CAP_M else config.LARGE_CAP_CACHE_DAYS
        return (NOW - datetime.fromisoformat(c["ts"])).days < days
    skipped_large = [b["ticker"] for b in cands + news_cands + shadow_cands if known_large(b["ticker"])]
    cands = [b for b in cands if not known_large(b["ticker"])]
    news_cands = [b for b in news_cands if not known_large(b["ticker"])]
    shadow_cands = [b for b in shadow_cands if not known_large(b["ticker"])]
    shadow_cands.sort(key=lambda b: (b["accel"], b["m_now"]), reverse=True)
    FUNNEL["skipped_large_cache"] = len(skipped_large)
    cands.sort(key=lambda b: (b["accel"], b["m_now"]), reverse=True)
    # news first (few, time-sensitive), shadow last (the time budget cuts them first)
    cands = (news_cands + cands + shadow_cands)[:config.MAX_CANDIDATES_TO_ENRICH]
    FUNNEL["to_check"] = len(cands)

    state = store.load_json("state.json", {})
    # one-time hello message once Telegram secrets are set, so the connection can be verified
    if config.TELEGRAM_BOT_TOKEN and not state.get("_telegram_ok"):
        if send_telegram("✅ small-cap-buzz מחובר. מכאן יגיעו התראות על באז מתהווה במניות קטנות."):
            state["_telegram_ok"] = NOW.isoformat(timespec="minutes")
    if config.TELEGRAM_BOT_TOKEN and config.TELEGRAM_URGENT_CHAT_ID and not state.get("_urgent_ok"):
        if _tg_post(config.TELEGRAM_URGENT_CHAT_ID, "✅ ערוץ המועמדות החזקות 🔥 מחובר. לכאן יגיעו רק התראות חזקות והודעות ההמשך שלהן."):
            state["_urgent_ok"] = NOW.isoformat(timespec="minutes")
    alerts = store.load_alerts()
    # cleanup from the first run, when market cap was missing and large caps slipped through
    bad = {a["ticker"] for a in alerts if not (a.get("fund") or {}).get("market_cap_m")}
    alerts = [a for a in alerts if a["ticker"] not in bad or (a.get("fund") or {}).get("market_cap_m")]
    for t in bad:
        state.pop(t, None)
    record = track_record(alerts)
    report = []
    t_start = time.monotonic()
    not_checked = []
    for i, b in enumerate(cands):
        t = b["ticker"]
        if time.monotonic() - t_start > config.ENRICH_TIME_BUDGET_S:
            not_checked = [x["ticker"] for x in cands[i:]]
            FUNNEL["not_checked_time"] = len(not_checked)
            sources.ERRORS.append(f"time budget: {len(not_checked)} candidates not checked")
            break
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
        x = {"price": price, "xueqiu": xq.get(t), "youtube": yt.get(t), "promoted": t in promoted,
             "telegram": tg_counts.get(t)}
        rs_ages = [int(rf["days_since_rsplit"])] if rf.get("days_since_rsplit") is not None else []
        st, prof = None, None
        if not block:  # extra calls only for names that can still alert
            st = sources.stocktwits_stream(t)
            x["watch_added"], x["watch_pct"] = extras.watchers_growth(watch_hist, t, (st or {}).get("watchlist"))
            x["borrow"] = extras.borrow(t, borrow_hist)
            prof = extras.sec_profile(t, profiles, store.path("cik.json"))
            x["sec"] = sources.sec_filings(t, store.path("cik.json"))
            x["news"] = news.news_signal(t, wire_map, NOW)
            if x["news"] and x["news"].get("offering") and config.BLOCK_ON_OFFERING_PR:
                block.append("PR על הנפקה: " + x["news"]["title"][:60])
            if x["sec"] and x["sec"].get("rsplit"):
                rs_ages.append((NOW.date() - datetime.fromisoformat(x["sec"]["rsplit"]).date()).days)
        x["rsplit_age"] = min(rs_ages) if rs_ages else None
        if x["rsplit_age"] is not None and x["rsplit_age"] <= config.RSPLIT_BLOCK_DAYS:
            block.append(f"איחוד מניות טרי ({x['rsplit_age']} ימים) – חשד לעיוות")
        pts, why = score(b, rd, st_trend, st, fund, x)
        if b.get("path") == "news":
            why = [f"📰 נכנסה דרך ידיעה: {b['news_title'][:70]}"] + why
        elif b.get("news_title"):
            why = [f"📰 ידיעה טרייה: {b['news_title'][:70]}"] + why
        elif b.get("path") == "shadow":
            why = [f"🧪 שער מוקל ({b['m_24h_ago']}→{b['m_now']}), מעקב בלבד"] + why
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
        c["urgent"], c["urgent_why"] = urgent_check(c, b, x)
        report.append(c)
        FUNNEL["checked"] += 1

        min_score = config.ALERT_MIN_SCORE
        if mc and mc < config.SMALL_CAP_M:
            min_score = config.ALERT_MIN_SCORE_SMALL
            # news path: a strong, still-fresh release on a small cap that hasn't moved yet
            if b.get("news_title") and (x.get("news") or {}).get("class") == "strong":
                min_score = min(min_score, config.ALERT_MIN_SCORE_NEWS)
        if block:
            for r in block:
                k = r.split(" ")[0]
                FUNNEL["blocked"][k] = FUNNEL["blocked"].get(k, 0) + 1
            continue
        if pts < min_score:
            FUNNEL["below_score"] += 1
            continue
        if b.get("path") == "shadow":  # experiment: measured in candidates.json, never sent
            FUNNEL["shadow_would_alert"] += 1
            continue
        last = state.get(t)
        if last:
            age_h = (NOW - datetime.fromisoformat(last["ts"])).total_seconds() / 3600
            if age_h < config.REALERT_HOURS and pts < last["score"] + config.REALERT_SCORE_JUMP:
                FUNNEL["cooldown"] += 1
                continue
        text = format_alert(c, record)
        sent = send_telegram(text, urgent=c["urgent"])
        alert = {"id": f"{NOW:%Y%m%d%H%M}-{t}", "ts": NOW.isoformat(timespec="minutes"), "ticker": t,
                 "score": pts, "why": why, "price": price, "fund": fund, "m_now": b["m_now"],
                 "m_24h_ago": b["m_24h_ago"], "spread": b["spread"], "reddit": rd or {},
                 "stocktwits": st, "borrow": x.get("borrow"), "xueqiu": x.get("xueqiu"),
                 "youtube": x.get("youtube"), "telegram": x.get("telegram"), "profile": prof, "sec": x.get("sec"),
                 "forum": {"rising": b.get("rise_seq"), "novel": b.get("novel")},
                 "invalidation": review.invalidation_level(price), "news": x.get("news"),
                 "urgent": c["urgent"], "urgent_why": c["urgent_why"], "path": b.get("path", "buzz"),
                 "telegram_sent": sent, "outcome": {}}
        if sent:
            alerts.append(alert)
            state[t] = {"ts": alert["ts"], "score": pts}  # cooldown starts only after a delivered message
            FUNNEL["sent"] += 1
        else:
            # not delivered: keep it out of alerts.json and the cooldown, so the next run tries again
            failed = store.load_json("alerts_failed.json", [])
            failed = (failed + [{"ts": alert["ts"], "ticker": t, "score": pts,
                                 "error": (sources.ERRORS[-1] if sources.ERRORS else "")[:160]}])[-200:]
            store.save_json("alerts_failed.json", failed)
            FUNNEL["send_failed"] += 1

    run_ts = NOW.isoformat(timespec="minutes")
    alerted_now = {a["ticker"] for a in alerts if a["ts"] == run_ts}
    followed, weekly = [], None
    try:
        cand_log = review.log_candidates(store, report, NOW, alerted_now)
        review.evaluate_candidates(store, cand_log, NOW)
        followed = review.follow_up(alerts, NOW, send_telegram)  # urgent alerts' follow-ups go to the urgent chat too
        weekly = review.maybe_send_weekly(state, cand_log, alerts, NOW, send_telegram)
    except Exception as e:  # the learning loop must never break the scan
        sources.ERRORS.append(f"review: {type(e).__name__}: {e}")
    REVIEW_STATUS.update(followed=followed, weekly=weekly)
    store.save_json("caps.json", caps)
    store.save_json("st_watch.json", watch_hist)
    store.save_json("profiles.json", profiles)
    store.save_json("borrow_hist.json", borrow_hist)
    evaluate(alerts)
    store.save_alerts(alerts)
    store.save_json("state.json", state)
    try:  # Reddit feed for outside readers (the Claude scheduled scans read this from GitHub)
        export_reddit(store, rd_counts, rd_ok, snap, subs)
    except Exception as e:
        sources.ERRORS.append(f"reddit export: {type(e).__name__}: {e}")
    scrub_errors()
    store.save_json("last_run.json", {"ts": NOW.isoformat(timespec="minutes"), "reddit_ok": rd_ok,
                                      "stocktwits_trending": len(st_trend), "apewisdom_main": len(snap["main"]),
                                      "apewisdom_subs": sorted(subs), "candidates": len(report),
                                      "telegram_groups": {k: tg_groups.STATUS[k] for k in ("mode", "chats_ok", "chats_failed", "messages")},
                                      "funnel": FUNNEL, "not_checked": not_checked[:50],
                                      "errors": sources.ERRORS[-30:]})
    try:  # daily digest for outside readers (data/digest/<date>.json); never breaks the scan
        digest.log_run(store, NOW, FUNNEL, tg_groups.STATUS, sorted(alerted_now), sources.ERRORS)
        digest.update_tg_hist(store, NOW, tg_counts)
        digest.build(store, NOW, alerts, store.load_json("candidates.json", {}))
    except Exception as e:
        sources.ERRORS.append(f"digest: {type(e).__name__}: {e}")
    store.prune(config.KEEP_SNAPSHOT_DAYS)
    write_summary(report, alerts, rd_ok, snap, subs, record, skipped_large)


def export_reddit(store, rd_counts, rd_ok, snap, subs):
    """data/reddit_latest.json: this run's Reddit counts + ApeWisdom per-forum counts.
    data/reddit_hist.json: per day, per ticker, the peak 24h mentions and the first time seen (10 days kept)."""
    ts = NOW.isoformat(timespec="minutes")
    top = dict(sorted(rd_counts.items(), key=lambda kv: -kv[1]["m24"])[:200])
    forums = {}
    for sub, rows in (snap.get("subs") or {}).items():
        forums[sub] = {t: v for t, v in rows.items() if (v[0] or 0) >= 2}
    store.save_json("reddit_latest.json", {"ts": ts, "reddit_ok": rd_ok, "mode": sources.REDDIT_MODE,
                                           "reddit": top, "apewisdom_forums": forums})
    hist = store.load_json("reddit_hist.json", {})
    day = hist.setdefault(NOW.strftime("%Y-%m-%d"), {})
    for t, r in top.items():
        h = day.setdefault(t, {"m24_max": 0, "authors24_max": 0, "subs": [], "first_utc": r["first_utc"]})
        h["m24_max"] = max(h["m24_max"], r["m24"])
        h["authors24_max"] = max(h["authors24_max"], r["authors24"])
        h["subs"] = sorted(set(h["subs"]) | set(r["subs24"]))
        h["first_utc"] = min(h["first_utc"], r["first_utc"])
        if r.get("sample") and not h.get("sample"):
            h["sample"] = r["sample"]
    for d in sorted(hist)[:-10]:
        hist.pop(d)
    store.save_json("reddit_hist.json", hist)


def write_summary(report, alerts, rd_ok, snap, subs, record, skipped_large=()):
    lines = [f"## סריקת באז {NOW:%Y-%m-%d %H:%M} UTC", "",
             f"ApeWisdom: {len(snap['main'])} טיקרים, פורומים: {', '.join(sorted(subs)) or 'אין'} | "
             f"Reddit: {dict(api='API פעיל', rss='RSS פעיל (פוסטים בלבד)', failed='נחסם', off='כבוי').get(sources.REDDIT_MODE, sources.REDDIT_MODE)} | Telegram: {'פעיל' if config.TELEGRAM_BOT_TOKEN and config.TELEGRAM_CHAT_ID else ('חסר CHAT_ID' if config.TELEGRAM_BOT_TOKEN else 'לא מוגדר')}",
             f"Xueqiu (סין): {extras.STATUS.get('xueqiu') or str(len(EXTRA_STATUS.get('xq', {}))) + ' מניות חמות'} | "
             f"קידום בתשלום: {extras.STATUS.get('promoters') or str(len(EXTRA_STATUS.get('promoted', ()))) + ' מניות'} | "
             f"השאלות IBKR: {extras.STATUS.get('ibkr', 'לא נבדק')} | YouTube: {'פעיל' if config.YOUTUBE_API_KEY else 'לא מוגדר'}",
             f"קבוצות טלגרם: {dict(off='לא מוגדר', failed='נכשל', ok='פעיל').get(tg_groups.STATUS['mode'])}"
             f" ({tg_groups.STATUS['chats_ok']} נקראו, {tg_groups.STATUS['chats_failed']} נכשלו, {tg_groups.STATUS['messages']} הודעות)",
             f"הודעות לעיתונות: {news.STATUS.get('wires', 'לא נבדק')} | Yahoo חדשות: "
             f"{news.STATUS.get('yahoo_ok', 0)} הצליחו, {news.STATUS.get('yahoo_fail', 0)} נכשלו",
             f"שגיאות בהרצה: {len(sources.ERRORS)}", "", f"**{record}**", "",
             "| טיקר | ציון | מוכנות | אזכורים 24ש | מחיר | תזוזה | שווי | שורט | סיבות | חסימה |",
             "|---|---|---|---|---|---|---|---|---|---|"]
    for c in sorted(report, key=lambda x: -x["score"]):
        p, f = c.get("price") or {}, c.get("fund") or {}
        rdy = (p.get("readiness") or {})
        rtxt = (f"{rdy['points']} ({', '.join(rdy['why'][:3]) or 'אין סימנים'})" if rdy else "?")
        lines.append(f"| {'🔥 ' if c.get('urgent') else ''}{c['ticker']} | {c['score']} | {rtxt} | {c['m_24h_ago']}→{c['m_now']} | {p.get('price','?')} | "
                     f"{p.get('move_pct','?')}% | {round(f['market_cap_m']) if f.get('market_cap_m') else '?'} | {f.get('short_float_pct') or '?'} | "
                     f"{'; '.join(c['why'])} | {'; '.join(c['blocked']) or '✅'} |")
    if skipped_large:
        lines += ["", f"דולגו (שווי גבוה, ממטמון): {', '.join(skipped_large)}"]
    fz = FUNNEL
    lines += ["", "**משפך:** "
              f"{fz['buzz_tickers']} טיקרים בשיח → {fz['mention_gate']} עברו שער אזכורים + {fz['reddit_path']} מרדיט"
              f" + {fz['telegram_path']} מטלגרם + {fz['news_path']} מידיעות + {fz['shadow_gate']} בשער מוקל (מעקב) → {fz['skipped_large_cache']} דולגו (גדולות) → {fz['checked']} נבדקו"
              f" ({fz['not_checked_time']} לא נבדקו, זמן) → חסומות: "
              + (", ".join(f"{k} {v}" for k, v in sorted(fz['blocked'].items(), key=lambda kv: -kv[1])) or "0")
              + f" → מתחת לציון {fz['below_score']} → שער מוקל שהיה מתריע {fz['shadow_would_alert']} → השתקה {fz['cooldown']} → נשלחו {fz['sent']}, נכשלו {fz['send_failed']}"]
    new = [a for a in alerts if a["ts"] == NOW.isoformat(timespec="minutes")]
    lines += ["", f"התראות בהרצה הזו: {', '.join(a['ticker'] for a in new) or 'אין'}"]
    if REVIEW_STATUS.get("followed"):
        lines.append(f"הודעות המשך: {', '.join(REVIEW_STATUS['followed'])}")
    if REVIEW_STATUS.get("weekly"):
        lines.append("הדוח השבועי: " + ("נשלח" if REVIEW_STATUS["weekly"] == "sent" else "השליחה נכשלה"))
    if TG_LOG:
        ok = [x for x in TG_LOG if x[0]]
        lines.append(f"טלגרם: {len(ok)} הודעות נשלחו, {len(TG_LOG) - len(ok)} נכשלו")
        # chat name/id is not printed: the run summary of a public repo is visible to everyone
    scrub_errors()
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
