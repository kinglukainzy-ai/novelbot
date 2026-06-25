#  Thoth

This is basically my DIY version of Thoth, yeah, like the god from *Gods of
Egypt*. Yeah yeah yeah, I know. But come on, I've read so many novels at
this point that I genuinely lost track of what I'm reading, what I dropped,
what I finished, and which ones quietly got a new chapter while I wasn't
looking. So I built something for that. Figured if I'm doing it for myself,
might as well do it properly and put it out there too, for the youth, man.

Took about 2 weeks, but Thoth finally cooked.

It tracks novels (via URL/selector or NovelFire's site search) and anime
(via AniList) for new chapters/episodes, keeps a personal library with
status/ratings/tags/progress, fixes its own broken trackers when it can,
and pings you on Telegram, WhatsApp, and/or Discord. Same command set,
identical everywhere.

There's also a talk-to-it mode (`/ask`) that's actually got a personality
now. It'll introduce itself as Thoth and hold a real conversation, not just
spit back command syntax. Heads up though: the AI side of it yaps a bit
more than I'd like, and it occasionally forgets that it *is* Thoth mid
conversation and goes off about Egyptian mythology unprompted. Working on
it. Either way, it's an add-on, not the core. Everything (`/add`, `/list`,
`/status`, etc.) still works perfectly with zero AI involved at all.

## 1. Get a Telegram bot token

1. Open Telegram, message **@BotFather**
2. Send `/newbot`, follow the prompts, copy the token it gives you
3. Message **@userinfobot** to get your own numeric Telegram user ID
   (you'll need this for `ALLOWED_TELEGRAM_IDS` so randoms can't run your bot)

## 1b. Get a Discord bot token (free, simplest of the three)

1. Go to https://discord.com/developers/applications, click **New Application**
2. Go to the **Bot** tab, **Reset Token**, copy it into `DISCORD_BOT_TOKEN`
3. On the same Bot tab, scroll to **Privileged Gateway Intents** and turn on
   **Message Content Intent** (required so the bot can read your commands)
