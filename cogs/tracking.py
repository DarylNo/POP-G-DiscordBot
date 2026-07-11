import logging
from typing import Optional

import discord
from discord.ext import commands

import config
import database

log = logging.getLogger("popg.tracking")

STALE_CAP = 4 * 3600  # max seconds credited to any session closed on bot restart


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
        # on_ready fires again on every gateway reconnect, not just startup
        self._recovered_once = False

    def _ensure_user(self, member: discord.Member) -> None:
        database.upsert_user(
            member.id,
            str(member),
            member.display_name,
        )

    @commands.Cog.listener()
    async def on_ready(self) -> None:
        """Reconcile tracking state with reality on startup AND every reconnect.

        First run (process start): the bot may have been down for a while, so
        every open session is stale — close all with the STALE_CAP and re-open
        from current member state.

        Reconnects: the process never died, so an open session that still
        matches the member's current state (same game, same voice channel,
        still online) is left untouched — closing and re-opening it would
        erase streak days, partner overlap, and session counts every time the
        websocket blips.
        """
        guild = self.bot.get_guild(config.GUILD_ID)
        if guild is None:
            return

        first_run = not self._recovered_once
        self._recovered_once = True

        open_sessions: dict[tuple[int, str], dict] = {}
        for s in database.get_active_sessions():
            open_sessions[(s["user_id"], s["session_type"])] = s

        kept = closed = opened = 0
        for member in guild.members:
            if member.bot:
                continue
            self._ensure_user(member)

            game = _get_game(member)
            in_voice = member.voice and member.voice.channel

            # Online if online/dnd, OR actively gaming/in voice
            # (you can't be gaming or in voice while truly offline)
            desired: dict[str, dict] = {}
            if _is_online(member.status) or game or in_voice:
                desired["online"] = {"platform": _get_platform(member)}
            if game:
                desired["gaming"] = {"game_name": game}
            if in_voice:
                desired["voice"] = {"voice_channel_id": member.voice.channel.id}

            for stype in ("online", "gaming", "voice"):
                sess = open_sessions.pop((member.id, stype), None)
                want = desired.get(stype)

                keep = False
                if sess and want and not first_run:
                    if stype == "gaming":
                        keep = sess["game_name"] == want["game_name"]
                    elif stype == "voice":
                        keep = sess["voice_channel_id"] == want["voice_channel_id"]
                    else:
                        keep = True

                if keep:
                    kept += 1
                    continue
                if sess:
                    database.close_session(
                        member.id, stype,
                        cap_seconds=STALE_CAP if first_run else None,
                        stale=first_run,
                    )
                    closed += 1
                if want:
                    database.open_session(member.id, stype, **want)
                    opened += 1

        # Anything left belongs to members no longer visible — close as stale
        for (uid, stype), _sess in open_sessions.items():
            database.close_session(uid, stype, cap_seconds=STALE_CAP, stale=True)
            closed += 1

        log.info("Presence recovery (%s) for guild %s: %d kept, %d closed, %d opened",
                 "startup" if first_run else "reconnect", guild.name, kept, closed, opened)

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
                self._check_milestones(after.id)
            if game_after:
                database.open_session(after.id, "gaming", game_name=game_after)
                log.debug("%s started playing %s", after.display_name, game_after)

    def _check_milestones(self, user_id: int) -> None:
        """Detect newly-crossed streak/playtime milestones and hand them to the
        LLM cog to (maybe) announce. Silent if nothing new or auto-posts muted."""
        try:
            events = database.check_and_record_milestones(user_id)
        except Exception:
            log.exception("Milestone check failed for %d", user_id)
            return
        if events:
            self.bot.dispatch("popg_milestone", events)

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
            self._check_milestones(member.id)
        elif was_in_voice and now_in_voice and before.channel.id != after.channel.id:
            # Moved between channels — close old, open new
            database.close_session(member.id, "voice")
            database.open_session(member.id, "voice", voice_channel_id=after.channel.id)
            log.debug("%s moved voice: %s → %s", member.display_name, before.channel.name, after.channel.name)


def setup(bot: commands.Bot) -> None:
    bot.add_cog(Tracking(bot))
