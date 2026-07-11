# POPG Discord Bot — Toaster

The Discord bot for **Past our Prime Gamers**. Silently tracks member activity, runs an AI chat assistant powered by local LLMs, and transcribes voice sessions.

Current version: **1.9.1**

---

## What it does

### Activity tracking
Presence, gaming, and voice activity are recorded automatically with no configuration needed.

| Activity | How |
|---|---|
| Online / idle / DND | Presence events |
| Gaming (game name + duration) | Activity events |
| Voice channel presence | Voice state events |

All data is stored locally in a SQLite database (`popg.db`). Nothing is posted to channels automatically.

### AI assistant (Toaster)
`!chat` and DMs go to **Qwen 2.5 14B** running on a dedicated dual-GPU Ollama machine. Toaster has persistent memory built from voice session transcripts, chat history, and game stats — it remembers what happened in past sessions and can reference them in conversation. It can also search the web and read linked pages for current info.

### Voice transcription
`!join` starts recording a voice channel. Whisper transcribes audio in rolling 5-minute chunks. When the session ends with `!leave`, an AI-generated recap is posted and memories are extracted from the transcript and saved for future conversations.

---

## Commands

### Stats
| Command | Who | Description |
|---|---|---|
| `!profile [@member]` | Everyone | Activity stats (online, gaming, voice time) |
| `!stats [@member]` | Everyone | Alias for `!profile` |
| `!leaderboard [online\|gaming\|voice]` | Everyone | Top 10 by category (default: online) |
| `!weekly [online\|gaming\|voice]` | Everyone | Last 7 days leaderboard |
| `!monthly [online\|gaming\|voice]` | Everyone | Last 30 days leaderboard |

### AI
| Command | Who | Description |
|---|---|---|
| `!chat <message>` | Everyone | Chat with Toaster (also `!ask`) |
| `!when [@member]` | Everyone | Predict when a member will next be online |
| `!recap <session_id>` | Everyone | AI-generated summary of a past voice session |
| `!transcript <session_id>` | Everyone | Raw transcript of a voice session |
| `!sessions` | Everyone | List recent recorded voice sessions |
| `!memorybuild` | Admin | Rebuild Toaster's memory from all stored transcripts |
| `!reset` | Everyone (DM) / Admin (server) | Clear chat history; `!reset all` also wipes server memories |

Toaster also responds to DMs directly.

### Voice recording
| Command | Who | Description |
|---|---|---|
| `!join` | Admin | Start recording the voice channel you're in |
| `!leave` | Admin | Stop recording, post recap, save memories |

### Admin
| Command | Who | Description |
|---|---|---|
| `!admin sessions` | Admin | Live list of all active tracking sessions |
| `!admin reset @member` | Admin | Zero out a member's stats |
| `!admin info @member` | Admin | Raw stat dump for debugging |
| `!admin reload` | Admin | Reload all cogs without restarting |

Admin commands require the `Administrator` permission or a role named `Admin`.

---

## Setup

### 1. Create your Discord bot

1. Go to [discord.com/developers/applications](https://discord.com/developers/applications)
2. **New Application** → name it → **Create**
3. Click **Bot** → **Reset Token** → copy and save the token
4. Enable all three Privileged Gateway Intents:
   - **Server Members Intent**
   - **Presence Intent**
   - **Message Content Intent**
5. **OAuth2 → URL Generator** → check `bot` under Scopes
6. Bot Permissions: `Read Messages/View Channels`, `Send Messages`, `Embed Links`, `Read Message History`, `Connect`, `Speak`
7. Copy the generated URL → invite the bot to your server

### 2. Get your Server ID

**Settings → Advanced → Developer Mode**, then right-click your server icon → **Copy Server ID**.

### 3. Install

```bash
git clone https://github.com/DarylNo/POP-G-DiscordBot.git
cd POP-G-DiscordBot

python3 -m venv venv
source venv/bin/activate   # Windows: venv\Scripts\activate

pip install -r requirements.txt

cp .env.example .env
```

### 4. Configure `.env`

```
BOT_TOKEN=your_bot_token_here
PREFIX=!
GUILD_ID=your_server_id_here
```

### 5. Ollama (required for AI features)

The bot expects a single Ollama instance on the network. Edit the URL at the top of `cogs/llm.py` to match your setup:

```python
OLLAMA_URL   = "http://<your-ip>:11434"
OLLAMA_MODEL = "qwen2.5:14b"
```

Pull the model on your Ollama machine:
```bash
ollama pull qwen2.5:14b
```

The 14B model needs ~10 GB of VRAM. With two smaller GPUs, expose both to one
Ollama instance (no `CUDA_VISIBLE_DEVICES` restriction) and llama.cpp splits
the model layers across the cards automatically.

### 6. Whisper (required for voice transcription)

```bash
pip install openai-whisper
```

Set `WHISPER_DEVICE=cuda` in `.env` to use GPU (recommended). Default model is `small` — change with `WHISPER_MODEL=medium` if you have VRAM headroom.

### 7. Run

```bash
python3 bot.py
```

---

## Running with Docker

```bash
# Build and start
docker compose up -d --build

# View logs
docker logs -f popg-bot

# Apply updates
git pull && docker compose up -d --build
```

---

## Architecture

| File | Purpose |
|---|---|
| `bot.py` | Entry point, loads cogs |
| `config.py` | Reads `.env` |
| `database.py` | All SQLite access (never bypassed) |
| `cogs/tracking.py` | Presence and voice state events |
| `cogs/profile.py` | `!profile` / `!stats` |
| `cogs/leaderboard.py` | `!leaderboard`, `!weekly`, `!monthly` |
| `cogs/admin.py` | Admin subcommands |
| `cogs/llm.py` | AI chat, memory system, `!when`, `!recap` |
| `cogs/voice_listener.py` | Voice recording, Whisper transcription |

Database: SQLite with WAL mode. Three core tables (`users`, `sessions`, `game_stats`) plus LLM tables (`memories`, `chat_messages`, `voice_sessions`, `transcript_segments`).
