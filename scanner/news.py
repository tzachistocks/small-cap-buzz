"""Press releases and SEC 8-K items, classified as strong / weak / negative.

How "strong" is decided: by the TYPE of news, using keyword rules for the kinds of news that usually move
small caps (clinical results, FDA decisions, contracts and orders, deals, buybacks, ATM termination) versus
routine updates (conference talks, meeting dates, appointments) and negative news (offerings, delisting).
The rules are a starting point: every class is logged as a tag, and the weekly report shows whether
"strong" news really leads to more +30% runs than "weak" news.
"""
import re
import time
from datetime import datetime, timezone, timedelta
from email.utils import parsedate_to_datetime

from . import config
from .sources import _get, ERRORS

STATUS = {}

# ---------------- keyword rules (checked in this order: negative first) ----------------
NEGATIVE_OFFERING = [
    r"pricing of .*offering", r"prices? .*offering", r"public offering", r"registered direct", r"private placement",
    r"warrant inducement", r"at[- ]the[- ]market", r"\bATM\b", r"equity distribution agreement", r"\bsales agreement",
    r"securities purchase agreement", r"equity line", r"standby equity", r"convertible (?:notes?|debentures?) financing",
]
NEGATIVE_OTHER = [
    r"reverse (?:stock )?split", r"delist", r"deficiency (?:notice|letter)", r"non-?compliance", r"going concern",
    r"bankruptcy", r"chapter 11", r"wind[- ]down", r"halts? (?:the )?(?:trial|program|study)", r"did not meet",
    r"failed to meet", r"discontinu", r"complete response letter", r"\bCRL\b", r"clinical hold",
]
STRONG = [
    r"topline", r"top-line", r"positive (?:interim |final |preliminary )?(?:data|results)", r"met (?:its |the )?primary endpoint",
    r"statistically significant", r"FDA (?:approv|clear|grant)", r"(?:fast track|breakthrough therapy|orphan drug) designation",
    r"(?:FDA|EMA|regulatory|marketing|CE mark) approval", r"\b510\(k\)", r"(?:awarded|wins?|secures?|receives?) .{0,40}(?:contract|order|award|grant)",
    r"purchase order", r"\$[\d.,]+ ?(?:million|billion|m\b|b\b).{0,40}(?:contract|order|deal|agreement)",
    r"definitive (?:merger |acquisition )?agreement", r"to be acquired", r"acquisition of", r"to acquire",
    r"merger agreement", r"licens(?:e|ing) agreement", r"(?:signs?|enters? into|executes?) .{0,40}agreement",
    r"agreement with", r"distribution agreement", r"supply agreement", r"strategic (?:partnership|collaboration|investment)",
    r"(?:share|stock) (?:repurchase|buyback)", r"buyback", r"terminat\w* (?:of )?(?:its |the )?(?:ATM|at[- ]the[- ]market)",
    r"record (?:revenue|quarter|sales)", r"preliminary (?:revenue|results)", r"raises? (?:full[- ]year )?guidance",
    r"regains? compliance", r"uplist", r"short squeeze", r"bitcoin treasury|crypto treasury|digital asset treasury",
]
# checked before the negative rules: "termination of ATM program" is good news, not an offering
STRONG_FIRST = [
    r"terminat\w*.{0,40}(?:ATM|at[- ]the[- ]market|offering|equity line)", r"(?:ATM|at[- ]the[- ]market).{0,40}terminat",
    r"waives?.{0,40}fees", r"(?:extension|additional \d+[- ]day).{0,60}nasdaq|nasdaq.{0,60}(?:extension|additional \d+[- ]day)",
]
WEAK = [
    r"to present", r"to participate", r"will present", r"conference", r"fireside chat", r"webinar", r"to host",
    r"to report .*results", r"announces? (?:date|timing)", r"earnings (?:call|release) date", r"annual (?:general )?meeting",
    r"appoints?", r"names? .* (?:chief|ceo|cfo|director)", r"joins? (?:the )?board", r"letter to shareholders",
    r"shareholder update", r"corporate update", r"newsletter", r"reminds? investors", r"class action", r"investigation",
    r"schedules?", r"earnings call",
]
WEAK_STRONG_SUBJECTS = [r"\bAI\b", r"artificial intelligence", r"quantum", r"drone", r"nuclear", r"rare earth"]


