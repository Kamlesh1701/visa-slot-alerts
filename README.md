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

## Where it runs: Render (free web service, 24/7)

Code: https://github.com/Kamlesh1701/visa-slot-alerts (public; no secrets in it)
Service: https://dashboard.render.com/web/srv-dbj5mj8473hc73b80at0
Status + recent log: https://visa-slot-alerts-nr4x.onrender.com/

Secrets (Telegram session, API keys, bot token) live only in Render's
environment variables. The free instance sleeps after 15 idle minutes, so the
app pings its own URL every 5 minutes to stay awake. Render may restart it
occasionally; it reconnects on its own.

    # redeploy after changing alert.py (commit + push, then):
    curl -s -X POST -H "Authorization: Bearer $RENDER_KEY" \
      https://api.render.com/v1/services/srv-dbj5mj8473hc73b80at0/deploys
    # change channels / cities / max_days_away: edit the CONFIG_JSON env var
    # in the Render dashboard (Environment tab) → it redeploys automatically.

## Running on the Mac instead (fallback)

`launchagent.plist.disabled` is the macOS launch agent that was used before.
To use it again:

    cp ~/visa-slot-alerts/launchagent.plist.disabled ~/Library/LaunchAgents/com.kamlesh.visa-slot-alerts.plist
    launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/com.kamlesh.visa-slot-alerts.plist

(Stop the Render service first or you'll get every alert twice.)

## Uninstall (when you've got your slot)

1. Render dashboard → service → Settings → Delete service.
2. Delete the GitHub repo (or leave it; it has no secrets).
3. `rm -rf ~/visa-slot-alerts`
4. Telegram → Settings → Devices → terminate the "Visa Slot Alerts" session.
5. Revoke the Render API key (Account Settings → API Keys) and delete the
   Hugging Face token you created (unused).

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
