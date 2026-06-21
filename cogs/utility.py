import logging

import discord
from discord.ext import commands

import config
import database
from cogs.admin import _is_admin

log = logging.getLogger("popg.utility")

_USER_COMMANDS_TOP = """
**Stats**
`!profile [@member]` — Full stat dump: time, streak, top games, gaming partners, voice crew

**Leaderboards**
`!leaderboard [online|gaming|voice]` — This week's top 10 (default: online)
`!leaderboard month [online|gaming|voice]` — This month's top 10
`!leaderboard all [online|gaming|voice]` — All-time top 10
`!leaderboard <game name>` — Top players for a specific game
`!weekly [online|gaming|voice]` — Shortcut for this week's leaderboard
`!monthly [online|gaming|voice]` — Shortcut for this month's leaderboard

**Predictions**""".strip()

_WHEN_LINE = "`!when [@member]` — AI prediction of when they'll next be online"

_USER_COMMANDS_BOTTOM = """
**Ask the AI**
`!chat <message>` — Chat with Toaster (alias `!ask`)
`!memorybuild` — Rebuild Toaster's memory from all stored transcripts
`!reset` — Clear your chat history with Toaster

**DM Conversations**
Message me directly (no `!` prefix) for a continuous chat — I'll remember the conversation.

**Voice Recap**
`!recap [id]` — AI summary of last (or specific) voice session
`!recap redo [id]` — Force-regenerate the summary
`!transcript [id]` — Full attributed transcript with timestamps
`!sessions` — List recent voice recording sessions

**Other**
`!ping` — Check bot latency and version
`!help` — Show this message

━━━━━━━━━━━━━━━━━━━━━━━━
**AI**

**Qwen 2.5 14B**
`!chat`, DMs, `!recap`, `!when`, `!memorybuild`, memory extraction, transcript summarization

**Whisper**
Voice transcription (`!join` / `!leave`)""".strip()

ADMIN_VOICE_COMMANDS = """
**Voice Recording (Admin)**
`!join [#channel]` — Bot joins voice and starts recording
`!leave` — Stop recording and process transcript + summary
""".strip()

ADMIN_COMMANDS = """
**Admin**
`!admin sessions` — All active tracking sessions
`!admin info @member` — Raw stat dump
`!admin reset @member` — Zero out a member's stats
`!admin reload <cog>` — Reload a cog without restarting
`!log all` — Log every channel the bot can see
`!log none` — Stop watch-all mode
`!log watch <#channel|id>` — Start logging a channel
`!log unwatch <#channel|id>` — Stop logging a channel
`!log list` — Show watched channels
`!log tail <#channel|id> [n]` — Show recent logged messages
(DM the bot these commands with a channel ID to manage logging privately.)
""".strip()


class Utility(commands.Cog):
    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot

    @commands.command(name="ping")
    async def ping(self, ctx: commands.Context) -> None:
        """Check if the bot is alive and show its latency."""
        latency_ms = round(self.bot.latency * 1000)
        await ctx.send(f"Pong! `{latency_ms}ms` — v{config.VERSION}")

    @commands.command(name="help", aliases=["commands"])
    async def help(self, ctx: commands.Context) -> None:
        """Show all available commands."""
        log.info("help invoked by %s (DM=%s)", ctx.author, ctx.guild is None)

        social_section = _USER_COMMANDS_TOP
        if len(database.get_session_history(ctx.author.id, days=60)) >= 5:
            social_section += "\n" + _WHEN_LINE

        description = social_section + "\n\n" + _USER_COMMANDS_BOTTOM

        embed = discord.Embed(
            title="POPG Bot Commands",
            description=description,
            color=discord.Color.blurple(),
        )
        if ctx.guild and _is_admin(ctx):
            embed.add_field(name="​", value=ADMIN_COMMANDS, inline=False)
            embed.add_field(name="​", value=ADMIN_VOICE_COMMANDS, inline=False)
        embed.set_footer(text="Past our Prime Gamers")
        try:
            await ctx.send(embed=embed)
            log.info("help sent ok to %s (DM=%s)", ctx.author, ctx.guild is None)
        except Exception as e:
            log.exception("help ctx.send failed for %s: %s", ctx.author, e)


def setup(bot: commands.Bot) -> None:
    bot.add_cog(Utility(bot))

