import logging
import os
import sqlite3
import threading
from datetime import datetime, timedelta, timezone
from typing import Optional

DB_PATH = os.path.join(os.getenv("DATA_DIR", "."), "popg.db")

log = logging.getLogger("popg.database")

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

        CREATE TABLE IF NOT EXISTS game_partners (
            user_id        INTEGER NOT NULL REFERENCES users(user_id),
            partner_id     INTEGER NOT NULL REFERENCES users(user_id),
            game_name      TEXT    NOT NULL,
            shared_seconds INTEGER NOT NULL DEFAULT 0,
            session_count  INTEGER NOT NULL DEFAULT 0,
            last_played    TEXT    NOT NULL,
            UNIQUE(user_id, partner_id, game_name)
        );

        CREATE TABLE IF NOT EXISTS voice_partners (
            user_id        INTEGER NOT NULL REFERENCES users(user_id),
            partner_id     INTEGER NOT NULL REFERENCES users(user_id),
            shared_seconds INTEGER NOT NULL DEFAULT 0,
            session_count  INTEGER NOT NULL DEFAULT 0,
            last_together  TEXT    NOT NULL,
            UNIQUE(user_id, partner_id)
        );

        CREATE TABLE IF NOT EXISTS activity_days (
            user_id INTEGER NOT NULL REFERENCES users(user_id),
            date    TEXT    NOT NULL,
            UNIQUE(user_id, date)
        );

        CREATE TABLE IF NOT EXISTS voice_transcripts (
            id           INTEGER PRIMARY KEY AUTOINCREMENT,
            channel_id   INTEGER NOT NULL,
            channel_name TEXT    NOT NULL,
            started_at   TEXT    NOT NULL,
            ended_at     TEXT,
            status       TEXT    NOT NULL DEFAULT 'recording',
            summary      TEXT
        );

        CREATE TABLE IF NOT EXISTS transcript_segments (
            id           INTEGER PRIMARY KEY AUTOINCREMENT,
            session_id   INTEGER NOT NULL REFERENCES voice_transcripts(id),
            user_id      INTEGER NOT NULL,
            display_name TEXT    NOT NULL,
            timestamp    REAL    NOT NULL,
            text         TEXT    NOT NULL
        );

        CREATE TABLE IF NOT EXISTS member_dossiers (
            user_id      INTEGER PRIMARY KEY REFERENCES users(user_id),
            content      TEXT    NOT NULL DEFAULT '',
            last_updated TEXT    NOT NULL
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
        except sqlite3.OperationalError:
            pass  # column already exists — expected on existing databases
        except Exception as e:
            log.warning("Migration failed: %s — %s", migration, e)
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


def close_session(user_id: int, session_type: str, cap_seconds: Optional[int] = None, stale: bool = False) -> Optional[int]:
    """Close the active session and return elapsed seconds, or None if none was open.

    cap_seconds: if set, credits at most this many seconds regardless of actual elapsed time.
    stale: if True, the session is being closed due to a bot restart — time is credited but
           session_count is not incremented (the session wasn't intentionally ended by the user).
    """
    conn = get_conn()
    row = conn.execute(
        "SELECT id, game_name, voice_channel_id, platform, started_at FROM sessions WHERE user_id=? AND session_type=? AND ended_at IS NULL",
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
        platform_col = "total_mobile_seconds" if (row["platform"] or "desktop") == "mobile" else "total_desktop_seconds"
        conn.execute(
            f"UPDATE users SET {platform_col}={platform_col}+? WHERE user_id=?",
            (elapsed, user_id),
        )

    if session_type == "gaming" and row["game_name"]:
        count_increment = 0 if stale else 1
        conn.execute("""
            INSERT INTO game_stats (user_id, game_name, total_seconds, session_count, last_played)
            VALUES (?, ?, ?, ?, ?)
            ON CONFLICT(user_id, game_name) DO UPDATE SET
                total_seconds = total_seconds + excluded.total_seconds,
                session_count = session_count + excluded.session_count,
                last_played   = excluded.last_played
        """, (user_id, row["game_name"], elapsed, count_increment, now))

    if not stale:
        _record_activity_day(conn, user_id, now[:10])
        if session_type == "gaming" and row["game_name"]:
            _update_game_partners(conn, user_id, row["game_name"], row["started_at"], now)
        if session_type == "voice" and row["voice_channel_id"]:
            _update_voice_partners(conn, user_id, row["voice_channel_id"], row["started_at"], now)

    conn.commit()
    return elapsed


def _record_activity_day(conn: sqlite3.Connection, user_id: int, date_str: str) -> None:
    conn.execute(
        "INSERT OR IGNORE INTO activity_days (user_id, date) VALUES (?, ?)",
        (user_id, date_str),
    )


def _update_game_partners(
    conn: sqlite3.Connection, user_id: int, game_name: str, started_at_str: str, ended_at_str: str
) -> None:
    # Only match partner sessions that have already ended. Each pair's overlap is then
    # credited exactly once — when the later-ending session closes — instead of once per
    # participant (which double-counted, using _now() as a phantom end for open sessions).
    overlapping = conn.execute(
        """SELECT user_id, started_at, ended_at FROM sessions
           WHERE session_type='gaming' AND game_name=? AND user_id!=?
             AND ended_at IS NOT NULL AND started_at < ? AND ended_at > ?""",
        (game_name, user_id, ended_at_str, started_at_str),
    ).fetchall()
    if not overlapping:
        return

    a_start = datetime.fromisoformat(started_at_str)
    a_end = datetime.fromisoformat(ended_at_str)

    for partner in overlapping:
        b_start = datetime.fromisoformat(partner["started_at"])
        b_end = datetime.fromisoformat(partner["ended_at"])
        overlap = max(0, int((min(a_end, b_end) - max(a_start, b_start)).total_seconds()))
        if overlap <= 0:
            continue
        partner_id = partner["user_id"]
        for uid, pid in [(user_id, partner_id), (partner_id, user_id)]:
            conn.execute("""
                INSERT INTO game_partners (user_id, partner_id, game_name, shared_seconds, session_count, last_played)
                VALUES (?, ?, ?, ?, 1, ?)
                ON CONFLICT(user_id, partner_id, game_name) DO UPDATE SET
                    shared_seconds = shared_seconds + excluded.shared_seconds,
                    session_count  = session_count + 1,
                    last_played    = excluded.last_played
            """, (uid, pid, game_name, overlap, ended_at_str))


def _update_voice_partners(
    conn: sqlite3.Connection, user_id: int, channel_id: int, started_at_str: str, ended_at_str: str
) -> None:
    # Only match partner sessions that have already ended (see _update_game_partners for why).
    overlapping = conn.execute(
        """SELECT user_id, started_at, ended_at FROM sessions
           WHERE session_type='voice' AND voice_channel_id=? AND user_id!=?
             AND ended_at IS NOT NULL AND started_at < ? AND ended_at > ?""",
        (channel_id, user_id, ended_at_str, started_at_str),
    ).fetchall()
    if not overlapping:
        return

    a_start = datetime.fromisoformat(started_at_str)
    a_end = datetime.fromisoformat(ended_at_str)

    for partner in overlapping:
        b_start = datetime.fromisoformat(partner["started_at"])
        b_end = datetime.fromisoformat(partner["ended_at"])
        overlap = max(0, int((min(a_end, b_end) - max(a_start, b_start)).total_seconds()))
        if overlap <= 0:
            continue
        partner_id = partner["user_id"]
        for uid, pid in [(user_id, partner_id), (partner_id, user_id)]:
            conn.execute("""
                INSERT INTO voice_partners (user_id, partner_id, shared_seconds, session_count, last_together)
                VALUES (?, ?, ?, 1, ?)
                ON CONFLICT(user_id, partner_id) DO UPDATE SET
                    shared_seconds = shared_seconds + excluded.shared_seconds,
                    session_count  = session_count + 1,
                    last_together  = excluded.last_together
            """, (uid, pid, overlap, ended_at_str))


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
                platform_col = "total_mobile_seconds" if (active["platform"] or "desktop") == "mobile" else "total_desktop_seconds"
                stats[platform_col] = stats.get(platform_col, 0) + live_seconds

    # Inject active gaming session into top_games if not yet committed to game_stats
    active_game = conn.execute(
        "SELECT game_name, started_at FROM sessions WHERE user_id=? AND session_type='gaming' AND ended_at IS NULL AND game_name IS NOT NULL",
        (user_id,),
    ).fetchone()
    if active_game:
        now = datetime.now(timezone.utc)
        live_secs = max(0, int((now - datetime.fromisoformat(active_game["started_at"])).total_seconds()))
        game_name = active_game["game_name"]
        for g in stats["top_games"]:
            if g["game_name"] == game_name:
                g["total_seconds"] += live_secs
                break
        else:
            stats["top_games"].insert(0, {"game_name": game_name, "total_seconds": live_secs, "session_count": 1})
        stats["top_games"].sort(key=lambda x: x["total_seconds"], reverse=True)
        stats["top_games"] = stats["top_games"][:3]

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
        counts[name] = counts.get(name, 0) + 1

    results = [
        {"game_name": name, "total_seconds": secs, "session_count": counts.get(name, 0)}
        for name, secs in totals.items()
        if secs > 0
    ]
    results.sort(key=lambda x: x["total_seconds"], reverse=True)
    return results[:limit]


def get_activity_heatmap(user_id: int) -> dict:
    """Return hour-of-day and day-of-week session counts for a user.

    Uses online session start times as the activity signal.
    Returns {"hours": [24 ints], "days": [7 ints], "total": int}
    where days[0]=Monday and days[6]=Sunday.
    """
    conn = get_conn()
    rows = conn.execute(
        "SELECT started_at FROM sessions WHERE user_id=? AND session_type='online' AND ended_at IS NOT NULL",
        (user_id,),
    ).fetchall()

    hours = [0] * 24
    days = [0] * 7
    for row in rows:
        try:
            dt = datetime.fromisoformat(row["started_at"])
            hours[dt.hour] += 1
            days[dt.weekday()] += 1  # Monday=0, Sunday=6
        except (ValueError, TypeError):
            pass

    return {"hours": hours, "days": days, "total": len(rows)}


def get_active_sessions() -> list[dict]:
    conn = get_conn()
    rows = conn.execute(
        "SELECT * FROM sessions WHERE ended_at IS NULL",
    ).fetchall()
    return [dict(r) for r in rows]


def reset_user(user_id: int) -> bool:
    conn = get_conn()
    affected = conn.execute(
        "UPDATE users SET total_online_seconds=0, total_gaming_seconds=0, total_voice_seconds=0, total_desktop_seconds=0, total_mobile_seconds=0 WHERE user_id=?",
        (user_id,),
    ).rowcount
    conn.execute("DELETE FROM sessions WHERE user_id=?", (user_id,))
    conn.execute("DELETE FROM game_stats WHERE user_id=?", (user_id,))
    conn.commit()
    return affected > 0


def wipe_all_data() -> dict[str, int]:
    """Delete every row from all data tables. Returns rows deleted per table.

    Irreversible. Child tables are cleared before their parents so foreign-key
    constraints are satisfied. Schema (tables/columns) is left intact.
    """
    conn = get_conn()
    # Order matters: delete referencing tables before the tables they reference.
    tables = [
        "transcript_segments",
        "voice_transcripts",
        "member_dossiers",
        "activity_days",
        "voice_partners",
        "game_partners",
        "chat_messages",
        "watched_channels",
        "game_stats",
        "sessions",
        "users",
    ]
    counts: dict[str, int] = {}
    for t in tables:
        counts[t] = conn.execute(f"DELETE FROM {t}").rowcount
    conn.commit()
    return counts


def get_all_users() -> list[dict]:
    conn = get_conn()
    rows = conn.execute("SELECT * FROM users").fetchall()
    return [dict(r) for r in rows]


def get_leaderboard_for_game(game_name: str, limit: int = 10) -> tuple[str | None, list[dict]]:
    """Fuzzy-match game_name against game_stats and active sessions, return (matched_name, ranked_members)."""
    conn = get_conn()
    now = datetime.now(timezone.utc)

    # Try game_stats first (closed sessions)
    match = conn.execute(
        "SELECT game_name FROM game_stats WHERE game_name LIKE ? GROUP BY game_name ORDER BY SUM(total_seconds) DESC LIMIT 1",
        (f"%{game_name}%",),
    ).fetchone()

    # Fall back to active sessions if nothing in game_stats yet
    if not match:
        match = conn.execute(
            "SELECT game_name FROM sessions WHERE session_type='gaming' AND ended_at IS NULL AND game_name LIKE ? LIMIT 1",
            (f"%{game_name}%",),
        ).fetchone()
    if not match:
        return None, []

    matched = match["game_name"]
    rows = conn.execute(
        """SELECT gs.user_id, u.display_name, gs.total_seconds, gs.session_count
           FROM game_stats gs JOIN users u ON gs.user_id = u.user_id
           WHERE gs.game_name = ?
           ORDER BY gs.total_seconds DESC
           LIMIT ?""",
        (matched, limit),
    ).fetchall()

    # Add time from any currently active session for this game
    live = conn.execute(
        "SELECT user_id, started_at FROM sessions WHERE session_type='gaming' AND game_name=? AND ended_at IS NULL",
        (matched,),
    ).fetchall()
    live_map: dict[int, int] = {}
    for s in live:
        started = datetime.fromisoformat(s["started_at"])
        live_map[s["user_id"]] = max(0, int((now - started).total_seconds()))

    results = []
    for row in rows:
        score = row["total_seconds"] + live_map.get(row["user_id"], 0)
        results.append({
            "user_id": row["user_id"],
            "display_name": row["display_name"],
            "total_seconds": score,
            "session_count": row["session_count"],
        })

    # Include members currently playing but with no historical game_stats entry
    existing_ids = {r["user_id"] for r in results}
    for uid, elapsed in live_map.items():
        if uid not in existing_ids:
            user = conn.execute("SELECT display_name FROM users WHERE user_id=?", (uid,)).fetchone()
            if user:
                results.append({
                    "user_id": uid,
                    "display_name": user["display_name"],
                    "total_seconds": elapsed,
                    "session_count": 1,  # currently in a session
                })

    results.sort(key=lambda x: x["total_seconds"], reverse=True)
    return matched, results[:limit]


def get_accomplices(user_id: int, limit: int = 5) -> list[dict]:
    conn = get_conn()
    rows = conn.execute(
        """SELECT gp.partner_id, u.display_name,
                  SUM(gp.shared_seconds) as total_seconds,
                  SUM(gp.session_count) as session_count,
                  MAX(gp.last_played) as last_played
           FROM game_partners gp JOIN users u ON gp.partner_id = u.user_id
           WHERE gp.user_id = ?
           GROUP BY gp.partner_id ORDER BY total_seconds DESC LIMIT ?""",
        (user_id, limit),
    ).fetchall()
    return [dict(r) for r in rows]


def get_voice_crew(user_id: int, limit: int = 5) -> list[dict]:
    conn = get_conn()
    rows = conn.execute(
        """SELECT vp.partner_id, u.display_name,
                  vp.shared_seconds as total_seconds,
                  vp.session_count,
                  vp.last_together
           FROM voice_partners vp JOIN users u ON vp.partner_id = u.user_id
           WHERE vp.user_id = ?
           ORDER BY total_seconds DESC LIMIT ?""",
        (user_id, limit),
    ).fetchall()
    return [dict(r) for r in rows]


def _period_leaderboard(category: str, period_start: datetime, limit: int) -> list[dict]:
    """Shared logic for weekly and monthly leaderboards."""
    session_type = {"online": "online", "gaming": "gaming", "voice": "voice"}.get(category, "online")
    period_start_str = period_start.isoformat()
    now = datetime.now(timezone.utc)

    conn = get_conn()
    rows = conn.execute(
        """SELECT s.user_id, u.display_name, s.started_at, s.ended_at
           FROM sessions s JOIN users u ON s.user_id = u.user_id
           WHERE s.session_type = ?
             AND (s.ended_at IS NULL OR s.ended_at >= ?)""",
        (session_type, period_start_str),
    ).fetchall()

    totals: dict[int, dict] = {}
    for row in rows:
        started = max(datetime.fromisoformat(row["started_at"]), period_start)
        ended = datetime.fromisoformat(row["ended_at"]) if row["ended_at"] else now
        elapsed = max(0, int((ended - started).total_seconds()))
        uid = row["user_id"]
        if uid not in totals:
            totals[uid] = {"display_name": row["display_name"], "score": 0}
        totals[uid]["score"] += elapsed

    results = [
        {"user_id": uid, "display_name": v["display_name"], "score": v["score"]}
        for uid, v in totals.items() if v["score"] > 0
    ]
    results.sort(key=lambda x: x["score"], reverse=True)
    return results[:limit]


def get_weekly_leaderboard(category: str, limit: int = 10) -> list[dict]:
    now = datetime.now(timezone.utc)
    week_start = (now - timedelta(days=now.weekday())).replace(
        hour=0, minute=0, second=0, microsecond=0
    )
    return _period_leaderboard(category, week_start, limit)


def get_monthly_leaderboard(category: str, limit: int = 10) -> list[dict]:
    now = datetime.now(timezone.utc)
    month_start = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    return _period_leaderboard(category, month_start, limit)


def get_streaks(user_id: int) -> tuple[int, int]:
    conn = get_conn()
    rows = conn.execute(
        "SELECT date FROM activity_days WHERE user_id=? ORDER BY date DESC",
        (user_id,),
    ).fetchall()
    if not rows:
        return 0, 0

    dates = [r["date"] for r in rows]
    today = datetime.now(timezone.utc).date()
    yesterday = (today - timedelta(days=1)).isoformat()
    today_str = today.isoformat()

    # Current streak
    current = 0
    if dates[0] in (today_str, yesterday):
        prev = datetime.strptime(dates[0], "%Y-%m-%d").date()
        current = 1
        for d in dates[1:]:
            curr_date = datetime.strptime(d, "%Y-%m-%d").date()
            if (prev - curr_date).days == 1:
                current += 1
                prev = curr_date
            else:
                break

    # Longest streak
    longest = current
    streak = 1
    for i in range(1, len(dates)):
        d1 = datetime.strptime(dates[i - 1], "%Y-%m-%d").date()
        d2 = datetime.strptime(dates[i], "%Y-%m-%d").date()
        if (d1 - d2).days == 1:
            streak += 1
            longest = max(longest, streak)
        else:
            streak = 1

    return current, longest


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


# --- Voice transcription ---

def open_transcript_session(channel_id: int, channel_name: str) -> int:
    """Open a new recording session and return its id."""
    conn = get_conn()
    cur = conn.execute(
        "INSERT INTO voice_transcripts (channel_id, channel_name, started_at, status) VALUES (?, ?, ?, 'recording')",
        (channel_id, channel_name, _now()),
    )
    conn.commit()
    return cur.lastrowid


def close_transcript_session(session_id: int) -> None:
    conn = get_conn()
    conn.execute(
        "UPDATE voice_transcripts SET ended_at=?, status='processing' WHERE id=?",
        (_now(), session_id),
    )
    conn.commit()


def set_transcript_status(session_id: int, status: str) -> None:
    conn = get_conn()
    conn.execute(
        "UPDATE voice_transcripts SET status=? WHERE id=?",
        (status, session_id),
    )
    conn.commit()


def set_transcript_summary(session_id: int, summary: str) -> None:
    conn = get_conn()
    conn.execute(
        "UPDATE voice_transcripts SET summary=?, status='done' WHERE id=?",
        (summary, session_id),
    )
    conn.commit()


def add_transcript_segment(session_id: int, user_id: int, display_name: str, timestamp: float, text: str) -> None:
    conn = get_conn()
    conn.execute(
        "INSERT INTO transcript_segments (session_id, user_id, display_name, timestamp, text) VALUES (?, ?, ?, ?, ?)",
        (session_id, user_id, display_name, timestamp, text),
    )
    conn.commit()


def get_transcript_session(session_id: Optional[int] = None) -> Optional[dict]:
    """Return a specific session by id, or the most recent one if session_id is None."""
    conn = get_conn()
    if session_id is not None:
        row = conn.execute(
            "SELECT * FROM voice_transcripts WHERE id=?", (session_id,)
        ).fetchone()
    else:
        row = conn.execute(
            "SELECT * FROM voice_transcripts ORDER BY id DESC LIMIT 1"
        ).fetchone()
    return dict(row) if row else None


def get_transcript_segments(session_id: int) -> list[dict]:
    """Return all segments for a session ordered by timestamp."""
    conn = get_conn()
    rows = conn.execute(
        "SELECT * FROM transcript_segments WHERE session_id=? ORDER BY timestamp",
        (session_id,),
    ).fetchall()
    return [dict(r) for r in rows]


def list_transcript_sessions(limit: int = 10) -> list[dict]:
    """Return the most recent N sessions (summary, no segments)."""
    conn = get_conn()
    rows = conn.execute(
        "SELECT * FROM voice_transcripts ORDER BY id DESC LIMIT ?", (limit,)
    ).fetchall()
    return [dict(r) for r in rows]


# --- Member dossiers ---

def get_dossier(user_id: int) -> Optional[dict]:
    conn = get_conn()
    row = conn.execute(
        "SELECT content, last_updated FROM member_dossiers WHERE user_id=?", (user_id,)
    ).fetchone()
    return dict(row) if row else None


def delete_dossier(user_id: int) -> bool:
    conn = get_conn()
    affected = conn.execute(
        "DELETE FROM member_dossiers WHERE user_id=?", (user_id,)
    ).rowcount
    conn.commit()
    return affected > 0


def upsert_dossier(user_id: int, content: str) -> None:
    conn = get_conn()
    conn.execute(
        """INSERT INTO member_dossiers (user_id, content, last_updated)
           VALUES (?, ?, ?)
           ON CONFLICT(user_id) DO UPDATE SET content=excluded.content, last_updated=excluded.last_updated""",
        (user_id, content, _now()),
    )
    conn.commit()


def get_user_chat_messages(user_id: int, since: Optional[str] = None, limit: int = 200) -> list[dict]:
    """Return chat messages for a user. If since is None, returns last 30 days."""
    conn = get_conn()
    if since is None:
        cutoff = (datetime.now(timezone.utc) - timedelta(days=30)).isoformat()
    else:
        cutoff = since
    rows = conn.execute(
        "SELECT content, sent_at FROM chat_messages WHERE user_id=? AND sent_at > ? ORDER BY sent_at ASC LIMIT ?",
        (user_id, cutoff, limit),
    ).fetchall()
    return [dict(r) for r in rows]
