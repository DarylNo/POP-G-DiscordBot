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
**Talking to Toaster**
Just **@Toaster** or reply to one of my messages — I read the channel like a barkeep, so I already have the context.
I join voice automatically when people gather — ask me who's in there, what they're playing, what's been said.
DM me directly (no `!` prefix) for a private conversation I'll remember.
`!chat <message>` — Prefix-style chat if you prefer it (alias `!ask`)
`!memories [page]` — See what Toaster remembers
`!reset` — DM: clear your history · Server (admin): clear this channel's chat history
`!reset all` — (admin) also wipe Toaster's server-wide memories

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

**Memory Management (Admin)**
`!memorybuild [full]` — Backfill memories (`full` = wipe and re-extract everything)
`!forget <number|text>` — Remove a specific memory (see `!memories`)
`!memoryrestore` — Undo a bad consolidation or full rebuild from backup
""".strip()

ADMIN_COMMANDS = """
**Admin**
`!admin sessions` — All active tracking sessions
`!admin info @member` — Raw stat dump
`!admin reset @member` — Zero out a member's stats
`!admin reload <cog>` — Reload a cog without restarting
`!barkeep on|off` — Toggle Toaster reading this channel
`!chatlog [#channel|id] [n]` — Show recent archived messages
""".strip()


class Utility(commands.Cog):
    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot

    @commands.command(name="ping")
    async def ping(self, ctx: commands.Context) -> None:
        """Check if the bot is alive and show its latency."""
        latency_ms = round(self.bot.latency * 1000)
        await ctx.send(f"Pong! `{latency_ms}ms` — v{config.VERSION}")

    @commands.command(name="chatlog")
    async def chatlog(self, ctx: commands.Context, channel_ref: str = None, limit: int = 10) -> None:
        """Admin: show the last N archived messages from a channel (default: current, 10)."""
        if not _is_admin(ctx):
            await ctx.send("You need Administrator permission to use this.")
            return
        channel = None
        if channel_ref is None:
            if ctx.guild is not None:
                channel = ctx.channel
        else:
            ref = channel_ref.strip()
            channel_id = None
            if ref.startswith("<#") and ref.endswith(">") and ref[2:-1].isdigit():
                channel_id = int(ref[2:-1])
            elif ref.isdigit():
                channel_id = int(ref)
            if channel_id is not None:
                channel = self.bot.get_channel(channel_id)
        if not isinstance(channel, discord.TextChannel):
            await ctx.send("Could not find that channel. Use a #mention or numeric channel ID.")
            return

        limit = max(1, min(limit, 25))
        messages = database.get_recent_messages(channel.id, limit=limit)
        if not messages:
            await ctx.send(f"No archived messages for {channel.mention} yet.")
            return
        lines = []
        for m in messages:
            content = m["content"][:80] + ("…" if len(m["content"]) > 80 else "")
            lines.append(f"**{m['username']}**: {content}")
        embed = discord.Embed(
            title=f"Last {len(messages)} messages — #{channel.name}",
            description="\n".join(lines),
            color=discord.Color.blue(),
        )
        await ctx.send(embed=embed)

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

