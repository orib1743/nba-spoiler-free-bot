# 🏀 NBA Agent

**A personal NBA assistant for people who watch games on replay.** Every
morning it tells you which of last night's games are worth watching, **without
spoilers**. You can also chat with it on Telegram or WhatsApp: ask about games,
or change your settings just by telling it.

```
🏀 NBA picks for Wed, Nov 18 — no spoilers

Worth watching:
• Grizzlies @ Cavaliers
• 76ers @ Magic
• Celtics @ Nuggets

Your players:
• Nikola Jokic — played
```

> You: was the Celtics game worth watching?
> Agent: Yes, it's worth watching by your rules 🍿 I'll keep the details to myself.
>
> You: also follow Wembanyama, and send the message at 7:30
> Agent: Done. Wemby's on your list and the message now arrives at 07:30 ✅

## Features

- **Morning message** with the games worth watching, sent at the time you choose.
  It waits for late West Coast games to finish.
- **Your rules.** A game is recommended if any rule matches:
  - ⭐ **Matchup:** two of your favorite teams, not a blowout (margin under 15 entering the 4th).
  - 🔥 **Close finish:** within N points at any point in the last M minutes, or overtime.
    Add as many as you like, e.g. *my teams within 5 in the last 5 minutes* plus
    *any game within 3 in the last 2 minutes*.
- **Player check-ins:** whether the players you follow played. Stats are opt-in.
- **A chat agent** (Claude) that answers questions and edits your settings. The
  first conversation walks you through setup.
- **Telegram and WhatsApp.** Connect either or both, and choose where the
  morning message goes.
- **Settings page** in your browser, with a live preview of the message.

## How spoilers are prevented

- The morning message is built by plain code, not by the AI. It never contains
  scores, winners, margins or the word "overtime". Games are listed in tip-off
  order, never "best first", and games that aren't recommended are never
  mentioned. Tests check all of this.
