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
    "h1b": ["h1b", "h-1b", "h1", "h-1"],
    "h4": ["h4", "h-4"],
    "l1": ["l1", "l-1", "l1a", "l1b", "l2", "l-2"],
    "f2": ["f2", "f-2"],
    "j1": ["j1", "j-1", "j2"],
    "o1": ["o1", "o-1"],
    "dropbox": ["dropbox", "drop box", "iw", "interview waiver"],
}

# ---------------------------------------------------------------------------
# Classification rules. Tuned against ~200 recent messages per channel.
# ---------------------------------------------------------------------------
MONTH_RE = (r"(jan(?:uary)?|feb(?:ruary)?|mar(?:ch)?|apr(?:il)?|may|june?|july?|aug(?:ust)?|"
            r"sep(?:t(?:ember)?)?|oct(?:ober)?|nov(?:ember)?|dec(?:ember)?)")
MONTH_NUM = {"jan": 1, "feb": 2, "mar": 3, "apr": 4, "may": 5, "jun": 6, "jul": 7, "aug": 8, "sep": 9, "oct": 10, "nov": 11, "dec": 12}
# "14 Feb", "05 Nov, 26", "14th feb 2026", "27-May"
DATE_DM = re.compile(rf"\b(\d{{1,2}})(?:st|nd|rd|th)?\s*[-/ ]?\s*{MONTH_RE}\b\.?,?\s*'?(\d{{2}}(?:\d{{2}})?)?\b", re.I)
# "Feb 14", "Nov 04 2026", "May 19 '27", "June 7"
DATE_MD = re.compile(rf"\b{MONTH_RE}\b\.?\s+(\d{{1,2}})(?:st|nd|rd|th)?(?!\s*(?:days?|slots?|min|hrs?|hours?|months?|weeks?|years?|am|pm)\b)(?:,?\s*'?(\d{{2}}(?:\d{{2}})?))?\b", re.I)
DATE_ISO = re.compile(r"\b(20\d{2})-(\d{2})-(\d{2})\b")
# "May", "June 2027", "dec'26" — month with optional year, used when no full date present
MONTH_ONLY = re.compile(rf"\b{MONTH_RE}\b\.?,?\s*'?(20\d{{2}}|\d{{2}})?\b", re.I)
ANY_DATE = re.compile(rf"\b\d{{1,2}}\s*[-/ ]?\s*{MONTH_RE}|\b{MONTH_RE}\.?\s+\d{{1,2}}\b|\b20\d{{2}}-\d{{2}}-\d{{2}}\b", re.I)

CITY_RE = "|".join(re.escape(a) for al in ALL_CITIES.values() for a in al)
SLOT_NOUN = re.compile(rf"\b(slots?|dates?|appointments?|appts?|ofc|vac|biometrics?|consular|interview|all|both|{MONTH_RE}|{CITY_RE})\b", re.I)
AVAIL_VERB = re.compile(r"\b(open(?:ed|ing|ings|s)?|available|avail|showing|released?|bulk|appeared|popped)\b", re.I)
# "got/booked X in <city>" confirmations: weak but real signal that a release is happening right now.
CONFIRM = re.compile(rf"\b(got|booked|grabbed|secured)\b.*\b(ofc|vac|consular|biometrics?|slots?|{CITY_RE})\b|\b(ofc|vac|consular|biometrics?|slots?|{CITY_RE})\b.*\b(got|booked|grabbed|secured)\b", re.I)
CONFIRM_BLOCK = re.compile(r"\b(thank\w*|thx|yesterday|ago|days?\s+back|last\s+(week|night|time|month)|earlier|my\s+company|agenc\w*|reschedul\w*|already)\b", re.I)
# Things that mean "go check right now" regardless of anything else.
STRONG = re.compile(r"GUYS\s+CHECK|\bcheck\s+(now|portal|guys|fast)\b|\bhurry\b|\btry\s+now\b|\bbook\s+now\b|\bavailable\s+now\b|\bslots?\s+(are\s+)?(open|out|up|live)\b|\bbulk\b|\bburst\b|✅|🚨\s*slots?\s+open", re.I)
STRONG_CASE = re.compile(r"\bGO\b|GUYS\s+CHECK|\bGo\s+(?=\w)")  # case-sensitive: "GO", "Go Mumbai OFC" (not "go to the page")
BOT_COUNT = re.compile(r"\b(slots?|number\s+of\s+slots?)\s*:\s*([1-9]\d*)\b", re.I)

