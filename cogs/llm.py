import logging
import os
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import aiohttp
import discord
from discord.ext import commands

import config
import database
from cogs.profile import _fmt_duration

log = logging.getLogger("popg.llm")

OLLAMA_URL = os.getenv("OLLAMA_URL", "http://localhost:11434")
OLLAMA_MODEL = os.getenv("OLLAMA_MODEL", "llama3")
OLLAMA_TIMEOUT = int(os.getenv("OLLAMA_TIMEOUT", "600"))
# Context window in tokens. Ollama defaults to 2048, which truncates long
# transcripts — bump it so full voice sessions fit in the prompt.
OLLAMA_NUM_CTX = int(os.getenv("OLLAMA_NUM_CTX", "8192"))

_DM_MAX_HISTORY_TURNS = int(os.getenv("DM_MAX_HISTORY_TURNS",      "20"))   # user+assistant pairs kept
_DM_HISTORY_TTL       = int(os.getenv("DM_HISTORY_TTL_SECONDS",    str(2 * 3600)))  # 2h idle expiry
_DM_RATE_PERIOD       = int(os.getenv("DM_RATE_PERIOD",             "8"))    # min seconds between DM replies
_DM_MAX_INPUT_CHARS   = int(os.getenv("DM_MAX_INPUT_CHARS",         "3000")) # cap single user message

_CH_MAX_HISTORY_TURNS = int(os.getenv("CHAT_MAX_HISTORY_TURNS", "30"))   # more turns for shared channels

# Write-through in-memory caches over the DB tables.
# user_id → {"messages": list[dict], "last_active": datetime}
_dm_sessions: dict[int, dict] = {}
# channel_id → {"messages": list[dict], "last_active": datetime}
_ch_sessions: dict[int, dict] = {}
# user_id → datetime of last processed DM (rate limiting)
_dm_last_message: dict[int, datetime] = {}

_TZ_TORONTO = ZoneInfo("America/Toronto")

_SUMMARY_SYSTEM = (
    'You are a recap writer for "Past our Prime Gamers" (POPG), a Discord server of older casual gamers. '
    "Write short, fun summaries of their voice chat sessions."
)

_SUMMARY_PROMPT = """\
Write a short, fun summary of this voice chat session: what was discussed, any games mentioned, \
notable moments or jokes. Keep it under 200 words and match the casual tone of the server.

TRANSCRIPT:
{transcript}"""

_WHEN_SYSTEM = (
    'You are analyzing Discord activity patterns for a member of "Past our Prime Gamers" (POPG), '
    "a server of older casual gamers. Give concise, honest predictions based only on the data provided."
)

_WHEN_PROMPT = """\
Member: {display_name}
Today: {today} ({day_of_week}, Toronto time / ET)

SESSION HISTORY — last {days} days (online/gaming sessions, Toronto time / ET):
{session_list}

DAY-OF-WEEK BREAKDOWN (sessions per day, Mon–Sun):
{day_summary}

HOUR-OF-DAY BREAKDOWN (sessions per hour, 24h ET):
{hour_summary}

{gaming_section}\
Answer these three questions:
1. What days and times is this person most likely to be online?
2. Do you see any rotating shift pattern (e.g. schedule repeating every 2 weeks)?
3. When is the NEXT time they're most likely to appear — be specific (day + rough time in ET)?

Be honest if data is too sparse. Keep it under 200 words, write casually."""

_CHAT_SYSTEM = (
    "You are the assistant for the POPG Discord server, a private group of adult friends. "
    "Answer questions directly and honestly. Mirror the tone of the conversation — stay neutral and "
    "professional if the chat is calm, but match the energy if people are casual or joking around. "
    "No content warnings or disclaimers. Keep replies concise."
)

_CHARS_PER_PAGE = 1800  # Discord embed field limit safety margin

_CHAT_REPLY_LIMIT = 1900  # Discord message hard limit is 2000; leave headroom


def _fmt_timestamp(seconds: float) -> str:
    m, s = divmod(int(seconds), 60)
    h, m = divmod(m, 60)
    if h:
        return f"{h}:{m:02d}:{s:02d}"
    return f"{m}:{s:02d}"


def _build_transcript_text(segments: list[dict]) -> str:
    lines = []
    for seg in segments:
        ts = _fmt_timestamp(seg["timestamp"])
        lines.append(f"[{ts}] {seg['display_name']}: {seg['text']}")
    return "\n".join(lines)


