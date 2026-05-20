import asyncio
import logging
import os
import signal
from logging.handlers import RotatingFileHandler

import discord
from discord.ext import commands

import config
import database

# --- Logging: console + rotating file ---
_data_dir = os.getenv("DATA_DIR", ".")
os.makedirs(_data_dir, exist_ok=True)
_fmt = logging.Formatter("%(asctime)s [%(levelname)s] %(name)s: %(message)s")
_console = logging.StreamHandler()
_console.setFormatter(_fmt)
_file = RotatingFileHandler(
    os.path.join(_data_dir, "popg.log"),
    maxBytes=5 * 1024 * 1024, backupCount=3, encoding="utf-8",
)
_file.setFormatter(_fmt)
logging.basicConfig(level=logging.INFO, handlers=[_console, _file])
log = logging.getLogger("popg")

COGS = [
    "cogs.tracking",
    "cogs.profile",
    "cogs.leaderboard",
    "cogs.admin",
    "cogs.chat_logger",
    "cogs.utility",
    "cogs.social",
]


class POPGBot(commands.Bot):
    def __init__(self) -> None:
        intents = discord.Intents.default()
        intents.members = True
        intents.presences = True
        intents.message_content = True

        super().__init__(command_prefix=config.PREFIX, intents=intents)

    async def setup_hook(self) -> None:
        database.init_db()
        for cog in COGS:
            await self.load_extension(cog)
            log.info("Loaded cog: %s", cog)

    async def on_ready(self) -> None:
        log.info("POPG Bot ready — logged in as %s (id=%s)", self.user, self.user.id)
        guild = self.get_guild(config.GUILD_ID)
        if guild is None:
            log.warning("Guild %s not found. Check GUILD_ID in .env", config.GUILD_ID)
        await self.change_presence(
            activity=discord.Activity(type=discord.ActivityType.watching, name="POPG")
        )

    async def on_command_error(self, ctx: commands.Context, error: commands.CommandError) -> None:
        # Unwrap CheckFailure wrappers from guild_only etc.
        error = getattr(error, "original", error)

        if isinstance(error, commands.CommandNotFound):
            return  # Ignore silently — avoids noise from other bots' prefixes

        if isinstance(error, commands.NoPrivateMessage):
            await ctx.send("This command can only be used in a server.")
            return

        if isinstance(error, commands.CommandOnCooldown):
            await ctx.send(f"Slow down! Try again in {error.retry_after:.1f}s.")
            return

        if isinstance(error, (commands.MissingRequiredArgument, commands.BadArgument)):
            # Let per-command error handlers deal with these if they exist
            if ctx.command and ctx.command.has_error_handler():
                return
            await ctx.send(f"Invalid usage. Try `{config.PREFIX}help {ctx.command}`.")
            return

        # Anything else is unexpected — log it and tell the user
        log.exception("Unhandled exception in command %s", ctx.command, exc_info=error)
        await ctx.send("Something went wrong. Try again.")


async def main() -> None:
    bot = POPGBot()

    loop = asyncio.get_event_loop()

    def _shutdown():
        log.info("Shutdown signal received — closing bot.")
        loop.create_task(bot.close())

    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, _shutdown)
        except NotImplementedError:
            pass  # Windows doesn't support add_signal_handler

    async with bot:
        await bot.start(config.BOT_TOKEN)

    log.info("Bot stopped cleanly.")


if __name__ == "__main__":
    asyncio.run(main())
