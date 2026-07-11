# CLAUDE.md — POPG Discord Bot

This file is the AI workspace entry point. It gives Claude full context about this project so every session starts with a complete picture. Humans do not need to read or modify anything in the `.claude/` directory.

---

## Project Overview

**POPG** (Past our Prime Gamers) Discord bot. Python 3.11 / py-cord 2.x / SQLite / Ollama (qwen2.5:14b) / Whisper.

**Direction: barkeep bot.** Toaster hangs out in the server like a barkeep — it passively reads every text channel (ambient context + permanent archive + periodic memory extraction), auto-joins voice channels when friends gather and transcribes them, but **only speaks when addressed** (@mention, reply to its message, `!chat`, or DM). Stats commands (`!profile`, `!leaderboard`) remain from the original tracker heritage. As of v1.14 it also speaks unprompted in three rate-limited ways (voice greeting, milestone reactions, ambient chime-in) — all governed and mutable via `!barkeep quiet`.

**Branch:** `claude/discord-popg-chatbot-REQLv`  
**Database file:** `popg.db` (auto-created, git-ignored)  
**Entry point:** `bot.py`

---

## File Map

```
bot.py              Entry point. Loads cogs, startup diagnostics, error handling.
config.py           Reads .env → BOT_TOKEN, PREFIX, GUILD_ID. VERSION constant.
database.py         All SQLite operations. The only file that touches the DB.
cogs/
  tracking.py       Presence/game/voice session tracking; reconnect-safe on_ready recovery
  profile.py        !profile / !stats + shared helpers (_fmt_duration, _resolve_target)
  leaderboard.py    !leaderboard / !weekly / !monthly (period + per-game)
  admin.py          !admin subcommands, !wipe, _is_admin helper
  utility.py        !help, !ping, !chatlog
  voice_listener.py Voice recording: auto-join/leave, TimestampedSink, Whisper, chunk rotation
  llm.py            All AI: chat pipeline, barkeep absorption, memory system, web search,
                    !recap/!transcript/!when/!memories/!forget/!barkeep
.claude/
  CLAUDE.md         ← you are here
  architecture.md   Schema, data-flow diagrams, design decisions (may lag reality)
  roadmap.md        Original Phase 2 plan (largely implemented)
```

---

## The Barkeep Model (core behavior)

