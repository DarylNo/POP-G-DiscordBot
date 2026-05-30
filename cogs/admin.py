from datetime import datetime, timezone

import discord
from discord.ext import commands

import database
from cogs.profile import _fmt_duration, _fmt_dt


def _is_admin(ctx: commands.Context) -> bool:
    # guild_permissions only exists on Member; ctx.author is a plain User in DMs
    perms = getattr(ctx.author, "guild_permissions", None)
    return bool(perms and perms.administrator)


class Admin(commands.Cog):
    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot

    @commands.group(name="admin", invoke_without_command=True)
    async def admin_group(self, ctx: commands.Context) -> None:
        """Admin commands for managing POPG bot data."""
        if not _is_admin(ctx):
            await ctx.send("You need Administrator permission or the Admin role to use this.")
            return
        cmds = (
            "`!admin reset @member` — zero a member's stats\n"
            "`!admin info @member` — raw stat dump\n"
            "`!admin sessions` — show active tracking sessions\n"
            "`!admin reload <cog>` — hot-reload a cog (e.g. `tracking`)"
        )
        embed = discord.Embed(title="POPG Admin Commands", description=cmds, color=discord.Color.red())
        await ctx.send(embed=embed)

    @admin_group.command(name="reset")
    async def admin_reset(self, ctx: commands.Context, member: discord.Member) -> None:
        """Reset all tracked stats for a member."""
        if not _is_admin(ctx):
            await ctx.send("You need Administrator permission or the Admin role to use this.")
            return
        ok = database.reset_user(member.id)
        if ok:
            await ctx.send(f"Stats for **{member.display_name}** have been reset.")
        else:
            await ctx.send(f"No data found for **{member.display_name}**.")

    @admin_group.command(name="info")
    async def admin_info(self, ctx: commands.Context, member: discord.Member) -> None:
        """Show a raw stat dump for a member."""
        if not _is_admin(ctx):
            await ctx.send("You need Administrator permission or the Admin role to use this.")
            return
        stats = database.get_user_stats(member.id)
        if stats is None:
            await ctx.send(f"No data for **{member.display_name}**.")
            return

        embed = discord.Embed(
            title=f"Admin Info — {stats['display_name']}",
            color=discord.Color.red(),
        )
        embed.add_field(name="User ID", value=str(stats["user_id"]), inline=True)
        embed.add_field(name="Username", value=stats["username"], inline=True)
        embed.add_field(name="​", value="​", inline=True)
        embed.add_field(name="First Seen", value=_fmt_dt(stats["first_seen"]), inline=True)
        embed.add_field(name="Last Seen", value=_fmt_dt(stats["last_seen"]), inline=True)
        embed.add_field(name="​", value="​", inline=True)
        embed.add_field(name="Online Seconds", value=str(stats["total_online_seconds"]), inline=True)
        embed.add_field(name="Gaming Seconds", value=str(stats["total_gaming_seconds"]), inline=True)
        embed.add_field(name="Voice Seconds", value=str(stats["total_voice_seconds"]), inline=True)

        if stats["top_games"]:
            game_lines = [f"• {g['game_name']}: {g['total_seconds']}s ({g['session_count']} sessions)" for g in stats["top_games"]]
            embed.add_field(name="Top Games (raw)", value="\n".join(game_lines), inline=False)

        await ctx.send(embed=embed)

    @admin_group.command(name="sessions")
    async def admin_sessions(self, ctx: commands.Context) -> None:
        """List all currently active tracking sessions."""
        if not _is_admin(ctx):
            await ctx.send("You need Administrator permission or the Admin role to use this.")
            return

        sessions = database.get_active_sessions()
        if not sessions:
            await ctx.send("No active sessions right now.")
            return

        embed = discord.Embed(
            title="Active Tracking Sessions",
            color=discord.Color.red(),
        )
        lines = []
        now = datetime.now(timezone.utc)
        for s in sessions:
            member = ctx.guild.get_member(s["user_id"])
            name = member.display_name if member else f"ID:{s['user_id']}"
            started = datetime.fromisoformat(s["started_at"])
            elapsed = int((now - started).total_seconds())
            detail = f" ({s['game_name']})" if s["game_name"] else ""
            channel = f" in <#{s['voice_channel_id']}>" if s["voice_channel_id"] else ""
            lines.append(f"• **{name}** — `{s['session_type']}`{detail}{channel} for {_fmt_duration(elapsed)}")

        embed.description = "\n".join(lines)
        await ctx.send(embed=embed)

    @admin_group.command(name="reload")
    async def admin_reload(self, ctx: commands.Context, cog_name: str) -> None:
        """Reload a cog without restarting the bot. Use the short name, e.g. tracking."""
        if not _is_admin(ctx):
            await ctx.send("You need Administrator permission to use this.")
            return
        full_name = cog_name if "." in cog_name else f"cogs.{cog_name}"
        try:
            await self.bot.reload_extension(full_name)
            await ctx.send(f"Reloaded `{full_name}`.")
        except commands.ExtensionNotLoaded:
            await ctx.send(f"`{full_name}` is not loaded. Use `!admin load {cog_name}` instead.")
        except Exception as e:
            await ctx.send(f"Failed to reload `{full_name}`: {e}")

    @admin_reset.error
    @admin_info.error
    async def member_arg_error(self, ctx: commands.Context, error: Exception) -> None:
        if isinstance(error, commands.BadArgument):
            await ctx.send("Could not find that member. Try mentioning them with @.")
        elif isinstance(error, commands.MissingRequiredArgument):
            await ctx.send("Please mention a member: `!admin reset @member`")


def setup(bot: commands.Bot) -> None:
    bot.add_cog(Admin(bot))
