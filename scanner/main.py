"""One scan: snapshot buzz -> find building buzz -> enrich -> alert -> evaluate past alerts."""
import json
import os
import statistics
from datetime import datetime, timezone, timedelta

import requests

from . import config, sources
from .storage import Store

NOW = datetime.now(timezone.utc)


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
        past3, base_vals = None, []
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
                  "gate": main_gate or penny_gate, "word": t in config.WORD_TICKERS}
    return out


def score(b, rd, st_trend, st, fund):
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
    if b["ticker"] in st_trend:
        pts += 1; why.append("בטרנד של סטוקטוויטס")
    if st and st.get("msgs_6h", 0) >= 10:
        pts += 1; why.append(f"{st['msgs_6h']} הודעות בסטוקטוויטס ב-6 שעות")
    if fund:
        if (fund.get("short_float_pct") or 0) >= 20:
            pts += 1; why.append(f"שורט {fund['short_float_pct']:.0f}%")
        if fund.get("float_m") and fund["float_m"] < 50:
            pts += 1; why.append(f"פלואט {fund['float_m']:.1f}M")
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
    done = [a for a in alerts if a.get("outcome", {}).get("days", 0) >= 1]
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
    lines.append("למה: " + "; ".join(c["why"]))
    if c.get("reddit", {}).get("sample"):
        lines.append(c["reddit"]["sample"])
    if c.get("dilution") is None:
        lines.append("⚠️ לא נבדקו דיווחי הנפקה (אין CIK)")
    lines.append(f"ביטול: ירידה מתחת ל-{p['prev_close']} (סגירה קודמת)")
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

    # candidates: passed the mention gate, or strong reddit-only spread
    cands = [b for b in buzz.values() if b["gate"]]
    for t, r in rd_counts.items():
        if t not in buzz and r["authors6"] >= 4 and len(r["subs6"]) >= 2 and t not in config.WORD_TICKERS:
            cands.append({"ticker": t, "name": "", "m_now": r["m24"], "m_24h_ago": 0, "m_3h_ago": None,
                          "baseline7d": 0, "vel3h": None, "accel": 0, "spread": [], "gate": True, "word": False})
    cands.sort(key=lambda b: (b["accel"], b["m_now"]), reverse=True)
    cands = cands[:config.MAX_CANDIDATES_TO_ENRICH]

    state = store.load_json("state.json", {})
    alerts = store.load_alerts()
    record = track_record(alerts)
    report = []
    for b in cands:
        t = b["ticker"]
        price = sources.price_snapshot(t)
        fund = sources.fundamentals(t)
        st = sources.stocktwits_stream(t)
        rd = rd_counts.get(t)
        pts, why = score(b, rd, st_trend, st, fund)
        c = dict(b, price=price, fund=fund, stocktwits=st, reddit=rd or {}, score=pts, why=why)
        block = []
        if not price:
            block.append("אין מחיר")
        else:
            if price["move_pct"] >= config.MAX_MOVE_PCT:
                block.append(f"כבר זזה {price['move_pct']:+.0f}%")
            if price["price"] < config.MIN_PRICE:
                block.append("מחיר נמוך מדי")
        mc = (fund or {}).get("market_cap_m")
        if mc and mc > config.MAX_MARKET_CAP_M:
            block.append(f"שווי ${mc:.0f}M")
        if not block and pts >= config.ALERT_MIN_SCORE - 1:
            dil = sources.dilution_filings(t, store.path("cik.json"))
            c["dilution"] = dil
            if dil and config.BLOCK_ON_DILUTION:
                block.append("דיווח הנפקה: " + ", ".join(dil[:3]))
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
                 "stocktwits": st, "telegram_sent": sent, "outcome": {}}
        alerts.append(alert)
        state[t] = {"ts": alert["ts"], "score": pts}

    evaluate(alerts)
    store.save_alerts(alerts)
    store.save_json("state.json", state)
    store.save_json("last_run.json", {"ts": NOW.isoformat(timespec="minutes"), "reddit_ok": rd_ok,
                                      "stocktwits_trending": len(st_trend), "apewisdom_main": len(snap["main"]),
                                      "apewisdom_subs": sorted(subs), "candidates": len(report),
                                      "errors": sources.ERRORS[-30:]})
    store.prune(config.KEEP_SNAPSHOT_DAYS)
    write_summary(report, alerts, rd_ok, snap, subs, record)


def write_summary(report, alerts, rd_ok, snap, subs, record):
    lines = [f"## סריקת באז {NOW:%Y-%m-%d %H:%M} UTC", "",
             f"ApeWisdom: {len(snap['main'])} טיקרים, פורומים: {', '.join(sorted(subs)) or 'אין'} | "
             f"Reddit API: {'פעיל' if rd_ok else 'לא מוגדר/נכשל'} | Telegram: {'פעיל' if config.TELEGRAM_BOT_TOKEN else 'לא מוגדר'}",
             "", f"**{record}**", "",
             "| טיקר | ציון | אזכורים 24ש | מחיר | תזוזה | שווי | שורט | סיבות | חסימה |",
             "|---|---|---|---|---|---|---|---|---|"]
    for c in sorted(report, key=lambda x: -x["score"]):
        p, f = c.get("price") or {}, c.get("fund") or {}
        lines.append(f"| {c['ticker']} | {c['score']} | {c['m_24h_ago']}→{c['m_now']} | {p.get('price','?')} | "
                     f"{p.get('move_pct','?')}% | {f.get('market_cap_m') or '?'} | {f.get('short_float_pct') or '?'} | "
                     f"{'; '.join(c['why'])} | {'; '.join(c['blocked']) or '✅'} |")
    new = [a for a in alerts if a["ts"] == NOW.isoformat(timespec="minutes")]
    lines += ["", f"התראות בהרצה הזו: {', '.join(a['ticker'] for a in new) or 'אין'}"]
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
