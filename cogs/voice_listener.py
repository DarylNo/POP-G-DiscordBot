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
# Barkeep: automatically join & record when friends gather in voice, and leave
# when the channel empties. Set VOICE_AUTO_RECORD=0 to require manual !join.
VOICE_AUTO_RECORD      = os.getenv("VOICE_AUTO_RECORD", "1").lower() not in ("0", "false", "no")
VOICE_AUTO_MIN_MEMBERS = int(os.getenv("VOICE_AUTO_MIN_MEMBERS", "2"))

try:
    import whisper as _whisper
    _WHISPER_AVAILABLE = True
except ImportError:
    _whisper = None
    _WHISPER_AVAILABLE = False
    log.warning("openai-whisper not installed — voice transcription disabled")


class TimestampedSink(WaveSink):
    """WaveSink that maps positions in each user's speech-only audio to wall-clock time.

    Discord only sends voice packets while a user is speaking (no silence), so
    each user's WAV is their speech concatenated with the gaps removed. Whisper
    timestamps are relative to that compressed buffer. Anchoring only the first
    packet would timestamp later utterances minutes early, scrambling the merged
    transcript's ordering.

    Instead, every time a user resumes speaking after a gap (> GAP_SECS since
    their previous packet) we record an anchor: (bytes written so far, wall
    offset). wall_offset() then maps any position in the audio back to real
    time by finding the anchor it falls after.
    """

    GAP_SECS = 1.5
    BYTES_PER_SEC = 48000 * 2 * 2  # 48kHz, stereo, 16-bit PCM (py-cord decoded output)

    def __init__(self) -> None:
        super().__init__()
        self._start: float = _time.monotonic()
        self._anchors: dict[int, list[tuple[int, float]]] = {}  # uid → [(byte_pos, wall_offset)]
        self._written: dict[int, int] = {}
        self._last_packet: dict[int, float] = {}

    @staticmethod
    def _pcm_len(data) -> int:
        """Byte length of a write payload. `data` is raw PCM bytes on stock
        py-cord but a VoiceData wrapper on the voice-receive fork — handle both
        without ever raising, so audio capture can't be broken by a bad guess."""
        for candidate in (data, getattr(data, "pcm", None), getattr(data, "decoded_data", None)):
            try:
                if candidate is not None:
                    return len(candidate)
            except TypeError:
                continue
        return 0

    def write(self, data, user) -> None:
        uid = user.id if hasattr(user, "id") else int(user)
        now = _time.monotonic() - self._start
        written = self._written.get(uid, 0)
        last = self._last_packet.get(uid)
        if last is None or (now - last) > self.GAP_SECS:
            self._anchors.setdefault(uid, []).append((written, now))
        self._last_packet[uid] = now
        self._written[uid] = written + self._pcm_len(data)
        # MUST run even if length bookkeeping above found nothing — this is what
        # actually stores the audio.
        super().write(data, user)

    def wall_offset(self, uid: int, audio_secs: float) -> float:
        """Map a position (seconds) in the user's speech-only audio to a wall-clock offset."""
        anchors = self._anchors.get(uid)
        if not anchors:
            return audio_secs
        byte_pos = audio_secs * self.BYTES_PER_SEC
        best_bytes, best_wall = anchors[0]
        for a_bytes, a_wall in anchors:
            if a_bytes <= byte_pos:
                best_bytes, best_wall = a_bytes, a_wall
            else:
                break
        return best_wall + (byte_pos - best_bytes) / self.BYTES_PER_SEC


def _get_game_label(member: discord.Member) -> str:
    """Return the game name a member is currently playing, or 'idle'."""
    for activity in member.activities:
        if isinstance(activity, discord.Game):
            return activity.name
        if isinstance(activity, discord.Activity) and activity.type == discord.ActivityType.playing:
            return activity.name
    return "idle"