_AMOUNT = re.compile(r"(US\$|USD\s?|\$|€|EUR\s?)\s?(\d[\d,]*(?:\.\d+)?)\s*(billion|million|thousand|bn|mm|mn|m|b|k)?\b", re.I)
_UNIT = {"billion": 1000, "bn": 1000, "b": 1000, "million": 1, "mm": 1, "mn": 1, "m": 1, "thousand": 0.001, "k": 0.001}


def amount_m(title):
    """Largest money amount in a headline, in $M (EUR ~ x1.1). None if there is none."""
    best = None
    for cur, num, unit in _AMOUNT.findall(title or ""):
        try:
            v = float(num.replace(",", ""))
        except ValueError:
            continue
        if unit:
            v *= _UNIT[unit.lower()]
        elif v >= 100_000:
            v /= 1e6
        else:
            continue  # "$2" without a unit is a price, not a deal size
        if cur.strip().upper() in ("€", "EUR"):
            v *= 1.1
        best = v if best is None else max(best, v)
    return best


def classify(title):
    """('negative'|'strong'|'weak'|'neutral', matched rule) for one headline."""
    t = title or ""
    for pat in STRONG_FIRST:
        if re.search(pat, t, re.I):
            return "strong", re.search(pat, t, re.I).group(0)
    for pat in NEGATIVE_OFFERING:
        if re.search(pat, t, re.I):
            return "negative", "offering"
    for pat in NEGATIVE_OTHER:
        if re.search(pat, t, re.I):
            return "negative", re.search(pat, t, re.I).group(0)
    for pat in WEAK:  # routine items win over strong words inside them ("to present topline data at conference")
        if re.search(pat, t, re.I):
            return "weak", re.search(pat, t, re.I).group(0)
    for pat in STRONG:
        if re.search(pat, t, re.I):
            return "strong", re.search(pat, t, re.I).group(0)
    for pat in WEAK_STRONG_SUBJECTS:
        if re.search(pat, t, re.I):
            return "weak", "hype subject: " + re.search(pat, t, re.I).group(0)
    return "neutral", ""


# ---------------- sources ----------------
_TICKER_IN_TEXT = re.compile(r"\((?:NASDAQ|Nasdaq|NYSE(?: American| Arca)?|NYSE American|AMEX|Cboe|OTC\w*|TSXV?|CSE)"
                             r"(?:\s*[A-Za-z]*)?\s*:\s*\$?([A-Z]{1,5})(?:\.[A-Z])?\b")


def _rss_items(xml):
    """Minimal RSS 2.0 / Atom parser: [{'title','desc','ts'}]."""
    import xml.etree.ElementTree as ET
    out = []
    try:
        root = ET.fromstring(xml)
    except ET.ParseError:
        return out
    for it in root.iter():
        tag = it.tag.split("}")[-1]
        if tag not in ("item", "entry"):
            continue
        get = lambda name: next((c.text or "" for c in it if c.tag.split("}")[-1] == name), "")
        title = re.sub(r"\s+", " ", get("title")).strip()
        desc = re.sub(r"<[^>]+>", " ", get("description") or get("summary") or get("content"))
        cats = " ".join((c.text or "") + " " + (c.get("term") or "") for c in it if c.tag.split("}")[-1] == "category")
        raw = get("pubDate") or get("published") or get("updated") or get("date")
        ts = None
        try:
            ts = parsedate_to_datetime(raw) if "," in raw else datetime.fromisoformat(raw.replace("Z", "+00:00"))
            if ts and ts.tzinfo is None:
                ts = ts.replace(tzinfo=timezone.utc)
        except Exception:
            pass
        out.append({"title": title, "desc": f"{desc} {cats}", "ts": ts})
    return out


def wire_news(store, known):
    """Read the newswires' public RSS feeds, keep a 3-day cache in data/pr_cache.json (feeds only show the
    latest items, so the cache fills the gaps between runs). Returns {ticker: [item, ...]}."""
    cache = store.load_json("pr_cache.json", [])
    seen = {(c["ticker"], c["title"]) for c in cache}
    ok = []
    for name, url in config.PR_FEEDS:
        n_err = len(ERRORS)
        xml = _get(url, expect_json=False, retries=1, headers={"Accept": "application/rss+xml, application/xml, */*"})
        if not xml:
            del ERRORS[n_err:]  # optional source
            continue
        items = _rss_items(xml)
        if items:
            ok.append(f"{name} {len(items)}")
        for it in items:
            if not it["ts"]:
                continue
            for t in set(_TICKER_IN_TEXT.findall(f"{it['title']} {it['desc']}")):
                if t in known and (t, it["title"]) not in seen:
                    seen.add((t, it["title"]))
                    cache.append({"ticker": t, "title": it["title"][:200], "src": name,
                                  "ts": it["ts"].astimezone(timezone.utc).isoformat(timespec="minutes")})
    cutoff = (datetime.now(timezone.utc) - timedelta(days=3)).isoformat(timespec="minutes")
    cache = [c for c in cache if c["ts"] >= cutoff][-3000:]
    store.save_json("pr_cache.json", cache)
    STATUS["wires"] = ", ".join(ok) if ok else "חסום / לא זמין"
    out = {}
    for c in cache:
        out.setdefault(c["ticker"], []).append(c)
    return out


