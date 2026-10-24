"""Network data sources. Every function fails soft: returns None/empty and records the error."""
import re
import time
import json
import os
from datetime import datetime, timezone, timedelta

import requests

from . import config

UA_BROWSER = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
              "(KHTML, like Gecko) Chrome/124.0 Safari/537.36")
ERRORS = []  # human-readable problems collected during a run


def _get(url, headers=None, params=None, timeout=20, retries=2, expect_json=True):
    h = {"User-Agent": UA_BROWSER, "Accept": "application/json, text/html;q=0.9, */*;q=0.8"}
    if headers:
        h.update(headers)
    last = None
    for attempt in range(retries + 1):
        try:
            r = requests.get(url, headers=h, params=params, timeout=timeout)
            if r.status_code == 429:
                last = f"429 {url}"
                time.sleep(3 * (attempt + 1))
                continue
            if r.status_code >= 400:
                last = f"HTTP {r.status_code} {url}"
                break
            return r.json() if expect_json else r.text
        except Exception as e:  # network / json errors
            last = f"{type(e).__name__} {url}: {e}"
            time.sleep(1.5)
    ERRORS.append(last)
    return None


# ---------------- ApeWisdom ----------------
def apewisdom(filter_name, pages=1):
    rows = []
    for p in range(1, pages + 1):
        d = _get(f"https://apewisdom.io/api/v1.0/filter/{filter_name}/page/{p}")
        if not d or "results" not in d:
            break
        for x in d["results"]:
            rows.append({
                "ticker": (x.get("ticker") or "").upper(),
                "name": x.get("name") or "",
                "mentions": int(x.get("mentions") or 0),
                "mentions_24h_ago": int(x.get("mentions_24h_ago") or 0),
                "rank": x.get("rank"),
                "upvotes": int(x.get("upvotes") or 0),
            })
        if p >= int(d.get("pages") or 1):
            break
    return rows


# ---------------- StockTwits ----------------
def stocktwits_trending():
    d = _get("https://api.stocktwits.com/api/2/trending/symbols.json")
    out = {}
    if d and "symbols" in d:
        for s in d["symbols"]:
            out[s.get("symbol", "").upper()] = {
                "title": s.get("title"),
                "summary": ((s.get("trending_summary") or {}).get("summary") if isinstance(s.get("trending_summary"), dict) else s.get("trending_summary")) or "",
                "watchlist_count": s.get("watchlist_count"),
            }
    return out


