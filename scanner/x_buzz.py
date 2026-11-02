"""X (Twitter) buzz through Weir (weirapp.io): a CONFIRMATION layer for candidates we already have, not discovery.

How it runs (cheap on purpose; Weir's free tier is 5,000 credits a month):
- Only in two daily slots (config.X_SLOTS_UTC, weekdays only). The first scan run at or after a slot hour does it.
- In a slot: one search per ticker for the top config.X_MAX_TICKERS candidates (bot candidates first, then the
  Telegram leaders), newest posts of the last 24h, up to config.X_POSTS_PER_TICKER posts each.
- Once a day: one narrow discovery search (min likes) whose cashtags are only logged, never alerted.
- Billing is 1 credit per call + 1 per post. Hard guards: a daily and a monthly credit cap, kept in data/x_buzz.json.
  402 (out of credit) stops X until next month; any other error just skips X for this run. The scan never breaks.

Privacy (the repo is public): only per-ticker counts are stored. No usernames, post text or ids.
Weir scrapes X (no licence from X), so access can change: everything here fails soft.
"""
import hashlib
import re
import time
from datetime import datetime, timezone, timedelta

import requests

from . import config
from .sources import ERRORS

URL = "https://api.weirapp.io/v1/x/search"
STATUS = {"mode": "off", "queried": 0, "credits": 0, "month_credits": 0, "hits": 0, "note": ""}
CASHTAG = re.compile(r"\$([A-Za-z]{1,5})\b")


def configured():
    return bool(config.WEIR_API_KEY)


def _due_slot(now, done):
    """The slot this run should fill, or None. A slot is filled by the first run at or after its hour."""
    if now.weekday() >= 5 and not config.X_WEEKENDS:
        return None
    due = [h for h in config.X_SLOTS_UTC if h <= now.hour]
    if not due:
        return None
    key = f"{now:%Y-%m-%d}T{max(due):02d}"
    return None if key in done else key


def _search(body):
    """(posts, credits, error_code). error_code: None, 'out_of_credit', 'auth', 'rate', 'error'."""
    try:
        r = requests.post(URL, json=body, timeout=config.X_HTTP_TIMEOUT_S,
                          headers={"Authorization": f"Bearer {config.WEIR_API_KEY}"})
    except requests.RequestException as e:
        return [], 0, f"error ({type(e).__name__})"
    if r.status_code == 402:
        return [], 0, "out_of_credit"
    if r.status_code in (401, 403):
        return [], 0, "auth"
    if r.status_code == 429:
        return [], 0, "rate"
    if r.status_code != 200:
        return [], 0, f"error (HTTP {r.status_code})"
    try:
        posts = r.json().get("data") or []
    except ValueError:
        return [], 0, "error (bad json)"
    try:
        credits = int(r.headers.get("x-credits-charged"))
    except (TypeError, ValueError):
        credits = 1 + len(posts)
    return posts, credits, None


def _clean(posts, ticker=None):
    """Drop reposts, cashtag-list spam and posts that don't really carry the ticker."""
    out, seen = [], set()
    for p in posts:
        pid = p.get("id")
        if not pid or pid in seen or p.get("retweetedPost"):
            continue
        seen.add(pid)
        tags = {m.upper() for m in CASHTAG.findall(p.get("text") or "")}
        if len(tags) > config.X_MAX_CASHTAGS_PER_POST:
            continue
        if ticker and ticker not in tags:
            continue
        out.append(p)
    return out


def _summarize(posts, now):
    authors, likes, m6 = set(), 0, 0
    for p in posts:
        authors.add(((p.get("author") or {}).get("id")) or ((p.get("author") or {}).get("username")))
        likes += p.get("likeCount") or 0
        try:
            if now - datetime.fromisoformat(p["createdAt"].replace("Z", "+00:00")) <= timedelta(hours=6):
                m6 += 1
        except (KeyError, ValueError, AttributeError):
            pass
    return {"posts": len(posts), "posts6": m6, "authors": len(authors - {None}), "likes": likes}


def run(store, now, tickers):
    """Fill a due slot (if any) and return {ticker: counts} that are still fresh enough to score."""
    STATUS.update(mode="off", queried=0, credits=0, hits=0, note="")
    state = store.load_json("x_buzz.json", {})
    month = f"{now:%Y-%m}"
    if state.get("month") != month:
        state.update(month=month, month_credits=0, exhausted=False)
    day = f"{now:%Y-%m-%d}"
    if state.get("day") != day:
        state.update(day=day, day_credits=0)
    state.setdefault("done", [])
    state.setdefault("tickers", {})
    STATUS["month_credits"] = state["month_credits"]

    if not configured():
        return _fresh(state, now)
    if state.get("exhausted"):
        STATUS.update(mode="out_of_credit", note="נגמרו הקרדיטים החודשיים")
        return _fresh(state, now)
    slot = _due_slot(now, set(state["done"]))
    if not slot:
        STATUS.update(mode="idle", note="לא בחלון X")
        return _fresh(state, now)

    STATUS["mode"] = "ok"
    since = (now - timedelta(hours=24)).strftime("%Y-%m-%dT%H:%M:%SZ")
    t0, stop, spent = time.monotonic(), None, 0

    def can_spend(cost):
        return (state["month_credits"] + spent + cost <= config.X_MONTHLY_CREDITS
                and state["day_credits"] + spent + cost <= config.X_DAILY_CREDITS)

    queried = []
    for t in tickers[:config.X_MAX_TICKERS]:
        if time.monotonic() - t0 > config.X_TIME_BUDGET_S:
            stop = "time"
            break
        if not can_spend(1 + config.X_POSTS_PER_TICKER):
            stop = "budget"
            break
        posts, cost, err = _search({"phrase": f"${t}", "since": since, "sort": "latest",
                                    "limit": config.X_POSTS_PER_TICKER})
        spent += cost
        if err:
            stop = err
            break
        queried.append(t)
        state["tickers"][t] = dict(_summarize(_clean(posts, t), now), ts=now.isoformat(timespec="minutes"),
                                   full=len(posts) >= config.X_POSTS_PER_TICKER)
        time.sleep(config.X_SLEEP_S)

    # one narrow discovery search per day; its tickers are only logged (measure first, alert never)
    if not stop and state.get("discovery_day") != day and can_spend(1 + config.X_DISCOVERY_LIMIT):
        posts, cost, err = _search({"query": config.X_DISCOVERY_QUERY, "language": "en", "since": since,
                                    "minLikes": config.X_DISCOVERY_MIN_LIKES, "sort": "top",
                                    "limit": config.X_DISCOVERY_LIMIT})
        spent += cost
        if err:
            stop = err
        else:
            counts = {}
            for p in _clean(posts):
                for tag in {m.upper() for m in CASHTAG.findall(p.get("text") or "")}:
                    counts[tag] = counts.get(tag, 0) + 1
            state["discovery"] = {"day": day, "tickers": dict(sorted(counts.items(), key=lambda kv: -kv[1])[:20])}
            state["discovery_day"] = day

    state["month_credits"] += spent
    state["day_credits"] += spent
    if stop == "out_of_credit":
        state["exhausted"] = True
        STATUS.update(mode="out_of_credit", note="402: נגמרו הקרדיטים")
        ERRORS.append("x_buzz: Weir 402 out of credit – X paused until next month")
    elif stop == "auth":
        STATUS.update(mode="failed", note="המפתח נדחה (401/403)")
        ERRORS.append("x_buzz: Weir key rejected (401/403) – check the WEIR_API_KEY secret")
    elif stop and stop not in ("time", "budget"):
        STATUS.update(mode="failed", note=stop)
        ERRORS.append(f"x_buzz: Weir {stop} – slot will retry next run")
    elif stop:
        STATUS["note"] = "עצר: " + ("תקציב זמן" if stop == "time" else "תקרת קרדיטים")
    # the slot counts as done unless a temporary failure stopped it before anything was read
    if not (stop in ("rate",) or (stop or "").startswith("error")) or queried:
        state["done"] = (state["done"] + [slot])[-20:]
    # forget old tickers so the public file stays small
    cutoff = (now - timedelta(days=3)).isoformat(timespec="minutes")
    state["tickers"] = {k: v for k, v in state["tickers"].items() if v.get("ts", "") >= cutoff}
    store.save_json("x_buzz.json", state)
    STATUS.update(queried=len(queried), credits=spent, month_credits=state["month_credits"])
    fresh = _fresh(state, now)
    STATUS["hits"] = sum(1 for v in fresh.values() if is_buzz(v))
    return fresh


def _fresh(state, now):
    cutoff = now - timedelta(hours=config.X_MAX_AGE_H)
    out = {}
    for t, v in (state.get("tickers") or {}).items():
        try:
            if datetime.fromisoformat(v["ts"]) >= cutoff:
                out[t] = v
        except (KeyError, ValueError):
            pass
    return out


def is_buzz(v):
    return bool(v) and v["authors"] >= config.X_MIN_AUTHORS and (
        v["posts"] >= config.X_MIN_POSTS or v["likes"] >= config.X_MIN_LIKES)


def why_line(v):
    """Text for the candidate's 'why' list (review.TAGS turns it into the x_buzz / x_weak tag), or None."""
    if not v or not v.get("posts"):
        return None
    more = "+" if v.get("full") else ""
    txt = f"{v['posts']}{more} פוסטים מ-{v['authors']} כותבים, {v['likes']} לייקים ב-24ש ({v['posts6']} ב-6ש)"
    return ("באז ב-X: " if is_buzz(v) else "X רגיל: ") + txt


# ---------- reverse-analysis pilot ----------
def _pt(s):
    try:
        return datetime.fromisoformat(s.replace("Z", "+00:00"))
    except (AttributeError, ValueError):
        return None


def _pilot_row(posts, ticker, open_utc, pm_start_utc):
    """Counts of cleaned posts BEFORE the open. `window_ok` is False when the API ignored `until` (posts after the
    open came back), in which case the numbers can't be trusted and the row is left out of the summary."""
    posts = _clean(posts, ticker)
    times = [(p, _pt(p.get("createdAt"))) for p in posts]
    times = [(p, t) for p, t in times if t]
    after = [1 for _, t in times if t >= open_utc]
    before = [(p, t) for p, t in times if t < open_utc]
    pre_pm = [(p, t) for p, t in before if t < pm_start_utc]
    authors = lambda xs: len({((p.get("author") or {}).get("id")) or ((p.get("author") or {}).get("username")) for p, _ in xs} - {None})
    first = min((t for _, t in before), default=None)
    return {"posts_before_open": len(before), "authors_before_open": authors(before),
            "likes_before_open": sum(p.get("likeCount") or 0 for p, _ in before),
            "posts_before_premarket": len(pre_pm), "authors_before_premarket": authors(pre_pm),
            "first_utc": first.isoformat(timespec="minutes") if first else None,
            "lead_h": round((open_utc - first).total_seconds() / 3600, 1) if first else None,
            "full": len(posts) >= config.X_PILOT_POSTS, "window_ok": not after}


def _pilot_summary(days):
    """Per group: n, with any X post before premarket / before open (3+ authors), median hours of lead."""
    import statistics
    out = {}
    for grp in ("עולה", "פרה", "בקרה"):
        rows = [r for d in days.values() for r in d.values() if r.get("group") == grp and r.get("window_ok")]
        leads = [r["lead_h"] for r in rows if r.get("lead_h") is not None and r["authors_before_open"] >= 3]
        out[grp] = {"n": len(rows),
                    "x3_before_open": sum(1 for r in rows if r["authors_before_open"] >= 3),
                    "x3_before_premarket": sum(1 for r in rows if r["authors_before_premarket"] >= 3),
                    "median_lead_h": round(statistics.median(leads), 1) if leads else None}
    return out


def pilot(store, now, uni, open_utc):
    """Run once per weekday inside [X_PILOT_FROM, X_PILOT_UNTIL]. Returns today's rows ({} if nothing ran).
    Persists data/x_pilot.json = {"days": {date: {ticker: row}}, "summary": {...}, "credits": n}."""
    from datetime import timezone
    from zoneinfo import ZoneInfo
    day = now.date().isoformat()
    if not configured() or now.weekday() >= 5 or not (config.X_PILOT_FROM <= day <= config.X_PILOT_UNTIL):
        return {}
    st = store.load_json("x_pilot.json", {"days": {}, "credits": 0})
    st.setdefault("days", {})
    done = st["days"].setdefault(day, {})
    gainers = ((uni.get("_gainers") or {}).get(day) or [])
    prem = ((uni.get("_premarket") or {}).get(day) or [])
    caps = store.load_json("caps.json", {})
    ok = lambda t: t not in config.LARGE_CAPS and t not in config.WORD_TICKERS and ((caps.get(t) or {}).get("cap") or 0) <= config.DIGEST_MAX_CAP_M
    plan = [(t, "עולה") for t in [t for t in gainers if ok(t)][:config.X_PILOT_GAINERS]]
    plan += [(t, "פרה") for t in [t for t in prem if ok(t) and t not in gainers][:config.X_PILOT_PREMARKET]]
    taken = {t for t, _ in plan} | set(gainers) | set(prem)
    pool = sorted((t for t, d in uni.items() if not t.startswith("_") and isinstance(d, str) and t not in taken and ok(t)
                   and (caps.get(t) or {}).get("cap")), key=lambda t: hashlib.md5(f"{day}{t}".encode()).hexdigest())
    plan += [(t, "בקרה") for t in pool[:config.X_PILOT_CONTROL]]
    if not plan:
        return {}
    state = store.load_json("x_buzz.json", {})
    month_used = state.get("month_credits", 0) if state.get("month") == f"{now:%Y-%m}" else 0
    since = (open_utc - timedelta(hours=config.X_PILOT_HOURS)).strftime("%Y-%m-%dT%H:%M:%SZ")
    until = open_utc.strftime("%Y-%m-%dT%H:%M:%SZ")
    pm_start = datetime(open_utc.astimezone(ZoneInfo("America/New_York")).year, open_utc.astimezone(ZoneInfo("America/New_York")).month,
                        open_utc.astimezone(ZoneInfo("America/New_York")).day, 4, 0, tzinfo=ZoneInfo("America/New_York")).astimezone(timezone.utc)
    t0, spent, stop = time.monotonic(), 0, None
    for t, grp in plan:
        if t in done:
            continue
        cost_max = 1 + config.X_PILOT_POSTS
        if (st.get("credits", 0) + spent + cost_max > config.X_PILOT_CREDITS_MAX
                or month_used + spent + cost_max > config.X_MONTHLY_HARD
                or time.monotonic() - t0 > config.X_TIME_BUDGET_S * 2):
            stop = "budget"
            break
        posts, cost, err = _search({"phrase": f"${t}", "since": since, "until": until, "sort": "latest",
                                    "limit": config.X_PILOT_POSTS})
        spent += cost
        if err:
            stop = err
            break
        row = _pilot_row(posts, t, open_utc, pm_start)
        row["group"] = grp
        done[t] = row
        time.sleep(config.X_SLEEP_S)
    st["credits"] = st.get("credits", 0) + spent
    st["days"] = {d: v for d, v in sorted(st["days"].items())[-15:]}
    st["summary"] = _pilot_summary(st["days"])
    store.save_json("x_pilot.json", st)
    if spent:  # keep the shared monthly counter honest
        state.setdefault("month", f"{now:%Y-%m}")
        state["month_credits"] = state.get("month_credits", 0) + spent
        store.save_json("x_buzz.json", state)
    if stop in ("auth", "out_of_credit") or (stop or "").startswith("error") or stop == "rate":
        ERRORS.append(f"x_pilot: Weir {stop}")
    return done
