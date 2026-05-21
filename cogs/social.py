import discord
from discord.ext import commands

import database
from cogs.profile import _fmt_duration, _guild_member

MEDALS = {1: "🥇", 2: "🥈", 3: "🥉"}

# Hour strip label line — positions align with the 24-char strip (one char per hour)
_HOUR_STRIP_LABEL = "12a" + " " * 9 + "12p" + " " * 6 + "11p"
_DAY_LABELS = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]


def _density(count: int, max_count: int) -> str:
    """Map a count to a 5-level density character."""
    if max_count == 0 or count == 0:
        return "·"
    pct = count / max_count
    if pct <= 0.25:
        return "░"
    if pct <= 0.50:
        return "▒"
    if pct <= 0.75:
        return "▓"
    return "█"


def _build_heatmap_block(hours: list[int], days: list[int]) -> str:
    hour_max = max(hours) if any(hours) else 1
    day_max = max(days) if any(days) else 1

    strip = "".join(_density(h, hour_max) for h in hours)
    day_lines = [
        f"{label} {'█' * round(c / day_max * 12) if c else '·'}"
        for label, c in zip(_DAY_LABELS, days)
    ]

    return (
        f"Hour of Day (UTC)\n"
        f"{_HOUR_STRIP_LABEL}\n"
        f"{strip}\n"
        f"\n"
        f"Day of Week\n"
        + "\n".join(day_lines)
    )


class Social(commands.Cog):
    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot

    @commands.cooldown(1, 5, commands.BucketType.user)
    @commands.command(name="accomplices", aliases=["partners"])
    async def accomplices(self, ctx: commands.Context, member: discord.Member = None) -> None:
        """Show who you (or another member) play games with most."""
        if ctx.guild is None:
            target = _guild_member(ctx)
            if target is None:
                await ctx.send("You need to be a member of the POPG server to use this command.")
                return
        else:
            target = member or ctx.author
        rows = database.get_accomplices(target.id, limit=5)

        embed = discord.Embed(
            title=f"Known Accomplices — {target.display_name}",
            color=discord.Color.red(),
        )
        if not rows:
            embed.description = "No gaming partners tracked yet. Play the same game as someone else to build history."
        else:
            lines = []
            for rank, row in enumerate(rows, 1):
                medal = MEDALS.get(rank, f"`{rank}.`")
                lines.append(
                    f"{medal} **{row['display_name']}** — {_fmt_duration(row['total_seconds'])} together "
                    f"({row['session_count']} session{'s' if row['session_count'] != 1 else ''})"
                )
            embed.description = "\n".join(lines)

        embed.set_footer(text="Past our Prime Gamers")
        await ctx.send(embed=embed)

    @accomplices.error
    async def accomplices_error(self, ctx: commands.Context, error: Exception) -> None:
        if isinstance(error, commands.BadArgument):
            await ctx.send("Could not find that member. Try mentioning them with @.")

    @commands.cooldown(1, 5, commands.BucketType.user)
    @commands.command(name="crew")
    async def crew(self, ctx: commands.Context, member: discord.Member = None) -> None:
        """Show who you (or another member) spend the most voice time with."""
        if ctx.guild is None:
            target = _guild_member(ctx)
            if target is None:
                await ctx.send("You need to be a member of the POPG server to use this command.")
                return
        else:
            target = member or ctx.author
        rows = database.get_voice_crew(target.id, limit=5)

        embed = discord.Embed(
            title=f"Voice Crew — {target.display_name}",
            color=discord.Color.purple(),
        )
        if not rows:
            embed.description = "No voice crew data yet. Spend time in voice channels with others to build history."
        else:
            lines = []
            for rank, row in enumerate(rows, 1):
                medal = MEDALS.get(rank, f"`{rank}.`")
                lines.append(
                    f"{medal} **{row['display_name']}** — {_fmt_duration(row['total_seconds'])} together "
                    f"({row['session_count']} session{'s' if row['session_count'] != 1 else ''})"
                )
            embed.description = "\n".join(lines)

        embed.set_footer(text="Past our Prime Gamers")
        await ctx.send(embed=embed)

    @crew.error
    async def crew_error(self, ctx: commands.Context, error: Exception) -> None:
        if isinstance(error, commands.BadArgument):
            await ctx.send("Could not find that member. Try mentioning them with @.")

    @commands.cooldown(1, 5, commands.BucketType.user)
    @commands.command(name="achievements", aliases=["badges", "ach"])
    async def achievements(self, ctx: commands.Context, member: discord.Member = None) -> None:
        """Show earned and locked achievements for yourself or another member."""
        if ctx.guild is None:
            target = _guild_member(ctx)
            if target is None:
                await ctx.send("You need to be a member of the POPG server to use this command.")
                return
        else:
            target = member or ctx.author

        all_achievements = database.get_achievements(target.id)
        earned = [a for a in all_achievements if a["earned"]]
        locked = [a for a in all_achievements if not a["earned"]]

        embed = discord.Embed(
            title=f"Achievements — {target.display_name}",
            description=f"**{len(earned)}/{len(all_achievements)}** badges earned",
            color=discord.Color.gold(),
        )
        if target.avatar:
            embed.set_thumbnail(url=target.avatar.url)

        if earned:
            lines = [f"{a['emoji']} **{a['name']}** — {a['description']}" for a in earned]
            embed.add_field(name=f"Earned ({len(earned)})", value="\n".join(lines), inline=False)

        if locked:
            lines = [f"{a['emoji']} {a['name']} — {a['description']}" for a in locked]
            embed.add_field(name=f"Locked ({len(locked)})", value="\n".join(lines), inline=False)

        embed.set_footer(text="Past our Prime Gamers")
        await ctx.send(embed=embed)

    @achievements.error
    async def achievements_error(self, ctx: commands.Context, error: Exception) -> None:
        if isinstance(error, commands.BadArgument):
            await ctx.send("Could not find that member. Try mentioning them with @.")

    @commands.cooldown(1, 5, commands.BucketType.user)
    @commands.command(name="heatmap", aliases=["when", "schedule"])
    async def heatmap(self, ctx: commands.Context, member: discord.Member = None) -> None:
        """Show when a member is typically active, by hour and day of week."""
        if ctx.guild is None:
            target = _guild_member(ctx)
            if target is None:
                await ctx.send("You need to be a member of the POPG server to use this command.")
                return
        else:
            target = member or ctx.author

        data = database.get_activity_heatmap(target.id)

        embed = discord.Embed(
            title=f"Activity Heatmap — {target.display_name}",
            color=discord.Color.teal(),
        )
        if target.avatar:
            embed.set_thumbnail(url=target.avatar.url)

        if data["total"] == 0:
            embed.description = "No session history yet — check back after they've been online a while."
            embed.set_footer(text="Past our Prime Gamers")
            await ctx.send(embed=embed)
            return

        block = _build_heatmap_block(data["hours"], data["days"])
        embed.description = f"```{block}```"
        embed.set_footer(text=f"Based on {data['total']} sessions · · none  ░▒▓█ low→peak · Past our Prime Gamers")
        await ctx.send(embed=embed)

    @heatmap.error
    async def heatmap_error(self, ctx: commands.Context, error: Exception) -> None:
        if isinstance(error, commands.BadArgument):
            await ctx.send("Could not find that member. Try mentioning them with @.")


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(Social(bot))
