from datetime import datetime, timezone

import discord
from discord.ext import commands

import database


def _fmt_duration(seconds: int) -> str:
    """Convert seconds to a human-readable string like '3h 42m'."""
    if seconds < 60:
        return f"{seconds}s"
    minutes, secs = divmod(seconds, 60)
    hours, mins = divmod(minutes, 60)
    days, hrs = divmod(hours, 24)
    if days:
        return f"{days}d {hrs}h {mins}m"
    if hours:
        return f"{hours}h {mins}m"
    return f"{minutes}m {secs}s"


def _fmt_dt(iso: str) -> str:
    """Format an ISO datetime string as a readable date."""
    try:
        dt = datetime.fromisoformat(iso)
        return dt.strftime("%b %d, %Y")
    except (ValueError, TypeError):
        return "Unknown"


def _fmt_relative(iso: str) -> str:
    """Return a human-readable relative time string like '2h ago' or 'just now'."""
    try:
        dt = datetime.fromisoformat(iso)
        delta = int((datetime.now(timezone.utc) - dt).total_seconds())
    except (ValueError, TypeError):
        return "Unknown"
    if delta < 60:
        return "just now"
    if delta < 3600:
        return f"{delta // 60}m ago"
    if delta < 86400:
        return f"{delta // 3600}h ago"
    if delta < 30 * 86400:
        return f"{delta // 86400}d ago"
    return _fmt_dt(iso)


def build_profile_embed(member: discord.Member, stats: dict) -> discord.Embed:
    embed = discord.Embed(
        title=f"POPG Profile — {stats['display_name']}",
        color=discord.Color.blurple(),
    )
    if member.avatar:
        embed.set_thumbnail(url=member.avatar.url)

    try:
        first_dt = datetime.fromisoformat(stats["first_seen"])
        days_member = (datetime.now(timezone.utc) - first_dt).days
        member_since = f"{_fmt_dt(stats['first_seen'])} ({days_member}d)"
    except (ValueError, TypeError):
        member_since = "Unknown"

    embed.add_field(name="Member Since", value=member_since, inline=True)
    embed.add_field(name="Last Active", value=_fmt_relative(stats["last_seen"]), inline=True)
    embed.add_field(name="​", value="​", inline=True)

    desktop = stats.get("total_desktop_seconds", 0)
    mobile = stats.get("total_mobile_seconds", 0)
    online_detail = f"{_fmt_duration(stats['total_online_seconds'])}\n🖥️ {_fmt_duration(desktop)}  📱 {_fmt_duration(mobile)}"
    embed.add_field(name="Online Time", value=online_detail, inline=True)
    embed.add_field(
        name="Gaming Time", value=_fmt_duration(stats["total_gaming_seconds"]), inline=True
    )
    embed.add_field(
        name="Voice Time", value=_fmt_duration(stats["total_voice_seconds"]), inline=True
    )

    if stats["top_games"]:
        lines = []
        for i, g in enumerate(stats["top_games"], 1):
            lines.append(f"{i}. **{g['game_name']}** — {_fmt_duration(g['total_seconds'])} ({g['session_count']} session{'s' if g['session_count'] != 1 else ''})")
        embed.add_field(name="Top Games", value="\n".join(lines), inline=False)
    else:
        embed.add_field(name="Top Games", value="No games tracked yet.", inline=False)

    embed.set_footer(text="Past our Prime Gamers")
    return embed


class Profile(commands.Cog):
    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot

    @commands.guild_only()
    @commands.cooldown(1, 5, commands.BucketType.user)
    @commands.command(name="profile", aliases=["stats"])
    async def profile(self, ctx: commands.Context, member: discord.Member = None) -> None:
        """Show activity stats for yourself or another member."""
        target = member or ctx.author
        stats = database.get_user_stats(target.id)
        if stats is None:
            await ctx.send(f"No data found for **{target.display_name}**. They may need to be online while the bot is running.")
            return
        embed = build_profile_embed(target, stats)
        await ctx.send(embed=embed)

    @profile.error
    async def profile_error(self, ctx: commands.Context, error: Exception) -> None:
        if isinstance(error, commands.BadArgument):
            await ctx.send("Could not find that member. Try mentioning them with @.")


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(Profile(bot))
