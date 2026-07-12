# POPG Discord Bot — Toaster

The Discord bot for **Past our Prime Gamers**. A local-LLM "barkeep" that hangs out in the server — passively reading chat, auto-transcribing voice, and answering when addressed — while silently tracking member activity in the background.

Current version: **1.14.2**

---

## What it does

### Activity tracking
Presence, gaming, and voice activity are recorded automatically with no configuration needed.

| Activity | How |
|---|---|
| Online / idle / DND | Presence events |
| Gaming (game name + duration) | Activity events |
| Voice channel presence | Voice state events |

All data is stored locally in a SQLite database (`popg.db`).

### AI assistant (Toaster — the barkeep)
Toaster runs on **Qwen 2.5 14B** on a dedicated dual-GPU Ollama machine and behaves like a barkeep: it passively reads every text channel for context and quietly remembers the useful bits (schedules, plans, life events), but only speaks when you address it — **@mention it, reply to one of its messages, `!chat`, or DM**. It has persistent memory built from voice transcripts, chat, and game stats, and can search the web and read linked pages for current info. `!barkeep off` stops it reading a given channel.

Like a real barkeep, it occasionally speaks up on its own — greeting a voice channel when it joins and dropping the odd one-liner into chat (**ambient chime-in**, on by default; `!barkeep chime off` to stop). It can also mark milestones like long streaks and playtime landmarks, but **milestone posts are off by default** (`!barkeep milestones on` to enable). All auto-posts are heavily rate-limited so they stay a welcome surprise, not noise; `!barkeep quiet` mutes everything server-wide.

### Voice transcription
Toaster **auto-joins a voice channel** when two or more people gather in it and starts recording (set `VOICE_AUTO_RECORD=0` to require a manual `!join`). Whisper transcribes audio in rolling 5-minute chunks, and while a session is live you can ask Toaster who's in the channel, what they're playing, and what's been said. It auto-leaves when the channel empties; memories and an AI recap are saved from the transcript.

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
| `!memories [page]` | Everyone | See what Toaster remembers (server-wide in channels, personal in DMs) |
| `!forget <number\|text>` | Admin (server) / Everyone (DM) | Remove a specific memory |
| `!memoryrestore` | Admin (server) / Everyone (DM) | Restore memories from the pre-consolidation backup |
| `!memorybuild [full]` | Admin | Backfill memories; `full` wipes and re-extracts from every transcript |
| `!reset` | Everyone (DM) / Admin (server) | Clear chat history; `!reset all` also wipes server memories |

Toaster also responds to DMs directly, and in server channels when @mentioned or when you reply to one of its messages — no `!chat` needed. It passively reads channel conversation like a barkeep, so when you address it, it already knows what everyone was just talking about. While a voice recording is running, you can ask it about the live session — who's in the channel, what they're playing, and what's been said so far (the transcript updates in 5-minute chunks). `!reset` clears a channel's conversation context.

### Voice recording
Recording starts automatically when people gather in voice. These override it manually:

| Command | Who | Description |
|---|---|---|
| `!join` | Admin | Force-start recording the voice channel you're in |
| `!leave` | Admin | Stop recording, post recap, save memories |

### Admin
| Command | Who | Description |
|---|---|---|
| `!admin sessions` | Admin | Live list of all active tracking sessions |
| `!admin reset @member` | Admin | Zero out a member's stats |
| `!admin info @member` | Admin | Raw stat dump for debugging |
| `!admin reload` | Admin | Reload all cogs without restarting |
| `!barkeep on\|off` | Admin | Toggle whether Toaster reads the current channel |
| `!barkeep quiet\|speak` | Admin | Mute / unmute unprompted auto-posts (server-wide) |
| `!barkeep chime on\|off` | Admin | Toggle ambient chime-in (on by default) |
| `!barkeep milestones on\|off` | Admin | Toggle milestone posts (off by default) |
| `!chatlog [#channel] [n]` | Admin | Show recent archived messages from a channel |

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
| `cogs/admin.py` | Admin subcommands, `!wipe` |
| `cogs/utility.py` | `!help`, `!ping`, `!chatlog` |
| `cogs/llm.py` | AI chat pipeline, barkeep absorption, memory system, web search, `!when`, `!recap` |
| `cogs/voice_listener.py` | Voice auto-join/leave, Whisper transcription |

Database: SQLite with WAL mode. Core tables (`users`, `sessions`, `game_stats`, partners, `activity_days`) plus AI tables (`memories`, `chat_messages`, `channel_chat_history`, `dm_history`, `voice_transcripts`, `transcript_segments`, `barkeep_optout`).
