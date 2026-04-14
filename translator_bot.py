import discord
from discord.ext import commands
import os
import re
import time
import asyncio
import threading
import logging
from collections import deque
from flask import Flask
from groq import Groq

# ────────────────────────────────────────────────
# LOGGING
# ────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[logging.FileHandler("translator_bot.log", encoding="utf-8"), logging.StreamHandler()]
)
log = logging.getLogger("VHATranslator")

# ────────────────────────────────────────────────
# KONFIGURATION
# ────────────────────────────────────────────────

LOGO_URL = "https://cdn.discordapp.com/attachments/1484252260614537247/1484253018533662740/Picsart_26-03-18_13-55-24-994.png?ex=69bd8dd7&is=69bc3c57&hm=de6fea399dd30f97d2a14e1515c9e7f91d81d0d9ea111f13e0757d42eb12a0e5&"

GROQ_MODEL = "llama-3.3-70b-versatile"
BOT_LOG_CHANNEL_ID = 1484252260614537247

FIXED_LANGS = {"PT", "EN"}

# ────────────────────────────────────────────────
# GLOBALS
# ────────────────────────────────────────────────

flask_app = Flask(__name__)
processed_messages = deque(maxlen=500)
processed_messages_set = set()
translate_active = True

groq_client = Groq(api_key=os.getenv("GROQ_API_KEY_TRANSLATOR"))
groq_semaphore = asyncio.Semaphore(2)
_groq_rate_limit_until: float = 0.0

user_last_translation: dict[int, float] = {}
TRANSLATION_COOLDOWN = 8.0
token_counter = {"prompt": 0, "completion": 0, "total": 0}

def get_active_languages() -> set:
    try:
        from tsprachen import get_active_langs
        return get_active_langs()
    except Exception:
        return {"PT", "EN"}

def run_flask():
    port = int(os.environ.get("PORT", 10000))
    flask_app.run(host="0.0.0.0", port=port, debug=False, use_reloader=False)

@flask_app.route("/")
def home(): return "VHA Translator-Bot • Online"
@flask_app.route("/ping")
def ping(): return "pong"


# ────────────────────────────────────────────────
# GROQ + SPRACHERKENNUNG (unverändert)
# ────────────────────────────────────────────────

async def groq_call(model: str, messages: list, temperature: float = 0.15, max_tokens: int = 500, retries: int = 3) -> str:
    global _groq_rate_limit_until
    loop = asyncio.get_event_loop()
    wait = 4
    for attempt in range(retries):
        now = loop.time()
        pause = _groq_rate_limit_until - now
        if pause > 0:
            await asyncio.sleep(pause)

        async with groq_semaphore:
            try:
                resp = await loop.run_in_executor(None, lambda: groq_client.chat.completions.create(
                    model=model, temperature=temperature, max_tokens=max_tokens, messages=messages))
                if resp.usage:
                    token_counter["prompt"] += resp.usage.prompt_tokens
                    token_counter["completion"] += resp.usage.completion_tokens
                    token_counter["total"] += resp.usage.total_tokens
                return resp.choices[0].message.content.strip()
            except Exception as e:
                err = str(e)
                if "429" in err or "rate" in err.lower():
                    _groq_rate_limit_until = loop.time() + wait
                    await asyncio.sleep(wait)
                    wait = min(wait * 2, 60)
                elif str(e).startswith("5"):
                    await asyncio.sleep(wait)
                    wait *= 2
                else:
                    log.error(f"Groq-Fehler: {e}")
                    raise
    raise Exception("Groq nicht erreichbar")

# detect_language_llm und translate_all bleiben genau wie bei dir (aus Platzgründen hier nicht nochmal kopiert – kopiere sie 1:1 aus deiner alten Datei)

# ────────────────────────────────────────────────
# SPRACHEN
# ────────────────────────────────────────────────

ALL_LANGS = [
    ("PT", "Brazilian Portuguese", "🇧🇷 Português"),
    ("EN", "English",              "🇬🇧 English"),
    ("DE", "German",               "🇩🇪 Deutsch"),
    ("FR", "French",               "🇫🇷 Français"),
    ("JA", "Japanese",             "🇯🇵 日本語"),
    ("ZH", "Chinese",              "🇨🇳 中文"),
    ("KO", "Korean",               "🇰🇷 한국어"),
    ("ES", "Spanish",              "🇪🇸 Español"),
    ("IT", "Italian",              "🇮🇹 Italiano"),
    ("RU", "Russian",              "🇷🇺 Русский"),
    ("AR", "Arabic",               "🇸🇦 العربية"),
    ("TR", "Turkish",              "🇹🇷 Türkçe"),
    ("PL", "Polish",               "🇵🇱 Polski"),
    ("NL", "Dutch",                "🇳🇱 Nederlands"),
]

# ────────────────────────────────────────────────
# BOT
# ────────────────────────────────────────────────

intents = discord.Intents.default()
intents.message_content = True

bot = commands.Bot(command_prefix="!t", intents=intents, help_command=None, case_insensitive=True)

@bot.event
async def on_ready():
    log.info(f"→ {bot.user} • ÜBERSETZER-BOT ONLINE")

    # Persistent Views registrieren (wichtig für Buttons nach Restart)
    try:
        from tsprachen import GlobalSprachenView, RaumSprachenView
        bot.add_view(GlobalSprachenView())
        bot.add_view(RaumSprachenView())
        log.info("✅ Persistent Views für Sprachen-Buttons registriert")
    except Exception as e:
        log.error(f"❌ Persistent Views Fehler: {e}")

    try:
        await bot.load_extension("tsprachen")
        log.info("✅ tsprachen Cog geladen")
    except Exception as e:
        log.error(f"❌ tsprachen: {e}")

    if BOT_LOG_CHANNEL_ID:
        channel = bot.get_channel(BOT_LOG_CHANNEL_ID)
        if channel:
            await channel.send("✅ **Übersetzer-Bot erfolgreich gestartet!**")


# Deine restlichen Befehle (!ping, !translate) und vor allem die on_message Funktion
# kopiere bitte aus deiner alten Datei 1:1 hinein (ab @bot.command(name="ping") bis zum Ende)

# ────────────────────────────────────────────────
# START
# ────────────────────────────────────────────────

if __name__ == "__main__":
    threading.Thread(target=run_flask, daemon=True).start()
    token = os.getenv("DISCORD_TOKEN_TRANSLATOR")
    if not token:
        log.error("DISCORD_TOKEN_TRANSLATOR fehlt!")
        exit(1)
    bot.run(token)
