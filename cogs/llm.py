import asyncio
import logging
import os
from collections import defaultdict
from datetime import datetime, timezone

import aiohttp
import discord
from discord.ext import commands, tasks

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

_DOSSIER_INIT_PROMPT = """\
You are building a character sheet for a member of "Past our Prime Gamers" (POPG), a Discord server of older casual gamers.

Based on the data below, fill in what you can confidently infer. Leave sections sparse if there isn't enough evidence yet — do NOT speculate. This sheet will be updated after every session.

MEMBER: {display_name}
STATS: {stats_summary}
GAMING PARTNERS (plays games with): {accomplices}
VOICE CREW (hangs out in voice with): {voice_crew}
VOICE QUOTES (this session): {voice_quotes}
RECENT CHAT MESSAGES: {chat_messages}

Write the character sheet using exactly these sections. Use casual, fun language:

**Nicknames:** [any nicknames used in chat or voice — only real examples found in the data]
**Personality:** [communication style, humor, energy — only if evident]
**Gaming Style:** [what they play, how they approach games — only if evident]
**Social Role:** [group dynamic, leader vs follower, instigator vs peacemaker — only if evident]
**Runs With:** [names of gaming partners and voice crew with a note on what they do together]
**Catchphrases:** [recurring phrases or notable real quotes — only real examples]
**Still Learning:** [things not yet clear, to fill in over time]"""

_DOSSIER_UPDATE_PROMPT = """\
You are updating a character sheet for {display_name}, a member of "Past our Prime Gamers" (POPG).

CURRENT SHEET:
{existing_content}

NEW DATA FROM THIS SESSION:
GAMING PARTNERS (plays games with): {accomplices}
VOICE CREW (hangs out in voice with): {voice_crew}
VOICE QUOTES: {voice_quotes}
RECENT CHAT MESSAGES: {chat_messages}

Refine the sheet based on new evidence only. Rules:
- Only change a section if you have new evidence for it
- Update "Runs With" if partner data has changed or new people appear
- Add real quotes to Catchphrases if you spot recurring phrases
- Add to Nicknames only if you actually see one used in the new data
- Move items from "Still Learning" to the right section if evidence is now clear
- Keep the same section format, keep it under 400 words total"""

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


async def _update_member_dossier(user_id: int, display_name: str, session_quotes: list[str]) -> None:
    """Build or refine the LLM character sheet for one member."""
    stats = database.get_user_stats(user_id)
    accomplices = database.get_accomplices(user_id, limit=5)
    crew = database.get_voice_crew(user_id, limit=5)
    existing = database.get_dossier(user_id)

    # Build social summary strings
    def _fmt_accomplices(rows):
        if not rows:
            return "(none yet)"
        parts = []
        for r in rows:
            t = _fmt_duration(r["total_seconds"])
            parts.append(f"{r['display_name']} — {t} gaming together")
        return "; ".join(parts)

    def _fmt_crew(rows):
        if not rows:
            return "(none yet)"
        parts = []
        for r in rows:
            t = _fmt_duration(r["total_seconds"])
            parts.append(f"{r['display_name']} — {t} in voice together")
        return "; ".join(parts)

    accomplices_str = _fmt_accomplices(accomplices)
    crew_str = _fmt_crew(crew)

    # Build stats summary
    stats_parts = []
    if stats:
        stats_parts.append(f"Online: {_fmt_duration(stats['total_online_seconds'])}")
        stats_parts.append(f"Gaming: {_fmt_duration(stats['total_gaming_seconds'])}")
        stats_parts.append(f"Voice: {_fmt_duration(stats['total_voice_seconds'])}")
        if stats.get("top_games"):
            games = ", ".join(g["game_name"] for g in stats["top_games"])
            stats_parts.append(f"Top games: {games}")
    stats_summary = "; ".join(stats_parts) or "(no stats yet)"

    # Voice quotes for this session
    voice_quotes = "\n".join(f"- {q}" for q in session_quotes[:15]) or ""

    # Chat messages: all since last update (or last 30 days for first time)
    since = existing["last_updated"] if existing else None
    chat_msgs = database.get_user_chat_messages(user_id, since=since, limit=200)
    chat_sample = "\n".join(f"- {m['content']}" for m in chat_msgs)

    # Skip Ollama entirely if there's nothing new to work with on an update
    if existing and not voice_quotes and not chat_sample:
        log.debug("Dossier for user %d (%s): no new data, skipping.", user_id, display_name)
        return

    voice_quotes = voice_quotes or "(no voice data)"
    chat_sample = chat_sample or "(no chat messages logged)"

    if existing:
        prompt = _DOSSIER_UPDATE_PROMPT.format(
            display_name=display_name,
            existing_content=existing["content"],
            accomplices=accomplices_str,
            voice_crew=crew_str,
            voice_quotes=voice_quotes,
            chat_messages=chat_sample,
        )
    else:
        prompt = _DOSSIER_INIT_PROMPT.format(
            display_name=display_name,
            stats_summary=stats_summary,
            accomplices=accomplices_str,
            voice_crew=crew_str,
            voice_quotes=voice_quotes,
            chat_messages=chat_sample,
        )

    content = await _ollama_generate(prompt)
    database.upsert_dossier(user_id, content)
    log.info("Dossier updated for user %d (%s).", user_id, display_name)