def _chunk_text(text: str, limit: int) -> list[str]:
    """Split text into chunks no longer than limit, preferring paragraph/line breaks.

    Unlike _paginate, this guarantees no chunk exceeds the limit even when a
    single line is longer than it (it hard-splits as a last resort).
    """
    chunks: list[str] = []
    remaining = text.strip()
    while len(remaining) > limit:
        window = remaining[:limit]
        split = window.rfind("\n")
        if split == -1:
            split = window.rfind(" ")
        if split == -1:
            split = limit
        chunks.append(remaining[:split].rstrip())
        remaining = remaining[split:].lstrip()
    if remaining:
        chunks.append(remaining)
    return chunks or ["(empty)"]


def _paginate(text: str, page_size: int = _CHARS_PER_PAGE) -> list[str]:
    pages, current = [], []
    length = 0
    for line in text.splitlines():
        if length + len(line) + 1 > page_size and current:
            pages.append("\n".join(current))
            current, length = [], 0
        current.append(line)
        length += len(line) + 1
    if current:
        pages.append("\n".join(current))
    return pages or ["(empty)"]


def _get_dm_session(user_id: int) -> dict:
    """Return the in-memory DM session for a user, loading from DB on cache miss.

    Evicts expired sessions (idle > _DM_HISTORY_TTL) and starts fresh.
    """
    now = datetime.now(timezone.utc)
    session = _dm_sessions.get(user_id)

    if session is None:
        stored = database.get_dm_history(user_id)
        session = {"messages": stored, "last_active": now}
        _dm_sessions[user_id] = session

    if (now - session["last_active"]).total_seconds() > _DM_HISTORY_TTL:
        session["messages"] = []
        session["last_active"] = now
        database.delete_dm_history(user_id)

    return session


def _get_ch_session(channel_id: int) -> dict:
    """Return the in-memory channel chat session, loading from DB on cache miss."""
    now = datetime.now(timezone.utc)
    session = _ch_sessions.get(channel_id)

    if session is None:
        stored = database.get_channel_chat_history(channel_id)
        session = {"messages": stored, "last_active": now}
        _ch_sessions[channel_id] = session

    # No TTL — channel history persists indefinitely until !reset

    return session


async def _ollama_generate(prompt: str = "", system: str = "", *, messages: list[dict] | None = None) -> str:
    if messages is None:
        messages = []
        if system:
            messages.append({"role": "system", "content": system})
        messages.append({"role": "user", "content": prompt})
    async with aiohttp.ClientSession() as session:
        resp = await session.post(
            f"{OLLAMA_URL}/api/chat",
            json={
                "model": OLLAMA_MODEL,
                "messages": messages,
                "stream": False,
                "options": {"num_ctx": OLLAMA_NUM_CTX},
            },
            timeout=aiohttp.ClientTimeout(total=OLLAMA_TIMEOUT),
        )
        resp.raise_for_status()
        data = await resp.json()
    return data.get("message", {}).get("content", "").strip()


