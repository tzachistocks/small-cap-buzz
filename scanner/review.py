"""Learning loop: log every candidate (not only alerts), measure what happened 5 trading days later,
send a weekly Telegram report, and follow up on live alerts (+30% / invalidation).

Everything here fails soft: a problem is logged to sources.ERRORS and the scan continues.
"""
import statistics
from datetime import datetime, timezone, timedelta

from . import config, sources

# why-text fragment -> short tag used for statistics (order matters: first match wins per reason)
TAGS = [
    ("נכנסה דרך ידיעה", "news_path"), ("ידיעה טרייה:", "news_fresh"), ("בטלגרם:", "telegram"), ("טלגרם רגיל", "telegram_weak"), ("שער מוקל", "relaxed_gate"), ("כותבים בלבד", "one_author"), ("שיח עולה", "rising"), ("חדשה בשיח", "novel"),
    ("עסקה גדולה ביחס לשווי", "pr_big"), ("PR חזק", "pr_strong"), ("PR שלילי", "pr_negative"), ("PR רגיל", "pr_weak"),
    ("SEC חזק", "sec_strong"), ("SEC שלילי", "sec_negative"), ("SEC רגיל", "sec_weak"),
    ("האצה", "accel"), ("אזכורים ב-3 שעות", "vel3h"), ("פיזור", "spread"), ("כותבים שונים", "authors"),
    ("בטרנד", "st_trend"), ("הודעות בסטוקטוויטס", "st_msgs"), ("עוקבים", "watchers"), ("שורט ", "short"),
    ("פלואט", "low_float"), ("נפח פי", "rvol"), ("השאלה מתייקרת", "borrow_trend"), ("עמלת השאלה", "borrow_fee"),
    ("Xueqiu", "xueqiu"), ("YouTube", "youtube"), ("איחוד", "rsplit"), ("קידום", "promoted"), ("מילה", "word"),
    ("חברה מ", "asia"),
]
TAG_HE = {
    "news_path": "נכנסה דרך ידיעה (בלי שיח)", "news_fresh": "ידיעה טרייה + שיח", "telegram": "שיח בכמה קבוצות טלגרם", "telegram_weak": "אזכורים בטלגרם (חלש)", "relaxed_gate": "שער אזכורים מוקל (ניסוי)",
    "rising": "שיח עולה כמה ימים", "novel": "חדשה בשיח", "pr_strong": "PR חזק", "pr_big": "עסקה ≥10% מהשווי", "pr_weak": "PR רגיל", "pr_negative": "PR שלילי",
    "sec_strong": "SEC חזק (8-K 1.01/2.01, 13D)", "sec_weak": "SEC רגיל", "sec_negative": "SEC שלילי", "accel": "האצה מול הבסיס",
    "vel3h": "קצב ב-3 שעות", "spread": "פיזור בין פורומים", "authors": "כותבים שונים ברדיט", "st_trend": "טרנד בסטוקטוויטס",
    "st_msgs": "הודעות בסטוקטוויטס", "watchers": "עוקבים חדשים בסטוקטוויטס", "short": "שורט 20%+", "low_float": "פלואט קטן",
    "rvol": "נפח חריג", "borrow_trend": "השאלה מתייקרת", "borrow_fee": "עמלת השאלה גבוהה", "xueqiu": "חמה בסין",
    "youtube": "YouTube", "rsplit": "איחוד מניות", "promoted": "קידום בתשלום", "word": "מילה (התראת שווא)",
    "one_author": "כותב אחד דוחף", "asia": "חברה מאסיה",
}


def tags_of(why):
    out = []
    for w in why or []:
        for frag, tag in TAGS:
            if frag in w:
                if tag not in out:
                    out.append(tag)
                break
    return out


def invalidation_level(price):
    """Price below which the alert's thesis is considered broken."""
    p, prev = price.get("price"), price.get("prev_close")
    if p and prev and prev < p:
        return round(prev, 4)
    return round(p * (1 - config.INVALIDATION_FALLBACK_PCT / 100), 4) if p else None


