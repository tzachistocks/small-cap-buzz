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
]

# Gates for "buzz is building" (same spirit as the daily routine)
MAIN_MIN_MENTIONS = 5          # all-stocks: at least N mentions in last 24h
MAIN_MIN_ADD = 4               # ...and at least +N vs 24h ago
PENNY_MIN_MENTIONS = 3         # pennystocks filter
PENNY_MIN_ADD = 2
MIN_SMOOTHED_GROWTH = 2.0      # (now+2)/(base+2)

# ---------- Price / fundamentals gates ----------
MAX_MOVE_PCT = 15.0            # price vs previous close; above this = "already moved"
MAX_MARKET_CAP_M = 2000.0      # $M
MIN_PRICE = 0.05
DILUTION_LOOKBACK_DAYS = 30
DILUTION_FORMS = ("S-1", "S-1/A", "S-3", "S-3/A", "F-1", "F-1/A", "F-3", "F-3/A",
                  "424B1", "424B2", "424B3", "424B4", "424B5", "424B7", "EFFECT")
BLOCK_ON_DILUTION = True

# ---------- Scoring ----------
ALERT_MIN_SCORE = 5
REALERT_HOURS = 24
REALERT_SCORE_JUMP = 2
MAX_CANDIDATES_TO_ENRICH = 15  # limit network calls per run

# ---------- Reddit (optional, needs secrets) ----------
REDDIT_SUBS = ["pennystocks", "Shortsqueeze", "smallstreetbets", "RobinHoodPennyStocks",
               "wallstreetbets", "stocks", "Daytrading", "WallStreetbetsELITE"]
REDDIT_CLIENT_ID = os.getenv("REDDIT_CLIENT_ID", "").strip()
REDDIT_SECRET = os.getenv("REDDIT_SECRET", "").strip()
REDDIT_USER_AGENT = os.getenv("REDDIT_USER_AGENT", "").strip() or "script:small-cap-buzz:0.1 (personal research)"

# ---------- Telegram (optional) ----------
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "").strip()

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
