import asyncio
import logging
import os
import tempfile
import time as _time
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


class TimestampedSink(WaveSink):
    """WaveSink that records when each user's first audio packet arrives.

    Discord only sends voice packets while a user is speaking (no silence).
    Each user's WAV is therefore speech-only, and Whisper's timestamps are
    relative to that speech buffer — not wall-clock time.  By recording the
    monotonic offset of each user's first packet we can anchor their Whisper
    timestamps to the real session timeline.
    """

    def __init__(self) -> None:
        super().__init__()
        self._start: float = _time.monotonic()
        self._user_offsets: dict[int, float] = {}

    def write(self, data: bytes, user) -> None:
        uid = user.id if hasattr(user, "id") else int(user)
        if uid not in self._user_offsets:
            self._user_offsets[uid] = _time.monotonic() - self._start
        super().write(data, user)


def _transcribe(model, audio_bytes: bytes) -> list[dict]:
    """Blocking: write bytes to temp WAV, run Whisper, return segments list."""
    with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as f:
        f.write(audio_bytes)
        tmp_path = f.name
    try:
        result = model.transcribe(
            tmp_path,
            language="en",
            initial_prompt="Gaming session on Discord server 'Past our Prime Gamers'. Players discussing video games, strategies, and casual conversation.",
        )
        return result.get("segments", [])
    finally:
        os.unlink(tmp_path)


class VoiceListener(commands.Cog):
    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot
        self._model = None
        # guild_id -> (voice_client, session_id, notify_channel)
        self._active: dict[int, tuple[discord.VoiceClient, int, discord.TextChannel]] = {}

    @commands.Cog.listener()
    async def on_ready(self) -> None:
        if not _WHISPER_AVAILABLE or self._model is not None:
            return
        loop = asyncio.get_event_loop()
        log.info("Loading Whisper '%s' on %s...", WHISPER_MODEL_NAME, WHISPER_DEVICE)
        try:
            self._model = await loop.run_in_executor(
                None, lambda: _whisper.load_model(WHISPER_MODEL_NAME, device=WHISPER_DEVICE)
            )
            log.info("Whisper model ready on %s.", WHISPER_DEVICE)
        except RuntimeError as e:
            if WHISPER_DEVICE != "cpu" and "CUDA" in str(e):
                log.warning("CUDA unavailable — falling back to CPU for Whisper (%s)", e)
                self._model = await loop.run_in_executor(
                    None, lambda: _whisper.load_model(WHISPER_MODEL_NAME, device="cpu")
                )
                log.info("Whisper model ready on CPU (fallback).")
            else:
                log.exception("Failed to load Whisper model: %s", e)

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
        try:
            vc.start_recording(TimestampedSink(), self._recording_finished, ctx.guild.id)
        except Exception as e:
            self._active.pop(ctx.guild.id, None)
            database.close_transcript_session(session_id)
            await vc.disconnect()
            log.exception("start_recording failed (likely DAVE E2E encryption incompatibility): %s", e)
            await ctx.send(
                "Voice recording failed to start. This is a known py-cord incompatibility "
                "with Discord's DAVE E2E encryption protocol — see "
                "https://github.com/Pycord-Development/pycord/issues/3139"
            )
            return
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
            log.warning("Session %d: no audio captured.", session_id)
            return

        guild = self.bot.get_guild(guild_id)
        loop = asyncio.get_event_loop()
        all_segments: list[tuple[float, int, str, str]] = []

        for user_id, audio_data in sink.audio_data.items():
            # py-cord dev branch keys audio_data by Member objects; released builds use int
            if isinstance(user_id, discord.Member):
                member = user_id
                user_id = member.id
            else:
                member = guild.get_member(user_id) if guild else None
            display_name = member.display_name if member else str(user_id)

            # Wall-clock offset: when did this user's first audio packet arrive?
            wall_offset = sink._user_offsets.get(user_id, 0.0)

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
                        # Anchor Whisper's speech-relative timestamp to session wall time
                        wall_ts = seg["start"] + wall_offset
                        all_segments.append((wall_ts, user_id, display_name, text))
            except Exception:
                log.exception("Whisper failed for user %s in session %d", user_id, session_id)

        all_segments.sort(key=lambda s: s[0])
        for ts, uid, name, text in all_segments:
            database.add_transcript_segment(session_id, uid, name, ts, text)

        if not all_segments:
            database.set_transcript_status(session_id, "failed")
            log.warning("Session %d: no speech detected.", session_id)
            return

        database.set_transcript_status(session_id, "processing")
        log.info("Session %d: %d segments saved, dispatching transcript_ready.", session_id, len(all_segments))

        # Dispatch event so the LLM cog can pick it up
        self.bot.dispatch("transcript_ready", session_id)

    @join.error
    async def join_error(self, ctx: commands.Context, error: Exception) -> None:
        if isinstance(error, commands.BadArgument):
            await ctx.send("Could not find that voice channel.")

    @leave.error
    async def leave_error(self, ctx: commands.Context, error: Exception) -> None:
        pass


def setup(bot: commands.Bot) -> None:
    bot.add_cog(VoiceListener(bot))
