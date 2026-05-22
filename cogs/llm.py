import asyncio
import logging
import os
from datetime import datetime, timezone

import aiohttp
import discord
from discord.ext import commands

import database
from cogs.admin import _is_admin
from cogs.profile import _fmt_duration

log = logging.getLogger("popg.llm")

OLLAMA_URL = os.getenv("OLLAMA_URL", "http://localhost:11434")
OLLAMA_MODEL = os.getenv("OLLAMA_MODEL", "llama3")

_SUMMARY_PROMPT = """\
You are summarizing a voice chat session from a Discord gaming server called "Past our Prime Gamers" (POPG). \
Members are older gamers who play together casually.

Below is a timestamped transcript. Write a short, fun summary: what was discussed, any games mentioned, \
notable moments or jokes. Keep it under 200 words and match the casual tone of the server.

TRANSCRIPT:
{transcript}"""

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
            timeout=aiohttp.ClientTimeout(total=120),
        )
        resp.raise_for_status()
        data = await resp.json()
    return data.get("response", "").strip()


class LLM(commands.Cog):
    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot

    @commands.Cog.listener()
    async def on_transcript_ready(self, session_id: int, notify_channel: discord.TextChannel) -> None:
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
            if notify_channel:
                await notify_channel.send(
                    f"LLM summary failed for session #{session_id}. "
                    "Use `!transcript` to read the raw transcript."
                )
            return

        database.set_transcript_summary(session_id, summary)

        if notify_channel:
            embed = discord.Embed(
                title=f"Session #{session_id} — Summary Ready",
                description=summary,
                color=discord.Color.blurple(),
            )
            embed.set_footer(text=f"Use !recap {session_id} or !transcript {session_id} · Past our Prime Gamers")
            await notify_channel.send(embed=embed)

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
            await ctx.send(f"Session #{sid} failed to process. Use `!transcript {sid}` to see raw segments if any were captured.")
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
