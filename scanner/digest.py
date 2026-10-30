"""Daily digest: one file per day that gathers everything the hourly runs found.

data/runs_log.jsonl      one line per run: funnel, Telegram status, alerts sent, errors (14 days kept)
data/tg_hist.json        per day, per ticker: peak Telegram counts (numbers only, no chat names; 10 days)
data/digest/<date>.json  the day's digest, rebuilt every run (cheap, local data only)
data/digest/latest.json  copy of the newest digest (a fixed address for readers)

After DIGEST_CLOSES_HOUR_UTC (after the US close) the first run of the day also adds daily closes for every
ticker the bot alerted or logged recently, so outcomes can be tracked without fetching prices elsewhere.
The digest is written for outside readers (the Claude scheduled scans): it must never contain secrets,
chat names or message text.
"""
import json
import os
import re
import time
from datetime import datetime, timedelta

from . import config, sources

DIGEST_DIR = "digest"


def log_run(store, now, funnel, tg_status, alerts_sent, errors):
    path = store.path("runs_log.jsonl")
    line = {"ts": now.isoformat(timespec="minutes"), "funnel": funnel,
            "telegram": {k: tg_status.get(k) for k in ("mode", "chats_ok", "chats_failed", "messages")},
            "alerts": alerts_sent, "errors_n": len(errors), "errors": [str(e)[:160] for e in errors[-10:]]}
    with open(path, "a") as f:
        f.write(json.dumps(line, ensure_ascii=False, separators=(",", ":")) + "\n")
    cutoff = (now - timedelta(days=config.DIGEST_KEEP_RUNLOG_DAYS)).isoformat(timespec="minutes")
    try:
        with open(path) as f:
            keep = [l for l in f if l.strip() and json.loads(l)["ts"] >= cutoff]
        with open(path, "w") as f:
            f.writelines(keep)
    except Exception:
        pass


def update_tg_hist(store, now, tg_counts):
    hist = store.load_json("tg_hist.json", {})
    day = hist.setdefault(now.strftime("%Y-%m-%d"), {})
    for t, r in tg_counts.items():
        h = day.setdefault(t, {"m24_max": 0, "authors24_max": 0, "chats24_max": 0, "first_utc": r["first_utc"]})
        h["m24_max"] = max(h["m24_max"], r["m24"])
        h["authors24_max"] = max(h["authors24_max"], r["authors24"])
        h["chats24_max"] = max(h["chats24_max"], r["chats24"])
        h["first_utc"] = min(h["first_utc"], r["first_utc"])
    for d in sorted(hist)[:-10]:
        hist.pop(d)
    store.save_json("tg_hist.json", hist)


def _runs_today(store, day):
    out = []
    try:
        with open(store.path("runs_log.jsonl")) as f:
            for l in f:
                if l.strip():
                    r = json.loads(l)
                    if r["ts"][:10] == day:
                        out.append(r)
    except FileNotFoundError:
        pass
    return out


def _alert_row(a):
    p, f = a.get("price") or {}, a.get("fund") or {}
    return {"ts": a["ts"], "ticker": a["ticker"], "score": a["score"], "path": a.get("path", "buzz"),
            "urgent": a.get("urgent", False), "price": p.get("price"), "prev_close": p.get("prev_close"),
            "move_pct": p.get("move_pct"), "rel_vol": p.get("rel_vol"), "cap_m": f.get("market_cap_m"),
            "float_m": f.get("float_m"), "short_pct": f.get("short_float_pct"),
            "invalidation": a.get("invalidation"), "why": a.get("why"),
            "news": (a.get("news") or {}).get("title"), "outcome": a.get("outcome") or {}}


def _sum_funnel(runs):
    tot = {}
    for r in runs:
        for k, v in (r.get("funnel") or {}).items():
            if isinstance(v, dict):
                d = tot.setdefault(k, {})
                for kk, vv in v.items():
                    d[kk] = d.get(kk, 0) + vv
            elif isinstance(v, (int, float)):
                tot[k] = tot.get(k, 0) + v
    return tot


