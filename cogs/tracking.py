import logging
from datetime import datetime, timezone
from typing import Optional

import discord
from discord.ext import commands, tasks

import config
import database

log = logging.getLogger("popg.tracking")

GAMING_SESSION_CAP = 6 * 3600  # 6 hours in seconds


def _get_game(member: discord.Member) -> Optional[str]:
    """Return the name of the game a member is currently playing, or None."""
    for activity in member.activities:
        if isinstance(activity, discord.Game):
            return activity.name
        if isinstance(activity, discord.Activity) and activity.type == discord.ActivityType.playing:
            return activity.name
    return None


def _is_online(status: discord.Status) -> bool:
    return status in (discord.Status.online, discord.Status.dnd)


def _get_platform(member: discord.Member) -> str:
    """Detect the platform the member is actively using.

    Uses per-platform status to distinguish: if desktop is idle but mobile
    is online, they're on their phone. Desktop priority only applies when
    both platforms show the same level of activity.
    """
    desktop_online = member.desktop_status == discord.Status.online or member.web_status == discord.Status.online
    mobile_online  = member.mobile_status  == discord.Status.online
    desktop_dnd    = member.desktop_status == discord.Status.dnd    or member.web_status == discord.Status.dnd
    mobile_dnd     = member.mobile_status  == discord.Status.dnd

    # One platform is clearly active (online), the other is not
    if desktop_online and not mobile_online:
        return "desktop"
    if mobile_online and not desktop_online:
        return "mobile"

    # Both are online — fall back to desktop priority
    if desktop_online and mobile_online:
        return "desktop"

    # Neither is 'online'; check dnd
    if desktop_dnd and not mobile_dnd:
        return "desktop"
    if mobile_dnd and not desktop_dnd:
        return "mobile"

    return "desktop"  # last resort fallback


class Tracking(commands.Cog):
    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot

    def _ensure_user(self, member: discord.Member) -> None:
        database.upsert_user(
            member.id,
            str(member),
            member.display_name,
        )

    @commands.Cog.listener()
    async def on_ready(self) -> None:
        """Recover tracking state for all current guild members on bot start/restart."""
        guild = self.bot.get_guild(config.GUILD_ID)
        if guild is None:
            return

        # Close any sessions the DB thinks are still open (stale from last run)
        stale = database.get_active_sessions()
        for s in stale:
            database.close_session(s["user_id"], s["session_type"])
        if stale:
            log.info("Closed %d stale sessions from previous run", len(stale))

        # Re-open sessions based on current member states
        for member in guild.members:
            if member.bot:
                continue
            self._ensure_user(member)

            game = _get_game(member)
            in_voice = member.voice and member.voice.channel

            # Open online session if online/dnd, OR if actively gaming/in voice
            # (you can't be gaming or in voice while truly offline)
            if _is_online(member.status) or game or in_voice:
                database.open_session(member.id, "online", platform=_get_platform(member))

            if game:
                database.open_session(member.id, "gaming", game_name=game)

            if in_voice:
                database.open_session(member.id, "voice", voice_channel_id=member.voice.channel.id)

        log.info("Presence recovery complete for guild %s", guild.name)

        if not self.enforce_gaming_cap.is_running():
            self.enforce_gaming_cap.start()

    @tasks.loop(minutes=30)
    async def enforce_gaming_cap(self) -> None:
        """Close any gaming session that has exceeded the cap, crediting only the cap amount."""
        sessions = database.get_active_sessions()
        now = datetime.now(timezone.utc)
        for s in sessions:
            if s["session_type"] != "gaming":
                continue
            started = datetime.fromisoformat(s["started_at"])
            elapsed = (now - started).total_seconds()
            if elapsed >= GAMING_SESSION_CAP:
                database.close_session(s["user_id"], "gaming", cap_seconds=GAMING_SESSION_CAP)
                log.info(
                    "Capped gaming session for user %d at 6h (was %.1fh)",
                    s["user_id"], elapsed / 3600,
                )

    @commands.Cog.listener()
    async def on_member_join(self, member: discord.Member) -> None:
        if member.bot or member.guild.id != config.GUILD_ID:
            return
        self._ensure_user(member)

    @commands.Cog.listener()
    async def on_presence_update(self, before: discord.Member, after: discord.Member) -> None:
        if after.bot or after.guild.id != config.GUILD_ID:
            return
        self._ensure_user(after)

        # --- Online / offline tracking ---
        was_online = _is_online(before.status)
        now_online = _is_online(after.status)

        if not was_online and now_online:
            database.open_session(after.id, "online", platform=_get_platform(after))
            log.debug("%s came online (%s)", after.display_name, _get_platform(after))
        elif was_online and not now_online:
            elapsed = database.close_session(after.id, "online")
            log.debug("%s went offline (online for %ss)", after.display_name, elapsed)
            # Also close gaming/voice if they somehow disappear without separate events
            database.close_session(after.id, "gaming")
            database.close_session(after.id, "voice")

        # --- Gaming tracking ---
        game_before = _get_game(before)
        game_after = _get_game(after)

        if game_before != game_after:
            if game_before:
                elapsed = database.close_session(after.id, "gaming")
                log.debug("%s stopped playing %s (%ss)", after.display_name, game_before, elapsed)
            if game_after:
                database.open_session(after.id, "gaming", game_name=game_after)
                log.debug("%s started playing %s", after.display_name, game_after)

    @commands.Cog.listener()
    async def on_voice_state_update(
        self,
        member: discord.Member,
        before: discord.VoiceState,
        after: discord.VoiceState,
    ) -> None:
        if member.bot or member.guild.id != config.GUILD_ID:
            return
        self._ensure_user(member)

        was_in_voice = before.channel is not None
        now_in_voice = after.channel is not None

        if not was_in_voice and now_in_voice:
            database.open_session(member.id, "voice", voice_channel_id=after.channel.id)
            log.debug("%s joined voice: %s", member.display_name, after.channel.name)
        elif was_in_voice and not now_in_voice:
            elapsed = database.close_session(member.id, "voice")
            log.debug("%s left voice (%ss)", member.display_name, elapsed)
        elif was_in_voice and now_in_voice and before.channel.id != after.channel.id:
            # Moved between channels — close old, open new
            database.close_session(member.id, "voice")
            database.open_session(member.id, "voice", voice_channel_id=after.channel.id)
            log.debug("%s moved voice: %s → %s", member.display_name, before.channel.name, after.channel.name)


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(Tracking(bot))