# ---------------- 1. candidate log ----------------
def log_candidates(store, report, now, alerted):
    """One record per ticker per day: first sighting fixes price/score/signals, later runs raise max_score."""
    log = store.load_json("candidates.json", {})
    day = now.date().isoformat()
    for c in report:
        p = c.get("price") or {}
        if c["score"] < config.CANDLOG_MIN_SCORE or not p.get("price"):
            continue
        k = f"{day}|{c['ticker']}"
        r = log.get(k)
        if r is None:
            log[k] = {"ts": now.isoformat(timespec="minutes"), "ticker": c["ticker"], "score": c["score"],
                      "max_score": c["score"], "price": p["price"], "move_pct": p.get("move_pct"),
                      "cap_m": (c.get("fund") or {}).get("market_cap_m"), "blocked": c.get("blocked") or [],
                      "tags": tags_of(c.get("why")), "alerted": c["ticker"] in alerted,
                      "urgent": bool(c.get("urgent")), "outcome": {}}
        else:
            r["max_score"] = max(r.get("max_score", 0), c["score"])
            r["alerted"] = r.get("alerted") or c["ticker"] in alerted
            r["urgent"] = r.get("urgent") or bool(c.get("urgent"))
            for t in tags_of(c.get("why")):
                if t not in r["tags"]:
                    r["tags"].append(t)
    cutoff = (now - timedelta(days=config.CANDLOG_KEEP_DAYS)).date().isoformat()
    log = {k: v for k, v in log.items() if k[:10] >= cutoff}
    store.save_json("candidates.json", log)
    return log


def evaluate_candidates(store, log, now):
    """Fill outcome from daily bars, at most once a day per record and CANDLOG_EVAL_PER_RUN records per run."""
    today = now.date().isoformat()
    todo = [r for r in log.values() if not r["outcome"].get("final") and r["outcome"].get("checked") != today
            and r["ts"][:10] < today]
    todo.sort(key=lambda r: r["ts"])
    for r in todo[:config.CANDLOG_EVAL_PER_RUN]:
        o = r["outcome"]
        o["checked"] = today
        bars = sources.daily_bars(r["ticker"], "1mo")
        after = [b for b in bars if b["date"] >= r["ts"][:10]]
        if not after:
            continue
        ep = r["price"]
        o["days"] = len(after)
        o["last_pct"] = round((after[min(4, len(after) - 1)]["close"] / ep - 1) * 100, 1)
        o["max_close_pct"] = round((max(b["close"] for b in after[:5]) / ep - 1) * 100, 1)
        o["max_high_pct"] = round((max((b["high"] or 0) for b in after[:5]) / ep - 1) * 100, 1)
        if len(after) >= 5:
            o["final"] = True
    store.save_json("candidates.json", log)


# ---------------- 2. weekly report ----------------
def _stats(rows):
    if not rows:
        return None
    hit = sum(1 for r in rows if (r["outcome"].get("max_close_pct") or -99) >= 30)
    med = statistics.median(r["outcome"].get("last_pct") or 0 for r in rows)
    return len(rows), round(hit / len(rows) * 100), med


def weekly_report(log, alerts):
    done = [r for r in log.values() if r["outcome"].get("final")]
    lines = ["📊 דוח שבועי – small-cap-buzz",
             f"מועמדות שנמדדו (5 ימי מסחר): {len(done)}"]
    if not done:
        lines.append("עדיין אין מספיק נתונים. הדוח יתמלא כשמועמדות ישלימו 5 ימי מסחר.")
        return "\n".join(lines)
    base = _stats(done)
    lines.append(f"בסיס: {base[1]}% הגיעו ל-+30% בסגירה תוך 5 ימים | חציון יום 5: {base[2]:+.1f}%")
    lines.append("")
    lines.append("לפי ציון (n | הגיעו ל-30% | חציון):")
    for lo, hi, name in ((config.CANDLOG_MIN_SCORE, 4, f"{config.CANDLOG_MIN_SCORE}-4"), (5, 5, "5"),
                         (6, 7, "6-7"), (8, 99, "8+")):
        s = _stats([r for r in done if lo <= r["max_score"] <= hi])
        if s:
            lines.append(f"• ציון {name}: {s[0]} | {s[1]}% | {s[2]:+.1f}%")
    for name, rows in (("לא נחסמו", [r for r in done if not r["blocked"]]), ("נחסמו", [r for r in done if r["blocked"]]),
                       ("קיבלו התראה", [r for r in done if r.get("alerted")]),
                       ("🔥 מועמדות חזקות", [r for r in done if r.get("urgent")])):
        s = _stats(rows)
        if s:
            lines.append(f"• {name}: {s[0]} | {s[1]}% | {s[2]:+.1f}%")
    lines.append("")
    lines.append(f"לפי סימן (לפחות {config.WEEKLY_MIN_N} מקרים; n | 30% | חציון | פי כמה מהבסיס):")
    rows = []
    for tag in TAG_HE:
        s = _stats([r for r in done if tag in r["tags"]])
        if s and s[0] >= config.WEEKLY_MIN_N:
            lift = (s[1] / base[1]) if base[1] else None
            rows.append((tag, s, lift))
    rows.sort(key=lambda x: (x[1][1], x[1][2]), reverse=True)
    for tag, s, lift in rows:
        lines.append(f"• {TAG_HE[tag]}: {s[0]} | {s[1]}% | {s[2]:+.1f}%" + (f" | x{lift:.1f}" if lift else ""))
    if not rows:
        lines.append("• עדיין אין סימן עם מספיק מקרים")
    # one suggestion: best and worst signals with enough data (no rule is changed automatically)
    if len(rows) >= 2:
        best, worst = rows[0], rows[-1]
        lines += ["", f"הצעה (לא מופעלת אוטומטית): לחזק את \"{TAG_HE[best[0]]}\" ({best[1][1]}% מול בסיס {base[1]}%)"
                      f" ולהחליש את \"{TAG_HE[worst[0]]}\" ({worst[1][1]}%). מדגם קטן, לבדוק שוב בשבוע הבא."]
    a_done = [a for a in alerts if a.get("outcome", {}).get("final")]
    if a_done:
        hit = sum(1 for a in a_done if (a["outcome"].get("max_close_pct") or -99) >= 30)
        lines += ["", f"התראות שהסתיימו: {len(a_done)}, מהן {hit} הגיעו ל-+30%"]
    lines.append("מידע לסריקה, לא המלצת השקעה.")
    return "\n".join(lines)