def _closes(tickers, now):
    out, t0 = {}, time.monotonic()
    for t in tickers:
        if time.monotonic() - t0 > config.DIGEST_CLOSES_BUDGET_S:
            break
        bars = sources.daily_bars(t, "1mo")
        if bars:
            out[t] = [{"date": b["date"], "close": round(b["close"], 4), "high": round(b["high"] or b["close"], 4)}
                      for b in bars[-config.DIGEST_CLOSES_DAYS:]]
    return out


_HREF = re.compile(r'href="/stocks/([a-z0-9.\-]{1,8})/"')
# stockanalysis pages the scans pick stocks from; each is read once a day, from the given UTC hour on
UNIVERSE_PAGES = (
    ("premarket", "https://stockanalysis.com/markets/premarket/gainers/", 13),   # ~09:00 ET
    ("gainers", "https://stockanalysis.com/markets/gainers/", 20),               # after the close
    ("afterhours", "https://stockanalysis.com/markets/after-hours/gainers/", 21),
    ("shorted", "https://stockanalysis.com/list/most-shorted-stocks/", 13),
)


def update_universe(store, now, alerts, cand_log):
    """data/closes_universe.json: {ticker: last date it was relevant}. Everything the scans may track:
    alerts, candidates, tickers with 3+ forum mentions, fresh strong press releases, the day's premarket /
    regular / after-hours gainers and the most-shorted list, plus a one-time seed (TRACK_SEED).
    Kept DIGEST_UNIVERSE_DAYS after the last sighting; closes are collected for all of them."""
    from . import news
    uni = store.load_json("closes_universe.json", {})
    day = now.date().isoformat()
    caps = store.load_json("caps.json", {})
    big = lambda t: ((caps.get(t) or {}).get("cap") or 0) > config.DIGEST_MAX_CAP_M or t in config.LARGE_CAPS

    def add(t, force=False):
        t = (t or "").upper().strip()
        if t and len(t) <= 6 and (force or (not big(t) and t not in config.WORD_TICKERS)):
            uni[t] = max(uni.get(t, ""), day)

    if not uni.get("_seeded"):
        for t in config.TRACK_SEED:
            add(t, force=True)  # the dashboard tracks these as they are (word-like tickers included)
        uni["_seeded"] = day
    for a in alerts:
        if a["ts"][:10] == day:
            add(a["ticker"])
    for k, v in (cand_log or {}).items():
        if k[:10] == day:
            add(v["ticker"])
    hist = store.load_history(days=1)
    for s in hist[-1:]:
        for t, v in (s.get("main") or {}).items():
            if v[0] >= 3:
                add(t)
        for d in (s.get("subs") or {}).values():
            for t, v in d.items():
                if v[0] >= 3:
                    add(t)
    cut = (now - timedelta(hours=24)).isoformat(timespec="minutes")
    for c in store.load_json("pr_cache.json", []):
        if c["ts"] >= cut and news.classify(c["title"])[0] == "strong":
            add(c["ticker"])
    scraped = uni.setdefault("_scraped", {})
    for name, url, hour in UNIVERSE_PAGES:
        if now.hour >= hour and scraped.get(name) != day:
            n_err = len(sources.ERRORS)
            html = sources._get(url, expect_json=False, retries=1)
            del sources.ERRORS[n_err:]  # optional source
            if html:
                for t in dict.fromkeys(_HREF.findall(html)):
                    add(t)
                scraped[name] = day
    old = (now - timedelta(days=config.DIGEST_UNIVERSE_DAYS)).date().isoformat()
    for t in [t for t, d in uni.items() if not t.startswith("_") and d < old]:
        uni.pop(t)
    store.save_json("closes_universe.json", uni)
    return uni