def _snapshot_game_context(channel: discord.VoiceChannel) -> Optional[str]:
    """Describe what each human member in the channel is playing (or idle)."""
    parts = []
    for member in channel.members:
        if member.bot:
            continue
        label = _get_game_label(member)
        if label == "idle":
            parts.append(f"{member.display_name} idle/chatting")
        else:
            parts.append(f"{member.display_name} playing {label}")
    return ("Games: " + ", ".join(parts)) if parts else None


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
        # Keep references to fire-and-forget tasks so they can't be GC'd mid-flight
        self._bg_tasks: set = set()
        self._chunk_rotator.start()

    def cog_unload(self) -> None:
        self._chunk_rotator.cancel()

    def _spawn(self, coro) -> None:
        task = asyncio.create_task(coro)
        self._bg_tasks.add(task)
        task.add_done_callback(self._bg_tasks.discard)

    def _make_entry(
        self,
        vc: discord.VoiceClient,
        session_id: int,
        notify_channel: Optional[discord.TextChannel],
        auto: bool = False,
    ) -> dict:
        return {
            "vc":           vc,
            "session_id":   session_id,
            "notify":       notify_channel,
            "session_start": _time.monotonic(),   # absolute reference for all chunks
            "chunk_start":   _time.monotonic(),   # start of the current chunk
            "is_final":      False,               # True when !leave called
            "rotating":      False,               # True while a rotation is in-flight
            "proc_lock":     asyncio.Lock(),      # serializes chunk processing per session
            "auto":          auto,                # True when auto-joined (barkeep mode)
        }

    # ------------------------------------------------------------------ #
    #  Chunk rotation task + connection watchdog                           #
    # ------------------------------------------------------------------ #

    @tasks.loop(seconds=30)
    async def _chunk_rotator(self) -> None:
        """Rotate the audio buffer for any session that has hit VOICE_CHUNK_SECS.

        Also acts as a watchdog: if the voice connection died externally
        (kick, channel delete, network drop), finalize the session instead of
        leaking it forever.
        """
        for guild_id, entry in list(self._active.items()):
            if entry["is_final"]:
                continue
            if not entry["vc"].is_connected():
                self._spawn(self._abort_session(guild_id, "voice connection lost"))
                continue
            if entry["rotating"]:
                continue
            elapsed = _time.monotonic() - entry["chunk_start"]
            if elapsed >= VOICE_CHUNK_SECS:
                self._spawn(self._rotate_chunk(guild_id))

    @_chunk_rotator.before_loop
    async def _before_rotator(self) -> None:
        await self.bot.wait_until_ready()

    async def _rotate_chunk(self, guild_id: int) -> None:
        """Stop the current chunk; _recording_finished restarts recording and transcribes."""
        entry = self._active.get(guild_id)
        if entry is None or entry["rotating"] or entry["is_final"]:
            return
        entry["rotating"] = True
        log.info("Session %d: rotating chunk after %.0fs", entry["session_id"],
                 _time.monotonic() - entry["chunk_start"])
        try:
            entry["vc"].stop_recording()  # fires _recording_finished with rotating=True
        except Exception:
            entry["rotating"] = False
            log.exception("Session %d: stop_recording failed during rotation", entry["session_id"])
            self._spawn(self._abort_session(guild_id, "recording stopped unexpectedly"))

    async def _abort_session(self, guild_id: int, reason: str) -> None:
        """Finalize a session whose voice connection died outside the normal !leave path."""
        entry = self._active.get(guild_id)
        if entry is None or entry["is_final"]:
            return
        entry["is_final"] = True
        session_id = entry["session_id"]
        log.warning("Session %d: aborting — %s", session_id, reason)
        try:
            # Flush the last chunk through the normal final path if possible
            entry["vc"].stop_recording()
        except Exception:
            # Recording already dead — finalize with whatever was stored so far,
            # waiting out any in-flight chunk transcription first.
            self._active.pop(guild_id, None)
            async with entry["proc_lock"]:
                database.close_transcript_session(session_id)
                total = database.count_transcript_segments(session_id)
                if total:
                    database.set_transcript_status(session_id, "processing")
                    self.bot.dispatch("transcript_ready", session_id)
                else:
                    database.set_transcript_status(session_id, "failed")
        try:
            await entry["notify"].send(
                f"Voice recording stopped — {reason}. Processing what was captured."
            )
        except Exception:
            pass

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
            database.set_transcript_status(session_id, "failed")
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
        # If a rotation is in-flight, recording is already stopped (or about to
        # restart); _recording_finished re-checks is_final after restarting and
        # stops again immediately. Calling stop_recording here would raise.
        if not entry["rotating"]:
            try:
                entry["vc"].stop_recording()
            except Exception:
                log.exception("Session %d: stop_recording failed on !leave", entry["session_id"])
                # Reset the flag so _abort_session's is_final guard doesn't skip cleanup
                entry["is_final"] = False
                self._spawn(self._abort_session(ctx.guild.id, "recording was no longer active"))
                return
        await ctx.send("Stopping the recording — transcribing the final chunk now.")

    # ------------------------------------------------------------------ #
    #  Audio processing                                                    #
    # ------------------------------------------------------------------ #

    async def _recording_finished(self, sink: TimestampedSink, guild_id: int) -> None:
        """Called by py-cord when stop_recording() completes.

        For a mid-session rotation, recording is restarted IMMEDIATELY (before
        transcription) so nothing said while Whisper runs is lost. The finished
        chunk is then transcribed under a per-session lock, so a final chunk
        can't be dispatched before an earlier chunk's segments are stored.
        """
        entry = self._active.get(guild_id)
        if entry is None:
            return

        vc         = entry["vc"]
        session_id = entry["session_id"]
        chunk_wall_offset = entry["chunk_start"] - entry["session_start"]

        # Snapshot game context while the channel reference is still live
        game_context: Optional[str] = None
        if vc.channel:
            game_context = _snapshot_game_context(vc.channel)

        is_final = entry["is_final"]
        if not is_final:
            # Restart recording FIRST — audio loss window is now ~0 instead of
            # the full Whisper transcription time.
            entry["chunk_start"] = _time.monotonic()
            try:
                vc.start_recording(TimestampedSink(), self._recording_finished, guild_id)
            except Exception:
                log.exception("Session %d: failed to restart recording after rotation", session_id)
                entry["is_final"] = True
                is_final = True
            finally:
                entry["rotating"] = False
            # !leave may have landed while recording was stopped — honor it now.
            # This callback keeps handling the finished chunk; the stop fires a
            # separate final callback for the (near-empty) new chunk.
            if not is_final and entry["is_final"]:
                try:
                    vc.stop_recording()
                except Exception:
                    log.exception("Session %d: failed to stop after late !leave", session_id)
                    self._spawn(self._abort_session(guild_id, "recording stopped unexpectedly"))

        if is_final:
            self._active.pop(guild_id, None)
            try:
                await vc.disconnect()
            except Exception:
                log.exception("Session %d: disconnect failed (already dead?)", session_id)
            database.close_transcript_session(session_id)

        # Transcribe the captured chunk (serialized per session)
        async with entry["proc_lock"]:
            try:
                new_segs = await self._process_sink(
                    sink, guild_id, session_id, chunk_wall_offset, game_context
                )
            except Exception:
                log.exception("Session %d: chunk processing failed", session_id)
                new_segs = []

            if is_final:
                total = database.count_transcript_segments(session_id)
                if total == 0:
                    database.set_transcript_status(session_id, "failed")
                    log.warning("Session %d: no speech detected across all chunks.", session_id)
                    return
                # Final chunk memories are extracted via the chunk event like every
                # other chunk; on_transcript_ready only summarizes.
                if new_segs:
                    self.bot.dispatch("transcript_chunk_ready", session_id, new_segs)
                database.set_transcript_status(session_id, "processing")
                log.info("Session %d: finalised — %d total segments, dispatching transcript_ready.",
                         session_id, total)
                self.bot.dispatch("transcript_ready", session_id)
            else:
                if new_segs:
                    self.bot.dispatch("transcript_chunk_ready", session_id, new_segs)
                log.info("Session %d: chunk done (%d new segs).", session_id, len(new_segs))

    async def _process_sink(
        self,
        sink: TimestampedSink,
        guild_id: int,
        session_id: int,
        chunk_wall_offset: float,
        game_context: Optional[str] = None,
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
                        # Map the position in this user's speech-only audio back
                        # to wall-clock time via the sink's gap anchors.
                        wall_ts = chunk_wall_offset + sink.wall_offset(user_id, seg["start"])
                        chunk_segments.append((wall_ts, user_id, display_name, text))
            except Exception:
                log.exception("Whisper failed for user %s in session %d", user_id, session_id)

        chunk_segments.sort(key=lambda s: s[0])

        # Prepend a context marker so the LLM knows what was being played
        if game_context:
            database.add_transcript_segment(session_id, 0, "[Session]", chunk_wall_offset, game_context)
        for ts, uid, name, text in chunk_segments:
            database.add_transcript_segment(session_id, uid, name, ts, text)

        result = [{"timestamp": ts, "display_name": name, "text": text}
                  for ts, uid, name, text in chunk_segments]
        if game_context:
            result.insert(0, {"timestamp": chunk_wall_offset, "display_name": "[Session]", "text": game_context})
        return result

    # ------------------------------------------------------------------ #
    #  Live session event listeners                                        #
    # ------------------------------------------------------------------ #

    def _record_event(self, guild_id: int, user_id: int, text: str) -> None:
        entry = self._active.get(guild_id)
        if entry is None:
            return
        ts = _time.monotonic() - entry["session_start"]
        database.add_transcript_segment(entry["session_id"], user_id, "[Session]", ts, text)
        log.info("Session %d event @ %.1fs: %s", entry["session_id"], ts, text)

    async def _maybe_auto_join(self, guild: discord.Guild, channel: discord.VoiceChannel) -> None:
        """Barkeep: join and start recording when enough friends gather in voice."""
        if not VOICE_AUTO_RECORD or self._model is None:
            return
        if guild.id in self._active:
            return
        humans = [m for m in channel.members if not m.bot]
        if len(humans) < VOICE_AUTO_MIN_MEMBERS:
            return

        try:
            vc = await channel.connect()
        except Exception:
            log.exception("Auto-join failed for channel %s", channel.name)
            return

        session_id = database.open_transcript_session(channel.id, channel.name)
        entry = self._make_entry(vc, session_id, guild.system_channel, auto=True)
        self._active[guild.id] = entry
        try:
            vc.start_recording(TimestampedSink(), self._recording_finished, guild.id)
            log.info("Session %d: auto-joined '%s' (%d members)", session_id, channel.name, len(humans))
            self.bot.dispatch("popg_voice_joined", channel.name,
                              [m.display_name for m in humans])
        except Exception:
            self._active.pop(guild.id, None)
            database.close_transcript_session(session_id)
            database.set_transcript_status(session_id, "failed")
            try:
                await vc.disconnect()
            except Exception:
                pass
            log.exception("Auto-join start_recording failed for channel %s", channel.name)

    def _maybe_auto_leave(self, guild_id: int, entry: dict) -> None:
        """Barkeep: finalize an auto session when the channel empties."""
        if not entry.get("auto") or entry["is_final"]:
            return
        channel = entry["vc"].channel
        if channel is None:
            return
        if any(not m.bot for m in channel.members):
            return
        log.info("Session %d: channel empty — auto-leaving.", entry["session_id"])
        entry["is_final"] = True
        if not entry["rotating"]:
            try:
                entry["vc"].stop_recording()
            except Exception:
                entry["is_final"] = False
                self._spawn(self._abort_session(guild_id, "channel emptied"))

    @commands.Cog.listener()
    async def on_voice_state_update(
        self,
        member: discord.Member,
        before: discord.VoiceState,
        after: discord.VoiceState,
    ) -> None:
        entry = self._active.get(member.guild.id)
        if entry is None:
            if not member.bot and after.channel is not None:
                self._spawn(self._maybe_auto_join(member.guild, after.channel))
            return

        # The bot itself was kicked, disconnected, or dragged to another channel
        if member.id == self.bot.user.id:
            if after.channel is None and not entry["is_final"]:
                self._spawn(self._abort_session(member.guild.id, "bot was disconnected from voice"))
            elif after.channel is not None and before.channel != after.channel:
                self._record_event(member.guild.id, 0, f"Recording moved to {after.channel.name}")
            return
        if member.bot:
            return

        recorded = entry["vc"].channel
        if recorded is None:
            return  # connection dead — watchdog will clean up; avoid None==None misfires
        if before.channel == recorded and after.channel != recorded:
            self._record_event(member.guild.id, member.id,
                               f"{member.display_name} left the voice channel")
            self._maybe_auto_leave(member.guild.id, entry)
        elif before.channel != recorded and after.channel == recorded:
            self._record_event(member.guild.id, member.id,
                               f"{member.display_name} joined the voice channel")

    @commands.Cog.listener()
    async def on_presence_update(
        self,
        before: discord.Member,
        after: discord.Member,
    ) -> None:
        if after.bot:
            return
        entry = self._active.get(after.guild.id)
        if entry is None:
            return
        if after.voice is None or entry["vc"].channel is None:
            return
        if after.voice.channel != entry["vc"].channel:
            return
        before_game = _get_game_label(before)
        after_game  = _get_game_label(after)
        if before_game == after_game:
            return
        if before_game == "idle":
            msg = f"{after.display_name} started playing {after_game}"
        elif after_game == "idle":
            msg = f"{after.display_name} stopped playing {before_game}"
        else:
            msg = f"{after.display_name} switched from {before_game} to {after_game}"
        self._record_event(after.guild.id, after.id, msg)

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
