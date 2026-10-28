"""Thresholds and settings. Change values here to tune the scanner."""
import os

# ---------- ApeWisdom ----------
APEWISDOM_FILTERS_MAIN = "all-stocks"
APEWISDOM_MAIN_PAGES = 3
# Per-subreddit filters used to measure spread across forums. Unknown filters are skipped.
APEWISDOM_SUB_FILTERS = [
    "pennystocks",
    "wallstreetbets",
    "stocks",
    "Shortsqueeze",
    "smallstreetbets",
    "RobinHoodPennyStocks",
    "Daytrading",
    "WallStreetbetsELITE",
    "Wallstreetbetsnew",
    "4chan",
]

# Gates for "buzz is building" (same spirit as the daily routine)
MAIN_MIN_MENTIONS = 5          # all-stocks: at least N mentions in last 24h
MAIN_MIN_ADD = 4               # ...and at least +N vs 24h ago
PENNY_MIN_MENTIONS = 3         # pennystocks filter
PENNY_MIN_ADD = 2
MIN_SMOOTHED_GROWTH = 2.0      # (now+2)/(base+2)
# Shadow gate (experiment): tickers that pass these looser values but not the gate above are still checked
# and logged in candidates.json (tag relaxed_gate) but never alerted. After 1-2 weeks the weekly report shows
# whether they run as often as the regular ones; only then lower the real gate.
SHADOW_GATE = True
SHADOW_MAIN_MIN_MENTIONS = 3
SHADOW_MAIN_MIN_ADD = 2
SHADOW_PENNY_MIN_MENTIONS = 2
SHADOW_PENNY_MIN_ADD = 1
SHADOW_MIN_SMOOTHED_GROWTH = 1.5

# ---------- Price / fundamentals gates ----------
MAX_MOVE_PCT = 15.0            # price vs previous close; above this = "already moved"
MAX_MARKET_CAP_M = 500.0       # $M (small caps only)
MIN_PRICE = 0.05
MIN_MARKET_CAP_M = 3.0         # below this the data is usually wrong or the company is a shell/delisting case
# "Already ran" over several days (daily closes up to yesterday). A forum run we can still catch has not
# started yet; these block the alert like MAX_MOVE_PCT does for today's move.
MAX_RUN_3D_PCT = 30.0
MAX_RUN_10D_PCT = 60.0
# Reverse split / ADS ratio change: a fresh one distorts price and volume (and usually precedes more dilution).
RSPLIT_BLOCK_DAYS = 10         # blocked if the split was this recent
RSPLIT_PENALTY_DAYS = 60       # -1 point and a warning up to this age
DILUTION_LOOKBACK_DAYS = 30
DILUTION_FORMS = ("S-1", "S-1/A", "S-3", "S-3/A", "F-1", "F-1/A", "F-3", "F-3/A",
                  "424B1", "424B2", "424B3", "424B4", "424B5", "424B7", "EFFECT")
BLOCK_ON_DILUTION = True
# Fresh company filings that forums tend to pick up (8-K/6-K news, activist 13D). +1 point.
CATALYST_FORMS = ("8-K", "6-K", "SC 13D", "SC 13D/A")
CATALYST_DAYS = 3

# ---------- Press releases (scanner/news.py) ----------
PR_HOURS = 24                  # news younger than this counts
PR_FEEDS = [                   # public RSS feeds; any that fail are skipped (status shown in the summary)
    ("GlobeNewswire", "https://www.globenewswire.com/RssFeed/orgclass/1/feedTitle/GlobeNewswire%20-%20News%20about%20Public%20Companies"),
    ("PR Newswire", "https://www.prnewswire.com/rss/news-releases-list.rss"),
    ("Business Wire", "https://feed.businesswire.com/rss/home/?rss=G1QFDERJXkJeEFpRWQ=="),
    ("ACCESSWIRE", "https://www.accesswire.com/users/newswire/rss.ashx"),
    ("Newsfile", "https://feeds.newsfilecorp.com/feed/category/all"),
]
BLOCK_ON_OFFERING_PR = True
# Deal size vs company size for a "strong" press release with a $ amount in the headline:
PR_BIG_RATIO = 0.10            # amount >= 10% of market cap: +1 extra point
PR_SMALL_RATIO = 0.02          # amount < 2% of market cap: the release counts as "weak" (no point)    # a press release about an offering / registered direct / PIPE blocks the alert