def build(store, now, alerts, cand_log):
    day = now.date().isoformat()
    os.makedirs(store.path(DIGEST_DIR), exist_ok=True)
    path = store.path(os.path.join(DIGEST_DIR, f"{day}.json"))
    old = store.load_json(os.path.join(DIGEST_DIR, f"{day}.json"), {})
    runs = _runs_today(store, day)

    since10 = (now - timedelta(days=14)).isoformat(timespec="minutes")
    alerts_today = [_alert_row(a) for a in alerts if a["ts"][:10] == day]
    alerts_recent = [_alert_row(a) for a in alerts if a["ts"] >= since10 and a["ts"][:10] != day]
    cands_today = [dict(k=k, **{x: v.get(x) for x in ("ts", "ticker", "score", "max_score", "price", "move_pct",
                                                      "cap_m", "blocked", "tags", "alerted", "urgent")})
                   for k, v in (cand_log or {}).items() if k[:10] == day]
    for c in cands_today:
        c.pop("k", None)
    cands_measured = [{"date": k[:10], "ticker": v["ticker"], "max_score": v.get("max_score"), "price": v.get("price"),
                       "tags": v.get("tags"), "blocked": bool(v.get("blocked")), "alerted": v.get("alerted"),
                       "outcome": v.get("outcome")}
                      for k, v in (cand_log or {}).items() if (v.get("outcome") or {}).get("days")]
    reddit = (store.load_json("reddit_hist.json", {}) or {}).get(day, {})
    tg = (store.load_json("tg_hist.json", {}) or {}).get(day, {})
    top = lambda d, n: dict(sorted(d.items(), key=lambda kv: -kv[1].get("m24_max", 0))[:n])

    errs = {}
    for r in runs:
        for e in r.get("errors") or []:
            k = e.split(":")[0][:60] + ": " + e.split(":", 1)[-1][:80] if ":" in e else e[:140]
            errs[k] = errs.get(k, 0) + 1

    uni = update_universe(store, now, alerts, cand_log)
    closes, closes_asof = old.get("closes") or {}, old.get("closes_asof")
    if now.hour >= config.DIGEST_CLOSES_HOUR_UTC:
        # every run after the close fills what is still missing (late alerts, new universe names), within a budget
        order = ([a["ticker"] for a in alerts_today + alerts_recent]
                 + sorted((t for t in uni if not t.startswith("_")), key=lambda t: uni[t], reverse=True))
        todo = []
        for t in order:
            if t not in closes and t not in todo:
                todo.append(t)
        closes.update(_closes(todo[:config.DIGEST_CLOSES_MAX_TICKERS], now))
        closes_asof = day

    dg = {"date": day, "generated_at": now.isoformat(timespec="minutes"),
          "final": closes_asof == day,
          "runs": {"count": len(runs), "first": runs[0]["ts"] if runs else None, "last": runs[-1]["ts"] if runs else None,
                   "funnel_total": _sum_funnel(runs),
                   "telegram_last": runs[-1]["telegram"] if runs else None,
                   "per_run": [{"ts": r["ts"], "checked": (r.get("funnel") or {}).get("checked"),
                                "sent": (r.get("funnel") or {}).get("sent"),
                                "send_failed": (r.get("funnel") or {}).get("send_failed"),
                                "errors_n": r.get("errors_n")} for r in runs]},
          "alerts_today": alerts_today,
          "alerts_last_14d": alerts_recent,
          "candidates_today": sorted(cands_today, key=lambda c: -(c.get("max_score") or 0)),
          "candidates_measured": cands_measured,
          "social_today": {"reddit_top": top(reddit, 40), "telegram_top": top(tg, 40)},
          "errors_today": dict(sorted(errs.items(), key=lambda kv: -kv[1])[:30]),
          "closes_asof": closes_asof, "closes": closes}
    store.save_json(os.path.join(DIGEST_DIR, f"{day}.json"), dg)
    store.save_json(os.path.join(DIGEST_DIR, "latest.json"), dg)
    d = store.path(DIGEST_DIR)
    for fn in sorted(os.listdir(d)):
        if fn != "latest.json" and fn.endswith(".json") and fn[:10] < (now - timedelta(days=config.DIGEST_KEEP_DAYS)).date().isoformat():
            os.remove(os.path.join(d, fn))
    return dg