class LLM(commands.Cog):
    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot

    @commands.Cog.listener()
    async def on_transcript_ready(self, session_id: int) -> None:
        """Fired by voice_listener after Whisper finishes. Generate and store the LLM summary."""
        segments = database.get_transcript_segments(session_id)
        if not segments:
            return

        transcript_text = _build_transcript_text(segments)
        prompt = _SUMMARY_PROMPT.format(transcript=transcript_text)

        try:
            summary = await _ollama_generate(prompt, system=_SUMMARY_SYSTEM)
        except Exception:
            log.exception("Ollama request failed for session %d", session_id)
            database.set_transcript_status(session_id, "failed")
            return

        database.set_transcript_summary(session_id, summary)
        log.info("Session %d: summary stored — use !recap to view.", session_id)

    @commands.cooldown(1, 10, commands.BucketType.user)
    @commands.command(name="recap")
    async def recap(self, ctx: commands.Context, *args: str) -> None:
        """Show the LLM summary for the last (or a specific) voice session.

        Usage:
          !recap              — latest session
          !recap 5            — session #5
          !recap redo         — regenerate latest session's summary
          !recap redo 5       — regenerate session #5's summary
        """
        # Parse args: optional leading "redo" keyword, optional session id
        redo = False
        session_id = None
        for arg in args:
            if arg.lower() == "redo":
                redo = True
            elif arg.isdigit():
                session_id = int(arg)
            else:
                await ctx.send("Usage: `!recap [redo] [session_id]`")
                return

        session = database.get_transcript_session(session_id)
        if session is None:
            await ctx.send("No voice sessions recorded yet." if session_id is None else f"Session #{session_id} not found.")
            return

        status = session["status"]
        sid = session["id"]

        if status == "recording":
            await ctx.send(f"Session #{sid} is still recording.")
            return
        if status == "processing":
            await ctx.send(f"Session #{sid} is still being processed — check back in a moment.")
            return

        # Force regeneration if redo requested or previous attempt failed
        if redo or status == "failed":
            segments = database.get_transcript_segments(sid)
            if not segments:
                await ctx.send(f"Session #{sid} has no transcript data to summarise.")
                return
            msg = "Regenerating" if redo else "Retrying"
            await ctx.send(f"{msg} summary for session #{sid}... ({len(segments)} segments, may take a moment)")
            transcript_text = _build_transcript_text(segments)
            prompt = _SUMMARY_PROMPT.format(transcript=transcript_text)
            try:
                summary = await _ollama_generate(prompt, system=_SUMMARY_SYSTEM)
            except Exception:
                log.exception("Ollama recap failed for session %d", sid)
                await ctx.send(f"Summary generation failed. Use `!transcript {sid}` to read the raw transcript.")
                return
            database.set_transcript_summary(sid, summary)
        else:
            summary = session.get("summary") or "_No summary available._"

        started = session["started_at"][:16].replace("T", " ") + " UTC"
        embed = discord.Embed(
            title=f"Session #{sid} Recap — {session['channel_name']}",
            description=summary,
            color=discord.Color.blurple(),
        )
        embed.set_footer(text=f"Recorded {started} · !transcript {sid} for full text · Past our Prime Gamers")
        await ctx.send(embed=embed)

    @commands.cooldown(1, 10, commands.BucketType.user)
    @commands.command(name="transcript")
    async def transcript(self, ctx: commands.Context, session_id: int = None) -> None:
        """Show the full attributed transcript for the last (or a specific) voice session."""
        session = database.get_transcript_session(session_id)
        if session is None:
            await ctx.send("No voice sessions recorded yet." if session_id is None else f"Session #{session_id} not found.")
            return

        sid = session["id"]
        segments = database.get_transcript_segments(sid)

        if not segments:
            await ctx.send(f"Session #{sid} has no transcript segments.")
            return

        transcript_text = _build_transcript_text(segments)
        pages = _paginate(transcript_text)
        started = session["started_at"][:16].replace("T", " ") + " UTC"
        header = f"Session #{sid} — {session['channel_name']} — {started}"

        if len(pages) == 1:
            embed = discord.Embed(
                title=header,
                description=f"```{pages[0]}```",
                color=discord.Color.greyple(),
            )
            embed.set_footer(text=f"{len(segments)} segments · Past our Prime Gamers")
            await ctx.send(embed=embed)
        else:
            for i, page in enumerate(pages, 1):
                embed = discord.Embed(
                    title=f"{header} ({i}/{len(pages)})",
                    description=f"```{page}```",
                    color=discord.Color.greyple(),
                )
                if i == len(pages):
                    embed.set_footer(text=f"{len(segments)} segments · Past our Prime Gamers")
                await ctx.send(embed=embed)

    @commands.cooldown(1, 5, commands.BucketType.user)
    @commands.command(name="sessions", aliases=["recaps"])
    async def sessions(self, ctx: commands.Context) -> None:
        """List recent voice recording sessions."""
        rows = database.list_transcript_sessions(limit=10)
        if not rows:
            await ctx.send("No voice sessions recorded yet.")
            return

        lines = []
        for row in rows:
            started = row["started_at"][:16].replace("T", " ")
            status_icon = {"recording": "🔴", "processing": "⏳", "done": "✅", "failed": "❌"}.get(row["status"], "?")
            lines.append(f"{status_icon} **#{row['id']}** — {row['channel_name']} — {started} UTC")

        embed = discord.Embed(
            title="Recent Voice Sessions",
            description="\n".join(lines),
            color=discord.Color.blurple(),
        )
        embed.set_footer(text="!recap <id> · !transcript <id> · Past our Prime Gamers")
        await ctx.send(embed=embed)

    @commands.cooldown(1, 30, commands.BucketType.user)
    @commands.command(name="when")
    async def when(self, ctx: commands.Context, *, member_name: str = None) -> None:
        """Predict when a member will next be online based on their activity history."""
        from cogs.profile import _resolve_target
        target = await _resolve_target(ctx, member_name)
        if target is None:
            return

        sessions = database.get_session_history(target.id, days=60)
        if len(sessions) < 5:
            await ctx.send(
                f"Not enough activity data for **{target.display_name}** yet — "
                "need at least 5 recorded sessions over the past 60 days."
            )
            return

        status_msg = await ctx.send(f"Analysing **{target.display_name}**'s activity patterns... 🔍")

        # Build per-session list (cap at 120 lines to stay within token budget)
        day_names = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]
        day_counts = [0] * 7
        hour_counts = [0] * 24
        game_totals: dict[str, int] = defaultdict(int)
        session_lines = []

        for s in sessions:
            try:
                dt = datetime.fromisoformat(s["started_at"]).replace(tzinfo=timezone.utc).astimezone(_TZ_TORONTO)
            except (ValueError, TypeError):
                continue
            ended = s.get("ended_at")
            try:
                duration_secs = int(
                    (datetime.fromisoformat(ended) - datetime.fromisoformat(s["started_at"])).total_seconds()
                ) if ended else 0
            except (ValueError, TypeError):
                duration_secs = 0

            day_counts[dt.weekday()] += 1
            hour_counts[dt.hour] += 1

            gname = s.get("game_name") or ""
            if s["session_type"] == "gaming" and gname:
                game_totals[gname] += duration_secs

            dur_str = _fmt_duration(duration_secs) if duration_secs >= 60 else ""
            label = f"gaming:{gname}" if s["session_type"] == "gaming" and gname else s["session_type"]
            ts = dt.strftime("%a %Y-%m-%d %H:%M")
            line = f"{ts} ET  {label}"
            if dur_str:
                line += f"  ({dur_str})"
            session_lines.append(line)

        # Cap history fed to LLM
        if len(session_lines) > 120:
            session_lines = session_lines[-120:]

        day_summary = "  ".join(
            f"{day_names[i]}:{day_counts[i]}" for i in range(7)
        )
        hour_summary_parts = []
        for h in range(24):
            if hour_counts[h]:
                hour_summary_parts.append(f"{h:02d}h:{hour_counts[h]}")
        hour_summary = "  ".join(hour_summary_parts) or "(no data)"

        if game_totals:
            top_games = sorted(game_totals.items(), key=lambda x: x[1], reverse=True)[:5]
            gaming_lines = [f"  {name}: {_fmt_duration(secs)}" for name, secs in top_games]
            gaming_section = "TOP GAMES PLAYED:\n" + "\n".join(gaming_lines) + "\n\n"
        else:
            gaming_section = ""

        today = datetime.now(_TZ_TORONTO)
        prompt = _WHEN_PROMPT.format(
            display_name=target.display_name,
            today=today.strftime("%Y-%m-%d"),
            day_of_week=today.strftime("%A"),
            days=60,
            session_list="\n".join(session_lines),
            day_summary=day_summary,
            hour_summary=hour_summary,
            gaming_section=gaming_section,
        )

        try:
            prediction = await _ollama_generate(prompt, system=_WHEN_SYSTEM)
        except Exception:
            log.exception("!when: Ollama failed for user %d", target.id)
            await status_msg.edit(content="Prediction failed — Ollama is not responding. Try again in a moment.")
            return

        embed = discord.Embed(
            title=f"🔮 When will {target.display_name} be online?",
            description=prediction,
            color=discord.Color.teal(),
        )
        embed.set_footer(text=f"Based on {len(sessions)} sessions over 60 days · Past our Prime Gamers")
        try:
            await status_msg.delete()
        except discord.HTTPException:
            pass
        await ctx.send(embed=embed)

    @commands.cooldown(1, 15, commands.BucketType.user)
    @commands.command(name="chat", aliases=["ask"])
    async def chat(self, ctx: commands.Context, *, message: str = None) -> None:
        """Ask the local LLM a question, e.g. !chat what's a good co-op game?"""
        if not message:
            await ctx.send("Ask me something: `!chat <your question>`")
            return

        if ctx.guild is not None:
            # Guild channel — shared per-channel history; prefix message with who said it
            session = _get_ch_session(ctx.channel.id)
            user_content = f"{ctx.author.display_name}: {message}"
            full_messages: list[dict] = [{"role": "system", "content": _CHAT_SYSTEM}]
            full_messages.extend(session["messages"])
            full_messages.append({"role": "user", "content": user_content})
        else:
            # DM via !chat — merge into the user's existing DM session
            session = _get_dm_session(ctx.author.id)
            user_content = message
            full_messages = [{"role": "system", "content": _CHAT_SYSTEM}]
            full_messages.extend(session["messages"])
            full_messages.append({"role": "user", "content": user_content})

        async with ctx.typing():
            try:
                reply = await _ollama_generate(messages=full_messages)
            except Exception:
                log.exception("!chat: Ollama failed for user %d", ctx.author.id)
                await ctx.send("The LLM isn't responding right now. Try again in a moment.")
                return

        if not reply:
            await ctx.send("I didn't get a response. Try rephrasing.")
            return

        # Persist and trim history
        session["messages"].append({"role": "user",      "content": user_content})
        session["messages"].append({"role": "assistant", "content": reply})
        session["last_active"] = datetime.now(timezone.utc)
        max_items = (_CH_MAX_HISTORY_TURNS if ctx.guild else _DM_MAX_HISTORY_TURNS) * 2
        if len(session["messages"]) > max_items:
            session["messages"] = session["messages"][-max_items:]
        if ctx.guild:
            database.save_channel_chat_history(ctx.channel.id, session["messages"])
        else:
            database.save_dm_history(ctx.author.id, session["messages"])

        for chunk in _chunk_text(reply, _CHAT_REPLY_LIMIT):
            await ctx.send(chunk)

    @chat.error
    async def chat_error(self, ctx: commands.Context, error: Exception) -> None:
        if isinstance(error, commands.CommandOnCooldown):
            await ctx.send(f"Slow down a sec — try again in {error.retry_after:.0f}s.")

    @commands.Cog.listener()
    async def on_message(self, message: discord.Message) -> None:
        if message.author.bot:
            return
        if message.guild is not None:
            return  # only handle DMs
        if message.content.startswith(config.PREFIX):
            return  # prefixed command — process_commands handles it

        text = message.content.strip()
        if not text:
            await message.channel.send("I can only read text — send me something to chat about!")
            return

        # Rate limit
        now = datetime.now(timezone.utc)
        last = _dm_last_message.get(message.author.id)
        if last is not None and (now - last).total_seconds() < _DM_RATE_PERIOD:
            wait = _DM_RATE_PERIOD - (now - last).total_seconds()
            await message.channel.send(f"Slow down — try again in {wait:.0f}s.")
            return
        _dm_last_message[message.author.id] = now

        if len(text) > _DM_MAX_INPUT_CHARS:
            text = text[:_DM_MAX_INPUT_CHARS]
            log.warning("DM from user %d truncated to %d chars", message.author.id, _DM_MAX_INPUT_CHARS)

        session = _get_dm_session(message.author.id)

        full_messages: list[dict] = [{"role": "system", "content": _CHAT_SYSTEM}]
        full_messages.extend(session["messages"])
        full_messages.append({"role": "user", "content": text})

        async with message.channel.typing():
            try:
                reply = await _ollama_generate(messages=full_messages)
            except Exception:
                log.exception("DM chat failed for user %d", message.author.id)
                await message.channel.send("The AI isn't responding right now — try again in a moment.")
                return

        if not reply:
            await message.channel.send("No response — try rephrasing.")
            return

        session["messages"].append({"role": "user",      "content": text})
        session["messages"].append({"role": "assistant", "content": reply})
        session["last_active"] = datetime.now(timezone.utc)
        max_items = _DM_MAX_HISTORY_TURNS * 2
        if len(session["messages"]) > max_items:
            session["messages"] = session["messages"][-max_items:]
        database.save_dm_history(message.author.id, session["messages"])

        for chunk in _chunk_text(reply, _CHAT_REPLY_LIMIT):
            await message.channel.send(chunk)

    @commands.command(name="reset")
    async def reset_dm(self, ctx: commands.Context) -> None:
        """Clear DM conversation history (DMs) or this channel's !chat history (admins)."""
        if ctx.guild is not None:
            from cogs.admin import _is_admin
            if not _is_admin(ctx):
                await ctx.send("Only admins can reset a channel's chat history.")
                return
            _ch_sessions.pop(ctx.channel.id, None)
            database.delete_channel_chat_history(ctx.channel.id)
            await ctx.send("Channel chat history cleared — fresh start!")
        else:
            _dm_sessions.pop(ctx.author.id, None)
            database.delete_dm_history(ctx.author.id)
            await ctx.send("History cleared — fresh start!")

    @when.error
    async def when_error(self, ctx: commands.Context, error: Exception) -> None:
        if isinstance(error, commands.BadArgument):
            await ctx.send("Could not find that member. Try mentioning them with @.")
        elif isinstance(error, commands.CommandOnCooldown):
            await ctx.send(f"This command is on cooldown. Try again in {error.retry_after:.0f}s.")

    @recap.error
    async def recap_error(self, ctx: commands.Context, error: Exception) -> None:
        if isinstance(error, commands.BadArgument):
            await ctx.send("Usage: `!recap [session_id]`")

    @transcript.error
    async def transcript_error(self, ctx: commands.Context, error: Exception) -> None:
        if isinstance(error, commands.BadArgument):
            await ctx.send("Usage: `!transcript [session_id]`")


def setup(bot: commands.Bot) -> None:
    bot.add_cog(LLM(bot))