def yahoo_headlines(ticker):
    """Per-ticker headlines (all wires + news sites) from Yahoo Finance RSS. Empty list on failure."""
    n_err = len(ERRORS)
    xml = _get("https://feeds.finance.yahoo.com/rss/2.0/headline", params={"s": ticker, "region": "US", "lang": "en-US"},
               expect_json=False, retries=0)
    if not xml:
        del ERRORS[n_err:]
        STATUS.setdefault("yahoo_fail", 0)
        STATUS["yahoo_fail"] += 1
        return []
    STATUS["yahoo_ok"] = STATUS.get("yahoo_ok", 0) + 1
    return [{"ticker": ticker, "title": it["title"][:200], "src": "Yahoo",
             "ts": it["ts"].astimezone(timezone.utc).isoformat(timespec="minutes")} for it in _rss_items(xml) if it["ts"]]


def news_signal(ticker, wire_map, now):
    """Best news of the last PR_HOURS for one ticker: {'class','title','ts','src','rule','offering'} or None."""
    items = list(wire_map.get(ticker, [])) + yahoo_headlines(ticker)
    cutoff = (now - timedelta(hours=config.PR_HOURS)).isoformat(timespec="minutes")
    best, seen = None, set()
    rank = {"negative": 3, "strong": 2, "weak": 1, "neutral": 0}
    for it in sorted(items, key=lambda x: x["ts"], reverse=True):
        key = it["title"].lower()[:80]
        if it["ts"] < cutoff or key in seen:
            continue
        seen.add(key)
        cls, rule = classify(it["title"])
        if best is None or rank[cls] > rank[best["class"]]:
            best = dict(it, **{"class": cls, "rule": rule, "offering": rule == "offering",
                               "amount_m": amount_m(it["title"])})
    return best


def et_time(iso):
    """'HH:MM ET dd/mm' for messages."""
    try:
        from zoneinfo import ZoneInfo
        d = datetime.fromisoformat(iso).astimezone(ZoneInfo("America/New_York"))
        return d.strftime("%H:%M ET %d/%m")
    except Exception:
        return iso


# ---------------- SEC 8-K items ----------------
ITEM_NAMES = {
    "1.01": "הסכם מהותי", "2.01": "השלמת רכישה/מכירה", "2.02": "תוצאות", "7.01": "מצגת/FD", "8.01": "אירוע אחר",
    "1.03": "פשיטת רגל", "3.01": "הודעת מחיקה/אי-עמידה", "3.02": "הנפקה פרטית", "4.02": "דוחות לא אמינים",
    "5.02": "שינוי בהנהלה", "5.07": "הצבעת בעלי מניות", "3.03": "שינוי בזכויות", "5.03": "שינוי תקנון",
}
SEC_STRONG_ITEMS = {"1.01", "2.01"}
SEC_NEGATIVE_ITEMS = {"1.03", "3.01", "3.02", "4.02"}


def classify_filing(form, items):
    """('strong'|'weak'|'negative', label) for one recent filing."""
    f = (form or "").upper()
    its = [i.strip() for i in (items or "").split(",") if i.strip()]
    if f.startswith("SC 13D"):
        return "strong", "13D (משקיע אקטיביסט)"
    if f.startswith("8-K"):
        neg = [i for i in its if i in SEC_NEGATIVE_ITEMS]
        if neg:
            return "negative", f"8-K {neg[0]} ({ITEM_NAMES.get(neg[0], '')})"
        st = [i for i in its if i in SEC_STRONG_ITEMS]
        if st:
            return "strong", f"8-K {st[0]} ({ITEM_NAMES.get(st[0], '')})"
        main_item = next((i for i in its if i != "9.01"), its[0] if its else "")
        return "weak", f"8-K {main_item} ({ITEM_NAMES.get(main_item, 'אחר')})".replace(" ()", "")
    return "weak", f
