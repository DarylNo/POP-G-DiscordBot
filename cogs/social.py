import discord
from discord.ext import commands

import database
from cogs.profile import _fmt_duration, _guild_member

MEDALS = {1: "🥇", 2: "🥈", 3: "🥉"}


def _partner_lines(rows: list[dict], time_key: str) -> str:
    lines = []
    for rank, row in enumerate(rows, 1):
        medal = MEDALS.get(rank, f"`{rank}.`")
        count = row["session_count"]
        lines.append(
            f"{medal} **{row['display_name']}** — {_fmt_duration(row[time_key])} together "
            f"({count} session{'s' if count != 1 else ''})"
        )
    return "\n".join(lines)


async def _resolve_target(ctx: commands.Context, member_name: str | None):
    """Resolve the target member, handling DMs and failed name lookups.

    Returns the Member, or None if an error message was already sent to the user.
    Using a manual MemberConverter (rather than a `discord.Member` param) ensures
    a failed lookup reports an error instead of silently falling back to ctx.author.
    """
    if ctx.guild is None:
        target = _guild_member(ctx)
        if target is None:
            await ctx.send("You need to be a member of the POPG server to use this command.")
            return None
        return target
    if member_name:
        try:
            return await commands.MemberConverter().convert(ctx, member_name)
        except commands.BadArgument:
            await ctx.send("Could not find that member. Try mentioning them with @.")
            return None
    return ctx.author


class Social(commands.Cog):
    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot

    @commands.cooldown(1, 5, commands.BucketType.user)
    @commands.command(name="accomplices", aliases=["partners"])
    async def accomplices(self, ctx: commands.Context, *, member_name: str = None) -> None:
        """Show who you (or another member) play games with most."""
        target = await _resolve_target(ctx, member_name)
        if target is None:
            return
        rows = database.get_accomplices(target.id, limit=5)

        embed = discord.Embed(
            title=f"Known Accomplices — {target.display_name}",
            color=discord.Color.red(),
        )
        if not rows:
            embed.description = "No gaming partners tracked yet. Play the same game as someone else to build history."
        else:
            embed.description = _partner_lines(rows, "total_seconds")

        embed.set_footer(text="Past our Prime Gamers")
        await ctx.send(embed=embed)

    @accomplices.error
    async def accomplices_error(self, ctx: commands.Context, error: Exception) -> None:
        if isinstance(error, commands.BadArgument):
            await ctx.send("Could not find that member. Try mentioning them with @.")

    @commands.cooldown(1, 5, commands.BucketType.user)
    @commands.command(name="crew")
    async def crew(self, ctx: commands.Context, *, member_name: str = None) -> None:
        """Show who you (or another member) spend the most voice time with."""
        target = await _resolve_target(ctx, member_name)
        if target is None:
            return
        rows = database.get_voice_crew(target.id, limit=5)

        embed = discord.Embed(
            title=f"Voice Crew — {target.display_name}",
            color=discord.Color.purple(),
        )
        if not rows:
            embed.description = "No voice crew data yet. Spend time in voice channels with others to build history."
        else:
            embed.description = _partner_lines(rows, "total_seconds")

        embed.set_footer(text="Past our Prime Gamers")
        await ctx.send(embed=embed)

    @crew.error
    async def crew_error(self, ctx: commands.Context, error: Exception) -> None:
        if isinstance(error, commands.BadArgument):
            await ctx.send("Could not find that member. Try mentioning them with @.")


def setup(bot: commands.Bot) -> None:
    bot.add_cog(Social(bot))