# ---------- Forum-run signals (from our own snapshot history) ----------
NOVELTY_DAYS = 14              # "new in the conversation": no day with 2+ mentions in this window before the last 24h
NOVELTY_MIN_HISTORY_DAYS = 5   # only judge novelty once we have this much snapshot history
RISING_DAYS = 3                # daily peak mentions rising this many days in a row
ONE_AUTHOR_MIN_MENTIONS = 6    # reddit: this many mentions in 24h ...
ONE_AUTHOR_MAX_AUTHORS = 2     # ... from this few authors = one person pushing it (-1)

# ---------- Scoring ----------
ALERT_MIN_SCORE = 6           # raised from 5 when volume/watchers/borrow/xueqiu points were added
ALERT_MIN_SCORE_SMALL = 4     # lower bar for small caps (market cap under SMALL_CAP_M)
SMALL_CAP_M = 300.0           # $M
REALERT_HOURS = 24
REALERT_SCORE_JUMP = 2
MAX_CANDIDATES_TO_ENRICH = 200 # was 20: now every candidate is checked; the time budget below is the real limit
ENRICH_TIME_BUDGET_S = 480     # stop enriching after 8 minutes (the job times out at 12); the rest is logged as "not checked"
LARGE_CAP_CACHE_DAYS = 7       # a cached "too large" market cap skips the ticker for this many days...
LARGE_CAP_CACHE_DAYS_FAR = 30  # ...or this many when the cap is 4x+ the limit (a $14B stock won't drop under $500M)

# ---------- News entry path (a fresh strong press release creates a candidate, even with no forum buzz) ----------
NEWS_PATH = True
NEWS_PATH_HOURS = 18           # only releases this fresh open a candidate (covers last evening + overnight)
NEWS_PATH_MAX = 25             # at most this many news-only candidates per run, newest first
ALERT_MIN_SCORE_NEWS = 2       # strong release + at least one more point (float, short, volume, big deal...)
                               # (starting point, not proven: news-path alerts are tagged and measured separately)

# ---------- Learning loop (scanner/review.py) ----------
CANDLOG_MIN_SCORE = 3          # every candidate with this score or more is logged with its outcome after 5 days
CANDLOG_KEEP_DAYS = 90
CANDLOG_EVAL_PER_RUN = 40      # outcome checks per run (each is one Yahoo request)
WEEKLY_HOUR_UTC = 21           # weekly report from Friday 21:00 UTC (after the US close), once per week
WEEKLY_MIN_N = 8               # a signal needs this many measured cases to appear in the weekly report

# ---------- Follow-ups on alerts ----------
FOLLOW_DAYS = 8                # follow an alert this many calendar days (~5 trading days)
FOLLOW_UP_LEVELS = (30, 60)    # % above the alert price that trigger a message (each once)
INVALIDATION_FALLBACK_PCT = 15 # if the alert price was already below the previous close: stop = alert price -15%

# ---------- Reddit (optional, needs secrets) ----------
REDDIT_SUBS = ["pennystocks", "Shortsqueeze", "smallstreetbets", "RobinHoodPennyStocks",
               "wallstreetbets", "stocks", "Daytrading", "WallStreetbetsELITE"]
REDDIT_CLIENT_ID = os.getenv("REDDIT_CLIENT_ID", "").strip()
REDDIT_SECRET = os.getenv("REDDIT_SECRET", "").strip()
REDDIT_USER_AGENT = os.getenv("REDDIT_USER_AGENT", "").strip() or "script:small-cap-buzz:0.1 (personal research)"

BLOCK_ON_PROMOTION = True      # paid promotion on stockpromoters.com blocks the alert

# ---------- Telegram groups (optional, read with your own account via Telethon) ----------
# All four come from GitHub Secrets. TG_CHATS: comma/line separated @names, t.me links or numeric ids.
# The chat list is a secret on purpose: the repo is public, and nothing written to data/ names a chat.
TG_API_ID = os.getenv("TG_API_ID", "").strip()
TG_API_HASH = os.getenv("TG_API_HASH", "").strip()
TG_SESSION = os.getenv("TG_SESSION", "").strip()
TG_CHATS = os.getenv("TG_CHATS", "").strip()
TG_MAX_MESSAGES_PER_CHAT = 400  # newest first, stops at 24h back
TG_SLEEP_BETWEEN_CHATS_S = 1.5  # gentle pace (read-only, but don't look like a flood)
TG_TIME_BUDGET_S = 120
TG_MIN_AUTHORS = 3              # +1 point / entry path: this many distinct authors in 24h ...
TG_MIN_CHATS = 2                # ... across this many chats
TG_PATH_MIN_AUTHORS6 = 3        # entry path (no forum gate needed): authors in the last 6h ...
TG_PATH_MIN_CHATS6 = 2          # ... across this many chats