class LLM(commands.Cog):
    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot
        self._daily_dossier_task.start()

    def cog_unload(self) -> None:
        self._daily_dossier_task.cancel()

    @tasks.loop(hours=24)
    async def _daily_dossier_task(self) -> None:
        """Update dossiers for text-chat-active members who haven't had a voice session recently."""
        users = database.get_all_users()
        for user in users:
            uid = user["user_id"]
            existing = database.get_dossier(uid)
            since = existing["last_updated"] if existing else None
            msgs = database.get_user_chat_messages(uid, since=since, limit=200)
            if len(msgs) >= 10:
                try:
                    await _update_member_dossier(uid, user["display_name"], session_quotes=[])
                except Exception:
                    log.exception("Daily dossier update failed for user %d", uid)

    @_daily_dossier_task.before_loop
    async def _before_daily_dossier(self) -> None:
        await self.bot.wait_until_ready()

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

        # Update dossiers for each speaker in this session
        by_user: dict[int, tuple[str, list[str]]] = {}
        for seg in segments:
            uid = seg["user_id"]
            if uid not in by_user:
                by_user[uid] = (seg["display_name"], [])
            by_user[uid][1].append(seg["text"])

        for uid, (name, quotes) in by_user.items():
            try:
                await _update_member_dossier(uid, name, quotes)
            except Exception:
                log.exception("Dossier update failed for user %d", uid)

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

    @commands.cooldown(1, 5, commands.BucketType.user)
    @commands.command(name="dossier", aliases=["sheet"], hidden=True)
    async def dossier(self, ctx: commands.Context, *, target: str = None) -> None:
        """Show the AI-generated character sheet for you, another member, or all members (admin: all)."""
        # Show all dossiers: !dossier all
        if target and target.lower() == "all":
            users = database.get_all_users()
            if not users:
                await ctx.send("No members in the database yet.")
                return
            sent = 0
            for user in users:
                row = database.get_dossier(user["user_id"])
                if not row:
                    continue
                updated = row["last_updated"][:16].replace("T", " ") + " UTC"
                embed = discord.Embed(
                    title=f"📋 {user['display_name']} — POPG Dossier",
                    description=row["content"],
                    color=discord.Color.green(),
                )
                embed.set_footer(text=f"Last updated {updated} · Past our Prime Gamers")
                await ctx.send(embed=embed)
                sent += 1
            if sent == 0:
                await ctx.send("No dossiers built yet — run `!rebuild` to generate them.")
            return

        # Reset mode: !dossier reset [@member]
        if target and target.lower().startswith("reset"):
            rest = target[5:].strip()
            if rest and ctx.guild is not None:
                try:
                    member = await commands.MemberConverter().convert(ctx, rest)
                except commands.BadArgument:
                    await ctx.send(f"Member `{rest}` not found.")
                    return
            else:
                member = ctx.author
            deleted = database.delete_dossier(member.id)
            if deleted:
                await ctx.send(f"Dossier for **{member.display_name}** deleted. Run `!rebuild` to rebuild it fresh.")
            else:
                await ctx.send(f"No dossier found for **{member.display_name}**.")
            return

        # Single member mode
        if target:
            if ctx.guild is None:
                await ctx.send("Member lookup doesn't work in DMs — use `!dossier` with no arguments to see your own sheet.")
                return
            # Try to resolve as a Member mention/name
            try:
                converter = commands.MemberConverter()
                member = await converter.convert(ctx, target)
            except commands.BadArgument:
                await ctx.send(f"Member `{target}` not found.")
                return
        else:
            member = ctx.author

        row = database.get_dossier(member.id)
        if not row:
            await ctx.send(
                f"No dossier yet for **{member.display_name}** — "
                "participate in a recorded voice session or chat in a watched channel to start building one."
            )
            return
        updated = row["last_updated"][:16].replace("T", " ") + " UTC"
        embed = discord.Embed(
            title=f"📋 {member.display_name} — POPG Dossier",
            description=row["content"],
            color=discord.Color.green(),
        )
        embed.set_footer(text=f"Last updated {updated} · Past our Prime Gamers")
        await ctx.send(embed=embed)

    @roast.error
    async def roast_error(self, ctx: commands.Context, error: Exception) -> None:
        if isinstance(error, commands.BadArgument):
            await ctx.send("Usage: `!roast [session_id]`")

    @commands.cooldown(1, 60, commands.BucketType.guild)
    @commands.command(name="rebuild", hidden=True)
    async def rebuild(self, ctx: commands.Context, *, member_name: str = None) -> None:
        """Rebuild dossier(s) via Ollama. Pass a name to rebuild one person."""
        if member_name:
            if ctx.guild is None:
                await ctx.send("Member lookup doesn't work in DMs.")
                return
            try:
                member = await commands.MemberConverter().convert(ctx, member_name)
            except commands.BadArgument:
                await ctx.send(f"Member `{member_name}` not found.")
                return
            status_msg = await ctx.send(f"Rebuilding dossier for **{member.display_name}**... 📋")
            try:
                await _update_member_dossier(member.id, member.display_name, session_quotes=[])
                await status_msg.edit(content=f"Dossier rebuilt for **{member.display_name}**.")
            except Exception:
                log.exception("Rebuild: failed for user %d", member.id)
                await status_msg.edit(content=f"Rebuild failed for **{member.display_name}** — check logs.")
            return

        users = database.get_all_users()
        if not users:
            await ctx.send("No members in the database yet.")
            return
        status_msg = await ctx.send(f"Rebuilding dossiers for {len(users)} member(s)... 📋 (this will take a while)")
        done, failed = 0, 0
        for user in users:
            try:
                await _update_member_dossier(user["user_id"], user["display_name"], session_quotes=[])
                done += 1
            except Exception:
                log.exception("Rebuild: dossier update failed for user %d", user["user_id"])
                failed += 1
        summary = f"Dossier rebuild complete — {done} updated"
        if failed:
            summary += f", {failed} failed (check logs)"
        try:
            await status_msg.edit(content=summary)
        except Exception:
            await ctx.send(summary)

    @dossier.error
    async def dossier_error(self, ctx: commands.Context, error: Exception) -> None:
        if isinstance(error, commands.BadArgument):
            await ctx.send("Usage: `!dossier [@member]`")
        elif isinstance(error, commands.CommandInvokeError):
            log.exception("!dossier unhandled error", exc_info=error.original)
            await ctx.send(f"Error: `{error.original}`")

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