- The tool the agent uses for everyday questions **contains no scores at all**,
  so it can't leak what it never saw. Results and stats come from separate tools
  that it only uses when you explicitly ask ("who won?", "how many did Jokic
  score?").

## What it costs

| Part | Cost |
|---|---|
| Game data | Free (ESPN's public scoreboard feed) |
| Morning message | Free. It doesn't use the AI |
| Chatting with the agent | Claude API usage: roughly $0.03–0.10 per message with Claude Opus 5.5. Each conversation is capped at $1 by default (`NBA_AGENT_CHAT_BUDGET_CENTS`) |
| Telegram | Free |
| WhatsApp | Free test number. Meta charges per delivered *template* message (used when you haven't messaged the bot in 24 hours) |
| Running 24/7 | Free on your own PC; a few dollars a month on a cloud host |

---

## Quick start (your computer)

You need **Python 3.12+** and a Telegram account.

**1. Get the code and install**
```bash
git clone https://github.com/orib1743/nba-spoiler-free-bot.git
cd nba-spoiler-free-bot
python -m venv .venv
# Windows:   .venv\Scripts\activate
# macOS/Linux: source .venv/bin/activate
pip install -r requirements.txt
```

**2. Get two keys**
- **Claude API key:** sign up at https://platform.claude.com, add a little
  credit, and create a key under *API keys*.
- **Telegram bot token:** in Telegram, open **@BotFather**, send `/newbot`, and
  pick a name and a username ending in `bot`. BotFather replies with the token.

**3. Configure.** Copy `.env.example` to `.env` and fill in `ANTHROPIC_API_KEY`
and `TELEGRAM_BOT_TOKEN`.

**4. Run**
```bash
python run.py web
```
Wait for `Telegram bot @your_bot is listening`.

**5. Say hi.** Open your bot in Telegram and press **Start**. The agent walks
you through setup: teams, rules, players, send time and spoiler level. The
first person to message the bot becomes its owner; anyone else gets "this is a
private bot".

The settings page is at http://127.0.0.1:8000.

> The bot only works while `run.py web` is running. To have it run all the time
> without your computer, see [Run it 24/7 in the cloud](#run-it-247-in-the-cloud).

---

## Free: one hour a day on GitHub Actions

If you only need the morning message plus a little chatting, GitHub can run
the bot **for free** once a day (Actions minutes are free on public repos). The
workflow `.github/workflows/daily.yml`:
- starts the bot every morning and keeps it running for 70 minutes;
- sends the morning message at your send time if it falls inside that window,
  otherwise right away, and always before the window closes;
- answers your Telegram messages, including ones sent while it was off
  (Telegram keeps them for 24 hours). Replies to those come when the next
  window opens;
- keeps your settings between runs **encrypted** in the Actions cache.

**Setup**
1. Fork or push this repo to your GitHub account (keep it public for free minutes).
2. In the repo, open **Settings → Secrets and variables → Actions → New repository
   secret** and add:
   - `ANTHROPIC_API_KEY`
   - `TELEGRAM_BOT_TOKEN`
   - `NBA_AGENT_DATA_KEY`: a long random password. It encrypts your saved data;
     if you lose it, the bot starts fresh.
   - Optional: `TELEGRAM_ALLOWED_USER_IDS` (your Telegram user id), and
     `NBA_AGENT_SEED_SETTINGS` (the output of `python run.py export`) to start
     with your current settings.
3. Edit the `cron:` line in `daily.yml` to a few minutes before your send time,
   **in UTC**. For example, 08:00 in UTC+2 is `50 5 * * *`.
4. Go to **Actions → daily bot window → Run workflow** to try it right away. The
   *minutes* box sets how long it stays up.

**Good to know**
- GitHub may start scheduled runs 5–30 minutes late.
- GitHub pauses scheduled workflows in repos with no activity for 60 days;
  re-enable them on the Actions tab.
- Run only one copy of the bot at a time. Stop the one on your computer while
  Actions is running it.
- WhatsApp isn't supported in this mode, because it needs a public address all
  the time.

## Run it 24/7 in the cloud

For instant replies at any hour, run it on an always-on host. The app is one
process: the Telegram bot, the morning scheduler and the settings page. It
needs a **persistent disk** for `/data`, where your settings and conversation live.

### Option A: Railway (easiest, about $5/month)
1. Push this repo to your GitHub.
2. At https://railway.com, choose **New Project → Deploy from GitHub repo** and
   pick your repo. Railway finds the `Dockerfile` by itself.
3. Add a **volume** to the service, mounted at **`/data`**.
4. Under **Variables**, add `ANTHROPIC_API_KEY`, `TELEGRAM_BOT_TOKEN`, and
   `NBA_AGENT_ADMIN_PASSWORD` (so you can open the settings page online).
5. Optional: under **Settings → Networking → Generate Domain**, get a URL for
   the settings page (log in with any username and your admin password). You
   also need this URL for WhatsApp.

### Option B: any Docker host or VPS (Fly.io, Render, Hetzner, Oracle Cloud free tier…)
```bash
docker build -t nba-agent .
docker run -d --restart unless-stopped --name nba-agent \
  --env-file .env -v nba-agent-data:/data -p 8000:8000 nba-agent
```
Whatever the host, keep the `/data` volume and set the same environment
variables as in `.env`.

### Moving from your computer to the cloud
- **Run only one copy.** Telegram delivers messages to only one running copy
  of a bot, so stop the one on your computer once the cloud one is up.
- **Settings:** click **Export settings** on your local settings page, then
  **Import settings** on the cloud one. Or just tell the bot your preferences again.

---

## WhatsApp (optional)

WhatsApp is more setup than Telegram: a Meta developer app, plus a public URL
for incoming messages.

1. **Meta app.** At https://developers.facebook.com choose **My Apps → Create app**
   (Business type) and add **WhatsApp**. Under **WhatsApp → API Setup** you get a
   free test number:
   - Copy the **Phone number ID** → `WHATSAPP_PHONE_NUMBER_ID`.
   - Copy the **access token** → `WHATSAPP_TOKEN`. The temporary one lasts 24h;
     for a permanent one, create a System User token with
     `whatsapp_business_messaging`.
   - Add your own number as a recipient and confirm it. Put it, digits only, in
     `WHATSAPP_ALLOWED_NUMBERS`.
   - Copy **App settings → Basic → App secret** → `WHATSAPP_APP_SECRET`.
   - Set `WHATSAPP_VERIFY_TOKEN` to any random string.
2. **Public URL.**
   - In the cloud: your app's domain.
   - On your PC: a tunnel, e.g. [ngrok](https://ngrok.com) with a free static
     domain: `ngrok http --url=<your-domain> 8000`.
3. **Webhook.** In Meta, go to **WhatsApp → Configuration → Webhook**:
   - Callback URL: `https://<your-domain>/whatsapp/webhook`
   - Verify token: your `WHATSAPP_VERIFY_TOKEN`
   - Subscribe to the **messages** field.
4. **Template** for the morning message. WhatsApp only allows free text within
   24 hours of your last message to the bot; otherwise it requires a pre-approved
   template. In WhatsApp Manager, create:
   - Name: `nba_digest`
   - Category: Utility
   - Language: English
   - Body: `Your NBA picks are ready: {{1}}`

Incoming webhooks are verified with Meta's signature, and messages from numbers
other than yours are ignored.

---

## Settings page and security

- The settings page and the web chat have no user accounts. They open only on
  the machine running the app, **or** from anywhere with
  `NBA_AGENT_ADMIN_PASSWORD` (browser login: any username plus that password).
  Use the password only over HTTPS; cloud hosts provide it.
- From the internet, only `/whatsapp/webhook` (signature-checked) and
  `/healthz` respond.
- The Telegram bot needs no open port: it fetches messages from Telegram itself.
- Keys live in environment variables. `.env` and `data/` are git-ignored.

## Configuration reference

| Variable | Required | What it does |
|---|---|---|
| `ANTHROPIC_API_KEY` | for chat | Claude API key |
| `TELEGRAM_BOT_TOKEN` | for Telegram | From @BotFather |
| `TELEGRAM_ALLOWED_USER_IDS` | no | Lock the bot to these Telegram user ids |
| `NBA_AGENT_MODEL` / `NBA_AGENT_EFFORT` | no | Chat model (default `claude-opus-5-5`) and effort (default `medium`) |
| `NBA_AGENT_CHAT_BUDGET_CENTS` | no | Spending cap per conversation, in cents (default 100) |
| `NBA_AGENT_ADMIN_PASSWORD` | no | Open the settings page from other devices |
| `NBA_AGENT_DATA_DIR` | no | Where data is stored (default `./data`, `/data` in Docker) |
| `HOST` / `PORT` | no | Listen address (default `127.0.0.1:8000`; `0.0.0.0` in Docker) |
| `WHATSAPP_*` | for WhatsApp | See the WhatsApp section |

## Commands

```bash
python run.py web                      # bots + scheduler + settings page
python run.py preview 2026-11-18       # print the morning message for a night
python run.py send                     # send the morning message now
python run.py export > settings.json   # back up settings
python run.py import settings.json     # restore settings
python -m pytest tests                 # run the tests (no keys or network needed)
```

## How it works

```
nba_agent/
  espn.py       scores, play-by-play, rosters (ESPN public JSON, cached)
  analyzer.py   which games match your rules (pure functions)
  digest.py     builds the spoiler-free morning message
  scheduler.py  sends it at your time; waits for late games
  chat.py       the Claude agent (Managed Agents): instructions + tool list
  tools.py      what the agent can do: games, schedule, settings, results on request
  telegram.py   Telegram bot (long polling)
  whatsapp.py   WhatsApp Cloud API (webhook)
  web.py        settings page, API, webhook, access guard
  store.py      SQLite: settings, conversation state, send history
```

The chat agent runs on **Claude Managed Agents**:
- Anthropic hosts the agent loop and keeps each conversation (a "session") until
  you delete it. You can see them in the Claude Console under *Sessions*.
- The tools run in your app, because they read and change your local settings.
- The agent is created on first use; its ID is stored in `data/agent.json`. When
  its instructions change, it's updated and the next message starts a fresh
  conversation. Saved settings are kept.

## License

[MIT](LICENSE)

## Disclaimer

An independent hobby project, not affiliated with or endorsed by the NBA, ESPN,
Meta or Telegram. Game data comes from ESPN's public, undocumented JSON feed,
which can change or disappear without notice.