# ---------- YouTube (optional, free API key) ----------
YOUTUBE_API_KEY = os.getenv("YOUTUBE_API_KEY", "").strip()

# ---------- Telegram (optional) ----------
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "").strip()
# Optional second chat (e.g. a group with only you and the bot, NOT muted) for strong alerts only.
TELEGRAM_URGENT_CHAT_ID = os.getenv("TELEGRAM_URGENT_CHAT_ID", "").strip()

# ---------- "Strong candidate" (urgent) rule: all must hold ----------
# A starting point, not a proven edge: the weekly report measures the urgent group separately.
URGENT_MIN_SCORE = 9           # regular alert is 6+
URGENT_MAX_MOVE_PCT = 10.0     # still early: at most +10% vs previous close
URGENT_MAX_FLOAT_M = 20.0      # fuel: float under 20M ...
URGENT_MIN_BORROW_FEE = 50.0   # ... or borrow fee 50%+ ...
URGENT_MIN_SHORT_PCT = 20.0    # ... or short 20%+ of float
URGENT_FOLLOWUPS = True        # +30%/+60%/invalidation messages of an urgent alert also go to the urgent chat

# ---------- SEC ----------
SEC_USER_AGENT = os.getenv("SEC_USER_AGENT", "").strip() or "small-cap-buzz research github.com/tzachistocks"

# ---------- Storage ----------
DATA_DIR = os.getenv("DATA_DIR", "data")
KEEP_SNAPSHOT_DAYS = 21

# Words that look like tickers but usually are not
WORD_TICKERS = {
    "A", "I", "API", "IT", "ALL", "YOU", "UP", "AM", "ON", "EU", "OG", "IP", "HR", "SO", "BE", "ES", "GO",
    "NOW", "OR", "CC", "RR", "ET", "PR", "WTI", "CIA", "TP", "FOR", "AA", "DD", "CEO", "CFO", "USA", "US",
    "IPO", "ATM", "FDA", "SEC", "EPS", "ER", "AI", "EV", "OTC", "ETF", "YOLO", "FOMO", "HODL", "IMO", "LOL",
    "ATH", "PM", "AH", "TA", "FA", "RSI", "MACD", "EOD", "EOW", "OP", "GDP", "CPI", "FED", "THE", "AND",
    "ARE", "CAN", "HAS", "NEW", "ONE", "TWO", "OUT", "BIG", "LOW", "HIGH", "BUY", "SELL", "HOLD", "MOON",
    "RUN", "PUMP", "DUMP", "BULL", "BEAR", "CALL", "PUT", "NFA", "TLDR", "EDIT", "UPDATE", "LONG", "SHORT",
    "GAIN", "LOSS", "RIP", "WSB", "DM", "PT", "SL", "TP", "R", "S", "U", "X", "Y", "V", "T", "F", "C", "E",
    "K", "M", "B", "D", "L", "O", "P", "Q", "W", "Z", "H", "J", "G", "N", "AT", "AS", "BY", "IF", "IN",
    "IS", "MY", "NO", "OF", "TO", "WE", "DO", "HE", "ME", "AN", "AGO", "ANY", "WHO", "WHY", "HOW", "GET",
    "GOT", "LET", "SEE", "WAY", "DAY", "BAD", "OLD", "TOP", "OPEN", "NEXT", "JUST", "LIKE", "LOVE", "REAL",
    "BEST", "GOOD", "HUGE", "VERY", "WELL", "EVER", "PLAY", "CASH", "FREE", "SAFE", "FAST", "LIFE", "TECH",
}
ETF_WORDS = ("ETF", "Trust", "Fund", "Shares", "Direxion", "ProShares", "iShares", "SPDR", "Vanguard",
             "Invesco", "ETN", "Daily ")
LARGE_CAPS = {"AAPL", "MSFT", "NVDA", "AMZN", "META", "GOOGL", "GOOG", "TSLA", "AMD", "AVGO", "MU", "JPM",
              "BAC", "GS", "UNH", "PLTR", "NFLX", "ORCL", "INTC", "SPY", "QQQ", "IWM", "DIA", "SMCI", "COIN",
              "HOOD", "MSTR", "BABA", "TSM", "ARM", "CRM", "ADBE", "DIS", "BA", "WMT", "COST", "XOM", "CVX"}
