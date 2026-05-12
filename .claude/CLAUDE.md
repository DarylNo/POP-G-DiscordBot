# CLAUDE.md — POPG Discord Bot

This file is the AI workspace entry point. It gives Claude full context about this project so every session starts with a complete picture. Humans do not need to read or modify anything in the `.claude/` directory.

---

## Project Overview

**POPG** (Past our Prime Gamers) Discord bot. Python 3.10+ / discord.py 2.x / SQLite.

Tracks member presence (online, gaming, voice) silently and exposes stats via `!` prefix commands. No messages are posted to channels automatically.

**Branch:** `claude/discord-popg-chatbot-REQLv`  
**Database file:** `popg.db` (auto-created, git-ignored)  
**Entry point:** `bot.py`

---

## File Map

```
bot.py              Entry point. Loads cogs, starts bot.
config.py           Reads .env → BOT_TOKEN, PREFIX, GUILD_ID
database.py         All SQLite operations. The only file that touches the DB.
cogs/
  tracking.py       on_presence_update, on_voice_state_update, on_ready recovery
  profile.py        !profile / !stats commands
  leaderboard.py    !leaderboard command
  admin.py          !admin subcommands (reset, info, sessions)
.claude/
  CLAUDE.md         ← you are here
  architecture.md   Schema, data-flow diagrams, design decisions
  roadmap.md        Phase 2 LLM integration plan
```

---

## Key Conventions

- **All DB access goes through `database.py`.** Cogs import from it; they never use sqlite3 directly.
- **`database.get_conn()`** returns a thread-local connection with WAL mode and foreign keys on.
- **Session lifecycle:** `open_session()` is idempotent (checks for existing open session). `close_session()` sets `ended_at`, computes elapsed seconds, updates `users` aggregate totals, and updates `game_stats` for gaming sessions.
- **No direct `discord.Member` objects in `database.py`** — pass primitives (user_id, username, display_name).
- **Embed formatting helpers** live in `cogs/profile.py` (`_fmt_duration`, `_fmt_dt`) and are imported by `cogs/leaderboard.py` and `cogs/admin.py`.
- **Admin check** is a plain function `_is_admin(ctx)` in `cogs/admin.py` — checks `Administrator` permission or role named `Admin`.
- `discord.Game` and `discord.Activity(type=playing)` both count as gaming. The helper `_get_game(member)` in `cogs/tracking.py` handles both cases.

---

## Intents Required

```python
intents.members = True        # Member join/leave, profile data
intents.presences = True      # Online status + game activity
intents.message_content = True # Prefix commands
```

These must also be enabled in the Discord Developer Portal under the bot application.

---

## Database Schema (quick reference)

```sql
users        (user_id PK, username, display_name, first_seen, last_seen,
              total_online_seconds, total_gaming_seconds, total_voice_seconds)

sessions     (id PK, user_id FK, session_type CHECK('online','gaming','voice'),
              game_name, voice_channel_id, started_at, ended_at)
              -- ended_at IS NULL means currently active

game_stats   (id PK, user_id FK, game_name, total_seconds, session_count,
              last_played, UNIQUE(user_id, game_name))
```

---

## Adding a New Cog

1. Create `cogs/yourcog.py` with a `class YourCog(commands.Cog)` and `async def setup(bot)`.
2. Add `"cogs.yourcog"` to the `COGS` list in `bot.py`.
3. DB queries go in `database.py`; only UI/Discord logic goes in the cog.

---

## Testing Checklist

When modifying tracking logic:
- [ ] Member goes online → `sessions` row opens with `session_type='online'`
- [ ] Member goes offline → row closes, `users.total_online_seconds` increments
- [ ] Member starts a game → gaming session opens with correct `game_name`
- [ ] Member stops game → gaming session closes, `game_stats` upserted
- [ ] Member joins voice → voice session opens with `voice_channel_id`
- [ ] Member moves voice channels → old session closes, new one opens
- [ ] Bot restart → `on_ready` closes stale sessions and re-opens correct ones

When modifying commands:
- [ ] `!profile` shows live session time (adds in-progress seconds)
- [ ] `!leaderboard gaming` ranks by `total_gaming_seconds` descending
- [ ] `!admin sessions` shows all rows where `ended_at IS NULL`

---

## Phase 2 Hook Points (do not break these)

- `database.get_user_stats(user_id)` returns a plain dict — ready to serialize into an LLM prompt
- `database.get_leaderboard(category)` returns a list of plain dicts
- `database.get_all_users()` returns all user rows — usable for bulk LLM context
- A future `cogs/llm.py` will call Ollama's HTTP API (`http://localhost:11434`)
- A future `cogs/voice_listener.py` will use discord.py's `WaveSink` → Whisper

See `.claude/roadmap.md` for the full Phase 2 plan.

---

## Environment Variables

| Variable | Required | Description |
|---|---|---|
| `BOT_TOKEN` | Yes | Discord bot token |
| `PREFIX` | No (default `!`) | Command prefix |
| `GUILD_ID` | Yes | Discord server snowflake ID |

---

## Run

```bash
source venv/bin/activate
python3 bot.py
```
