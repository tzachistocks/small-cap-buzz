"""Ticker mentions in Telegram groups/channels, read with YOUR Telegram account (Telethon, read-only).

Privacy (the repo is public):
- The list of chats comes only from the TG_CHATS secret. Chat names/usernames are never written to data/,
  to the run summary or to errors: chats are referred to as "chat #N" (N = position in TG_CHATS).
- Message text and author ids are not stored. Only per-ticker counts are kept.
- The session string (TG_SESSION) gives full access to the account: it lives only in GitHub Secrets.

Read-only: the code never sends, joins, reacts or marks anything as read.
"""
import time
from datetime import datetime, timezone, timedelta

from . import config
from .sources import ERRORS, extract_tickers

STATUS = {"mode": "off", "chats_ok": 0, "chats_failed": 0, "messages": 0}


def _chat_ref(s):
    """'@name', 't.me/name', 'https://t.me/name' or a numeric id -> what Telethon's get_entity accepts."""
    s = s.strip()
    for pre in ("https://", "http://"):
        if s.startswith(pre):
            s = s[len(pre):]
    if s.startswith("t.me/"):
        s = s[len("t.me/"):]
        if s.startswith("s/"):
            s = s[2:]
    s = s.split("/")[0].split("?")[0]
    if s.lstrip("-").isdigit():
        return int(s)
    return s.lstrip("@")


def chat_list():
    raw = config.TG_CHATS.replace("\n", ",").replace(" ", ",")
    return [_chat_ref(x) for x in raw.split(",") if x.strip() and not x.strip().startswith("+")]


def chat_list_raw():
    """Chat names as written in the secret (only used to scrub them from error messages)."""
    return [str(x) for x in chat_list() if len(str(x)) >= 4]


def configured():
    return bool(config.TG_API_ID and config.TG_API_HASH and config.TG_SESSION and config.TG_CHATS)


def telegram_counts(known):
    """{ticker: {m6, m24, authors6, authors24, chats6, chats24, first_utc}}, ok.
    chats6/chats24 are COUNTS of distinct chats (no names)."""
    if not configured():
        STATUS["mode"] = "off"
        return {}, False
    try:
        from telethon.sync import TelegramClient
        from telethon.sessions import StringSession
        from telethon.errors import FloodWaitError
    except ImportError:
        ERRORS.append("Telegram groups: telethon is not installed")
        STATUS["mode"] = "failed"
        return {}, False

    now = datetime.now(timezone.utc)
    since = now - timedelta(hours=24)
    six = now - timedelta(hours=6)
    agg = {}
    try:
        client = TelegramClient(StringSession(config.TG_SESSION), int(config.TG_API_ID), config.TG_API_HASH,
                                receive_updates=False)
        client.connect()
        if not client.is_user_authorized():
            ERRORS.append("Telegram groups: the session is not authorized (create TG_SESSION again)")
            client.disconnect()
            STATUS["mode"] = "failed"
            return {}, False
    except Exception as e:
        ERRORS.append(f"Telegram groups: connect failed ({type(e).__name__})")
        STATUS["mode"] = "failed"
        return {}, False

    t0 = time.monotonic()
    try:
        for i, ref in enumerate(chat_list(), 1):
            if time.monotonic() - t0 > config.TG_TIME_BUDGET_S:
                ERRORS.append(f"Telegram groups: time budget reached at chat #{i}")
                break
            try:
                entity = client.get_entity(ref)
                n = 0
                for msg in client.iter_messages(entity, limit=config.TG_MAX_MESSAGES_PER_CHAT):
                    if not msg.date or msg.date < since:
                        break
                    n += 1
                    text = msg.message or ""
                    if not text:
                        continue
                    author = msg.sender_id or msg.post_author or f"chat{i}"
                    for t in extract_tickers(text, known):
                        a = agg.setdefault(t, {"m6": 0, "m24": 0, "authors6": set(), "authors24": set(),
                                               "chats6": set(), "chats24": set(), "first": msg.date})
                        a["m24"] += 1
                        a["authors24"].add(author)
                        a["chats24"].add(i)
                        a["first"] = min(a["first"], msg.date)
                        if msg.date >= six:
                            a["m6"] += 1
                            a["authors6"].add(author)
                            a["chats6"].add(i)
                STATUS["chats_ok"] += 1
                STATUS["messages"] += n
            except FloodWaitError as e:
                if e.seconds <= 30:
                    time.sleep(e.seconds + 1)
                else:
                    ERRORS.append(f"Telegram groups: flood wait {e.seconds}s, stopped at chat #{i}")
                    break
            except Exception as e:  # no chat name in the error: the repo is public
                STATUS["chats_failed"] += 1
                ERRORS.append(f"Telegram groups: chat #{i} unreadable ({type(e).__name__})")
            time.sleep(config.TG_SLEEP_BETWEEN_CHATS_S)
    finally:
        try:
            client.disconnect()
        except Exception:
            pass

    STATUS["mode"] = "ok" if STATUS["chats_ok"] else "failed"
    out = {}
    for t, a in agg.items():
        out[t] = {"m6": a["m6"], "m24": a["m24"], "authors6": len(a["authors6"]), "authors24": len(a["authors24"]),
                  "chats6": len(a["chats6"]), "chats24": len(a["chats24"]),
                  "first_utc": a["first"].astimezone(timezone.utc).isoformat(timespec="minutes")}
    return out, STATUS["chats_ok"] > 0
