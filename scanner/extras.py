"""Extra free sources. Every function fails soft (returns None/empty and logs to sources.ERRORS)."""
import re
import time
from datetime import datetime, timezone, timedelta

import requests

from . import config
from .sources import _get, ERRORS, UA_BROWSER, cik_map


STATUS = {}  # quiet status of optional sources, shown in the summary header instead of the error list


# ---------------- Borrow fee / availability: Interactive Brokers public short-stock file ----------------
_IB = None


def _ib_table():
    """Download IBKR's public file (ftp3.interactivebrokers.com, user 'shortstock') once per run.
    Columns: #SYM|CUR|NAME|CON|ISIN|REBATERATE|FEERATE|AVAILABLE|"""
    global _IB
    if _IB is not None:
        return _IB
    _IB = {}
    import ftplib, io
    buf, last = None, None
    for host in ("ftp2.interactivebrokers.com", "ftp3.interactivebrokers.com"):
        try:
            ftp = ftplib.FTP(host, timeout=30)
            ftp.login("shortstock", "")
            ftp.set_pasv(True)
            b = io.BytesIO()
            ftp.retrbinary("RETR usa.txt", b.write)
            ftp.quit()
            buf = b
            break
        except Exception as e:
            last = f"{host}: {type(e).__name__}: {e}"
    try:
        if buf is None:
            raise RuntimeError(last)
        for line in buf.getvalue().decode("latin-1").splitlines():
            if line.startswith("#") or "|" not in line:
                continue
            p = line.split("|")
            if len(p) < 8:
                continue
            avail = p[7].strip().lstrip(">")
            try:
                _IB[p[0].strip().upper()] = {"fee": float(p[6] or 0), "available": int(avail or 0)}
            except ValueError:
                continue
    except Exception as e:
        STATUS["ibkr"] = f"נכשל ({type(e).__name__})"
        ERRORS.append(f"IBKR short-stock file: {type(e).__name__}: {e}")
    if _IB:
        STATUS["ibkr"] = f"פעיל ({len(_IB):,} מניות)"
    return _IB


def borrow(ticker, hist=None):
    """{'fee','available','fee_5d','available_5d'} from today's IBKR file + our own saved history."""
    now = _ib_table().get(ticker.upper())
    if not now:
        return None
    out = dict(now, fee_5d=None, available_5d=None)
    if hist is not None:
        today = datetime.now(timezone.utc).date().isoformat()
        rows = hist.setdefault(ticker, {})
        rows[today] = [now["fee"], now["available"]]
        days = sorted(rows)[-15:]
        hist[ticker] = {d: rows[d] for d in days}
        old = [d for d in days if d <= (datetime.now(timezone.utc).date() - timedelta(days=5)).isoformat()]
        if old:
            out["fee_5d"], out["available_5d"] = rows[old[-1]]
    return out


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
    html = None
    for url in ("https://www.stockpromoters.com/", "https://stockpromoters.com/", "http://www.stockpromoters.com/"):
        try:
            r = requests.get(url, headers={"User-Agent": UA_BROWSER}, timeout=20)
            if r.status_code == 200 and len(r.text) > 2000:
                html = r.text
                break
        except Exception:
            continue
    out = set()
    if not html:
        STATUS["promoters"] = "חסום"
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
        s.headers.update({"User-Agent": UA_BROWSER, "Referer": "https://xueqiu.com/",
                          "Accept": "application/json, text/plain, */*", "Origin": "https://xueqiu.com"})
        s.get("https://xueqiu.com/", timeout=15)
        s.get("https://xueqiu.com/hq", timeout=15)
        last = None
        for params in ({"size": 50, "_type": 11, "type": 11}, {"size": 50, "_type": 10, "type": 11},
                       {"size": 50, "type": 11}, {"size": 50, "_type": 10, "type": 10}):
            r = s.get("https://stock.xueqiu.com/v5/stock/hot_stock/list.json", params=params, timeout=15)
            last = r.status_code
            if r.status_code != 200:
                continue
            items = ((r.json() or {}).get("data") or {}).get("items") or []
            out = {}
            for i, it in enumerate(items, 1):
                sym = (it.get("symbol") or "").upper()
                if re.fullmatch(r"[A-Z]{1,5}", sym):  # US tickers only (CN/HK symbols have prefixes/digits)
                    out[sym] = i
            if out:
                return out
        STATUS["xueqiu"] = f"חסום (HTTP {last})"
        return {}
    except Exception as e:
        STATUS["xueqiu"] = f"חסום ({type(e).__name__})"
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