# No slot / dead end. Overrides everything except STRONG bot markers.
NEGATIVE = re.compile(
    r"\b(na|n/?a|nsa|not\s+available|unavailable|no\s+(slots?|appointments?|dates?|availability|luck)|nothing|none|gone|"
    r"closed|vanish\w*|disappear\w*|ghost|no\s+longer|missed|missing|don'?t\s+see|not\s+seeing|can'?t\s+see|"
    r"never|always|usually|exceeded|rate\s*limit\w*|too\s+many\s+requests|error|locked|blocked|stuck|not\s+loading|"
    r"doesn'?t\s+load|expired|waiting\s+for|still\s+waiting|hoping|wish|no\s+point|pointless|useless|waste|lottery|unpredictab\w*|people'?s?|someone|everyone|it'?s\s+true|keeps|pushing|struggl\w*|frustrat\w*|sad|unlucky)\b|🛑|❌|⛔",
    re.I,
)
# Dates in these contexts are personal history/paperwork, not slot sightings.
NOT_SLOT = re.compile(
    r"\b(reject\w*|refus\w*|expir\w*|valid\s+(till|until|through|upto|up\s+to)|challan|receipt|passport|221\s*\(?g\)?|"
    r"approved|administrative|stamp\w*|interview\s+(went|was|done|on|is\s+on)|travel\w*\s+on|flight|tickets?|landed|cgi|"
    r"ds-?160|payment|paid|fee|mrv|petition|i-?797|i-?20|sevis|graduat\w*|wedding|my\s+(interview|appointment|slot)\s+(is|was)|yesterday|days?\s+back|\d+\s+days?\s+ago|last\s+(week|night|month))\b",
    re.I,
)
# Agents selling slots, promos, scams. Dropped outright.
AGENT_SPAM = re.compile(
    r"(contact\s*[:\-]|whatsapp|wa\.me|\bcharges?\b|\bgenuine\b|\bagent\b|recommend|ping\s+(him|her|me)|"
    r"low\s+cost|\bdm\b|\binbox\b|\bin\s+pm\b|booking\s*[:\-]|happy\s+client|payment|\bfee\b|cheap|"
    r"book\s+(an\s+)?early|all\s+categories|reach\s+out|booking\s+available|dates?\s+available\s+for|"
    r"exchange\s+rates?|salary|scammers?|\bpaid\b|\bprice\b|join\s+(our|this|the)|t\.me/|https?://(?!t\.me/[A-Za-z0-9_]+/\d+)|"
    r"\bexpert\b|\bagenc(y|ies)\b|\bsupport\s+for\b|ds-?160\s+form|\bhelp\s+you\b|\bservices?\b)",
    re.I,
)
# Questions and requests are never sightings.
QUESTION = re.compile(
    r"\?|\b(anyone|any\s+one|which|what|when|how|does|is\s+(it|this|there)|has\s+any|why|where|who|should|could|"
    r"can\s+(i|we|u|you|anyone|someone)|possible|suggest\w*|advi[cs]e|pls|plz|looking\s+for|need\s+(a\s+)?slot|want\s+(a\s+)?slot|"
    r"help\s+me|if\s+(they|anyone|someone|you)|do\s+(the|they|you|we|i|u)|are\s+(the|there|they|you)|still\s+open|any\s+(slots?|dates?|update|news|luck|idea))\b",
    re.I,
)
# Screenshot captions that are error/help posts rather than calendars.
ERRORISH = re.compile(r"\b(error|limit|load\w*|wait\w*|exceeded|try\s+again|stuck|block\w*|lock\w*|issue|problem|help|this|what|why|how|same|anyone)\b", re.I)
# Bot feeds like VSIHub say "Earliest Slot: Apr 1, 2027 (151 days away)".
DAYS_AWAY = re.compile(r"\((\d+)\s+days?\s+away\)", re.I)
MAX_DAYS_AWAY = int(cfg.get("max_days_away", 120))
NO_PHOTO_CHATS = {c.lower() for c in cfg.get("no_photo_channels", [])}

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


def _year(y, month, day, today):
    if y:
        y = int(y)
        return y + 2000 if y < 100 else y
    # No year given: the next occurrence (allow a week of slack for "booked yesterday" posts)
    try:
        cand = today.replace(year=today.year, month=month, day=day)
    except ValueError:
        return today.year
    return today.year if (cand - today).days >= -7 else today.year + 1


def days_away(text: str):
    """Smallest number of days from today to any slot-like date in the text, or None.
    Dates within +/-1 day are ignored (they're almost always 'posted at' timestamps)."""
    from datetime import date
    today = date.today()
    offsets = []

    def add(y, mo, d):
        try:
            offsets.append((date(y, mo, d) - today).days)
        except ValueError:
            pass

    for m in DATE_ISO.finditer(text):
        add(int(m.group(1)), int(m.group(2)), int(m.group(3)))
    for m in DATE_DM.finditer(text):
        mo = MONTH_NUM[m.group(2)[:3].lower()]
        d = int(m.group(1))
        if 1 <= d <= 31:
            add(_year(m.group(3), mo, d, today), mo, d)
    for m in DATE_MD.finditer(text):
        mo = MONTH_NUM[m.group(1)[:3].lower()]
        d = int(m.group(2))
        if 1 <= d <= 31:
            add(_year(m.group(3), mo, d, today), mo, d)
    if not offsets:
        for m in MONTH_ONLY.finditer(text):
            mo = MONTH_NUM[m.group(1)[:3].lower()]
            add(_year(m.group(2), mo, 1, today), mo, 1)
    real = [o for o in offsets if abs(o) > 1]
    return min(real) if real else None


