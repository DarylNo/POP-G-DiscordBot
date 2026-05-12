# Architecture — POPG Bot

Internal design reference. Documents the data flow, key invariants, and non-obvious decisions.

---

## Data Flow

```
Discord Gateway
      │
      ├── on_presence_update(before, after)
      │       └── cogs/tracking.py
      │               ├── _is_online(status)    → open/close 'online' session
      │               └── _get_game(member)     → open/close 'gaming' session
      │
      ├── on_voice_state_update(member, before, after)
      │       └── cogs/tracking.py
      │               └── channel diff          → open/close/move 'voice' session
      │
      └── on_ready()
              └── cogs/tracking.py
                      ├── close all stale sessions (ended_at IS NULL from last run)
                      └── re-open sessions based on live member states

All session mutations → database.py → popg.db (SQLite, WAL mode)
```

---

## Session State Machine

```
          open_session()
CLOSED ─────────────────► OPEN (ended_at IS NULL)
                              │
                    close_session()
                              │
                              ▼
                          CLOSED (ended_at SET, aggregate totals updated)
```

`open_session()` is idempotent — it queries for an existing open session first and returns early if one exists. This prevents double-counting if an event fires twice.

`close_session()` returns elapsed seconds (for logging) or `None` if no open session was found. Callers may safely ignore the return value.

---

## Aggregate Totals vs. Sessions

The `users` table holds **cumulative totals** (e.g. `total_gaming_seconds`). These are updated every time a session closes, not in real time.

`get_user_stats()` adds **live in-progress seconds** on top of the stored totals so `!profile` always shows current numbers without needing to close a session.

The leaderboard reads directly from `users` totals only (no live adjustment), which means a member actively gaming won't have their current session counted until it closes. This is an acceptable trade-off — fixing it would require a heavier query for every leaderboard request.

---

## Threading

SQLite is opened per-thread via `threading.local()` in `database.py`. discord.py's event loop runs in a single thread, so in practice there is only one connection. The `threading.local()` approach is defensive — it ensures correctness if any code is ever moved to a thread pool executor.

---

## on_ready Recovery

When the bot restarts, existing DB sessions have no `ended_at` (they were open when the process died). `on_ready`:

1. Calls `close_session()` on every stale open session — this correctly credits elapsed time up to now.
2. Re-opens sessions by inspecting current live member states.

This means at worst a member gets a small double-count for the period between the last session open and bot death (already credited) vs. the restart recovery open. This is an acceptable data quality trade-off.

---

## Game Detection

Discord exposes game activity via two types:
- `discord.Game` — simple game presence
- `discord.Activity(type=ActivityType.playing)` — rich presence

Both are checked in `_get_game(member)` in `cogs/tracking.py`. The function iterates `member.activities` (a tuple that may contain multiple activities, e.g. Spotify + a game) and returns the first game name found.

Custom statuses (`discord.CustomActivity`) are intentionally ignored.

---

## Admin Permission Check

`_is_admin(ctx)` in `cogs/admin.py` checks two things:
1. `ctx.author.guild_permissions.administrator` — Discord's built-in Administrator flag
2. Any role on the member named `"admin"` (case-insensitive)

This means you can grant bot admin access without giving Discord server admin to a member.

---

## Known Limitations

| Limitation | Impact | Notes |
|---|---|---|
| Leaderboard doesn't include live session time | Minor ranking inaccuracy for active members | Acceptable trade-off vs. query cost |
| Voice listener not yet implemented | No audio transcription | Phase 2 |
| Single-guild only | Bot ignores events from other guilds | By design — `config.GUILD_ID` filter in every event handler |
| SQLite WAL mode | Works great for one process, not for multiple bot instances | Use PostgreSQL if ever scaling to multiple shards |
