# US visa slot alerts (Telegram watcher)

Watches the public Telegram slot channels with your own Telegram account,
filters out "NA" noise, and fires a loud alert on your Mac (plus an optional
push to your phone) only when an open slot is reported for your cities.

It never logs in to usvisascheduling.com — that is what gets accounts banned.
You still book manually, which you'd have to do anyway.

## One-time setup (~10 min)

1. Install deps

       cd ~/visa-slot-alerts
       pip3 install -r requirements.txt

2. Get Telegram API credentials (free, takes 2 min)
   - Go to https://my.telegram.org → log in with your phone number
   - "API development tools" → create an app (any name) → copy `api_id` and `api_hash`

3. Join the channels in the Telegram app (the script can only read chats you're in):
   - @USvisaAppointmentsHelp
   - @USB1B2VisaSlots
   - @b1b2_visa_slots_official
   - @mumbaivisaslots (and the city channels linked from its description)

4. Create your config

       cp config.example.json config.json

   Edit `config.json`: paste `telegram_api_id` / `telegram_api_hash`,
   set `cities` and `visa_types` (leave `cities` as all five to maximise chances).

5. (Optional but recommended) phone push via your own Telegram bot
   - In Telegram, message @BotFather → `/newbot` → copy the token into `push_bot_token`
   - Open your new bot and send it `/start`
   - Visit `https://api.telegram.org/bot<TOKEN>/getUpdates` in a browser and copy
     the `"chat":{"id": ...}` number into `push_chat_id`
   - Set that bot's chat to a loud custom notification sound on your phone and
     exempt it from Do Not Disturb.

## Login

Interactive (asks for phone + code):   `python3 alert.py`
QR code (scan from phone, no code):    `python3 qr_login.py`
Check / join channels:                 `python3 tg_setup.py status` / `python3 tg_setup.py join`

Either way the login is saved in `session.session`.

## Run (installed as a macOS launch agent)

It runs in the background, starts at login, and restarts itself if it dies.
`caffeinate -i` stops the Mac from sleeping while it runs (lid must stay open,
or be plugged in with the lid closed + an external display).

    # status
    launchctl print gui/$(id -u)/com.kamlesh.visa-slot-alerts | grep -E "state|pid"
    # live log
    tail -f ~/visa-slot-alerts/watcher.out
    # stop / start
    launchctl bootout gui/$(id -u)/com.kamlesh.visa-slot-alerts
    launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/com.kamlesh.visa-slot-alerts.plist
    # after editing config.json, restart:
    launchctl kickstart -k gui/$(id -u)/com.kamlesh.visa-slot-alerts

Everything it sees is written to `alerts.log` so you can review what was
skipped vs alerted and tune the filters.

## Uninstall (when you've got your slot)

    launchctl bootout gui/$(id -u)/com.kamlesh.visa-slot-alerts
    rm ~/Library/LaunchAgents/com.kamlesh.visa-slot-alerts.plist
    rm -rf ~/visa-slot-alerts
    # then Telegram → Settings → Devices → terminate the "Visa Slot Alerts" session

## Security notes (work laptop)

- The script only reads its own folder and talks only to Telegram's servers.
  It never touches your repos, keychain, browser, or company files.
- `session.session` is a login token for your personal Telegram account and
  `config.json` holds your API keys. Both are set to owner-only (chmod 600).
  When you're done: delete this folder and revoke the session in
  Telegram → Settings → Devices.
- If your company's IT policy forbids personal messaging clients or
  unapproved software on the laptop, run this on a personal machine instead.

## Tuning

- Sound: `"sound"` in `config.json` is any name from `/System/Library/Sounds`
  (Glass, Hero, Submarine, Ping, Purr, Blow, Funk, ...). Plays once per alert,
  no speech. Set `"sound_repeats": 2` or `"speak": true` if you want more.
- Too noisy → set `"alert_on_photos": false` or remove the chattier discussion
  groups from `channels` and keep only alert-only channels.
- Missing things → look at `alerts.log` for `skip` lines and tell me the text;
  the keyword lists live at the top of `alert.py`.

## When an alert fires

Portal already open and logged in → Schedule/Reschedule → the reported city →
OFC first → click the first date → confirm. Aim for under 60 seconds.