- **Text:** `llm.py on_message` absorbs every non-command guild message into that channel's rolling chat session (`ambient: True` flag, 500 chars each, trimmed ambient-first at 40 turns), archives it to `chat_messages`, and every ~50 absorbed lines runs ambient memory extraction. Replies ONLY when @mentioned / replied to / `!chat` / DM. `!barkeep off` disables absorption per channel (mentions still work).
- **Voice:** `voice_listener.py` auto-joins a channel when ≥2 humans are in it (`VOICE_AUTO_RECORD=0` disables), transcribes in rolling 5-min chunks, records join/leave/game-change `[Session]` markers, auto-leaves when the channel empties. Live session context (who's there, what's playing, transcript so far) is injected into every chat while recording.
- **Auto-posts (unprompted, v1.14):** three behaviors — voice greeting (on auto-join, dispatched `popg_voice_joined`), milestone reactions (streaks/playtime; detected in tracking via `database.check_and_record_milestones`, dispatched `popg_milestone`, dedup'd in `announced_milestones`), and ambient chime-in (`_maybe_chime_in`, NOOP-biased LLM call every `CHIME_CONSIDER_EVERY` absorbed msgs; **off by default**, gated on `chime_enabled` flag / `!barkeep chime on`). ALL pass through one governor in `llm.py` (`_can_auto_post`/`_auto_post`): `AUTO_POST_MIN_GAP` between any two posts + per-behavior cooldowns + persisted quiet flag (`!barkeep quiet`/`speak`, `kv_store` key `auto_post_quiet`). Posts go to `ANNOUNCE_CHANNEL_ID` (else guild system channel); chime-ins post in-place. Keep it rare — the failure mode is an annoying bot.

---

## Key Conventions

- **All DB access goes through `database.py`.** Cogs never use sqlite3 directly. No `discord.Member` objects in database.py — primitives only. `get_conn()` = thread-local WAL connection, FKs on.
- **One chat pipeline:** `LLM._run_chat` serves `!chat`, DMs, and mentions. Context order: system+memory block → history (stripped via `_strip_message` to drop internal keys) → live voice block → relevant memories → user message.
- **Memory system:** facts extracted per engaged exchange, per voice chunk, and per ambient batch → `_merge_memories` (write-locked, exact-dedup, LLM consolidation at 150 entries with backup scope + sanity check). `!memories` / `!forget` / `!memoryrestore` / `!memorybuild [full]` manage it.
- **Web search:** `ddgs` with engine rotation (ddg→bing→brave→google), 10-min cache, circuit breaker, top-result deep-fetch, honest failure injection. Force-pattern regex for obvious lookups; LLM intent check (NOOP-biased for banter) otherwise.
- **Session lifecycle (tracking):** `close_session(cap_seconds=...)` clamps stored `ended_at`; stale closes still credit streaks/partners. on_ready reconciles instead of close-all on reconnects (`_recovered_once`).
- **Voice timestamps:** `TimestampedSink` records gap anchors per user; `wall_offset()` maps speech-only audio positions to wall clock. Recording restarts BEFORE transcription on rotation (no audio loss).
- **Background tasks** go through `_spawn` (llm.py module-level / VoiceListener method) so they aren't GC'd.
- **Ollama:** `_ollama_generate` for chat (16k ctx), `_ollama_analyse` for analysis (unlimited ctx, serialized behind `_analysis_lock`). URL/model hardcoded at top of llm.py.
- **Embed helpers** in `cogs/profile.py`; admin check `_is_admin(ctx)` in `cogs/admin.py`. `discord.Game` and `Activity(type=playing)` both count as gaming — `_get_game`/`_get_game_label`.

---

## Intents Required

```python
intents.members = True        # Member join/leave, profile data
intents.presences = True      # Online status + game activity
intents.message_content = True # Reading channel messages (barkeep) + prefix commands
```

These must also be enabled in the Discord Developer Portal under the bot application.

---

## Database Schema (quick reference)

```sql
users        (user_id PK, username, display_name, first_seen, last_seen,
              total_online/gaming/voice/desktop/mobile_seconds)
sessions     (id PK, user_id FK, session_type CHECK('online','gaming','voice'),
              game_name, voice_channel_id, platform, started_at, ended_at)  -- NULL = live
game_stats   (id PK, user_id FK, game_name, total_seconds, session_count, last_played,
              UNIQUE(user_id, game_name))
game_partners / voice_partners   (shared_seconds overlap, credited at later close)
activity_days (user_id, date UNIQUE)          -- streaks; recorded at open AND close
chat_messages (message_id UNIQUE, channel_id, user_id, username, content, sent_at)  -- barkeep archive
channel_chat_history / dm_history (JSON message lists, write-through cached in llm.py)
memories     (scope_type 'guild'|'dm'|'*_backup', scope_id, content JSON list)
voice_transcripts (id PK, channel_id/name, started/ended_at, status, summary, memory_extracted)
transcript_segments (session_id FK, user_id, display_name, timestamp REAL, text)
barkeep_optout (channel_id PK)
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

## Testing

Functional suite lives in the Claude session scratchpad (`test_functional.py`) — exercises database + llm + voice logic on a scratch DB with mocked Ollama/DDGS (60+ checks). Recreate/extend it when changing logic. Always `python3 -m py_compile` everything and import-check all cogs before pushing. When modifying voice: verify chunk rotation restarts recording BEFORE transcription, `!leave` during rotation, auto-join/auto-leave, external disconnect (watchdog in `_chunk_rotator`).

---

## Environment Variables

| Variable | Default | Description |
|---|---|---|
| `BOT_TOKEN` | (required) | Discord bot token |
| `GUILD_ID` | (required) | Server snowflake |
| `PREFIX` | `!` | Command prefix |
| `WHISPER_MODEL` / `WHISPER_DEVICE` | `small` / `cpu` | Whisper config |
| `VOICE_CHUNK_SECS` | `300` | Transcription chunk length |
| `VOICE_AUTO_RECORD` | `1` | Barkeep auto-join voice |
| `VOICE_AUTO_MIN_MEMBERS` | `2` | Humans needed to auto-join |
| `AMBIENT_MEMORY_EVERY` | `50` | Absorbed lines per ambient memory extraction |
| `CHAT_MAX_HISTORY_TURNS` | `40` | Channel context depth |
| `ANNOUNCE_CHANNEL_ID` | `0` | Channel for greetings/milestones (0 = guild system channel) |
| `AUTO_POST_MIN_GAP` | `600` | Seconds between ANY two auto-posts |
| `CHIME_COOLDOWN` | `2700` | Seconds between ambient chime-ins |
| `CHIME_CONSIDER_EVERY` | `20` | Absorbed msgs between chime considerations |
| `GREETING_COOLDOWN` | `1800` | Seconds between voice greetings |

---

## Run

```bash
source venv/bin/activate
python3 bot.py
```
