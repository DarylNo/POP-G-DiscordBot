import discord
from discord.ext import commands

import database
from cogs.profile import _fmt_duration, _guild_member

CATEGORIES = {
    "online":  ("total_online_seconds", "Online Time",  discord.Color.green()),
    "gaming":  ("total_gaming_seconds", "Gaming Time",  discord.Color.orange()),
    "voice":   ("total_voice_seconds",  "Voice Time",   discord.Color.purple()),
}

MEDALS = {1: "🥇", 2: "🥈", 3: "🥉"}


def _rank_footer(all_rows: list[dict], invoker_id: int, limit: int, suffix: str) -> str:
    """Return footer text, prepending 'You're #N' when invoker is outside the displayed top N."""
    rank = next((i for i, r in enumerate(all_rows, 1) if r["user_id"] == invoker_id), None)
    if rank and rank > limit:
        return f"You're #{rank} · {suffix}"
    return f"Past our Prime Gamers · {suffix}"


class Leaderboard(commands.Cog):
    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot

    @commands.cooldown(1, 5, commands.BucketType.user)
    @commands.command(name="leaderboard", aliases=["lb", "top"])
    async def leaderboard(self, ctx: commands.Context, *args: str) -> None:
        """Show this week's top members. Use 'all' for all-time, or a game name.

        Usage:
          !leaderboard               — online time this week (default)
          !leaderboard gaming        — gaming time this week
          !leaderboard all           — all-time online time
          !leaderboard all gaming    — all-time gaming time
          !leaderboard Battlefield   — per-game leaderboard
        """
        if ctx.guild is None and _guild_member(ctx) is None:
            await ctx.send("You need to be a member of the POPG server to use this command.")
            return

        all_time = len(args) > 0 and args[0].lower() == "all"
        if all_time:
            category = args[1].lower() if len(args) > 1 else "online"
        else:
            category = args[0].lower() if args else "online"

        invoker_id = ctx.author.id

        if category not in CATEGORIES:
            if all_time:
                await ctx.send("Usage: `!leaderboard all [online|gaming|voice]`")
                return
            matched_game, rows = database.get_leaderboard_for_game(category)
            if not matched_game:
                await ctx.send(
                    f"Unknown category or game `{category}`. "
                    "Try `online`, `gaming`, `voice`, or a game name like `!leaderboard Battlefield`."
                )
                return
            embed = discord.Embed(title=f"POPG — {matched_game}", color=discord.Color.gold())
            if not rows:
                embed.description = "No playtime recorded for this game yet."
            else:
                lines = []
                for rank, row in enumerate(rows, 1):
                    medal = MEDALS.get(rank, f"`{rank}.`")
                    sessions = row["session_count"]
                    lines.append(
                        f"{medal} **{row['display_name']}** — {_fmt_duration(row['total_seconds'])} "
                        f"({sessions} session{'s' if sessions != 1 else ''})"
                    )
                embed.description = "\n".join(lines)
            embed.set_footer(text=_rank_footer(rows, invoker_id, 10, "Past our Prime Gamers"))
            await ctx.send(embed=embed)
            return

        _, label, color = CATEGORIES[category]

        if all_time:
            all_rows = database.get_leaderboard(category, limit=100)
            title = f"POPG Leaderboard — {label}"
            footer_suffix = "All-time · This week: !leaderboard"
        else:
            all_rows = database.get_weekly_leaderboard(category, limit=100)
            title = f"POPG — {label} This Week"
            footer_suffix = "Resets Monday · All-time: !leaderboard all"

        embed = discord.Embed(title=title, color=color)
        rows = all_rows[:10]

        if not rows:
            embed.description = "No activity recorded yet. Get gaming!"
        else:
            lines = []
            for rank, row in enumerate(rows, 1):
                medal = MEDALS.get(rank, f"`{rank}.`")
                lines.append(f"{medal} **{row['display_name']}** — {_fmt_duration(row['score'])}")
            embed.description = "\n".join(lines)

        if all_time and category == "gaming":
            top_games = database.get_top_games(limit=5)
            if top_games:
                game_lines = [
                    f"{MEDALS.get(i, f'`{i}.`')} **{g['game_name']}** — {_fmt_duration(g['total_seconds'])}"
                    for i, g in enumerate(top_games, 1)
                ]
                embed.add_field(name="Top Games", value="\n".join(game_lines), inline=False)

        embed.set_footer(text=_rank_footer(all_rows, invoker_id, 10, footer_suffix))
        await ctx.send(embed=embed)

    @leaderboard.error
    async def leaderboard_error(self, ctx: commands.Context, error: Exception) -> None:
        await ctx.send("Usage: `!leaderboard [online|gaming|voice]` or `!leaderboard all [category]`")

    @commands.cooldown(1, 5, commands.BucketType.user)
    @commands.command(name="weekly", aliases=["week"])
    async def weekly(self, ctx: commands.Context, category: str = "online") -> None:
        """Show this week's top members by online, gaming, or voice time."""
        if ctx.guild is None and _guild_member(ctx) is None:
            await ctx.send("You need to be a member of the POPG server to use this command.")
            return
        category = category.lower()
        if category not in CATEGORIES:
            await ctx.send("Choose a category: `online`, `gaming`, or `voice`.")
            return

        _, label, color = CATEGORIES[category]
        all_rows = database.get_weekly_leaderboard(category, limit=100)

        embed = discord.Embed(title=f"POPG — {label} This Week", color=color)
        rows = all_rows[:10]

        if not rows:
            embed.description = "No activity recorded this week yet."
        else:
            lines = []
            for rank, row in enumerate(rows, 1):
                medal = MEDALS.get(rank, f"`{rank}.`")
                lines.append(f"{medal} **{row['display_name']}** — {_fmt_duration(row['score'])}")
            embed.description = "\n".join(lines)

        embed.set_footer(text=_rank_footer(all_rows, ctx.author.id, 10, "Resets Monday · All-time: !leaderboard all"))
        await ctx.send(embed=embed)

    @weekly.error
    async def weekly_error(self, ctx: commands.Context, error: Exception) -> None:
        await ctx.send("Usage: `!weekly [online|gaming|voice]`")


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(Leaderboard(bot))
