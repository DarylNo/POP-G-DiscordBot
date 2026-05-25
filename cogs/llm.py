import asyncio
import logging
import os
from collections import defaultdict
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
OLLAMA_TIMEOUT = int(os.getenv("OLLAMA_TIMEOUT", "600"))

_SUMMARY_PROMPT = """\
You are summarizing a voice chat session from a Discord gaming server called "Past our Prime Gamers" (POPG). \
Members are older gamers who play together casually.

Below is a timestamped transcript. Write a short, fun summary: what was discussed, any games mentioned, \
notable moments or jokes. Keep it under 200 words and match the casual tone of the server.

TRANSCRIPT:
{transcript}"""

_ROAST_PROMPT = """\
You are the roast master for "Past our Prime Gamers" (POPG), a Discord server of older casual gamers.
Based on what each person actually said in this voice chat, write a savage-but-friendly roast for each speaker.
Think group chat energy — the kind of thing you'd say to a mate's face. Keep each roast to 2-3 sentences.

WHAT EACH PERSON SAID:
{per_speaker}

Write one roast per person. Format exactly like this (one per line, no extra text before or after):
**[Name]**: [roast]"""

_CHARS_PER_PAGE = 1800  # Discord embed field limit safety margin


def _fmt_timestamp(seconds: float) -> str:
    m, s = divmod(int(seconds), 60)
    h, m = divmod(m, 60)
    if h:
        return f"{h}:{m:02d}:{s:02d}"
    return f"{m}:{s:02d}"


def _build_per_speaker_text(segments: list[dict], max_quotes: int = 12) -> str:
    quotes: dict[str, list[str]] = defaultdict(list)
    for seg in segments:
        quotes[seg["display_name"]].append(seg["text"])
    lines = []
    for name, texts in quotes.items():
        if len(texts) > max_quotes:
            step = max(1, len(texts) // max_quotes)
            texts = texts[::step][:max_quotes]
        lines.append(f"{name}:")
        for t in texts:
            lines.append(f"  - {t}")
    return "\n".join(lines)


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
            return

        database.set_transcript_summary(session_id, summary)
        log.info("Session %d: summary stored — use !recap or !roast to view.", session_id)

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

    @commands.cooldown(1, 30, commands.BucketType.guild)
    @commands.command(name="roast")
    async def roast(self, ctx: commands.Context, session_id: int = None) -> None:
        """Have the LLM roast each member based on their voice chat transcript."""
        session = database.get_transcript_session(session_id)
        if session is None:
            await ctx.send("No voice sessions recorded yet." if session_id is None else f"Session #{session_id} not found.")
            return

        sid = session["id"]
        status = session["status"]

        if status == "recording":
            await ctx.send(f"Session #{sid} is still recording.")
            return
        if status in ("processing", "failed") or not session.get("summary"):
            segments = database.get_transcript_segments(sid)
            if not segments:
                await ctx.send(f"Session #{sid} has no transcript data to roast.")
                return
        else:
            segments = database.get_transcript_segments(sid)
            if not segments:
                await ctx.send(f"Session #{sid} has no transcript data to roast.")
                return

        await ctx.send(f"Roasting session #{sid}... 🔥 this may take a moment.")

        per_speaker = _build_per_speaker_text(segments)
        prompt = _ROAST_PROMPT.format(per_speaker=per_speaker)

        try:
            roast_text = await _ollama_generate(prompt)
        except Exception:
            log.exception("Ollama roast failed for session %d", sid)
            await ctx.send(f"Roast failed. Judge them yourself with `!transcript {sid}`.")
            return

        started = session["started_at"][:16].replace("T", " ") + " UTC"
        embed = discord.Embed(
            title=f"🔥 Session #{sid} Roast — {session['channel_name']}",
            description=roast_text,
            color=discord.Color.orange(),
        )
        embed.set_footer(text=f"Recorded {started} · Past our Prime Gamers")
        await ctx.send(embed=embed)

    @roast.error
    async def roast_error(self, ctx: commands.Context, error: Exception) -> None:
        if isinstance(error, commands.BadArgument):
            await ctx.send("Usage: `!roast [session_id]`")

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
