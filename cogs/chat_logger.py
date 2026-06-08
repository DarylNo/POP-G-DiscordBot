import logging
from datetime import timezone

import discord
from discord.ext import commands

import config
import database
from cogs.profile import _fmt_dt, _guild_member

log = logging.getLogger("popg.chat_logger")

_ADMIN_DENY = "You need Administrator permission or the Admin role to use this."

# Sentinel channel_id stored in watched_channels to mean "log every channel the
# bot can see". Discord only delivers on_message for visible channels, so this
# effectively logs everything readable, including channels created later.
_WATCH_ALL = 0


def _author_is_admin(ctx: commands.Context) -> bool:
    """Admin check that also works in DMs by resolving the author in the POPG guild."""
    member = _guild_member(ctx)
    perms = getattr(member, "guild_permissions", None)
    return bool(perms and perms.administrator)


class ChatLogger(commands.Cog):
    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot
        # In-memory set for fast lookup on every message
        self._watched: set[int] = set(database.get_watched_channels())

    @commands.Cog.listener()
    async def on_ready(self) -> None:
        self._watched = set(database.get_watched_channels())
        log.info("Chat logger watching %d channel(s)", len(self._watched))

    @commands.Cog.listener()
    async def on_message(self, message: discord.Message) -> None:
        if message.author.bot:
            return
        if _WATCH_ALL not in self._watched and message.channel.id not in self._watched:
            return
        # In watch-all mode, only log channels in the configured guild
        if _WATCH_ALL in self._watched:
            guild = getattr(message.channel, "guild", None)
            if guild is None or guild.id != config.GUILD_ID:
                return
        if not message.content:
            return

        sent_at = message.created_at.replace(tzinfo=timezone.utc).isoformat()
        database.log_message(
            message_id=message.id,
            channel_id=message.channel.id,
            user_id=message.author.id,
            username=str(message.author),
            content=message.content,
            sent_at=sent_at,
        )

    # ------------------------------------------------------------------
    # Admin commands
    # ------------------------------------------------------------------

    async def _reply(self, ctx: commands.Context, content: str = None, *, embed: discord.Embed = None) -> None:
        """Reply privately via DM so channel management never posts in a public channel.

        In a DM this is identical to a normal reply. If the command was run in a server
        and the user's DMs are closed, fall back to the channel so they still get feedback.
        """
        try:
            await ctx.author.send(content=content, embed=embed)
        except discord.Forbidden:
            if ctx.guild is not None:
                await ctx.send(content=content, embed=embed)

    def _resolve_channel(self, ctx: commands.Context, ref: str) -> discord.TextChannel | None:
        """Resolve a channel from a mention (<#id>), a raw ID, or (in-guild) a name.

        Works in DMs as long as a mention or numeric ID is given. The channel must
        belong to the configured POPG guild.
        """
        ref = ref.strip()
        channel_id = None
        if ref.startswith("<#") and ref.endswith(">") and ref[2:-1].isdigit():
            channel_id = int(ref[2:-1])
        elif ref.isdigit():
            channel_id = int(ref)

        if channel_id is not None:
            ch = self.bot.get_channel(channel_id)
        elif ctx.guild is not None:
            ch = discord.utils.get(ctx.guild.text_channels, name=ref.lstrip("#"))
        else:
            ch = None

        if not isinstance(ch, discord.TextChannel):
            return None
        if ch.guild.id != config.GUILD_ID:
            return None
        return ch

    async def _bad_channel(self, ctx: commands.Context) -> None:
        await self._reply(
            ctx,
            "Could not find that channel in the POPG server. In a DM, use the channel's "
            "numeric ID — enable Developer Mode in Discord, right-click the channel → Copy Channel ID.",
        )

    @commands.group(name="log", invoke_without_command=True)
    async def log_group(self, ctx: commands.Context) -> None:
        """Manage chat channel logging."""
        if not _author_is_admin(ctx):
            await self._reply(ctx, _ADMIN_DENY)
            return
        cmds = (
            "`!log all` — record every channel the bot can see\n"
            "`!log none` — stop watch-all mode\n"
            "`!log watch <#channel|id>` — start recording a channel\n"
            "`!log unwatch <#channel|id>` — stop recording a channel\n"
            "`!log list` — show all recorded channels\n"
            "`!log tail <#channel|id> [n]` — show the last N messages (default 10)\n\n"
            "Works in DMs too — pass the channel's numeric ID (Developer Mode → Copy Channel ID)."
        )
        embed = discord.Embed(title="Chat Logging Commands", description=cmds, color=discord.Color.blue())
        await self._reply(ctx, embed=embed)

    @log_group.command(name="all", aliases=["watchall"])
    async def log_all(self, ctx: commands.Context) -> None:
        """Record every channel the bot can see, including ones created later."""
        if not _author_is_admin(ctx):
            await self._reply(ctx, _ADMIN_DENY)
            return
        if _WATCH_ALL in self._watched:
            await self._reply(ctx, "Already recording all visible channels. Use `!log none` to stop.")
            return
        database.add_watched_channel(_WATCH_ALL, "ALL", ctx.author.id)
        self._watched.add(_WATCH_ALL)
        log.info("Watch-all mode enabled by %s", ctx.author)
        await self._reply(ctx, "Now recording **all channels the bot can see** in the POPG server.")

    @log_group.command(name="none", aliases=["unwatchall"])
    async def log_none(self, ctx: commands.Context) -> None:
        """Turn off watch-all mode. Individually watched channels are kept."""
        if not _author_is_admin(ctx):
            await self._reply(ctx, _ADMIN_DENY)
            return
        if _WATCH_ALL not in self._watched:
            await self._reply(ctx, "Watch-all mode is not enabled.")
            return
        database.remove_watched_channel(_WATCH_ALL)
        self._watched.discard(_WATCH_ALL)
        log.info("Watch-all mode disabled by %s", ctx.author)
        await self._reply(ctx, "Watch-all mode off. Any individually watched channels are still recorded.")

    @log_group.command(name="watch")
    async def log_watch(self, ctx: commands.Context, *, channel_ref: str = None) -> None:
        """Start recording messages in a channel."""
        if not _author_is_admin(ctx):
            await self._reply(ctx, _ADMIN_DENY)
            return
        if not channel_ref:
            await self._reply(ctx, "Please specify a channel: `!log watch <#channel|id>`")
            return
        channel = self._resolve_channel(ctx, channel_ref)
        if channel is None:
            await self._bad_channel(ctx)
            return
        if channel.id in self._watched:
            await self._reply(ctx, f"{channel.mention} is already being recorded.")
            return
        database.add_watched_channel(channel.id, channel.name, ctx.author.id)
        self._watched.add(channel.id)
        log.info("Now watching channel #%s (%d)", channel.name, channel.id)
        await self._reply(ctx, f"Now recording messages in {channel.mention}.")

    @log_group.command(name="unwatch")
    async def log_unwatch(self, ctx: commands.Context, *, channel_ref: str = None) -> None:
        """Stop recording messages in a channel."""
        if not _author_is_admin(ctx):
            await self._reply(ctx, _ADMIN_DENY)
            return
        if not channel_ref:
            await self._reply(ctx, "Please specify a channel: `!log unwatch <#channel|id>`")
            return
        channel = self._resolve_channel(ctx, channel_ref)
        if channel is None:
            await self._bad_channel(ctx)
            return
        if channel.id not in self._watched:
            await self._reply(ctx, f"{channel.mention} is not currently being recorded.")
            return
        database.remove_watched_channel(channel.id)
        self._watched.discard(channel.id)
        log.info("Stopped watching channel #%s (%d)", channel.name, channel.id)
        await self._reply(ctx, f"Stopped recording {channel.mention}. Existing messages are kept in the database.")

    @log_group.command(name="list")
    async def log_list(self, ctx: commands.Context) -> None:
        """List all channels currently being recorded."""
        if not _author_is_admin(ctx):
            await self._reply(ctx, _ADMIN_DENY)
            return
        channels = database.get_watched_channels_detail()
        if not channels:
            await self._reply(ctx, "No channels are currently being recorded. Use `!log watch <#channel|id>` to start.")
            return
        guild = self.bot.get_guild(config.GUILD_ID)
        embed = discord.Embed(title="Recorded Channels", color=discord.Color.blue())
        lines = []
        for ch in channels:
            adder = guild.get_member(ch["added_by"]) if guild else None
            adder_name = adder.display_name if adder else f"ID:{ch['added_by']}"
            if ch["channel_id"] == _WATCH_ALL:
                label = "**ALL visible channels** (watch-all mode)"
            else:
                label = f"<#{ch['channel_id']}>"
            lines.append(f"• {label} — added by {adder_name} on {_fmt_dt(ch['added_at'])}")
        embed.description = "\n".join(lines)
        await self._reply(ctx, embed=embed)

    @log_group.command(name="tail")
    async def log_tail(self, ctx: commands.Context, channel_ref: str = None, limit: int = 10) -> None:
        """Preview the last N recorded messages from a channel."""
        if not _author_is_admin(ctx):
            await self._reply(ctx, _ADMIN_DENY)
            return
        if not channel_ref:
            await self._reply(ctx, "Please specify a channel: `!log tail <#channel|id> [n]`")
            return
        channel = self._resolve_channel(ctx, channel_ref)
        if channel is None:
            await self._bad_channel(ctx)
            return
        limit = max(1, min(limit, 25))
        messages = database.get_recent_messages(channel.id, limit=limit)
        if not messages:
            await self._reply(ctx, f"No recorded messages found for {channel.mention}.")
            return
        embed = discord.Embed(
            title=f"Last {len(messages)} messages — #{channel.name}",
            color=discord.Color.blue(),
        )
        lines = []
        for m in messages:
            ts = _fmt_dt(m["sent_at"])
            content = m["content"][:80] + ("…" if len(m["content"]) > 80 else "")
            lines.append(f"**{m['username']}** [{ts}]: {content}")
        embed.description = "\n".join(lines)
        await self._reply(ctx, embed=embed)


def setup(bot: commands.Bot) -> None:
    bot.add_cog(ChatLogger(bot))
