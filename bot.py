import asyncio
import logging
import os
import signal
from logging.handlers import RotatingFileHandler

import aiohttp
import discord
from discord.ext import commands

import config
import database

VERSION = "1.3.7"

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
logging.getLogger("discord.voice.receive.reader").setLevel(logging.WARNING)
logging.getLogger("discord.opus").setLevel(logging.ERROR)
log = logging.getLogger("popg")

COGS = [
    "cogs.tracking",
    "cogs.profile",
    "cogs.leaderboard",
    "cogs.admin",
    "cogs.chat_logger",
    "cogs.utility",
    "cogs.social",
    "cogs.voice_listener",
    "cogs.llm",
]


class POPGBot(commands.Bot):
    def __init__(self) -> None:
        intents = discord.Intents.default()
        intents.members = True
        intents.presences = True
        intents.message_content = True

        super().__init__(command_prefix=config.PREFIX, intents=intents, help_command=None)

        database.init_db()
        self._cog_errors: dict[str, str] = {}
        for cog in COGS:
            try:
                self.load_extension(cog)
                log.info("Loaded cog: %s", cog)
            except Exception as e:
                log.exception("FAILED to load cog: %s", cog)
                self._cog_errors[cog] = f"{type(e).__name__}: {e}"

    async def on_ready(self) -> None:
        log.info("POPG Bot v%s ready — logged in as %s (id=%s)", VERSION, self.user, self.user.id)
        log.info("Registered commands: %s", sorted(self.all_commands.keys()))
        guild = self.get_guild(config.GUILD_ID)
        if guild is None:
            log.warning("Guild %s not found. Check GUILD_ID in .env", config.GUILD_ID)
        await self.change_presence(
            activity=discord.Activity(type=discord.ActivityType.watching, name="POPG")
        )
        await self._run_startup_diagnostics(guild)

    async def on_message(self, message: discord.Message) -> None:
        # Explicit handler ensures prefix commands are processed under py-cord
        # (commands.Bot inherits from discord.Bot, whose on_message does not call
        # process_commands).
        if message.author.bot:
            return
        is_dm = message.guild is None
        if message.content.startswith(config.PREFIX):
            log.info("Command from %s (DM=%s): %s", message.author, is_dm, message.content[:80])
        elif is_dm:
            log.info("DM from %s (no prefix): %r", message.author, message.content[:40])
        try:
            await self.process_commands(message)
        except Exception:
            log.exception("process_commands raised for message from %s (DM=%s): %r",
                          message.author, is_dm, message.content[:80])

    async def _run_startup_diagnostics(self, guild: discord.Guild) -> None:
        checks: list[tuple[str, str]] = []

        # Cog load errors (shown first so they're not scrolled off)
        for cog, err in getattr(self, "_cog_errors", {}).items():
            checks.append(("❌", f"Cog load FAILED — {cog}: {err}"))

        # Guild
        if guild:
            checks.append(("✅", f"Guild: **{guild.name}** ({guild.member_count} members)"))
        else:
            checks.append(("❌", f"Guild: not found — check `GUILD_ID` in .env"))

        # Database
        try:
            database.get_all_users()
            checks.append(("✅", "Database: connected"))
        except Exception as e:
            checks.append(("❌", f"Database: {e}"))

        # Voice recording + Whisper
        vl = self.cogs.get("VoiceListener")
        whisper_model = os.getenv("WHISPER_MODEL", "small")
        whisper_device = os.getenv("WHISPER_DEVICE", "cpu")
        if vl is None:
            checks.append(("⚠️", "Voice: VoiceListener cog not loaded"))
        elif vl._model is None:
            from cogs.voice_listener import _WHISPER_AVAILABLE
            if not _WHISPER_AVAILABLE:
                checks.append(("⚠️", "Whisper: `openai-whisper` not installed — transcription disabled"))
            else:
                checks.append(("⚠️", f"Whisper: model still loading on `{whisper_device}`..."))
        else:
            checks.append(("✅", f"Whisper: `{whisper_model}` model loaded on `{whisper_device}`"))

        # GPU (only relevant if cuda requested)
        if whisper_device == "cuda":
            try:
                import torch
                if torch.cuda.is_available():
                    checks.append(("✅", f"GPU: {torch.cuda.get_device_name(0)}"))
                else:
                    checks.append(("❌", "GPU: `WHISPER_DEVICE=cuda` but CUDA not available — check Nvidia plugin/passthrough"))
            except ImportError:
                checks.append(("⚠️", "GPU: `torch` not installed"))

        # Ollama
        ollama_url = os.getenv("OLLAMA_URL", "http://localhost:11434")
        ollama_model = os.getenv("OLLAMA_MODEL", "llama3")
        try:
            async with aiohttp.ClientSession() as session:
                resp = await session.get(
                    f"{ollama_url}/api/tags",
                    timeout=aiohttp.ClientTimeout(total=5),
                )
                data = await resp.json()
            available = [m["name"] for m in data.get("models", [])]
            match = any(ollama_model in m for m in available)
            if match:
                checks.append(("✅", f"Ollama: reachable — `{ollama_model}` ready"))
            else:
                available_str = ", ".join(f"`{m}`" for m in available) or "none pulled"
                checks.append(("⚠️", f"Ollama: reachable but `{ollama_model}` not found — available: {available_str}"))
        except Exception as e:
            checks.append(("❌", f"Ollama: unreachable at `{ollama_url}` — {e}"))

        log.info("=== Startup Diagnostics ===")
        for icon, msg in checks:
            log.info("  %s %s", icon, msg)
        log.info("===========================")

    async def on_command_error(self, ctx: commands.Context, error: commands.CommandError) -> None:
        # Unwrap CheckFailure wrappers from guild_only etc.
        error = getattr(error, "original", error)

        if isinstance(error, commands.CommandNotFound):
            log.info("CommandNotFound (DM=%s): %r", ctx.guild is None, ctx.invoked_with)
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
