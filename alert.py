#!/usr/bin/env python3
"""
US visa slot alert watcher.

Listens to public Telegram slot-tracking channels/groups with YOUR OWN Telegram
account (read-only), filters out "NA"/no-slot noise, and fires a loud alert on
macOS plus an optional Telegram push to your phone when someone reports an open
slot for the cities / visa type you care about.

It never touches usvisascheduling.com. You still book manually.
"""

import asyncio
import json
import os
import re
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path

try:
    from telethon import TelegramClient, events
    from telethon.sessions import StringSession
except ImportError:
    sys.exit("telethon not installed. Run:  pip3 install -r requirements.txt")

try:
    import requests
except ImportError:
    requests = None

HERE = Path(__file__).resolve().parent
CONFIG_PATH = HERE / "config.json"

# Config comes from config.json locally, or from a CONFIG_JSON env var (plus
# TG_SESSION / TG_API_ID / TG_API_HASH / PUSH_BOT_TOKEN / PUSH_CHAT_ID secrets)
# when hosted somewhere like a Hugging Face Space.
if os.environ.get("CONFIG_JSON"):
    cfg = json.loads(os.environ["CONFIG_JSON"])
elif CONFIG_PATH.exists():
    cfg = json.loads(CONFIG_PATH.read_text())
else:
    sys.exit("config.json missing. Copy config.example.json to config.json and fill it in.")

for key, env in (
    ("telegram_api_id", "TG_API_ID"),
    ("telegram_api_hash", "TG_API_HASH"),
    ("push_bot_token", "PUSH_BOT_TOKEN"),
    ("push_chat_id", "PUSH_CHAT_ID"),
):
    if os.environ.get(env):
        cfg[key] = os.environ[env]
SESSION_STRING = os.environ.get("TG_SESSION", "")

API_ID = int(cfg["telegram_api_id"])
API_HASH = cfg["telegram_api_hash"]
CHANNELS = cfg["channels"]
MY_CITIES = [c.lower() for c in cfg.get("cities", [])]
MY_VISA_TYPES = [v.lower() for v in cfg.get("visa_types", [])]
ALERT_ON_PHOTOS = cfg.get("alert_on_photos", True)
# Any name from /System/Library/Sounds (Glass, Hero, Submarine, Ping, Purr, Blow, ...)
SOUND_NAME = cfg.get("sound", "Glass")
SOUND_REPEATS = int(cfg.get("sound_repeats", 1))
SPEAK = cfg.get("speak", False)
BOT_TOKEN = cfg.get("push_bot_token") or ""
BOT_CHAT_ID = cfg.get("push_chat_id") or ""
LOG_PATH = HERE / "alerts.log"

ALL_CITIES = {
    "delhi": ["delhi", "new delhi", "nd", "del"],
    "mumbai": ["mumbai", "bombay", "bom", "mum"],
    "chennai": ["chennai", "madras", "maa", "che"],
    "hyderabad": ["hyderabad", "hyd"],
    "kolkata": ["kolkata", "calcutta", "ccu", "kol"],
}

VISA_TYPE_ALIASES = {
    "b1/b2": ["b1/b2", "b1b2", "b2", "b1", "visitor", "tourist", "business"],
    "f1": ["f1", "f-1", "student"],
    "h1b": ["h1b", "h-1b", "h1"],
    "h4": ["h4", "h-4"],
    "l1": ["l1", "l-1"],
    "dropbox": ["dropbox", "drop box", "iw", "interview waiver"],
}

# Signals that a slot is actually open.
POSITIVE = re.compile(
    r"\b(go|open(ed|ing)?|available|avail|slots?\s+(are\s+)?(open|up|out|live|available)|"
    r"bulk|released?|showing|grab|hurry|check\s+(now|portal)|"
    r"ofc|vac|biometric|consular|vi\b)",
    re.I,
)
# A concrete date in the message is a strong signal too (e.g. "14 Feb", "Feb 14", "2026-02-14", "14/02").
DATE = re.compile(
    r"\b(\d{1,2}\s*[-/ ]\s*(jan|feb|mar|apr|may|jun|jul|aug|sep|sept|oct|nov|dec)[a-z]*|"
    r"(jan|feb|mar|apr|may|jun|jul|aug|sep|sept|oct|nov|dec)[a-z]*\s+\d{1,2}|"
    r"\d{4}-\d{2}-\d{2}|\d{1,2}/\d{1,2}(/\d{2,4})?)\b",
    re.I,
)
# Signals that there is NO slot — these override positives.
NEGATIVE = re.compile(
    r"\b(na|n/a|not\s+available|no\s+(slots?|appointments?|dates?)|nothing|none|gone|"
    r"closed|vanished|disappeared|ghost|anyone\s+(got|seeing)|any\s+(slots?|update|luck)\?*|"
    r"is\s+there|please\s+help|how\s+to|\?)\b",
    re.I,
)
# Agents selling slots. Anything matching this is dropped outright.
AGENT_SPAM = re.compile(
    r"(contact\s*[:\-]|whatsapp|wa\.me|\bcharges?\b|\bgenuine\b|\bagent\b|recommend|ping\s+(him|her|me)|"
    r"low\s+cost|\bdm\b|\binbox\b|\bin\s+pm\b|booking\s*[:\-]|happy\s+client|payment|\bfee\b|cheap|"
    r"book\s+(an\s+)?early|all\s+categories|reach\s+out|booking\s+available|dates?\s+available\s+for|"
    r"exchange\s+rates?|salary|scammers?|\bpaid\b|\bprice\b)",
    re.I,
)
# Questions are never slot reports.
QUESTION = re.compile(r"\?|\b(anyone|any\s+one|which|what|when|how|does|is\s+it|has\s+any)\b", re.I)
# Bot feeds like VSIHub say "Earliest Slot: Apr 1, 2027 (151 days away)".
DAYS_AWAY = re.compile(r"\((\d+)\s+days?\s+away\)", re.I)
MAX_DAYS_AWAY = int(cfg.get("max_days_away", 120))