def classify(text: str, has_photo: bool, chat: str = ""):
    """Return (should_alert, reason)."""
    raw = text or ""
    t = raw.lower().strip()
    chat = (chat or "").lower()

    if AGENT_SPAM.search(t):
        return False, "agent spam"

    # City / visa-type filters: if the message names some and none are mine, skip.
    cities = mentioned_cities(t)
    if MY_CITIES and cities and not (cities & set(MY_CITIES)):
        return False, f"other city {sorted(cities)}"
    vtypes = mentioned_visa_types(t)
    if MY_VISA_TYPES and vtypes and not (vtypes & set(MY_VISA_TYPES)):
        return False, f"other visa type {sorted(vtypes)}"

    # Structured bot feeds: "(N days away)".
    m = DAYS_AWAY.search(t)
    if m:
        days = int(m.group(1))
        if days > MAX_DAYS_AWAY:
            return False, f"earliest slot {days}d away (> {MAX_DAYS_AWAY})"
        return True, f"earliest slot {days}d away"

    far = days_away(raw)
    too_far = far is not None and far > MAX_DAYS_AWAY
    negative = bool(NEGATIVE.search(t))
    question = bool(QUESTION.search(t))

    # Screenshots: strong signal in slot-focused groups, unless the caption says otherwise.
    if has_photo:
        if not ALERT_ON_PHOTOS or chat in NO_PHOTO_CHATS:
            return False, "photo (channel not photo-alerting)"
        if negative or question or ERRORISH.search(t) or NOT_SLOT.search(t):
            return False, "photo but caption is negative/question/error"
        if too_far:
            return False, f"photo but dates {far}d away (> {MAX_DAYS_AWAY})"
        return True, "screenshot posted"

    if not t:
        return False, "empty"
    if re.fullmatch(r"n/?s?a\.?!*", t):
        return False, "NA"

    # Bot formats: "Slots: 5", checkmarks, "SLOTS OPEN" headers — unless marked closed.
    bot_hit = BOT_COUNT.search(t) or "✅" in raw
    if bot_hit and not re.search(r"\b(closed|no\s+longer|not\s+available)\b|🛑|❌|⛔|⌛", t):
        if too_far:
            return False, f"bot: dates {far}d away (> {MAX_DAYS_AWAY})"
        return True, "bot availability"

    if question:
        return False, "question"
    if NOT_SLOT.search(t):
        return False, "paperwork/history context"
    if negative:
        return False, "negative"
    if too_far:
        return False, f"dates {far}d away (> {MAX_DAYS_AWAY})"

    if STRONG.search(t) or STRONG_CASE.search(raw):
        return True, "strong signal"
    if ANY_DATE.search(t):
        return True, "date mentioned"
    if AVAIL_VERB.search(t) and SLOT_NOUN.search(t):
        return True, "availability + slot words"
    if CONFIRM.search(t) and not CONFIRM_BLOCK.search(t):
        return True, "booking confirmation"
    return False, "no signal"


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
            tail = LOG_PATH.read_text()[-40000:] if LOG_PATH.exists() else "(no log yet)"
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

    def process(chat, msg_id: int, text: str, has_photo: bool, posted=None):
        chat_name = getattr(chat, "title", None) or getattr(chat, "username", "chat")
        should, reason = classify(text, has_photo, getattr(chat, "username", "") or "")
        preview = text.replace("\n", " ")[:120]
        lag = ""
        if posted:
            secs = int((datetime.now(posted.tzinfo) - posted).total_seconds())
            lag = f"posted {posted.astimezone().strftime('%H:%M:%S')}, caught +{secs}s"
        if not should:
            log(f"skip  [{chat_name}] ({reason}) {preview}")
            return
        if dedupe(chat_name, text):
            log(f"dupe  [{chat_name}] {preview}")
            return
        link = f"https://t.me/{chat.username}/{msg_id}" if getattr(chat, "username", None) else ""
        log(f"ALERT [{chat_name}] ({reason}) [{lag}] {preview} {link}")
        body = text.strip() or "(screenshot)"
        macos_alert(f"VISA SLOT — {chat_name}", body)
        phone_push(f"🚨 {chat_name} ({reason})\n{lag}\n\n{body}\n\n{link}")

    @client.on(events.NewMessage(chats=entities))
    async def handler(event):
        process(await event.get_chat(), event.id, event.raw_text or "", bool(event.photo), event.message.date)

    log("watching... (Ctrl+C to stop)")
    await client.run_until_disconnected()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass
