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
