# 🪶 Thoth — Novel/Anime Tracker Bot

Named for the Egyptian god of wisdom: it doesn't just remind you a chapter
dropped, it tries hard not to bother you with noise about its own plumbing
along the way.

Tracks specific novels (by URL) and anime (via AniList) for new chapters/
episodes, keeps a personal library with status/ratings/tags, and notifies
you on Telegram, WhatsApp, and/or Discord. One shared command set works
identically on all three.

## 1. Get a Telegram bot token

1. Open Telegram, message **@BotFather**
2. Send `/newbot`, follow the prompts, copy the token it gives you
3. Message **@userinfobot** to get your own numeric Telegram user ID
   (you'll need this for `ALLOWED_TELEGRAM_IDS` so strangers can't use your bot)

## 1b. Get a Discord bot token (free, simplest of the three)

1. Go to https://discord.com/developers/applications, click **New Application**
2. Go to the **Bot** tab > **Reset Token** > copy it into `DISCORD_BOT_TOKEN`
3. On the same Bot tab, scroll to **Privileged Gateway Intents** and turn on
   **Message Content Intent** (required so the bot can read your commands)
4. Under **OAuth2 > URL Generator**, check `bot` scope, give it
   `Send Messages` + `Read Message History` permissions, open the
   generated URL to invite it to a private server you own (or just DM it
   directly once it's in any server with you)
5. Turn on **Developer Mode** in your own Discord settings (Advanced),
   then right-click your own name anywhere > **Copy User ID** — put that
   in `ALLOWED_DISCORD_IDS`

## 2. Set up WhatsApp (free, personal test mode)

1. Go to https://developers.facebook.com, create a developer account if needed
2. Create a new App > select the "WhatsApp" product
3. Under WhatsApp > API Setup you'll get a **temporary access token**,
   a **test phone number**, and a **Phone Number ID**
4. Under "To" field, add your own WhatsApp number as a test recipient
   (you verify it with a code WhatsApp sends you) — free test mode allows
   up to 5 numbers, no business verification needed
5. Note: the temporary access token expires in 24 hours. For a token that
   doesn't expire, generate a **System User token** under
   Business Settings > Users > System Users (still free, still no business
   verification required for personal test-mode use)
6. Copy your **App Secret** from App Settings > Basic — put it in
   `WHATSAPP_APP_SECRET` in `.env`. This lets the bot verify that incoming
   webhook requests really came from Meta and weren't forged by someone
   who found your webhook URL. Skip it for quick local testing, but set
   it before you point a real domain at this.
7. You'll configure the webhook URL (`https://yourdomain/webhook`) once
   the bot is running on your VM — see step 4 below

## 2.5. Optional: natural language mode (/ask) + scraper AI fallback

Every command (`/add`, `/list`, `/status`, etc.) works with **zero LLM
involvement and zero cost** — that's the default, always-on bot. The
recurring chapter check also works with zero LLM cost in the normal case
(CSS selector, or a "chapter" text heuristic if no selector is set).

One optional API key, `GEMINI_API_KEY`, unlocks two extra features:

1. **`/ask`** — type things like *"hey, did Frieren get a new episode?"*
   instead of exact command syntax. Gemini just translates your sentence
   into the right command(s); every other command still bypasses the LLM
   entirely. It can also answer read-only questions about your library and
   the bot itself (item details, history, stats, broken scrapers, etc).
2. **Scraper AI fallback** — if a novel's CSS selector *and* the built-in
   "chapter" heuristic both fail (typically after a site redesign), the bot
   makes one Gemini call with the page's text and asks it to find the
   latest chapter marker, before giving up and marking the item broken.

To enable either:
1. Get a free API key at https://aistudio.google.com/apikey (no credit card
   needed for the free tier, generous limits for personal use)
2. Put it in `GEMINI_API_KEY` in `.env`
3. Restart the bot

Leave `GEMINI_API_KEY` blank if you don't want either feature — `/ask` will
tell you it's not configured, and the scraper falls back to "marked broken"
exactly as before. Everything else works the same either way.

## 3. Configure the bot

```bash
cp .env.example .env
nano .env   # fill in TELEGRAM_BOT_TOKEN, WHATSAPP_TOKEN, etc.
```

Set `ALLOWED_TELEGRAM_IDS` and `ALLOWED_WHATSAPP_NUMBERS` to your own
IDs/numbers — this is what stops random people from controlling your bot.

## 4. Run it on your Oracle Always Free VM

```bash
sudo apt update && sudo apt install -y python3-pip python3-venv nginx certbot python3-certbot-nginx
git clone <wherever you put this project> novelbot   # or scp the folder up
cd novelbot
python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt
python main.py
```

For WhatsApp's webhook, Meta requires a public **HTTPS** URL. On the VM:
1. Point a domain (or free subdomain) at your VM's public IP
2. Use `certbot` to get a free TLS cert, set up nginx to reverse-proxy
   `https://yourdomain/webhook` -> `http://localhost:8080/webhook`
3. In the Meta App Dashboard > WhatsApp > Configuration, set the webhook
   URL to `https://yourdomain/webhook` and the verify token to whatever
   you set as `WHATSAPP_VERIFY_TOKEN` in `.env`
4. Subscribe to the `messages` field

If you only want Telegram for now, skip all of this — just leave the
WhatsApp variables blank in `.env` and it'll run Telegram-only.

## 5. Keep it running permanently (systemd)

Create `/etc/systemd/system/novelbot.service`:

```ini
[Unit]
Description=Novel/Anime Tracker Bot
After=network.target

[Service]
WorkingDirectory=/home/youruser/novelbot
ExecStart=/home/youruser/novelbot/venv/bin/python main.py
Restart=always
RestartSec=5

[Install]
WantedBy=multi-user.target
```

Then:
```bash
sudo systemctl daemon-reload
sudo systemctl enable novelbot
sudo systemctl start novelbot
sudo journalctl -u novelbot -f   # watch logs
```

It'll now survive reboots and auto-restart if it crashes.

## 6. Using the bot

Message it on Telegram or WhatsApp (same commands either way):

```
/add novel Omniscient Reader | https://example.com/orv-page | div.latest-chapter
/add anime Frieren
/list
/status 1 completed
/rate 2 9 loved the ending
/tag 1 isekai
/check
/history
/stats
/help
```

With `/ask` enabled (see section 2.5), you can also just say things like:
```
/ask did frieren get a new episode yet
/ask track The Beginning After The End at https://example.com/tbate, find the chapter selector yourself
/ask what's still on hold
```

**Finding a CSS selector for a novel site:** open the novel's page in
Chrome, right-click the latest chapter link/text, choose "Inspect," then
right-click the highlighted HTML and "Copy > Copy selector." Paste that
in as the third `/add novel` field. If you skip it, the bot tries a
best-effort guess, but it's less reliable and more likely to need fixing
later — worth doing once per title.

## Notes & honest limitations

- **Novel scraping accuracy depends on the site.** Sites that redesign
  their pages will break that title's tracking — you'll get a "broke"
  alert immediately so you know to re-add it with a fresh selector.
- **Anime tracking** uses AniList's airing schedule, which is reliable
  for "has episode X aired" but doesn't know which streaming site has it
  available.
- **WhatsApp temporary tokens expire in 24h** — use a System User token
  for anything long-running (see step 2.5).
- Everything (database, history) lives in `data/bot.db` — back this up
  if you care about your library/history.
