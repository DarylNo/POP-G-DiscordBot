import discord
from discord.ext import commands

from cogs.admin import _is_admin

USER_COMMANDS = """
**Stats**
`!profile [@member]` — Full stat dump: time, streak, badges, top games, accomplices, crew

**Leaderboards**
`!leaderboard [online|gaming|voice]` — This week's top 10 (default: online)
`!leaderboard month [online|gaming|voice]` — This month's top 10
`!leaderboard all [online|gaming|voice]` — All-time top 10
`!leaderboard <game name>` — Top players for a specific game
`!weekly [online|gaming|voice]` — Shortcut for this week's leaderboard
`!monthly [online|gaming|voice]` — Shortcut for this month's leaderboard

**Social**
`!accomplices [@member]` — Top gaming partners (who you play games with most)
`!crew [@member]` — Top voice crew (who you hang out in voice with most)
`!achievements [@member]` — Earned badges and locked ones still to unlock

**Other**
`!ping` — Check bot latency
`!help` — Show this message
""".strip()

ADMIN_COMMANDS = """
**Admin**
`!admin sessions` — All active tracking sessions
`!admin info @member` — Raw stat dump
`!admin reset @member` — Zero out a member's stats
`!admin reload <cog>` — Reload a cog without restarting
`!log watch #channel` — Start logging a channel
`!log unwatch #channel` — Stop logging a channel
`!log list` — Show watched channels
`!log tail #channel` — Show last 20 logged messages
""".strip()


class Utility(commands.Cog):
    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot

    @commands.command(name="ping")
    async def ping(self, ctx: commands.Context) -> None:
        """Check if the bot is alive and show its latency."""
        latency_ms = round(self.bot.latency * 1000)
        await ctx.send(f"Pong! `{latency_ms}ms`")

    @commands.command(name="help", aliases=["commands"])
    async def help(self, ctx: commands.Context) -> None:
        """Show all available commands."""
        embed = discord.Embed(
            title="POPG Bot Commands",
            description=USER_COMMANDS,
            color=discord.Color.blurple(),
        )
        if ctx.guild and _is_admin(ctx):
            embed.add_field(name="​", value=ADMIN_COMMANDS, inline=False)
        embed.set_footer(text="Past our Prime Gamers")
        await ctx.send(embed=embed)


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(Utility(bot))