4. Under **OAuth2 > URL Generator**, check `bot` scope, give it
   `Send Messages` + `Read Message History` permissions, open the
   generated URL to invite it to a private server you own (or just DM it
   directly once it's in any server with you)
5. Turn on **Developer Mode** in your own Discord settings (Advanced),
   then right-click your own name anywhere, **Copy User ID**, put that
   in `ALLOWED_DISCORD_IDS`

## 2. Set up WhatsApp (free, personal test mode)

1. Go to https://developers.facebook.com, create a developer account if needed
2. Create a new App, select the "WhatsApp" product
3. Under WhatsApp > API Setup you'll get a **temporary access token**,
   a **test phone number**, and a **Phone Number ID**
4. Under "To" field, add your own WhatsApp number as a test recipient
   (you verify it with a code WhatsApp sends you). Free test mode allows
   up to 5 numbers, no business verification needed
5. Note: the temporary access token expires in 24 hours. For a token that
   doesn't expire, generate a **System User token** under
   Business Settings > Users > System Users (still free, still no business
   verification required for personal test-mode use)
6. Copy your **App Secret** from App Settings > Basic, put it in
   `WHATSAPP_APP_SECRET` in `.env`. This lets the bot verify that incoming
   webhook requests really came from Meta and weren't forged by someone
   who found your webhook URL. Skip it for quick local testing, but set
   it before you point a real domain at this.
7. You'll configure the webhook URL (`https://yourdomain/webhook`) once
   the bot is running on your VM, see step 4 below

## 2.5. The AI add-on (`/ask` + self-healing broken trackers)

Quick honesty check first: every plain command (`/add`, `/list`, `/status`,
`/rate`, `/check`, all of it) runs with **zero AI involved, zero cost,
zero dependency on anything external.** That's the real bot. Everything
below is genuinely optional extra flavor on top.

If you want it, there's a 4-tier pipeline behind the scenes for keeping
novels from going "broken" every time a site hiccups:

1. **Tier 0**: for NovelFire titles with a known chapter number, it just
   requests chapter N+1 directly. Clean and free, no scraping ambiguity.
2. **CSS selector / heuristic scrape**: the original, no-AI approach.
3. **Local LLM** (self-hosted, e.g. via Ollama): reads the already-fetched
   page text and tries to spot the latest chapter itself. Needs no internet
   access of its own since the page is already in hand.
4. **Gemini, with real web search**: the actual last resort, only touched
   when the page genuinely can't be loaded at all.

`/ask` is the talk-to-it mode. Say things like *"hey, did Frieren get a
new episode?"* or *"what's my top rated book"* instead of exact syntax. It
also runs through your local LLM first if you've got one set up (see below),
and only escalates to Gemini for stuff that genuinely needs live web access.

**To turn on the local LLM tiers (recommended if you can spare the RAM):**
1. Install Ollama: `curl -fsSL https://ollama.com/install.sh | sh`
2. Pull a small model: `ollama pull phi4-mini` (a 3-8GB RAM model is plenty,
   this runs fine CPU-only on something like Oracle's free-tier ARM box)
3. Add to `.env`:
   ```
   OLLAMA_HOST=http://localhost:11434
   OLLAMA_MODEL=phi4-mini
   ```
4. Restart the bot, then send `/sources` to confirm it's live

**To turn on Gemini (the actual-internet-needed fallback):**
1. Get a free key at https://aistudio.google.com/apikey (no card needed)
2. Put it in `GEMINI_API_KEY` in `.env`, restart

Leave both blank and everything still works. `/ask` just won't exist, and
the scraper falls back to "marked broken" the old-fashioned way.

## 3. Configure the bot

```bash
cp .env.example .env
nano .env   # fill in TELEGRAM_BOT_TOKEN, WHATSAPP_TOKEN, etc.
```

Set `ALLOWED_TELEGRAM_IDS`, `ALLOWED_WHATSAPP_NUMBERS`, and
`ALLOWED_DISCORD_IDS` to your own IDs/number. Comma-separate if you want
to let a friend in too. Leave one blank and *anyone* who finds that
platform's bot can use it, so don't skip this.

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
   `https://yourdomain/webhook` to `http://localhost:8080/webhook`
3. In the Meta App Dashboard > WhatsApp > Configuration, set the webhook
   URL to `https://yourdomain/webhook` and the verify token to whatever
   you set as `WHATSAPP_VERIFY_TOKEN` in `.env`
4. Subscribe to the `messages` field

If you only want Telegram/Discord for now, skip all of this. Just leave
the WhatsApp variables blank in `.env`.

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

Same commands everywhere, Telegram, WhatsApp, Discord:

```
/add novel Omniscient Reader | https://example.com/orv-page | div.latest-chapter
/add novel Omniscient Reader's Viewpoint   <- or just the name, if it's on NovelFire
/add anime Frieren
/list
/status 1 completed
/rate 2 9 loved the ending
/set 2 url https://newsite.com/new-page
/tag 1 isekai
/fix broken
/check
/broken
/sources
/history
/stats
/help
```

With the AI add-on turned on, you can also just talk to it:
```
/ask hey
/ask what's my top rated book
/ask did frieren get a new episode yet
/ask track The Beginning After The End at https://example.com/tbate
```

**Finding a CSS selector for a novel site (only needed if it's not on
NovelFire):** open the novel's page in Chrome, right-click the latest
chapter link/text, choose "Inspect," then right-click the highlighted
HTML and "Copy > Copy selector." Paste that in as the third `/add novel`
field. Skip it and the bot tries a best-effort guess, works, but less
reliable.

## Notes & honest limitations

- **Novel scraping accuracy depends on the site.** Sites that redesign
  break that title's tracking. The multi-tier pipeline above tries hard
  to self-heal before ever flagging it broken, but it's not magic.
- **Anime tracking** uses AniList's airing schedule, reliable for "has
  episode X aired," doesn't know which streaming site has it.
- **The AI side will occasionally be weird about its own identity.** It
  knows it's named Thoth, but it's a small model and sometimes drifts into
  unprompted Egyptian-mythology trivia instead of just answering you. It's
  cosmetic, doesn't affect anything functional, just mildly annoying.
- **WhatsApp temporary tokens expire in 24h.** Use a System User token
  for anything long-running (see step 2.5).
- Everyone with an allowed ID shares the *same* library. There's no
  per-person separation of tracked titles, just per-person `/ask` chat
  history. Add a friend's ID and they're reading from/editing your shelf.
- Everything (database, history) lives in `data/bot.db`. Back this up
  if you care about your library/history.