def maybe_send_weekly(state, log, alerts, now, send):
    """Once per ISO week, from Friday WEEKLY_HOUR_UTC (after the US close) or any time on the weekend."""
    wd = now.weekday()  # Mon=0 .. Sun=6
    if not (wd == 4 and now.hour >= config.WEEKLY_HOUR_UTC) and wd < 5:
        return None
    week = f"{now.isocalendar()[0]}-W{now.isocalendar()[1]:02d}"
    if state.get("_weekly_sent") == week:
        return None
    text = weekly_report(log, alerts)
    if send(text):
        state["_weekly_sent"] = week
        return "sent"
    return "failed"


# ---------------- 3. follow-ups on live alerts ----------------
def follow_up(alerts, now, send):
    """For the newest alert per ticker in the last FOLLOW_DAYS days: +30% / +60% and invalidation messages."""
    sent_msgs, seen = [], set()
    cutoff = now - timedelta(days=config.FOLLOW_DAYS)
    for a in sorted(alerts, key=lambda x: x["ts"], reverse=True):
        t = a["ticker"]
        if t in seen or datetime.fromisoformat(a["ts"]) < cutoff:
            continue
        seen.add(t)
        if a["ts"] == now.isoformat(timespec="minutes"):
            continue  # just alerted in this run
        ep = (a.get("price") or {}).get("price")
        if not ep:
            continue
        fu = a.setdefault("followups", {})
        inv = a.get("invalidation") or invalidation_level(a["price"])
        a["invalidation"] = inv
        q = sources.quick_price(t)
        if not q:
            continue
        px = q["price"]
        chg = (px / ep - 1) * 100
        msg, new_keys = None, []
        for lvl in config.FOLLOW_UP_LEVELS:
            key = f"up{lvl}"
            if chg >= lvl and not fu.get(key):
                fu[key] = now.isoformat(timespec="minutes"); new_keys.append(key)
                msg = (f"🚀 ${t}: +{chg:.0f}% ממחיר ההתראה ({ep} → {px})\n"
                       f"התראה מ-{a['ts'][:16].replace('T', ' ')} UTC. יעד {lvl}% הושג: זה הזמן לתוכנית היציאה "
                       f"(למשל מימוש חלק והזזת הסטופ למחיר הכניסה).")
        if inv and px < inv and not fu.get("stop") and not msg:
            fu["stop"] = now.isoformat(timespec="minutes"); new_keys.append("stop")
            msg = (f"🔻 ${t}: ירדה מתחת לרמת הביטול {inv} (עכשיו {px}, {chg:+.0f}% ממחיר ההתראה)\n"
                   f"התזה של ההתראה מ-{a['ts'][:16].replace('T', ' ')} UTC נכשלה.")
        if msg:
            urgent = bool(a.get("urgent")) and config.URGENT_FOLLOWUPS
            if send(msg + "\nמידע לסריקה, לא המלצת השקעה.", urgent):
                sent_msgs.append(t)
            else:  # not delivered: try again next run
                for k in new_keys:
                    fu.pop(k, None)
                sent_msgs.append(f"{t} (לא נשלח)")
    return sent_msgs
