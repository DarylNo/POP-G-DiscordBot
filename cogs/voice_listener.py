import asyncio
import logging
import os
import tempfile
import time as _time
from typing import Optional

import discord
from discord.ext import commands, tasks
from discord.sinks import WaveSink

import database
from cogs.admin import _is_admin

log = logging.getLogger("popg.voice_listener")

WHISPER_MODEL_NAME = os.getenv("WHISPER_MODEL", "small")
WHISPER_DEVICE     = os.getenv("WHISPER_DEVICE", "cpu")
# How often to rotate the audio buffer and run Whisper (seconds). Lower = more
# frequent transcription, smaller memory footprint, but more Whisper CPU cycles.
VOICE_CHUNK_SECS   = int(os.getenv("VOICE_CHUNK_SECS", "300"))   # 5 minutes

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
    Each user's WAV is speech-only; Whisper timestamps are relative to that
    buffer, not wall-clock time.  Recording each user's first-packet offset
    lets us anchor their segments to the real session timeline.
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
            initial_prompt=(
                "Gaming session on Discord server 'Past our Prime Gamers'. "
                "Players discussing video games, strategies, and casual conversation."
            ),
        )
        return result.get("segments", [])
    finally:
        os.unlink(tmp_path)


class VoiceListener(commands.Cog):
    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot
        self._model = None
        # guild_id -> state dict (see _make_entry)
        self._active: dict[int, dict] = {}
        self._chunk_rotator.start()

    def cog_unload(self) -> None:
        self._chunk_rotator.cancel()

    def _make_entry(
        self,
        vc: discord.VoiceClient,
        session_id: int,
        notify_channel: discord.TextChannel,
    ) -> dict:
        return {
            "vc":           vc,
            "session_id":   session_id,
            "notify":       notify_channel,
            "session_start": _time.monotonic(),   # absolute reference for all chunks
            "chunk_start":   _time.monotonic(),   # start of the current chunk
            "is_final":      False,               # True when !leave called
            "rotating":      False,               # True while a rotation is in-flight
        }

    # ------------------------------------------------------------------ #
    #  Chunk rotation task                                                 #
    # ------------------------------------------------------------------ #

    @tasks.loop(seconds=30)
    async def _chunk_rotator(self) -> None:
        """Rotate the audio buffer for any session that has hit VOICE_CHUNK_SECS."""
        for guild_id, entry in list(self._active.items()):
            if entry["rotating"] or entry["is_final"]:
                continue
            elapsed = _time.monotonic() - entry["chunk_start"]
            if elapsed >= VOICE_CHUNK_SECS:
                asyncio.create_task(self._rotate_chunk(guild_id))

    @_chunk_rotator.before_loop
    async def _before_rotator(self) -> None:
        await self.bot.wait_until_ready()

    async def _rotate_chunk(self, guild_id: int) -> None:
        """Stop current chunk, transcribe it, immediately restart recording."""
        entry = self._active.get(guild_id)
        if entry is None or entry["rotating"] or entry["is_final"]:
            return
        entry["rotating"] = True
        log.info("Session %d: rotating chunk after %.0fs", entry["session_id"],
                 _time.monotonic() - entry["chunk_start"])
        entry["vc"].stop_recording()  # fires _recording_finished with rotating=True

    # ------------------------------------------------------------------ #
    #  Commands                                                            #
    # ------------------------------------------------------------------ #

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
        entry = self._make_entry(vc, session_id, ctx.channel)
        self._active[ctx.guild.id] = entry
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
        chunk_mins = VOICE_CHUNK_SECS // 60
        await ctx.send(
            f"Recording started in **{target.name}** — transcribing every {chunk_mins} min. "
            f"Use `!leave` to stop."
        )

    @commands.command(name="leave")
    async def leave(self, ctx: commands.Context) -> None:
        """Stop recording and finalise the transcript."""
        if not _is_admin(ctx):
            await ctx.send("Admin only.")
            return
        if ctx.guild.id not in self._active:
            await ctx.send("Not currently recording in this server.")
            return
        entry = self._active[ctx.guild.id]
        entry["is_final"] = True
        entry["vc"].stop_recording()  # triggers _recording_finished

    # ------------------------------------------------------------------ #
    #  Audio processing                                                    #
    # ------------------------------------------------------------------ #

    async def _recording_finished(self, sink: TimestampedSink, guild_id: int) -> None:
        """Called by py-cord when stop_recording() completes.

        If this is a mid-session chunk rotation (!= is_final), we transcribe
        the chunk and immediately restart recording on the same session.
        If this is the final stop (!leave), we close the session and dispatch.
        """
        entry = self._active.get(guild_id)
        if entry is None:
            return

        is_final  = entry["is_final"]
        vc        = entry["vc"]
        session_id = entry["session_id"]

        # Compute how far into the session this chunk started
        chunk_wall_offset = entry["chunk_start"] - entry["session_start"]

        if is_final:
            self._active.pop(guild_id, None)
            await vc.disconnect()
            database.close_transcript_session(session_id)

        # Transcribe the captured chunk
        new_segs = await self._process_sink(sink, guild_id, session_id, chunk_wall_offset)

        if is_final:
            total = database.count_transcript_segments(session_id)
            if total == 0:
                database.set_transcript_status(session_id, "failed")
                log.warning("Session %d: no speech detected across all chunks.", session_id)
                return
            database.set_transcript_status(session_id, "processing")
            log.info("Session %d: finalised — %d total segments, dispatching transcript_ready.",
                     session_id, total)
            self.bot.dispatch("transcript_ready", session_id)
        else:
            # Let the LLM cog extract memories from this chunk in the background
            if new_segs:
                self.bot.dispatch("transcript_chunk_ready", session_id, new_segs)

            # Restart recording on the same session
            entry["chunk_start"] = _time.monotonic()
            entry["rotating"] = False
            try:
                vc.start_recording(TimestampedSink(), self._recording_finished, guild_id)
                log.info("Session %d: chunk done (%d new segs), resumed recording.",
                         session_id, len(new_segs))
            except Exception:
                log.exception("Session %d: failed to restart recording after chunk rotation", session_id)
                self._active.pop(guild_id, None)
                await vc.disconnect()
                database.close_transcript_session(session_id)
                total = database.count_transcript_segments(session_id)
                if total:
                    database.set_transcript_status(session_id, "processing")
                    self.bot.dispatch("transcript_ready", session_id)
                else:
                    database.set_transcript_status(session_id, "failed")

    async def _process_sink(
        self,
        sink: TimestampedSink,
        guild_id: int,
        session_id: int,
        chunk_wall_offset: float,
    ) -> list[dict]:
        """Transcribe all audio in a sink, store segments, return them as dicts."""
        if not sink.audio_data:
            return []

        guild = self.bot.get_guild(guild_id)
        loop  = asyncio.get_event_loop()
        chunk_segments: list[tuple[float, int, str, str]] = []

        for user_id, audio_data in sink.audio_data.items():
            if isinstance(user_id, discord.Member):
                member  = user_id
                user_id = member.id
            else:
                member = guild.get_member(user_id) if guild else None
            display_name = member.display_name if member else str(user_id)

            user_chunk_offset = sink._user_offsets.get(user_id, 0.0)
            wall_base = chunk_wall_offset + user_chunk_offset

            audio_data.file.seek(0)
            audio_bytes = audio_data.file.read()
            if len(audio_bytes) < 4096:
                continue

            try:
                segments = await loop.run_in_executor(
                    None, _transcribe, self._model, audio_bytes
                )
                for seg in segments:
                    text = seg.get("text", "").strip()
                    if text:
                        wall_ts = wall_base + seg["start"]
                        chunk_segments.append((wall_ts, user_id, display_name, text))
            except Exception:
                log.exception("Whisper failed for user %s in session %d", user_id, session_id)

        chunk_segments.sort(key=lambda s: s[0])
        for ts, uid, name, text in chunk_segments:
            database.add_transcript_segment(session_id, uid, name, ts, text)

        return [{"timestamp": ts, "display_name": name, "text": text}
                for ts, uid, name, text in chunk_segments]

    # ------------------------------------------------------------------ #
    #  Error handlers                                                      #
    # ------------------------------------------------------------------ #

    @join.error
    async def join_error(self, ctx: commands.Context, error: Exception) -> None:
        if isinstance(error, commands.BadArgument):
            await ctx.send("Could not find that voice channel.")

    @leave.error
    async def leave_error(self, ctx: commands.Context, error: Exception) -> None:
        pass


def setup(bot: commands.Bot) -> None:
    bot.add_cog(VoiceListener(bot))