_last_alert_text = {}
_last_sound_ts = 0.0


def log(line: str):
    stamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    msg = f"[{stamp}] {line}"
    print(msg, flush=True)
    with LOG_PATH.open("a") as f:
        f.write(msg + "\n")


def mentioned_cities(text: str):
    found = set()
    for city, aliases in ALL_CITIES.items():
        for a in aliases:
            if re.search(rf"\b{re.escape(a)}\b", text):
                found.add(city)
                break
    return found


def mentioned_visa_types(text: str):
    found = set()
    for vt, aliases in VISA_TYPE_ALIASES.items():
        for a in aliases:
            if re.search(rf"\b{re.escape(a)}\b", text):
                found.add(vt)
                break
    return found


def classify(text: str, has_photo: bool):
    """Return (should_alert, reason)."""
    t = (text or "").lower().strip()

    if AGENT_SPAM.search(t):
        return False, "agent spam"

    # Structured bot feeds: alert only if the earliest date is within my window.
    m = DAYS_AWAY.search(t)
    if m:
        days = int(m.group(1))
        if days > MAX_DAYS_AWAY:
            return False, f"earliest slot {days}d away (> {MAX_DAYS_AWAY})"
        cities = mentioned_cities(t)
        if MY_CITIES and cities and not (cities & set(MY_CITIES)):
            return False, f"other city {sorted(cities)}"
        return True, f"earliest slot {days}d away"

    # City filter: if the message names cities and none are mine, skip.
    cities = mentioned_cities(t)
    if MY_CITIES and cities and not (cities & set(MY_CITIES)):
        return False, f"other city {sorted(cities)}"

    # Visa type filter: if the message names visa types and none are mine, skip.
    vtypes = mentioned_visa_types(t)
    if MY_VISA_TYPES and vtypes and not (vtypes & set(MY_VISA_TYPES)):
        return False, f"other visa type {sorted(vtypes)}"

    # A screenshot in an alerts channel is the highest-signal thing there is.
    if has_photo and ALERT_ON_PHOTOS:
        if NEGATIVE.search(t) and not DATE.search(t):
            return False, "photo but negative caption"
        return True, "screenshot posted"

    if not t:
        return False, "empty"

    # Very short "NA" style messages.
    if len(t) <= 4 and re.fullmatch(r"n/?a\.?", t):
        return False, "NA"

    has_date = bool(DATE.search(t))
    has_pos = bool(POSITIVE.search(t))
    has_neg = bool(NEGATIVE.search(t))

    if has_date and not re.search(r"\b(na|not\s+available|no\s+slots?|closed|gone|vanished|disappeared|missed)\b", t):
        return True, "date mentioned"
    if QUESTION.search(t):
        return False, "question"
    if has_pos and not has_neg:
        return True, "positive keywords"
    return False, "no signal" if not has_neg else "negative"


def macos_alert(title: str, body: str):
    global _last_sound_ts
    if sys.platform != "darwin":
        return
    safe_title = title.replace('"', "'")
    safe_body = body.replace('"', "'")[:200]
    # Notification is silent; we play the sound ourselves so it works even if
    # notification sounds for Script Editor are turned off in System Settings.
    subprocess.Popen(["osascript", "-e", f'display notification "{safe_body}" with title "{safe_title}"'])
    now = time.time()
    # Don't stack sounds on top of each other during a bulk release storm.
    if now - _last_sound_ts > 8:
        _last_sound_ts = now
        sound_file = f"/System/Library/Sounds/{SOUND_NAME}.aiff"
        if not os.path.exists(sound_file):
            sound_file = "/System/Library/Sounds/Glass.aiff"
        for i in range(max(1, SOUND_REPEATS)):
            if i:
                time.sleep(0.6)
            subprocess.Popen(["afplay", sound_file])
        if SPEAK:
            subprocess.Popen(["say", "-r", "190", "Visa slot alert."])


