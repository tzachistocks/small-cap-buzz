"""Extra free sources. Every function fails soft (returns None/empty and logs to sources.ERRORS)."""
import re
import time
from datetime import datetime, timezone, timedelta

import requests

from . import config
from .sources import _get, ERRORS, UA_BROWSER, cik_map


# ---------------- iBorrowDesk: borrow fee and shares available to short (IBKR data) ----------------
def borrow(ticker):
    d = _get(f"https://iborrowdesk.com/api/ticker/{ticker}", retries=1)
    if not d:
        return None
    rows = d.get("real_time") or []
    daily = d.get("daily") or []
    try:
        now = rows[0] if rows else (daily[-1] if daily else None)
        if not now:
            return None
        fee = float(now.get("fee") or 0)
        avail = int(now.get("available") or 0)
        old = daily[-6] if len(daily) >= 6 else (daily[0] if daily else None)
        fee_5d = float(old.get("fee") or 0) if old else None
        avail_5d = int(old.get("available") or 0) if old else None
        return {"fee": fee, "available": avail, "fee_5d": fee_5d, "available_5d": avail_5d}
    except (TypeError, ValueError, KeyError, IndexError):
        return None


# ---------------- StockTwits watchers history ----------------
def watchers_growth(hist, ticker, count):
    """hist: {ticker: [[iso_ts, count], ...]}. Appends count, returns (added_24h, pct_24h)."""
    if count is None:
        return None, None
    now = datetime.now(timezone.utc)
    rows = hist.setdefault(ticker, [])
    rows.append([now.isoformat(timespec="minutes"), int(count)])
    cutoff = now - timedelta(days=8)
    hist[ticker] = rows = [r for r in rows if datetime.fromisoformat(r[0]) >= cutoff][-400:]
    past = [r for r in rows if datetime.fromisoformat(r[0]) <= now - timedelta(hours=20)]
    if not past:
        return None, None
    base = past[-1][1]
    added = int(count) - base
    return added, round(added / base * 100, 1) if base else None


# ---------------- Paid stock promotions (red flag) ----------------
def promoted_tickers(known):
    """Tickers currently promoted on stockpromoters.com (paid promotions). Empty set on failure."""
    html = _get("https://www.stockpromoters.com/", expect_json=False, retries=1)
    out = set()
    if not html:
        return out
    text = re.sub(r"<[^>]+>", " ", html)
    for pat in (r"\b(?:NASDAQ|NYSE|AMEX|OTC|OTCQB|OTCQX|NYSE American|NYSEAMERICAN)\s*[:\-]\s*([A-Z]{1,5})\b",
                r"\$([A-Z]{2,5})\b", r"[?&/](?:symbol|ticker|t)=([A-Z]{1,5})\b"):
        for t in re.findall(pat, text + " " + html):
            if t in known and t not in config.WORD_TICKERS:
                out.add(t)
    return out


# ---------------- Xueqiu (雪球) – hot US stocks among Chinese retail investors ----------------
def xueqiu_hot_us():
    """{ticker: rank} from Xueqiu's hot US stock list. Needs a cookie from the homepage; often blocked abroad."""
    try:
        s = requests.Session()
        s.headers.update({"User-Agent": UA_BROWSER, "Referer": "https://xueqiu.com/"})
        s.get("https://xueqiu.com/", timeout=15)
        out = {}
        for typ in (11,):  # 11 = US market
            r = s.get("https://stock.xueqiu.com/v5/stock/hot_stock/list.json",
                      params={"size": 50, "_type": typ, "type": typ}, timeout=15)
            if r.status_code != 200:
                ERRORS.append(f"Xueqiu HTTP {r.status_code}")
                continue
            items = ((r.json() or {}).get("data") or {}).get("items") or []
            for i, it in enumerate(items, 1):
                sym = (it.get("symbol") or "").upper()
                if re.fullmatch(r"[A-Z]{1,5}", sym):
                    out[sym] = i
        return out
    except Exception as e:
        ERRORS.append(f"Xueqiu {type(e).__name__}: {e}")
        return {}


# ---------------- YouTube (optional: needs free YOUTUBE_API_KEY) ----------------
def youtube_mentions(known):
    """Tickers in titles of videos published in the last 12h for penny-stock searches. 100 quota units/run."""
    key = config.YOUTUBE_API_KEY
    if not key:
        return {}
    after = (datetime.now(timezone.utc) - timedelta(hours=12)).strftime("%Y-%m-%dT%H:%M:%SZ")
    d = _get("https://www.googleapis.com/youtube/v3/search",
             params={"part": "snippet", "q": "penny stock|small cap stock|short squeeze stock", "type": "video",
                     "order": "date", "publishedAfter": after, "maxResults": 50, "key": key}, retries=0)
    out = {}
    if not d:
        return out
    for it in d.get("items", []):
        sn = it.get("snippet") or {}
        text = f"{sn.get('title', '')} {sn.get('description', '')}"
        ch = sn.get("channelTitle", "")
        for t in set(re.findall(r"\$?\b([A-Z]{2,5})\b", text)):
            if t in known and t not in config.WORD_TICKERS and t not in config.LARGE_CAPS:
                o = out.setdefault(t, {"videos": 0, "channels": set()})
                o["videos"] += 1
                o["channels"].add(ch)
    return {t: {"videos": o["videos"], "channels": len(o["channels"])} for t, o in out.items()}


# ---------------- SEC company profile: country (China/HK flag) ----------------
ASIA_CODES = {"F4": "סין", "K3": "הונג קונג", "F5": "טייוואן", "U0": "סינגפור", "N8": "מלזיה", "1B": "מקאו"}
OFFSHORE_CODES = {"E9": "איי קיימן", "D8": "איי הבתולה הבריטיים", "D0": "ברמודה"}


def sec_profile(ticker, cache, cik_path):
    """{'country': code, 'asia': name or None, 'inc': code}. Cached 30 days in `cache` dict."""
    c = cache.get(ticker)
    if c and (datetime.now(timezone.utc) - datetime.fromisoformat(c["ts"])).days < 30:
        return c
    cik = cik_map(cik_path).get(ticker.upper())
    if not cik:
        return None
    d = _get(f"https://data.sec.gov/submissions/CIK{cik:010d}.json", headers={"User-Agent": config.SEC_USER_AGENT})
    if not d:
        return None
    addr = (d.get("addresses") or {}).get("business") or {}
    country = addr.get("stateOrCountry") or ""
    inc = d.get("stateOfIncorporation") or ""
    asia = ASIA_CODES.get(country) or (("רשומה ב" + OFFSHORE_CODES[inc]) if inc in OFFSHORE_CODES and country in ASIA_CODES else None)
    out = {"country": country, "inc": inc, "asia": asia, "sic": d.get("sicDescription"),
           "ts": datetime.now(timezone.utc).isoformat(timespec="minutes")}
    cache[ticker] = out
    return out