def stocktwits_stream(ticker):
    """Messages per hour over the latest page (max 30 msgs) + bull/bear counts."""
    d = _get(f"https://api.stocktwits.com/api/2/streams/symbol/{ticker}.json", retries=1)
    if not d or "messages" not in d:
        return None
    now = datetime.now(timezone.utc)
    times, bull, bear, users = [], 0, 0, set()
    for m in d["messages"]:
        try:
            t = datetime.strptime(m["created_at"], "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
        except Exception:
            continue
        times.append(t)
        users.add((m.get("user") or {}).get("id"))
        s = ((m.get("entities") or {}).get("sentiment") or {}) or {}
        if s.get("basic") == "Bullish":
            bull += 1
        elif s.get("basic") == "Bearish":
            bear += 1
    if not times:
        return {"msgs_6h": 0, "users": 0, "bull": 0, "bear": 0, "watchlist": None}
    msgs_6h = sum(1 for t in times if now - t <= timedelta(hours=6))
    sym = d.get("symbol") or {}
    return {"msgs_6h": msgs_6h, "users": len(users), "bull": bull, "bear": bear,
            "oldest_min": int((now - min(times)).total_seconds() / 60),
            "watchlist": sym.get("watchlist_count")}


# ---------------- Reddit (optional) ----------------
_TICKER_DOLLAR = re.compile(r"\$([A-Za-z]{1,5})\b")
_TICKER_CAPS = re.compile(r"\b([A-Z]{2,5})\b")


def extract_tickers(text, known):
    """$TICKER always counts (if known); bare CAPS counts only if known and not a common word."""
    found = set()
    if not text:
        return found
    for m in _TICKER_DOLLAR.findall(text):
        t = m.upper()
        if t in known and t not in config.LARGE_CAPS:
            found.add(t)
    for t in _TICKER_CAPS.findall(text):
        if t in known and t not in config.WORD_TICKERS and t not in config.LARGE_CAPS:
            found.add(t)
    return found


def reddit_token():
    if not (config.REDDIT_CLIENT_ID and config.REDDIT_SECRET):
        return None
    try:
        r = requests.post("https://www.reddit.com/api/v1/access_token",
                          auth=(config.REDDIT_CLIENT_ID, config.REDDIT_SECRET),
                          data={"grant_type": "client_credentials"},
                          headers={"User-Agent": config.REDDIT_USER_AGENT}, timeout=20)
        if r.status_code != 200:
            ERRORS.append(f"Reddit token HTTP {r.status_code}: {r.text[:120]}")
            return None
        return r.json().get("access_token")
    except Exception as e:
        ERRORS.append(f"Reddit token error: {e}")
        return None


def reddit_items(token, sub, kind="new", limit=100):
    url = f"https://oauth.reddit.com/r/{sub}/{kind}"
    d = _get(url, headers={"Authorization": f"bearer {token}", "User-Agent": config.REDDIT_USER_AGENT},
             params={"limit": limit, "raw_json": 1})
    out = []
    if not d:
        return out
    for c in (d.get("data") or {}).get("children", []):
        x = c.get("data") or {}
        out.append({
            "sub": sub,
            "author": x.get("author"),
            "created": x.get("created_utc"),
            "text": " ".join(filter(None, [x.get("title"), x.get("selftext"), x.get("body")])),
            "title": x.get("title") or (x.get("body") or "")[:80],
            "kind": "post" if kind == "new" else "comment",
        })
    return out


RSS_GROUPS = [
    # Only the small-cap forums: Reddit rate-limits anonymous feeds after ~2 requests per run, and the big
    # forums (wallstreetbets, stocks...) are already covered by ApeWisdom. The 2nd request is the comments feed.
    ["pennystocks", "Shortsqueeze", "smallstreetbets", "RobinHoodPennyStocks"],
]


def reddit_rss_items(subs, kind="new"):
    """Public RSS (Atom) feed of new posts for several subs in ONE request (multireddit a+b+c).
    Reddit rate-limits unauthenticated feeds hard, so we make few requests and back off on 429."""
    import xml.etree.ElementTree as ET
    ns = {"a": "http://www.w3.org/2005/Atom"}
    url = f"https://www.reddit.com/r/{'+'.join(subs)}/{kind}/.rss"
    xml = None
    for attempt in range(3):
        try:
            r = requests.get(url, params={"limit": 100}, timeout=25,
                             headers={"User-Agent": config.REDDIT_USER_AGENT, "Accept": "application/atom+xml"})
        except Exception as e:
            ERRORS.append(f"Reddit RSS {type(e).__name__}: {e}")
            return []
        if r.status_code == 429:
            wait = int(r.headers.get("Retry-After", "0") or 0) or 15 * (attempt + 1)
            time.sleep(min(wait, 45))
            continue
        if r.status_code != 200:
            ERRORS.append(f"Reddit RSS HTTP {r.status_code} {url}")
            return []
        xml = r.text
        break
    if not xml:
        if kind == "new":  # comments are a bonus; a 429 there is expected sometimes
            ERRORS.append(f"Reddit RSS 429 (rate limit) {url}")
        return []
    try:
        root = ET.fromstring(xml)
    except ET.ParseError:
        ERRORS.append(f"Reddit RSS parse error {url}")
        return []
    out = []
    for e in root.findall("a:entry", ns):
        author = (e.findtext("a:author/a:name", default="", namespaces=ns) or "").replace("/u/", "")
        upd = e.findtext("a:published", default="", namespaces=ns) or e.findtext("a:updated", default="", namespaces=ns)
        try:
            ts = datetime.fromisoformat(upd.replace("Z", "+00:00")).timestamp()
        except ValueError:
            ts = None
        cat = e.find("a:category", ns)
        sub = cat.get("term") if cat is not None else subs[0]
        title = e.findtext("a:title", default="", namespaces=ns) or ""
        body = re.sub(r"<[^>]+>", " ", e.findtext("a:content", default="", namespaces=ns) or "")
        out.append({"sub": sub, "author": author, "created": ts, "text": f"{title} {body}",
                    "title": title, "kind": "post" if kind == "new" else "comment"})
    return out


REDDIT_MODE = "off"


def reddit_counts(known):
    """Per ticker: mentions/authors/subs in last 6h and 24h. API if credentials exist, else public RSS."""
    global REDDIT_MODE
    tok = reddit_token()
    now = time.time()
    agg = {}
    fetched_any = False
    batches = ([(sub, kind) for sub in config.REDDIT_SUBS for kind in ("new", "comments")] if tok
               else [(g, "new") for g in RSS_GROUPS] + [(RSS_GROUPS[0], "comments")])
    for i, (sub, kind) in enumerate(batches):
        if not tok and i:
            time.sleep(8 if kind == "new" else 20)
        items = reddit_items(tok, sub, kind) if tok else reddit_rss_items(sub, kind)
        if items:
            fetched_any = fetched_any or bool(items)
            for it in items:
                if not it["created"]:
                    continue
                age_h = (now - it["created"]) / 3600
                if age_h > 24:
                    continue
                for t in extract_tickers(it["text"], known):
                    a = agg.setdefault(t, {"m6": 0, "m24": 0, "authors6": set(), "authors24": set(),
                                           "subs6": set(), "subs24": set(), "first_ts": it["created"], "sample": ""})
                    a["m24"] += 1
                    a["authors24"].add(it["author"])
                    a["subs24"].add(it["sub"])
                    a["first_ts"] = min(a["first_ts"], it["created"])
                    if age_h <= 6:
                        a["m6"] += 1
                        a["authors6"].add(it["author"])
                        a["subs6"].add(it["sub"])
                    if it["kind"] == "post" and not a["sample"]:
                        a["sample"] = f"r/{it['sub']}: {it['title'][:90]}"
        if tok:
            time.sleep(0.7)
    REDDIT_MODE = ("api" if tok else "rss") if fetched_any else "failed"
    if not fetched_any:
        return {}, False
    out = {}
    for t, a in agg.items():
        out[t] = {"m6": a["m6"], "m24": a["m24"], "authors6": len(a["authors6"]), "authors24": len(a["authors24"]),
                  "subs6": sorted(a["subs6"]), "subs24": sorted(a["subs24"]),
                  "first_utc": datetime.fromtimestamp(a["first_ts"], timezone.utc).isoformat(timespec="minutes"),
                  "sample": a["sample"]}
    return out, True


# ---------------- Prices (Yahoo chart) ----------------
def price_snapshot(ticker):
    """Latest price incl. pre/post market, previous close, today's volume, 20d avg volume."""
    d = _get(f"https://query1.finance.yahoo.com/v8/finance/chart/{ticker}",
             params={"range": "1d", "interval": "5m", "includePrePost": "true"})
    try:
        res = d["chart"]["result"][0]
        meta = res["meta"]
    except Exception:
        return None
    # early pre-market (or a quiet day) can return no intraday bars at all: fall back to meta
    q5 = ((res.get("indicators") or {}).get("quote") or [{}])[0] or {}
    closes = [c for c in (q5.get("close") or []) if c is not None]
    vol_session = sum(v for v in (q5.get("volume") or []) if v)  # today incl. pre/post market
    last = closes[-1] if closes else meta.get("regularMarketPrice")
    prev = meta.get("chartPreviousClose") or meta.get("previousClose")
    daily = _get(f"https://query1.finance.yahoo.com/v8/finance/chart/{ticker}",
                 params={"range": "6mo", "interval": "1d", "events": "split"})
    avg_vol = vol_today = None
    ready = None
    try:
        res_d = daily["chart"]["result"][0]
        q = res_d["indicators"]["quote"][0]
        vols = [v for v in q["volume"] if v is not None]
        if len(vols) > 1:
            vol_today = vols[-1]
            hist = vols[-21:-1]
            avg_vol = sum(hist) / len(hist) if hist else None
        ready = _readiness(res_d)
    except Exception:
        pass
    if not last or not prev:
        return None
    return {"price": round(float(last), 4), "prev_close": round(float(prev), 4),
            "move_pct": round((float(last) / float(prev) - 1) * 100, 2),
            "vol_today": max(vol_today or 0, vol_session or 0) or None, "avg_vol20": int(avg_vol) if avg_vol else None,
            "rel_vol": round(max(vol_today or 0, vol_session or 0) / avg_vol, 2) if avg_vol else None,
            "exchange": meta.get("exchangeName"), "name": meta.get("longName") or meta.get("shortName"),
            "readiness": ready}


def _readiness(res):
    """Readiness points from ~6 months of daily bars (see readiness.py)."""
    try:
        import pandas as pd
        from zoneinfo import ZoneInfo
        from . import readiness as R
        q = res["indicators"]["quote"][0]
        df = pd.DataFrame({"o": q["open"], "h": q["high"], "l": q["low"], "c": q["close"], "v": q["volume"]},
                          index=pd.to_datetime(res["timestamp"], unit="s").normalize()).dropna()
        df = df[~df.index.duplicated(keep="last")]
        splits = (res.get("events") or {}).get("splits") or {}
        rs = [pd.to_datetime(int(k), unit="s").normalize() for k, sp in splits.items()
              if sp.get("numerator") and sp.get("denominator") and sp["numerator"] < sp["denominator"]]
        today = datetime.now(ZoneInfo("America/New_York")).date()
        df = R.last_completed(df, today)
        if len(df) < 25:
            return None
        f = R.features(df, rs).iloc[-1].to_dict()
        pts, why, risk = R.points(f)
        keep = ("rvol_1", "atr_ratio", "drawdown_60", "vol20", "prior_runs_120", "ret_1d", "ret_3d", "ret_10d",
                "days_since_rsplit", "near_20d_high")
        return {"points": pts, "why": why, "risk": risk, "asof": str(df.index[-1].date()),
                "f": {k: (None if f.get(k) is None or f.get(k) != f.get(k) else round(float(f[k]), 4)) for k in keep}}
    except Exception as e:
        ERRORS.append(f"readiness: {type(e).__name__}: {e}")
        return None


def daily_bars(ticker, range_="1mo"):
    d = _get(f"https://query1.finance.yahoo.com/v8/finance/chart/{ticker}",
             params={"range": range_, "interval": "1d"})
    try:
        res = d["chart"]["result"][0]
        q = res["indicators"]["quote"][0]
        bars = []
        for i, ts in enumerate(res["timestamp"]):
            if q["close"][i] is None:
                continue
            bars.append({"date": datetime.fromtimestamp(ts, timezone.utc).date().isoformat(),
                         "high": q["high"][i], "close": q["close"][i]})
        return bars
    except Exception:
        return []


# ---------------- Fundamentals (Finviz) ----------------
def _num(s):
    if s is None:
        return None
    s = s.strip().replace(",", "")
    if s in ("-", ""):
        return None
    mult = 1.0
    if s[-1] in "KMBT":
        mult = {"K": 1e-3, "M": 1.0, "B": 1e3, "T": 1e6}[s[-1]]
        s = s[:-1]
    s = s.rstrip("%")
    try:
        return float(s) * mult
    except ValueError:
        return None


def _strip(html):
    return re.sub(r"<[^>]+>", " ", html).replace("&nbsp;", " ").strip()


def table_value(html, label):
    """Find a table cell whose text is `label` and return the text of the next cell."""
    m = re.search(r">\s*" + re.escape(label) + r"[^<]{0,40}<", html)
    if not m:
        return None
    end_td = html.find("</td>", m.end() - 1)
    if end_td < 0:
        return None
    nxt = html.find("<td", end_td)
    close = html.find("</td>", nxt)
    if nxt < 0 or close < 0 or nxt - end_td > 400:
        return None
    txt = _strip(html[html.find(">", nxt) + 1:close])
    return txt.split()[0] if txt else None


def _finviz(ticker):
    html = _get(f"https://finviz.com/quote.ashx?t={ticker}&p=d", expect_json=False, retries=1)
    if not html:
        return None
    return {"market_cap_m": _num(table_value(html, "Market Cap")), "float_m": _num(table_value(html, "Shs Float")),
            "short_float_pct": _num(table_value(html, "Short Float")),
            "days_to_cover": _num(table_value(html, "Short Ratio")), "src": "finviz"}


def _stockanalysis(ticker):
    html = _get(f"https://stockanalysis.com/stocks/{ticker.lower()}/statistics/", expect_json=False, retries=1)
    if not html:
        return None
    return {"market_cap_m": _num(table_value(html, "Market Cap")), "float_m": _num(table_value(html, "Float")),
            "short_float_pct": _num(table_value(html, "Short % of Float")),
            "days_to_cover": _num(table_value(html, "Short Ratio")), "src": "stockanalysis"}


def fundamentals(ticker):
    """Market cap ($M), float (M shares), short % of float, days to cover. Finviz, then stockanalysis."""
    best = None
    for fn in (_finviz, _stockanalysis):
        d = fn(ticker)
        if d and d.get("market_cap_m"):
            if best:  # fill gaps from the second source
                for k, v in d.items():
                    if best.get(k) is None:
                        best[k] = v
                return best
            best = d
            if all(d.get(k) is not None for k in ("float_m", "short_float_pct")):
                return d
    if not best:
        ERRORS.append(f"no fundamentals for {ticker}")
    return best


# ---------------- SEC EDGAR (dilution filings) ----------------
_CIK = None


def cik_map(cache_path):
    global _CIK
    if _CIK is not None:
        return _CIK
    try:
        if os.path.exists(cache_path) and time.time() - os.path.getmtime(cache_path) < 7 * 86400:
            with open(cache_path) as f:
                _CIK = json.load(f)
                return _CIK
    except Exception:
        pass
    d = _get("https://www.sec.gov/files/company_tickers.json", headers={"User-Agent": config.SEC_USER_AGENT})
    if d:
        _CIK = {v["ticker"].upper(): int(v["cik_str"]) for v in d.values()}
        os.makedirs(os.path.dirname(cache_path), exist_ok=True)
        with open(cache_path, "w") as f:
            json.dump(_CIK, f)
    else:
        _CIK = {}
    return _CIK


def sec_filings(ticker, cache_path):
    """One request to SEC submissions -> what matters for a forum run.
    Returns None if the company is unknown to SEC (foreign/OTC), else
    {'dilution': [...], 'catalysts': [...], 'rsplit': 'YYYY-MM-DD' or None, 'cik': int}."""
    cik = cik_map(cache_path).get(ticker.upper())
    if not cik:
        return None
    d = _get(f"https://data.sec.gov/submissions/CIK{cik:010d}.json", headers={"User-Agent": config.SEC_USER_AGENT})
    if not d:
        return None
    rec = (d.get("filings") or {}).get("recent") or {}
    today = datetime.now(timezone.utc).date()
    cut_dil = (today - timedelta(days=config.DILUTION_LOOKBACK_DAYS)).isoformat()
    cut_cat = (today - timedelta(days=config.CATALYST_DAYS)).isoformat()
    cut_rs = (today - timedelta(days=config.RSPLIT_PENALTY_DAYS)).isoformat()
    forms, dates = rec.get("form", []), rec.get("filingDate", [])
    items = rec.get("items") or [""] * len(forms)
    out = {"dilution": [], "catalysts": [], "rsplit": None, "cik": cik}
    has_6k = False
    for form, date, it in zip(forms, dates, items):
        if date < cut_rs:
            break
        f = form.upper()
        if date >= cut_dil and f in config.DILUTION_FORMS:
            out["dilution"].append(f"{form} {date}")
        if date >= cut_cat and f in config.CATALYST_FORMS:
            out["catalysts"].append(f"{form} {date}")
        # 8-K item 5.03 = change to the articles (that is how a reverse split is filed); 3.03 = holders' rights
        if f.startswith("8-K") and ("5.03" in (it or "")) and not out["rsplit"]:
            out["rsplit"] = date
        if f.startswith("6-K"):
            has_6k = True
    if not out["rsplit"] and has_6k:  # foreign issuers: no item codes, search the text instead
        out["rsplit"] = rsplit_fulltext(cik, cut_rs)
    return out


def rsplit_fulltext(cik, since):
    """SEC full-text search for a reverse split / ADS ratio change since `since`. Fails soft (None)."""
    n_err = len(ERRORS)
    d = _get("https://efts.sec.gov/LATEST/search-index",
             headers={"User-Agent": config.SEC_USER_AGENT},
             params={"q": '"reverse split" OR "reverse stock split" OR "ADS ratio" OR "ratio change"',
                     "ciks": f"{cik:010d}", "dateRange": "custom", "startdt": since,
                     "enddt": datetime.now(timezone.utc).date().isoformat()}, retries=0)
    if d is None:
        del ERRORS[n_err:]  # optional source: don't count its failures as scan errors
        return None
    try:
        hits = (d.get("hits") or {}).get("hits") or []
        dates = sorted((h.get("_source") or {}).get("file_date") for h in hits if (h.get("_source") or {}).get("file_date"))
        return dates[-1] if dates else None
    except Exception:
        return None


def dilution_filings(ticker, cache_path):
    """Kept for compatibility: list of dilution filings in the lookback window, None if unknown."""
    s = sec_filings(ticker, cache_path)
    return None if s is None else s["dilution"]
