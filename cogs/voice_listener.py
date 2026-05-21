import asyncio
import logging
import os
import tempfile
from typing import Optional

import discord
from discord.ext import commands
from discord.sinks import WaveSink

import database
from cogs.admin import _is_admin

log = logging.getLogger("popg.voice_listener")

WHISPER_MODEL_NAME = os.getenv("WHISPER_MODEL", "small")
WHISPER_DEVICE = os.getenv("WHISPER_DEVICE", "cpu")

try:
    import whisper as _whisper
    _WHISPER_AVAILABLE = True
except ImportError:
    _whisper = None
    _WHISPER_AVAILABLE = False
    log.warning("openai-whisper not installed — voice transcription disabled")


def _transcribe(model, audio_bytes: bytes) -> list[dict]:
    """Blocking: write bytes to temp WAV, run Whisper, return segments list."""
    with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as f:
        f.write(audio_bytes)
        tmp_path = f.name
    try:
        result = model.transcribe(tmp_path)
        return result.get("segments", [])
    finally:
        os.unlink(tmp_path)


class VoiceListener(commands.Cog):
    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot
        self._model = None
        # guild_id -> (voice_client, session_id, notify_channel)
        self._active: dict[int, tuple[discord.VoiceClient, int, discord.TextChannel]] = {}

    async def cog_load(self) -> None:
        if not _WHISPER_AVAILABLE:
            return
        log.info("Loading Whisper '%s' on %s...", WHISPER_MODEL_NAME, WHISPER_DEVICE)
        loop = asyncio.get_event_loop()
        self._model = await loop.run_in_executor(
            None, lambda: _whisper.load_model(WHISPER_MODEL_NAME, device=WHISPER_DEVICE)
        )
        log.info("Whisper model ready.")

    @commands.command(name="join")
    async def join(self, ctx: commands.Context, channel: Optional[discord.VoiceChannel] = None) -> None:
        """Join a voice channel and start recording. Defaults to your current channel."""
        if not _is_admin(ctx):
            await ctx.send("Admin only.")
            return
        if not _WHISPER_AVAILABLE or self._model is None:
            await ctx.send("Voice transcription unavailable — `openai-whisper` is not installed.")
            return
        if ctx.guild.id in self._active:
            await ctx.send("Already recording in this server. Use `!leave` to stop first.")
            return

        target = channel or (ctx.author.voice.channel if ctx.author.voice else None)
        if target is None:
            await ctx.send("Join a voice channel first, or specify one: `!join #channel`.")
            return

        try:
            vc = await target.connect()
        except discord.ClientException:
            await ctx.send("Already connected to a voice channel in this server.")
            return

        session_id = database.open_transcript_session(target.id, target.name)
        self._active[ctx.guild.id] = (vc, session_id, ctx.channel)
        vc.start_recording(WaveSink(), self._recording_finished, ctx.guild.id)
        await ctx.send(f"Recording started in **{target.name}**. Use `!leave` to stop.")

    @commands.command(name="leave")
    async def leave(self, ctx: commands.Context) -> None:
        """Stop recording and begin transcription."""
        if not _is_admin(ctx):
            await ctx.send("Admin only.")
            return
        if ctx.guild.id not in self._active:
            await ctx.send("Not currently recording in this server.")
            return
        vc, _, _ = self._active[ctx.guild.id]
        vc.stop_recording()  # triggers _recording_finished callback

    async def _recording_finished(self, sink: WaveSink, guild_id: int) -> None:
        entry = self._active.pop(guild_id, None)
        if entry is None:
            return
        vc, session_id, notify_channel = entry

        await vc.disconnect()
        database.close_transcript_session(session_id)

        if not sink.audio_data:
            database.set_transcript_status(session_id, "failed")
            if notify_channel:
                await notify_channel.send("No audio captured — transcript cancelled.")
            return

        if notify_channel:
            await notify_channel.send(
                f"Recording stopped ({len(sink.audio_data)} speaker(s)). "
                "Transcribing — this may take a minute..."
            )

        guild = self.bot.get_guild(guild_id)
        loop = asyncio.get_event_loop()
        all_segments: list[tuple[float, int, str, str]] = []

        for user_id, audio_data in sink.audio_data.items():
            member = guild.get_member(user_id) if guild else None
            display_name = member.display_name if member else str(user_id)

            audio_data.file.seek(0)
            audio_bytes = audio_data.file.read()
            if len(audio_bytes) < 4096:  # skip near-silent streams
                continue

            try:
                segments = await loop.run_in_executor(
                    None, _transcribe, self._model, audio_bytes
                )
                for seg in segments:
                    text = seg.get("text", "").strip()
                    if text:
                        all_segments.append((seg["start"], user_id, display_name, text))
            except Exception:
                log.exception("Whisper failed for user %s in session %d", user_id, session_id)

        all_segments.sort(key=lambda s: s[0])
        for ts, uid, name, text in all_segments:
            database.add_transcript_segment(session_id, uid, name, ts, text)

        if not all_segments:
            database.set_transcript_status(session_id, "failed")
            if notify_channel:
                await notify_channel.send("No speech detected — transcript empty.")
            return

        database.set_transcript_status(session_id, "processing")

        if notify_channel:
            await notify_channel.send(
                f"Transcription done — **{len(all_segments)} segments** saved (session #{session_id}). "
                "Generating summary... use `!recap` once it's ready."
            )

        # Dispatch event so the LLM cog can pick it up
        self.bot.dispatch("transcript_ready", session_id, notify_channel)

    @join.error
    async def join_error(self, ctx: commands.Context, error: Exception) -> None:
        if isinstance(error, commands.BadArgument):
            await ctx.send("Could not find that voice channel.")

    @leave.error
    async def leave_error(self, ctx: commands.Context, error: Exception) -> None:
        pass


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(VoiceListener(bot))
