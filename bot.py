import asyncio
import logging

import discord
from discord.ext import commands

import config
import database

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
log = logging.getLogger("popg")

COGS = [
    "cogs.tracking",
    "cogs.profile",
    "cogs.leaderboard",
    "cogs.admin",
    "cogs.chat_logger",
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


async def main() -> None:
    async with POPGBot() as bot:
        await bot.start(config.BOT_TOKEN)


if __name__ == "__main__":
    asyncio.run(main())
