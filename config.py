import os
from dotenv import load_dotenv

load_dotenv()

VERSION = "1.12.0"

BOT_TOKEN = os.getenv("BOT_TOKEN", "")
PREFIX = os.getenv("PREFIX", "!")
GUILD_ID = int(os.getenv("GUILD_ID", "0"))

if not BOT_TOKEN:
    raise ValueError("BOT_TOKEN is not set in .env")
if not GUILD_ID:
    raise ValueError("GUILD_ID is not set in .env")
