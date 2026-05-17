import os
import sqlite3
import threading
from datetime import datetime, timezone
from typing import Optional

DB_PATH = os.path.join(os.getenv("DATA_DIR", "."), "popg.db")

_local = threading.local()


def get_conn() -> sqlite3.Connection:
    if not hasattr(_local, "conn"):
        _local.conn = sqlite3.connect(DB_PATH, check_same_thread=False)
        _local.conn.row_factory = sqlite3.Row
        _local.conn.execute("PRAGMA journal_mode=WAL")
        _local.conn.execute("PRAGMA foreign_keys=ON")
    return _local.conn


def init_db() -> None:
    conn = get_conn()
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS users (
            user_id                INTEGER PRIMARY KEY,
            username               TEXT    NOT NULL,
            display_name           TEXT    NOT NULL,
            first_seen             TEXT    NOT NULL,
            last_seen              TEXT    NOT NULL,
            total_online_seconds   INTEGER NOT NULL DEFAULT 0,
            total_gaming_seconds   INTEGER NOT NULL DEFAULT 0,
            total_voice_seconds    INTEGER NOT NULL DEFAULT 0,
            total_desktop_seconds  INTEGER NOT NULL DEFAULT 0,
            total_mobile_seconds   INTEGER NOT NULL DEFAULT 0
        );

        CREATE TABLE IF NOT EXISTS sessions (
            id               INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id          INTEGER NOT NULL REFERENCES users(user_id),
            session_type     TEXT    NOT NULL CHECK(session_type IN ('online','gaming','voice')),
            game_name        TEXT,
            voice_channel_id INTEGER,
            platform         TEXT,
            started_at       TEXT    NOT NULL,
            ended_at         TEXT
        );

        CREATE TABLE IF NOT EXISTS game_stats (
            id             INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id        INTEGER NOT NULL REFERENCES users(user_id),
            game_name      TEXT    NOT NULL,
            total_seconds  INTEGER NOT NULL DEFAULT 0,
            session_count  INTEGER NOT NULL DEFAULT 0,
            last_played    TEXT    NOT NULL,
            UNIQUE(user_id, game_name)
        );

        CREATE TABLE IF NOT EXISTS watched_channels (
            channel_id INTEGER PRIMARY KEY,
            channel_name TEXT NOT NULL,
            added_at   TEXT NOT NULL,
            added_by   INTEGER NOT NULL
        );

        CREATE TABLE IF NOT EXISTS chat_messages (
            id         INTEGER PRIMARY KEY AUTOINCREMENT,
            message_id INTEGER NOT NULL UNIQUE,
            channel_id INTEGER NOT NULL REFERENCES watched_channels(channel_id),
            user_id    INTEGER NOT NULL,
            username   TEXT    NOT NULL,
            content    TEXT    NOT NULL,
            sent_at    TEXT    NOT NULL
        );
    """)
    # Migrations for existing databases
    for migration in [
        "ALTER TABLE users ADD COLUMN total_desktop_seconds INTEGER NOT NULL DEFAULT 0",
        "ALTER TABLE users ADD COLUMN total_mobile_seconds INTEGER NOT NULL DEFAULT 0",
        "ALTER TABLE sessions ADD COLUMN platform TEXT",
    ]:
        try:
            conn.execute(migration)
        except Exception:
            pass  # Column already exists
    conn.commit()


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def upsert_user(user_id: int, username: str, display_name: str) -> None:
    conn = get_conn()
    now = _now()
    conn.execute("""
        INSERT INTO users (user_id, username, display_name, first_seen, last_seen)
        VALUES (?, ?, ?, ?, ?)
        ON CONFLICT(user_id) DO UPDATE SET
            username     = excluded.username,
            display_name = excluded.display_name,
            last_seen    = excluded.last_seen
    """, (user_id, username, display_name, now, now))
    conn.commit()


def open_session(
    user_id: int,
    session_type: str,
    game_name: Optional[str] = None,
    voice_channel_id: Optional[int] = None,
    platform: Optional[str] = None,
) -> None:
    conn = get_conn()
    # Avoid duplicate open sessions of the same type
    existing = conn.execute(
        "SELECT id FROM sessions WHERE user_id=? AND session_type=? AND ended_at IS NULL",
        (user_id, session_type),
    ).fetchone()
    if existing:
        return
    conn.execute(
        """INSERT INTO sessions (user_id, session_type, game_name, voice_channel_id, platform, started_at)
           VALUES (?, ?, ?, ?, ?, ?)""",
        (user_id, session_type, game_name, voice_channel_id, platform, _now()),
    )
    conn.commit()


def close_session(user_id: int, session_type: str, cap_seconds: Optional[int] = None) -> Optional[int]:
    """Close the active session and return elapsed seconds, or None if none was open.

    cap_seconds: if set, credits at most this many seconds regardless of actual elapsed time.
    """
    conn = get_conn()
    row = conn.execute(
        "SELECT id, game_name, platform, started_at FROM sessions WHERE user_id=? AND session_type=? AND ended_at IS NULL",
        (user_id, session_type),
    ).fetchone()
    if not row:
        return None

    now = _now()
    started = datetime.fromisoformat(row["started_at"])
    ended = datetime.fromisoformat(now)
    elapsed = max(0, int((ended - started).total_seconds()))
    if cap_seconds is not None:
        elapsed = min(elapsed, cap_seconds)

    conn.execute(
        "UPDATE sessions SET ended_at=? WHERE id=?",
        (now, row["id"]),
    )

    col_map = {
        "online": "total_online_seconds",
        "gaming": "total_gaming_seconds",
        "voice":  "total_voice_seconds",
    }
    col = col_map[session_type]
    conn.execute(
        f"UPDATE users SET {col}={col}+?, last_seen=? WHERE user_id=?",
        (elapsed, now, user_id),
    )

    # Credit platform-specific column for online sessions
    if session_type == "online":
        platform_col = "total_mobile_seconds" if row["platform"] == "mobile" else "total_desktop_seconds"
        conn.execute(
            f"UPDATE users SET {platform_col}={platform_col}+? WHERE user_id=?",
            (elapsed, user_id),
        )

    if session_type == "gaming" and row["game_name"]:
        conn.execute("""
            INSERT INTO game_stats (user_id, game_name, total_seconds, session_count, last_played)
            VALUES (?, ?, ?, 1, ?)
            ON CONFLICT(user_id, game_name) DO UPDATE SET
                total_seconds = total_seconds + excluded.total_seconds,
                session_count = session_count + 1,
                last_played   = excluded.last_played
        """, (user_id, row["game_name"], elapsed, now))

    conn.commit()
    return elapsed


def get_user_stats(user_id: int) -> Optional[dict]:
    conn = get_conn()
    row = conn.execute("SELECT * FROM users WHERE user_id=?", (user_id,)).fetchone()
    if not row:
        return None
    stats = dict(row)
    top_games = conn.execute(
        "SELECT game_name, total_seconds, session_count FROM game_stats WHERE user_id=? ORDER BY total_seconds DESC LIMIT 3",
        (user_id,),
    ).fetchall()
    stats["top_games"] = [dict(g) for g in top_games]

    # Include seconds from any currently open sessions
    for session_type, col in [("online", "total_online_seconds"), ("gaming", "total_gaming_seconds"), ("voice", "total_voice_seconds")]:
        active = conn.execute(
            "SELECT started_at, platform FROM sessions WHERE user_id=? AND session_type=? AND ended_at IS NULL",
            (user_id, session_type),
        ).fetchone()
        if active:
            started = datetime.fromisoformat(active["started_at"])
            live_seconds = max(0, int((datetime.now(timezone.utc) - started).total_seconds()))
            stats[col] += live_seconds
            if session_type == "online":
                platform_col = "total_mobile_seconds" if active["platform"] == "mobile" else "total_desktop_seconds"
                stats[platform_col] = stats.get(platform_col, 0) + live_seconds

    return stats


def get_leaderboard(category: str, limit: int = 10) -> list[dict]:
    col_map = {
        "online":   "total_online_seconds",
        "gaming":   "total_gaming_seconds",
        "voice":    "total_voice_seconds",
        "desktop":  "total_desktop_seconds",
        "mobile":   "total_mobile_seconds",
    }
    # For live session lookup, desktop/mobile both come from online sessions
    session_type_map = {
        "online":  "online",
        "gaming":  "gaming",
        "voice":   "voice",
        "desktop": "online",
        "mobile":  "online",
    }
    col = col_map.get(category, "total_online_seconds")
    session_type = session_type_map.get(category, "online")
    conn = get_conn()

    rows = conn.execute(
        f"SELECT user_id, display_name, {col} as score FROM users",
    ).fetchall()

    now = datetime.now(timezone.utc)
    results = []
    for row in rows:
        score = row["score"]
        if category in ("desktop", "mobile"):
            # Only count live session if platform matches
            active = conn.execute(
                "SELECT started_at FROM sessions WHERE user_id=? AND session_type='online' AND platform=? AND ended_at IS NULL",
                (row["user_id"], category),
            ).fetchone()
        else:
            active = conn.execute(
                "SELECT started_at FROM sessions WHERE user_id=? AND session_type=? AND ended_at IS NULL",
                (row["user_id"], session_type),
            ).fetchone()
        if active:
            started = datetime.fromisoformat(active["started_at"])
            score += max(0, int((now - started).total_seconds()))
        if score > 0:
            results.append({"user_id": row["user_id"], "display_name": row["display_name"], "score": score})

    results.sort(key=lambda x: x["score"], reverse=True)
    return results[:limit]


def get_top_games(limit: int = 5) -> list[dict]:
    """Return top games by total time across all members, including live sessions."""
    conn = get_conn()
    rows = conn.execute(
        """SELECT game_name, SUM(total_seconds) as total_seconds, SUM(session_count) as session_count
           FROM game_stats GROUP BY game_name ORDER BY total_seconds DESC""",
    ).fetchall()

    # Add time from any currently active gaming sessions
    live = conn.execute(
        "SELECT game_name, started_at FROM sessions WHERE session_type='gaming' AND ended_at IS NULL AND game_name IS NOT NULL",
    ).fetchall()

    totals: dict[str, int] = {r["game_name"]: r["total_seconds"] for r in rows}
    counts: dict[str, int] = {r["game_name"]: r["session_count"] for r in rows}
    now = datetime.now(timezone.utc)
    for s in live:
        started = datetime.fromisoformat(s["started_at"])
        elapsed = max(0, int((now - started).total_seconds()))
        name = s["game_name"]
        totals[name] = totals.get(name, 0) + elapsed
        counts[name] = counts.get(name, 0)

    results = [
        {"game_name": name, "total_seconds": secs, "session_count": counts.get(name, 0)}
        for name, secs in totals.items()
        if secs > 0
    ]
    results.sort(key=lambda x: x["total_seconds"], reverse=True)
    return results[:limit]


def get_active_sessions() -> list[dict]:
    conn = get_conn()
    rows = conn.execute(
        "SELECT * FROM sessions WHERE ended_at IS NULL",
    ).fetchall()
    return [dict(r) for r in rows]


def reset_user(user_id: int) -> bool:
    conn = get_conn()
    affected = conn.execute(
        "UPDATE users SET total_online_seconds=0, total_gaming_seconds=0, total_voice_seconds=0 WHERE user_id=?",
        (user_id,),
    ).rowcount
    conn.execute("DELETE FROM sessions WHERE user_id=?", (user_id,))
    conn.execute("DELETE FROM game_stats WHERE user_id=?", (user_id,))
    conn.commit()
    return affected > 0


def get_all_users() -> list[dict]:
    conn = get_conn()
    rows = conn.execute("SELECT * FROM users").fetchall()
    return [dict(r) for r in rows]


# --- Chat logging ---

def add_watched_channel(channel_id: int, channel_name: str, added_by: int) -> None:
    conn = get_conn()
    conn.execute(
        "INSERT OR IGNORE INTO watched_channels (channel_id, channel_name, added_at, added_by) VALUES (?, ?, ?, ?)",
        (channel_id, channel_name, _now(), added_by),
    )
    conn.commit()


def remove_watched_channel(channel_id: int) -> bool:
    conn = get_conn()
    affected = conn.execute(
        "DELETE FROM watched_channels WHERE channel_id=?", (channel_id,)
    ).rowcount
    conn.commit()
    return affected > 0


def get_watched_channels() -> list[int]:
    conn = get_conn()
    rows = conn.execute("SELECT channel_id FROM watched_channels").fetchall()
    return [r["channel_id"] for r in rows]


def get_watched_channels_detail() -> list[dict]:
    conn = get_conn()
    rows = conn.execute("SELECT * FROM watched_channels ORDER BY added_at").fetchall()
    return [dict(r) for r in rows]


def log_message(message_id: int, channel_id: int, user_id: int, username: str, content: str, sent_at: str) -> None:
    conn = get_conn()
    conn.execute(
        """INSERT OR IGNORE INTO chat_messages (message_id, channel_id, user_id, username, content, sent_at)
           VALUES (?, ?, ?, ?, ?, ?)""",
        (message_id, channel_id, user_id, username, content, sent_at),
    )
    conn.commit()


def get_recent_messages(channel_id: int, limit: int = 100) -> list[dict]:
    conn = get_conn()
    rows = conn.execute(
        "SELECT * FROM chat_messages WHERE channel_id=? ORDER BY sent_at DESC LIMIT ?",
        (channel_id, limit),
    ).fetchall()
    return [dict(r) for r in reversed(rows)]


def get_messages_for_llm(channel_id: int, limit: int = 200) -> list[dict]:
    """Return messages as plain dicts ready to serialize into an LLM prompt."""
    return get_recent_messages(channel_id, limit)
