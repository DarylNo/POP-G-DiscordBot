# POPG Discord Bot

A Discord bot for **Past our Prime Gamers** — silently tracks member activity across the server and lets you query stats and leaderboards via simple `!` commands.

---

## What it tracks

| Activity | How |
|---|---|
| Online / idle / DND | Presence events |
| Gaming (game name + duration) | Activity events |
| Voice channel presence | Voice state events |

All data is stored locally in a SQLite database (`popg.db`). Nothing is posted to channels unless you run a command.

---

## Commands

| Command | Who can use | Description |
|---|---|---|
| `!profile` | Everyone | Your own activity stats |
| `!profile @member` | Everyone | Another member's stats |
| `!stats [@member]` | Everyone | Alias for `!profile` |
| `!leaderboard` | Everyone | Top 10 by online time |
| `!leaderboard gaming` | Everyone | Top 10 by gaming hours |
| `!leaderboard voice` | Everyone | Top 10 by voice hours |
| `!admin sessions` | Admin | Live list of all active tracking sessions |
| `!admin reset @member` | Admin | Zero out a member's stats |
| `!admin info @member` | Admin | Raw stat dump for debugging |

Admin commands require the `Administrator` permission or a role named `Admin`.

---

## Setup

### 1. Create your Discord bot

1. Go to [discord.com/developers/applications](https://discord.com/developers/applications)
2. **New Application** → name it → **Create**
3. Click **Bot** in the sidebar → **Reset Token** → copy and save the token
4. Scroll down and enable both:
   - **Server Members Intent**
   - **Presence Intent**
5. **OAuth2 → URL Generator** → check `bot` under Scopes
6. Under Bot Permissions check: `Read Messages/View Channels`, `Send Messages`, `Embed Links`, `Read Message History`
7. Copy the generated URL → open it → invite the bot to your server

### 2. Get your Server ID

In Discord: **Settings → Advanced → enable Developer Mode**.  
Right-click your server icon → **Copy Server ID**.

### 3. Install the bot

```bash
git clone https://github.com/DarylNo/POP-G-DiscordBot.git
cd POP-G-DiscordBot
git checkout claude/discord-popg-chatbot-REQLv

python3 -m venv venv
source venv/bin/activate          # Windows: venv\Scripts\activate

pip install -r requirements.txt

cp .env.example .env
```

### 4. Configure `.env`

Open `.env` and fill in your values:

```
BOT_TOKEN=your_bot_token_here
PREFIX=!
GUILD_ID=your_server_id_here
```

### 5. Run

```bash
python3 bot.py
```

You should see:
```
INFO popg: POPG Bot ready — logged in as POPG Bot#1234
```

### Keep it running after closing the terminal

```bash
# Option A — background process
nohup python3 bot.py &

# Option B — screen session
screen -S popgbot
python3 bot.py
# Ctrl+A then D to detach; screen -r popgbot to reattach
```

---

## Requirements

- Python 3.10+
- `discord.py >= 2.3.0`
- `python-dotenv >= 1.0.0`

---

## Database

The bot creates `popg.db` automatically on first run. Three tables:

- **`users`** — one row per member, aggregate totals
- **`sessions`** — one row per activity session (open until the activity ends)
- **`game_stats`** — per-user, per-game cumulative stats

The bot recovers open sessions correctly when restarted.

---

## Roadmap

Phase 2 will add a local LLM (via [Ollama](https://ollama.com/)) for:
- `!ask <question>` — natural language Q&A about the server
- AI-generated narrative player summaries
- Game recommendations based on play history
- Voice transcription and session recaps via Whisper

See `.claude/roadmap.md` for the full technical plan.