def phone_push(text: str):
    if not (BOT_TOKEN and BOT_CHAT_ID and requests):
        return
    try:
        requests.post(
            f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage",
            json={"chat_id": BOT_CHAT_ID, "text": text[:3500], "disable_web_page_preview": True},
            timeout=10,
        )
    except Exception as e:  # noqa: BLE001
        log(f"push failed: {e}")


def dedupe(key: str, text: str) -> bool:
    """True if this is a repeat of what we alerted on in the last 90s."""
    now = time.time()
    prev = _last_alert_text.get(key)
    _last_alert_text[key] = (text, now)
    return bool(prev and prev[0] == text and now - prev[1] < 90)


def start_keepalive_server():
    """Tiny HTTP server + periodic self-ping so hosts that sleep idle web apps
    (e.g. Hugging Face Spaces) keep this process alive. No-op unless PORT is set."""
    port = os.environ.get("PORT")
    if not port:
        return
    import threading
    from http.server import BaseHTTPRequestHandler, HTTPServer

    class H(BaseHTTPRequestHandler):
        def do_GET(self):
            self.send_response(200)
            self.send_header("Content-Type", "text/plain")
            self.end_headers()
            tail = LOG_PATH.read_text()[-4000:] if LOG_PATH.exists() else "(no log yet)"
            self.wfile.write(f"visa-slot-alerts up\n\n{tail}".encode())

        def log_message(self, *a):  # silence request logging
            pass

    threading.Thread(target=HTTPServer(("0.0.0.0", int(port)), H).serve_forever, daemon=True).start()

    # Render sets RENDER_EXTERNAL_URL automatically; free instances sleep after
    # 15 idle minutes, so we poke our own public URL every 5 minutes.
    self_url = os.environ.get("SELF_URL") or os.environ.get("RENDER_EXTERNAL_URL")
    if self_url and requests:
        def ping():
            while True:
                time.sleep(int(os.environ.get("SELF_PING_SECONDS", "300")))
                try:
                    requests.get(self_url, timeout=15)
                except Exception:  # noqa: BLE001
                    pass
        threading.Thread(target=ping, daemon=True).start()


async def main():
    start_keepalive_server()
    session = StringSession(SESSION_STRING) if SESSION_STRING else str(HERE / "session")
    client = TelegramClient(session, API_ID, API_HASH)
    await client.start()
    # The session file is a login token for your Telegram account: owner-only.
    for p in (HERE / "session.session", CONFIG_PATH):
        if p.exists():
            os.chmod(p, 0o600)
    me = await client.get_me()
    log(f"logged in as {me.first_name} (@{me.username}); watching {len(CHANNELS)} chats")

    # Telegram only pushes live updates for chats we're a member of. Channels
    # with a pending join request are still registered: updates start flowing
    # automatically the moment an admin approves, no restart needed.
    joined = set()
    async for d in client.iter_dialogs():
        u = getattr(d.entity, "username", None)
        if u:
            joined.add(u.lower())

    entities = []
    for ch in CHANNELS:
        try:
            ent = await client.get_entity(ch)
        except Exception as e:  # noqa: BLE001
            log(f"  ! could not resolve {ch}: {e}")
            continue
        entities.append(ent)
        if ch.lower() in joined:
            log(f"  + {ch}")
        else:
            log(f"  ~ {ch}  (join pending; alerts start automatically once approved)")

    if not entities:
        sys.exit("No channels resolved. Check the names in config.json.")

    def process(chat, msg_id: int, text: str, has_photo: bool):
        chat_name = getattr(chat, "title", None) or getattr(chat, "username", "chat")
        should, reason = classify(text, has_photo)
        preview = text.replace("\n", " ")[:120]
        if not should:
            log(f"skip  [{chat_name}] ({reason}) {preview}")
            return
        if dedupe(chat_name, text):
            log(f"dupe  [{chat_name}] {preview}")
            return
        link = f"https://t.me/{chat.username}/{msg_id}" if getattr(chat, "username", None) else ""
        log(f"ALERT [{chat_name}] ({reason}) {preview} {link}")
        body = text.strip() or "(screenshot)"
        macos_alert(f"VISA SLOT — {chat_name}", body)
        phone_push(f"🚨 {chat_name} ({reason})\n\n{body}\n\n{link}")

    @client.on(events.NewMessage(chats=entities))
    async def handler(event):
        process(await event.get_chat(), event.id, event.raw_text or "", bool(event.photo))

    log("watching... (Ctrl+C to stop)")
    await client.run_until_disconnected()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass
