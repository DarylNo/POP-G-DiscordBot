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

_BAR_WIDTH = 12


def _bar(score: int, top: int) -> str:
    """Return a proportional block-bar for `score` relative to the top score."""
    if top <= 0 or score <= 0:
        return ""
    filled = max(1, round(score / top * _BAR_WIDTH))
    return "█" * filled


def _ranked_lines(rows: list[dict], score_key: str, *, suffix_key: str = None) -> str:
    """Render ranked rows with medal, name, a relative bar, and the formatted time.

    `rows` must be sorted descending by `score_key`. If `suffix_key` is given,
    that field is appended in parentheses as a session count.
    """
    top = max((r[score_key] for r in rows), default=0)
    lines = []
    for rank, row in enumerate(rows, 1):
        medal = MEDALS.get(rank, f"`{rank}.`")
        bar = _bar(row[score_key], top)
        bar_part = f" `{bar}`" if bar else ""
        line = f"{medal} **{row['display_name']}**{bar_part} {_fmt_duration(row[score_key])}"
        if suffix_key is not None:
            count = row[suffix_key]
            line += f" ({count} session{'s' if count != 1 else ''})"
        lines.append(line)
    return "\n".join(lines)


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
        """Show leaderboards by time period and category.

        Usage:
          !leaderboard               — online time this week (default)
          !leaderboard gaming        — gaming time this week
          !leaderboard month         — online time this month
          !leaderboard month gaming  — gaming time this month
          !leaderboard all           — all-time online time
          !leaderboard all gaming    — all-time gaming time
          !leaderboard Battlefield   — per-game leaderboard
        """
        if ctx.guild is None and _guild_member(ctx) is None:
            await ctx.send("You need to be a member of the POPG server to use this command.")
            return

        invoker_id = ctx.author.id
        first = args[0].lower() if args else "online"

        # Determine scope (week / month / all) from first arg
        if first in ("all", "month", "week", "weekly"):
            scope = "all" if first == "all" else ("month" if first == "month" else "week")
            category = args[1].lower() if len(args) > 1 else "online"
        else:
            scope = "week"
            category = first

        if category not in CATEGORIES:
            if scope != "week":
                await ctx.send(f"Usage: `!leaderboard {scope} [online|gaming|voice]`")
                return
            # Per-game fuzzy lookup (week scope only)
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
                embed.description = _ranked_lines(rows, "total_seconds", suffix_key="session_count")
            embed.set_footer(text=_rank_footer(rows, invoker_id, 10, "Past our Prime Gamers"))
            await ctx.send(embed=embed)
            return

        _, label, color = CATEGORIES[category]

        if scope == "all":
            all_rows = database.get_leaderboard(category, limit=100)
            title = f"POPG Leaderboard — {label}"
            footer_suffix = "All-time · This week: !leaderboard"
        elif scope == "month":
            all_rows = database.get_monthly_leaderboard(category, limit=100)
            title = f"POPG — {label} This Month"
            footer_suffix = "Resets 1st · All-time: !leaderboard all"
        else:
            all_rows = database.get_weekly_leaderboard(category, limit=100)
            title = f"POPG — {label} This Week"
            footer_suffix = "Resets Monday · This month: !leaderboard month · All-time: !leaderboard all"

        embed = discord.Embed(title=title, color=color)
        rows = all_rows[:10]

        if not rows:
            embed.description = "No activity recorded yet. Get gaming!"
        else:
            embed.description = _ranked_lines(rows, "score")

        if scope == "all" and category == "gaming":
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
        await ctx.send("Usage: `!leaderboard [week|month|all] [online|gaming|voice]`")

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
            embed.description = _ranked_lines(rows, "score")

        embed.set_footer(text=_rank_footer(
            all_rows, ctx.author.id, 10,
            "Resets Monday · This month: !leaderboard month · All-time: !leaderboard all"
        ))
        await ctx.send(embed=embed)

    @weekly.error
    async def weekly_error(self, ctx: commands.Context, error: Exception) -> None:
        await ctx.send("Usage: `!weekly [online|gaming|voice]`")

    @commands.cooldown(1, 5, commands.BucketType.user)
    @commands.command(name="monthly", aliases=["month"])
    async def monthly(self, ctx: commands.Context, category: str = "online") -> None:
        """Show this month's top members by online, gaming, or voice time."""
        if ctx.guild is None and _guild_member(ctx) is None:
            await ctx.send("You need to be a member of the POPG server to use this command.")
            return
        category = category.lower()
        if category not in CATEGORIES:
            await ctx.send("Choose a category: `online`, `gaming`, or `voice`.")
            return

        _, label, color = CATEGORIES[category]
        all_rows = database.get_monthly_leaderboard(category, limit=100)

        embed = discord.Embed(title=f"POPG — {label} This Month", color=color)
        rows = all_rows[:10]

        if not rows:
            embed.description = "No activity recorded this month yet."
        else:
            embed.description = _ranked_lines(rows, "score")

        embed.set_footer(text=_rank_footer(
            all_rows, ctx.author.id, 10,
            "Resets 1st · All-time: !leaderboard all"
        ))
        await ctx.send(embed=embed)

    @monthly.error
    async def monthly_error(self, ctx: commands.Context, error: Exception) -> None:
        await ctx.send("Usage: `!monthly [online|gaming|voice]`")


def setup(bot: commands.Bot) -> None:
    bot.add_cog(Leaderboard(bot))
