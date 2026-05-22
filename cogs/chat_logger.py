import logging
from datetime import timezone

import discord
from discord.ext import commands

import database
from cogs.admin import _is_admin
from cogs.profile import _fmt_dt

log = logging.getLogger("popg.chat_logger")


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
        if message.channel.id not in self._watched:
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

    @commands.guild_only()
    @commands.group(name="log", invoke_without_command=True)
    async def log_group(self, ctx: commands.Context) -> None:
        """Manage chat channel logging."""
        if not _is_admin(ctx):
            await ctx.send("You need Administrator permission or the Admin role to use this.")
            return
        cmds = (
            "`!log watch #channel` — start recording a channel\n"
            "`!log unwatch #channel` — stop recording a channel\n"
            "`!log list` — show all recorded channels\n"
            "`!log tail #channel [n]` — show the last N messages (default 10)"
        )
        embed = discord.Embed(title="Chat Logging Commands", description=cmds, color=discord.Color.blue())
        await ctx.send(embed=embed)

    @log_group.command(name="watch")
    async def log_watch(self, ctx: commands.Context, channel: discord.TextChannel) -> None:
        """Start recording messages in a channel."""
        if not _is_admin(ctx):
            await ctx.send("You need Administrator permission or the Admin role to use this.")
            return
        if channel.id in self._watched:
            await ctx.send(f"{channel.mention} is already being recorded.")
            return
        database.add_watched_channel(channel.id, channel.name, ctx.author.id)
        self._watched.add(channel.id)
        log.info("Now watching channel #%s (%d)", channel.name, channel.id)
        await ctx.send(f"Now recording messages in {channel.mention}.")

    @log_group.command(name="unwatch")
    async def log_unwatch(self, ctx: commands.Context, channel: discord.TextChannel) -> None:
        """Stop recording messages in a channel."""
        if not _is_admin(ctx):
            await ctx.send("You need Administrator permission or the Admin role to use this.")
            return
        if channel.id not in self._watched:
            await ctx.send(f"{channel.mention} is not currently being recorded.")
            return
        database.remove_watched_channel(channel.id)
        self._watched.discard(channel.id)
        log.info("Stopped watching channel #%s (%d)", channel.name, channel.id)
        await ctx.send(f"Stopped recording {channel.mention}. Existing messages are kept in the database.")

    @log_group.command(name="list")
    async def log_list(self, ctx: commands.Context) -> None:
        """List all channels currently being recorded."""
        if not _is_admin(ctx):
            await ctx.send("You need Administrator permission or the Admin role to use this.")
            return
        channels = database.get_watched_channels_detail()
        if not channels:
            await ctx.send("No channels are currently being recorded. Use `!log watch #channel` to start.")
            return
        embed = discord.Embed(title="Recorded Channels", color=discord.Color.blue())
        lines = []
        for ch in channels:
            adder = ctx.guild.get_member(ch["added_by"])
            adder_name = adder.display_name if adder else f"ID:{ch['added_by']}"
            lines.append(f"• <#{ch['channel_id']}> — added by {adder_name} on {_fmt_dt(ch['added_at'])}")
        embed.description = "\n".join(lines)
        await ctx.send(embed=embed)

    @log_group.command(name="tail")
    async def log_tail(self, ctx: commands.Context, channel: discord.TextChannel, limit: int = 10) -> None:
        """Preview the last N recorded messages from a channel."""
        if not _is_admin(ctx):
            await ctx.send("You need Administrator permission or the Admin role to use this.")
            return
        limit = max(1, min(limit, 25))
        messages = database.get_recent_messages(channel.id, limit=limit)
        if not messages:
            await ctx.send(f"No recorded messages found for {channel.mention}.")
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
        await ctx.send(embed=embed)

    @log_watch.error
    @log_unwatch.error
    @log_tail.error
    async def channel_arg_error(self, ctx: commands.Context, error: Exception) -> None:
        if isinstance(error, commands.BadArgument):
            await ctx.send("Could not find that channel. Try mentioning it with #.")
        elif isinstance(error, commands.MissingRequiredArgument):
            await ctx.send("Please specify a channel: `!log watch #channel-name`")


def setup(bot: commands.Bot) -> None:
    bot.add_cog(ChatLogger(bot))
