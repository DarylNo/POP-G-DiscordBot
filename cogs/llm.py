import logging
import os
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import aiohttp
import discord
from discord.ext import commands

import database
from cogs.profile import _fmt_duration

log = logging.getLogger("popg.llm")

OLLAMA_URL = os.getenv("OLLAMA_URL", "http://localhost:11434")
OLLAMA_MODEL = os.getenv("OLLAMA_MODEL", "llama3")
OLLAMA_TIMEOUT = int(os.getenv("OLLAMA_TIMEOUT", "600"))

_TZ_TORONTO = ZoneInfo("America/Toronto")

_SUMMARY_PROMPT = """\
You are summarizing a voice chat session from a Discord gaming server called "Past our Prime Gamers" (POPG). \
Members are older gamers who play together casually.

Below is a timestamped transcript. Write a short, fun summary: what was discussed, any games mentioned, \
notable moments or jokes. Keep it under 200 words and match the casual tone of the server.

TRANSCRIPT:
{transcript}"""

_WHEN_PROMPT = """\
You are analyzing Discord activity patterns for {display_name}, a member of "Past our Prime Gamers" (POPG), \
a server of older casual gamers.

Today is {today} ({day_of_week}, Toronto time / ET).

SESSION HISTORY — last {days} days (online/gaming sessions, Toronto time / ET):
{session_list}

DAY-OF-WEEK BREAKDOWN (sessions per day, Mon–Sun):
{day_summary}

HOUR-OF-DAY BREAKDOWN (sessions per hour, 24h UTC):
{hour_summary}

{gaming_section}\
Based on this data, answer:
1. What days and times is this person most likely to be online?
2. Do you see any rotating shift pattern (e.g. schedule that repeats every 2 weeks)?
3. When is the NEXT time they're most likely to appear online — be specific (day + rough time)?

Be honest if data is too sparse for a confident prediction. Keep it under 200 words and write casually."""

_CHARS_PER_PAGE = 1800  # Discord embed field limit safety margin


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


async def _ollama_generate(prompt: str) -> str:
    async with aiohttp.ClientSession() as session:
        resp = await session.post(
            f"{OLLAMA_URL}/api/generate",
            json={"model": OLLAMA_MODEL, "prompt": prompt, "stream": False},
            timeout=aiohttp.ClientTimeout(total=OLLAMA_TIMEOUT),
        )
        resp.raise_for_status()
        data = await resp.json()
    return data.get("response", "").strip()


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
            summary = await _ollama_generate(prompt)
        except Exception:
            log.exception("Ollama request failed for session %d", session_id)
            database.set_transcript_status(session_id, "failed")
            return

        database.set_transcript_summary(session_id, summary)
        log.info("Session %d: summary stored — use !recap to view.", session_id)

    @commands.cooldown(1, 10, commands.BucketType.user)
    @commands.command(name="recap")
    async def recap(self, ctx: commands.Context, session_id: int = None) -> None:
        """Show the LLM summary for the last (or a specific) voice session."""
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
        if status == "failed":
            segments = database.get_transcript_segments(sid)
            if not segments:
                await ctx.send(f"Session #{sid} failed and has no transcript data.")
                return
            await ctx.send(f"Session #{sid} previously failed — retrying summary ({len(segments)} segments, may take a few minutes)...")
            transcript_text = _build_transcript_text(segments)
            prompt = _SUMMARY_PROMPT.format(transcript=transcript_text)
            try:
                summary = await _ollama_generate(prompt)
            except Exception:
                log.exception("Ollama retry failed for session %d", sid)
                await ctx.send(f"Summary generation failed again. Use `!transcript {sid}` to read the raw transcript.")
                return
            database.set_transcript_summary(sid, summary)
            started = session["started_at"][:16].replace("T", " ") + " UTC"
            embed = discord.Embed(
                title=f"Session #{sid} Recap — {session['channel_name']}",
                description=summary,
                color=discord.Color.blurple(),
            )
            embed.set_footer(text=f"Recorded {started} · !transcript {sid} for full text · Past our Prime Gamers")
            await ctx.send(embed=embed)
            return

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
            prediction = await _ollama_generate(prompt)
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
