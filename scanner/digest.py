"""Daily digest: one file per day that gathers everything the hourly runs found.

data/runs_log.jsonl      one line per run: funnel, Telegram status, alerts sent, errors (14 days kept)
data/tg_hist.json        per day, per ticker: peak Telegram counts (numbers only, no chat names; SOCIAL_HIST_DAYS)
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
    for d in sorted(hist)[:-config.SOCIAL_HIST_DAYS]:
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
    ("gainers", "https://stockanalysis.com/markets/gainers/", 21),               # after the close (21 UTC also works in winter time)
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
    tg = (store.load_json("tg_hist.json", {}) or {}).get(day, {})
    for t, r in tg.items():  # discussed in Telegram: kept so we can check later whether the talk came first
        if r.get("authors24_max", 0) >= config.TG_MIN_AUTHORS or r.get("chats24_max", 0) >= config.TG_MIN_CHATS:
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
                found = list(dict.fromkeys(_HREF.findall(html)))
                for t in found:
                    add(t)
                scraped[name] = day
                if name == "premarket":  # ordered list for the X pilot (the premarket jumpers)
                    p = uni.setdefault("_premarket", {})
                    p[day] = [t.upper() for t in found[:20]]
                    for dd in sorted(p)[:-10]:
                        p.pop(dd)
                if name == "gainers":  # ordered list (page is sorted by % change) for the social look-back
                    g = uni.setdefault("_gainers", {})
                    g[day] = [t.upper() for t in found[:40]]
                    for d in sorted(g)[:-10]:
                        g.pop(d)
    old = (now - timedelta(days=config.DIGEST_UNIVERSE_DAYS)).date().isoformat()
    for t in [t for t, d in uni.items() if not t.startswith("_") and isinstance(d, str) and d < old]:
        uni.pop(t)
    store.save_json("closes_universe.json", uni)
    return uni


def _open_utc(day):
    """US regular-session open (09:30 New York) for `day` as an aware UTC datetime."""
    from zoneinfo import ZoneInfo
    from datetime import timezone
    d = datetime.fromisoformat(day)
    return d.replace(hour=9, minute=30, tzinfo=ZoneInfo("America/New_York")).astimezone(timezone.utc)


def _iso(dt):
    return dt.isoformat(timespec="minutes") if dt else None


def social_lookback(store, now, uni, alerts, cand_log, done):
    """For the day's top gainers: when did the talk start, in the LOOKBACK_DAYS before the jump and on the day?
    Sources: StockTwits (paged back via the public API), ApeWisdom snapshots, Reddit and Telegram histories,
    bot candidates/alerts. Counts and times only, never message text or user names. Returns {ticker: row}."""
    from datetime import timezone
    day = now.date().isoformat()
    gainers = ((uni.get("_gainers") or {}).get(day) or [])
    caps = store.load_json("caps.json", {})
    todo = [t for t in gainers if t not in done and t not in config.LARGE_CAPS
            and ((caps.get(t) or {}).get("cap") or 0) <= config.DIGEST_MAX_CAP_M][:config.LOOKBACK_TOP_N]
    if not todo:
        return {}
    open_utc = _open_utc(day)
    since = open_utc - timedelta(days=config.LOOKBACK_DAYS)
    win_days = [(now.date() - timedelta(days=i)).isoformat() for i in range(config.LOOKBACK_DAYS, -1, -1)]
    snaps = [s for s in store.load_history(days=config.LOOKBACK_DAYS + 2) if s["ts"] >= _iso(since)]
    rh = store.load_json("reddit_hist.json", {})
    th = store.load_json("tg_hist.json", {})
    out, t0 = {}, time.monotonic()
    rank_of = {t: i for i, t in enumerate(gainers, 1)}
    for t in todo:
        if time.monotonic() - t0 > config.LOOKBACK_BUDGET_S:
            break  # the rest is done by the next runs (they skip tickers already in the digest)
        firsts = {}  # source -> first aware datetime in the window
        for s in snaps:
            hit = (s.get("main") or {}).get(t) or next((d[t] for d in (s.get("subs") or {}).values() if t in d), None)
            if hit and hit[0] >= 1:
                firsts.setdefault("apewisdom", datetime.fromisoformat(s["ts"]))
        for name, hist in (("reddit", rh), ("telegram", th)):
            fs = [datetime.fromisoformat(hist[d][t]["first_utc"]) for d in win_days if t in (hist.get(d) or {})]
            fs = [f for f in fs if f >= since]
            if fs:
                firsts[name] = min(fs)
        bot = [a["ts"] for a in alerts if a["ticker"] == t and a["ts"] >= _iso(since)]
        bot += [v["ts"] for k, v in (cand_log or {}).items() if v.get("ticker") == t and v.get("ts", "") >= _iso(since)]
        if bot:
            firsts["bot"] = datetime.fromisoformat(min(bot))
        st = None
        n_err = len(sources.ERRORS)
        h = sources.stocktwits_history(t, since, config.LOOKBACK_ST_PAGES)
        del sources.ERRORS[n_err:]  # optional source
        if h is not None:
            ts = [x for x in h["times"] if x >= since]
            by_day = {}
            for x in ts:
                by_day[x.date().isoformat()] = by_day.get(x.date().isoformat(), 0) + 1
            before = [x for x in ts if x < open_utc]
            ts_sorted = sorted(ts)
            burst = next((x for i, x in enumerate(ts_sorted)
                          if sum(1 for y in ts_sorted[i:] if y - x <= timedelta(hours=6)) >= config.LOOKBACK_ST_BURST), None)
            st = {"by_day": by_day, "msgs_before_open": len(before),
                  "burst_utc": _iso(burst),  # first time with LOOKBACK_ST_BURST+ messages within 6h
                  "users_before_open": len({h["users"].get(x) for x in before}),
                  "first_utc": _iso(min(ts)) if ts else None,
                  "full_window": bool(h["reached"])}  # False = more messages than pages, window not fully read
            if burst:  # a single old message is noise; the burst is when the talk really started
                firsts["stocktwits"] = burst
        out[t] = {"rank": rank_of.get(t), "first": {k: _iso(v) for k, v in sorted(firsts.items(), key=lambda kv: kv[1])},
                  "stocktwits": st, "timing": _timing(firsts, open_utc, st)}
    return out


def _timing(firsts, open_utc, st):
    """Short Hebrew label, same vocabulary as the dashboard's reverse analysis."""
    tail = ""
    if st is None:
        tail = " (StockTwits לא זמין)"
    else:
        tail = f"; StockTwits {st['msgs_before_open']} הודעות לפני הפתיחה" + ("" if st["full_window"] else " (חלון חלקי)")
    if not firsts:
        return "אין שיח במקורות הבוט" + tail
    src, first = min(firsts.items(), key=lambda kv: kv[1])
    day_start = open_utc - timedelta(hours=9, minutes=30)  # 00:00 New York
    if first < day_start:
        days = (open_utc.date() - first.date()).days
        label = f"לפני – {days} ימים לפני ({src})"
    elif first < open_utc:
        label = f"בפרה-מרקט ({src})"
    else:
        label = f"במהלך/אחרי ({src})"
    return label + tail


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

    lookback = old.get("social_lookback") or {}
    if now.hour >= config.LOOKBACK_HOUR_UTC:
        try:
            lookback.update(social_lookback(store, now, uni, alerts, cand_log, lookback))
        except Exception as e:  # never breaks the digest
            sources.ERRORS.append(f"lookback: {type(e).__name__}: {e}")

    x_pilot = {}
    if now.hour >= config.LOOKBACK_HOUR_UTC:  # same time as the social look-back: after the close, once per day
        try:
            from . import x_buzz
            x_buzz.pilot(store, now, uni, _open_utc(day))
        except Exception as e:  # never breaks the digest
            sources.ERRORS.append(f"x_pilot: {type(e).__name__}: {e}")
    if config.X_PILOT_FROM <= day <= config.X_PILOT_UNTIL:
        x_pilot = store.load_json("x_pilot.json", {})
        x_pilot = {"today": (x_pilot.get("days") or {}).get(day, {}), "summary": x_pilot.get("summary"),
                   "credits": x_pilot.get("credits")}

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
          "social_today": {"reddit_top": top(reddit, 40), "telegram_top": top(tg, 40),
                           "telegram_all": tg},
          "social_lookback": lookback,
          "x_pilot": x_pilot,
          "errors_today": dict(sorted(errs.items(), key=lambda kv: -kv[1])[:30]),
          "closes_asof": closes_asof, "closes": closes}
    store.save_json(os.path.join(DIGEST_DIR, f"{day}.json"), dg)
    store.save_json(os.path.join(DIGEST_DIR, "latest.json"), dg)
    d = store.path(DIGEST_DIR)
    for fn in sorted(os.listdir(d)):
        if fn != "latest.json" and fn.endswith(".json") and fn[:10] < (now - timedelta(days=config.DIGEST_KEEP_DAYS)).date().isoformat():
            os.remove(os.path.join(d, fn))
    return dg
