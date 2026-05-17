import discord
from discord.ext import commands

import database
from cogs.profile import _fmt_duration

CATEGORIES = {
    "online": ("total_online_seconds", "Online Time", discord.Color.green()),
    "gaming": ("total_gaming_seconds", "Gaming Time", discord.Color.orange()),
    "voice":  ("total_voice_seconds",  "Voice Time",  discord.Color.purple()),
}

MEDALS = {1: "🥇", 2: "🥈", 3: "🥉"}


class Leaderboard(commands.Cog):
    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot

    @commands.guild_only()
    @commands.cooldown(1, 5, commands.BucketType.user)
    @commands.command(name="leaderboard", aliases=["lb", "top"])
    async def leaderboard(self, ctx: commands.Context, category: str = "online") -> None:
        """Show the top 10 members by online, gaming, or voice time.

        Usage:
          !leaderboard          — online time (default)
          !leaderboard gaming   — gaming time
          !leaderboard voice    — voice time
        """
        category = category.lower()
        if category not in CATEGORIES:
            await ctx.send(f"Unknown category `{category}`. Choose from: `online`, `gaming`, `voice`.")
            return

        col, label, color = CATEGORIES[category]
        rows = database.get_leaderboard(category, limit=10)

        embed = discord.Embed(
            title=f"POPG Leaderboard — {label}",
            color=color,
        )

        if not rows:
            embed.description = "No data recorded yet. Get gaming!"
        else:
            lines = []
            for rank, row in enumerate(rows, 1):
                medal = MEDALS.get(rank, f"`{rank}.`")
                name = row["display_name"] or "Unknown"
                score = _fmt_duration(row["score"])
                lines.append(f"{medal} **{name}** — {score}")
            embed.description = "\n".join(lines)

        if category == "gaming":
            top_games = database.get_top_games(limit=5)
            if top_games:
                game_lines = []
                for rank, g in enumerate(top_games, 1):
                    medal = MEDALS.get(rank, f"`{rank}.`")
                    game_lines.append(f"{medal} **{g['game_name']}** — {_fmt_duration(g['total_seconds'])}")
                embed.add_field(name="Top Games", value="\n".join(game_lines), inline=False)

        embed.set_footer(text="Past our Prime Gamers")
        await ctx.send(embed=embed)

    @leaderboard.error
    async def leaderboard_error(self, ctx: commands.Context, error: Exception) -> None:
        await ctx.send("Usage: `!leaderboard [online|gaming|voice]`")


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(Leaderboard(bot))
