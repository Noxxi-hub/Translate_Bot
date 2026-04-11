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
    handlers=[
        logging.FileHandler("translator_bot.log", encoding="utf-8"),
        logging.StreamHandler()
    ]
)
log = logging.getLogger("VHATranslator")

# ────────────────────────────────────────────────
# KONFIGURATION
# ────────────────────────────────────────────────

LOGO_URL = (
    "https://cdn.discordapp.com/attachments/1484252260614537247/"
    "1484253018533662740/Picsart_26-03-18_13-55-24-994.png"
    "?ex=69bd8dd7&is=69bc3c57&hm=de6fea399dd30f97d2a14e1515c9e7f91d81d0d9ea111f13e0757d42eb12a0e5&"
)

GROQ_MODEL = "llama-3.3-70b-versatile"

# Feste Zielsprachen dieses Bots (PT + EN immer aktiv)
# Weitere können per !tsprachen zugeschaltet werden
FIXED_LANGS = {"PT", "EN"}

# ────────────────────────────────────────────────
# GLOBALS & FLASK (Keep-Alive)
# ────────────────────────────────────────────────

flask_app = Flask(__name__)

processed_messages     = deque(maxlen=500)
processed_messages_set = set()

translate_active = True

groq_client = Groq(api_key=os.getenv("GROQ_API_KEY_TRANSLATOR"))  # eigener API-Key!

# Semaphore: max. 2 gleichzeitige Groq-Calls
groq_semaphore = asyncio.Semaphore(2)

# Globale Rate-Limit-Pause
_groq_rate_limit_until: float = 0.0

user_last_translation: dict[int, float] = {}
TRANSLATION_COOLDOWN = 8.0

token_counter = {"prompt": 0, "completion": 0, "total": 0}

def get_active_languages() -> set:
    """Gibt aktive Sprachen zurück — liest aus tsprachen.py (MongoDB)."""
    try:
        from tsprachen import get_active_langs
        return get_active_langs()
    except Exception:
        return {"PT", "EN"}  # Fallback


def run_flask():
    port = int(os.environ.get("PORT", 10000))
    flask_app.run(host="0.0.0.0", port=port, debug=False, use_reloader=False)


@flask_app.route("/")
def home():
    return "VHA Translator-Bot • Online"

@flask_app.route("/ping")
def ping():
    return "pong"


# ────────────────────────────────────────────────
# GROQ ASYNC WRAPPER mit Retry
# ────────────────────────────────────────────────

async def groq_call(model: str, messages: list, temperature: float = 0.15,
                    max_tokens: int = 500, retries: int = 3) -> str:
    global _groq_rate_limit_until
    loop = asyncio.get_event_loop()
    wait = 4

    for attempt in range(retries):
        now = asyncio.get_event_loop().time()
        pause = _groq_rate_limit_until - now
        if pause > 0:
            log.info(f"Rate-Limit-Pause: warte {pause:.1f}s")
            await asyncio.sleep(pause)

        async with groq_semaphore:
            try:
                resp = await loop.run_in_executor(
                    None,
                    lambda: groq_client.chat.completions.create(
                        model=model,
                        temperature=temperature,
                        max_tokens=max_tokens,
                        messages=messages
                    )
                )
                if resp.usage:
                    token_counter["prompt"]     += resp.usage.prompt_tokens
                    token_counter["completion"] += resp.usage.completion_tokens
                    token_counter["total"]      += resp.usage.total_tokens
                    log.info(
                        f"Tokens: +{resp.usage.total_tokens} "
                        f"(heute gesamt: {token_counter['total']})"
                    )
                return resp.choices[0].message.content.strip()

            except Exception as e:
                err = str(e)
                if "429" in err or "rate" in err.lower():
                    _groq_rate_limit_until = asyncio.get_event_loop().time() + wait
                    log.warning(f"Rate-Limit (Versuch {attempt+1}/{retries}) – globale Pause {wait}s")
                    await asyncio.sleep(wait)
                    wait = min(wait * 2, 60)
                elif "5" in err[:3]:
                    log.warning(f"Server-Fehler (Versuch {attempt+1}/{retries}) – warte {wait}s")
                    await asyncio.sleep(wait)
                    wait *= 2
                else:
                    log.error(f"Groq-Fehler: {e}")
                    raise

    raise Exception("Groq nicht erreichbar nach mehreren Versuchen")


# ────────────────────────────────────────────────
# SPRACHE ERKENNEN — regelbasiert (kein API-Call)
# ────────────────────────────────────────────────

lang_cache: dict[str, str] = {}

_NEUTRAL = {
    "ok","okay","lol","gg","wp","xd","haha","hahaha","😂","👍","👋","gn","gm",
    "afk","brb","thx","ty","np","omg","wtf","irl","imo","btw","fyi","asap",
}

def _script_detect(text: str) -> str | None:
    """Erkennt Sprache anhand von Unicode-Blöcken — kein API-Call nötig."""
    cjk    = sum(1 for c in text if "\u4e00" <= c <= "\u9fff" or "\u3400" <= c <= "\u4dbf")
    hira   = sum(1 for c in text if "\u3040" <= c <= "\u309f")
    kata   = sum(1 for c in text if "\u30a0" <= c <= "\u30ff")
    hangul = sum(1 for c in text if "\uac00" <= c <= "\ud7a3")
    arabic = sum(1 for c in text if "\u0600" <= c <= "\u06ff")
    cyril  = sum(1 for c in text if "\u0400" <= c <= "\u04ff")
    total  = max(len(text), 1)

    if (hira + kata) / total > 0.15: return "JA"
    if hangul / total > 0.15:        return "KO"
    if cjk / total > 0.15:           return "ZH"
    if arabic / total > 0.15:        return "AR"
    if cyril / total > 0.15:         return "RU"
    return None


async def detect_language_llm(text: str) -> str:
    """Erkennt Sprache — zuerst regelbasiert, dann LLM nur wenn nötig."""
    stripped = text.strip()

    if not stripped or len(stripped) < 2:
        return "OTHER"
    words_lower = {w.strip(".,!?") for w in stripped.lower().split()}
    if words_lower <= _NEUTRAL:
        return "OTHER"
    if re.match(r"^[\d\s\W]+$", stripped):
        return "OTHER"

    script_lang = _script_detect(stripped)
    if script_lang:
        return script_lang

    key = stripped.lower()[:80]
    if key in lang_cache:
        return lang_cache[key]

    try:
        result = await groq_call(
            model=GROQ_MODEL,
            temperature=0.0,
            max_tokens=5,
            messages=[
                {
                    "role": "system",
                    "content": (
                        "Detect the language. Reply ONLY with the ISO 639-1 code in uppercase "
                        "(DE, FR, PT, EN, ES, IT, TR, PL, NL). "
                        "If neutral/unclear reply: OTHER. No explanation."
                    )
                },
                {"role": "user", "content": stripped[:200]}
            ]
        )
        result = result.upper().strip()
        if result.startswith("PT"):
            lang = "PT"
        elif re.match(r"^[A-Z]{2}$", result):
            lang = result
        else:
            m = re.search(r"\b([A-Z]{2})\b", result)
            lang = m.group(1) if m else "OTHER"

        known = {"DE","FR","PT","EN","ES","IT","TR","PL","NL","OTHER"}
        if lang in known:
            lang_cache[key] = lang
            if len(lang_cache) > 800:
                for k in list(lang_cache.keys())[:200]:
                    del lang_cache[k]
        return lang

    except Exception as e:
        log.error(f"Spracherkennungs-Fehler: {e}")
        return "OTHER"


# ────────────────────────────────────────────────
# ÜBERSETZEN
# ────────────────────────────────────────────────

async def translate_all(text: str, target_langs: list) -> dict:
    if not target_langs:
        return {}

    codes_str  = ", ".join(f"{code}={lang_name}" for code, lang_name, _ in target_langs)
    format_str = "\n".join(f"{code}: ..." for code, _, _ in target_langs)
    estimated  = max(1500, min(6000, int(len(text) * 1.5 * len(target_langs))))

    try:
        result = await groq_call(
            model=GROQ_MODEL,
            temperature=0.15,
            max_tokens=estimated,
            messages=[
                {
                    "role": "system",
                    "content": (
                        f"Translate the user text into: {codes_str}.\n"
                        f"Reply ONLY in this exact format (one language per block, "
                        f"no extra text, no markdown):\n{format_str}"
                    )
                },
                {"role": "user", "content": text}
            ]
        )

        codes = [code for code, _, _ in target_langs]
        translations = {}
        for i, code in enumerate(codes):
            if i + 1 < len(codes):
                next_code = codes[i + 1]
                m = re.search(
                    rf"^{code}:\s*(.+?)(?=^{next_code}:)",
                    result, re.MULTILINE | re.DOTALL
                )
            else:
                m = re.search(rf"^{code}:\s*(.+)", result, re.MULTILINE | re.DOTALL)

            if m:
                translation = m.group(1).strip()
                if translation and translation.lower() != text.lower():
                    translations[code] = translation
        return translations

    except Exception as e:
        log.error(f"Übersetzungsfehler (multi): {e}")
        return {}


# ────────────────────────────────────────────────
# FLAGGEN & SPRACHNAMEN
# ────────────────────────────────────────────────

LANG_FLAGS = {
    "DE": "🇩🇪", "FR": "🇫🇷", "PT": "🇧🇷", "EN": "🇬🇧",
    "JA": "🇯🇵", "ES": "🇪🇸", "IT": "🇮🇹", "RU": "🇷🇺",
    "ZH": "🇨🇳", "AR": "🇸🇦", "KO": "🇰🇷", "TR": "🇹🇷",
    "PL": "🇵🇱", "NL": "🇳🇱",
}

LANG_NAMES = {
    "DE": "German",               "FR": "French",
    "PT": "Brazilian Portuguese", "EN": "English",
    "JA": "Japanese",             "ES": "Spanish",
    "IT": "Italian",              "RU": "Russian",
    "ZH": "Chinese",              "AR": "Arabic",
    "KO": "Korean",               "TR": "Turkish",
    "PL": "Polish",               "NL": "Dutch",
}

ALL_LANGS = [
    ("PT", "Brazilian Portuguese", "🇧🇷 Português"),
    ("EN", "English",              "🇬🇧 English"),
    ("JA", "Japanese",             "🇯🇵 日本語"),
    ("ZH", "Chinese",              "🇨🇳 中文"),
    ("KO", "Korean",               "🇰🇷 한국어"),
    ("ES", "Spanish",              "🇪🇸 Español"),
    ("IT", "Italian",              "🇮🇹 Italiano"),
    ("RU", "Russian",              "🇷🇺 Русский"),
    ("AR", "Arabic",               "🇸🇦 العربية"),
    ("TR", "Turkish",              "🇹🇷 Türkçe"),
]

# ────────────────────────────────────────────────
# BOT SETUP
# ────────────────────────────────────────────────

intents = discord.Intents.default()
intents.message_content = True

bot = commands.Bot(
    command_prefix="!t",   # Prefix !t um Konflikte mit Haupt-Bot zu vermeiden
    intents=intents,
    help_command=None,
    case_insensitive=True
)

bot_ready = False

@bot.event
async def on_ready():
    global bot_ready
    if bot_ready:
        return
    bot_ready = True
    try:
        await bot.load_extension("tsprachen")
        log.info("✅ tsprachen geladen")
    except Exception as e:
        log.error(f"❌ tsprachen: {e}")

    log.info(f"→ {bot.user}  •  ÜBERSETZER-BOT ONLINE  •  {discord.utils.utcnow():%Y-%m-%d %H:%M UTC}")
    log.info(f"Aktive Sprachen: {get_active_languages()}")


# ────────────────────────────────────────────────
# BEFEHLE
# ────────────────────────────────────────────────

@bot.command(name="ping")
async def cmd_ping(ctx):
    latency = round(bot.latency * 1000)
    embed = discord.Embed(title="🏓 Übersetzer-Bot", color=0x57F287 if latency < 200 else 0xF39C12)
    embed.add_field(name="📡 Latenz", value=f"`{latency}ms`", inline=True)
    embed.add_field(name="📊 Tokens heute", value=f"`{token_counter['total']}`", inline=True)
    embed.add_field(name="🌐 Aktive Sprachen", value=", ".join(sorted(get_active_languages())), inline=False)
    embed.set_footer(text="VHA Übersetzer-Bot • Online", icon_url=LOGO_URL)
    await ctx.send(embed=embed)


@bot.command(name="translate")
@commands.has_permissions(manage_messages=True)
async def cmd_translate(ctx, action: str = None):
    global translate_active
    if action is None:
        await ctx.send("❓ Benutzung: `!ttranslate on` / `!ttranslate off` / `!ttranslate status`")
        return
    action = action.lower()
    if action == "on":
        translate_active = True
        await ctx.send("✅ Übersetzer-Bot **aktiviert**.")
    elif action == "off":
        translate_active = False
        await ctx.send("🔴 Übersetzer-Bot **deaktiviert**.")
    elif action == "status":
        status = "✅ Aktiv" if translate_active else "🔴 Inaktiv"
        await ctx.send(f"**Übersetzer-Bot Status:** {status}\n**Sprachen:** {', '.join(sorted(get_active_languages()))}")
    else:
        await ctx.send("❓ Unbekannte Option.")



# ────────────────────────────────────────────────
# SPRACHEN & RAUMSPRACHEN — via tsprachen.py Cog
# ────────────────────────────────────────────────
# Befehle: !tsprachen, !traumsprachen, !tkanalid
# Alles über tsprachen.py (MongoDB, Buttons)

@bot.event
async def on_message(message: discord.Message):
    global processed_messages, processed_messages_set, translate_active

    # Bots ignorieren (verhindert gegenseitige Übersetzung)
    if message.author.bot:
        return

    # GIF & YouTube ignorieren
    if (
        any(a.filename.lower().endswith(".gif") or (a.content_type and "gif" in a.content_type.lower())
            for a in message.attachments)
        or _SKIP_URL_PATTERN.search(message.content)
        or message.stickers
    ):
        return

    # Doppelverarbeitung verhindern
    if message.id in processed_messages_set:
        return
    if len(processed_messages) == processed_messages.maxlen:
        processed_messages_set.discard(processed_messages[0])
    processed_messages.append(message.id)
    processed_messages_set.add(message.id)

    # Commands verarbeiten
    if message.content.startswith(bot.command_prefix):
        await bot.process_commands(message)
        return

    if not translate_active:
        return

    content = message.content.strip()
    if not content or len(content) < 2:
        return

    # Nur-Link → skip
    if re.match(r'^https?://\S+$', content):
        return

    # Links aus Text entfernen
    content_cleaned = re.sub(r'https?://\S+', '', content).strip()
    if not content_cleaned or len(content_cleaned) < 2:
        return
    content = content_cleaned

    # Cooldown pro User
    now = time.time()
    if now - user_last_translation.get(message.author.id, 0) < TRANSLATION_COOLDOWN:
        return
    user_last_translation[message.author.id] = now

    # Sprache erkennen
    lang = await detect_language_llm(content)
    if lang == "OTHER":
        return

    # Kanal-spezifische Sprachen prüfen (aus tsprachen.py / MongoDB)
    try:
        from tsprachen import get_room_langs
        room_setting = get_room_langs(message.channel.id)
    except Exception:
        room_setting = None

    if room_setting is not None:
        if len(room_setting) == 0:
            return  # Deaktiviert für diesen Kanal
        active_langs = room_setting
    else:
        active_langs = get_active_languages()

    # Zielsprachen: nur was dieser Bot übernimmt, nicht DE/FR (Haupt-Bot)
    target_langs = [
        t for t in ALL_LANGS
        if t[0] != lang and t[0] in active_langs
    ]

    if not target_langs:
        return

    author_name = message.author.display_name

    def make_embed(fields: list) -> discord.Embed:
        embed = discord.Embed(title=f"💬 • {author_name}", color=0x2ECC71)
        for flag, text in fields:
            if len(text) <= 1000:
                embed.add_field(name=flag, value=text, inline=False)
            else:
                chunks = [text[i:i+1000] for i in range(0, len(text), 1000)]
                embed.add_field(name=flag, value=chunks[0], inline=False)
                for chunk in chunks[1:]:
                    embed.add_field(name="↳", value=chunk, inline=False)
        embed.set_footer(text="VHA Übersetzer", icon_url=LOGO_URL)
        return embed

    try:
        translations = await translate_all(content, target_langs)
        fields = []
        for code, _, label in target_langs:
            translation = translations.get(code, "")
            if translation:
                fields.append((label, translation))

        if fields:
            await message.reply(embed=make_embed(fields), mention_author=False)

    except Exception as e:
        log.error(f"Übersetzungsfehler: {type(e).__name__} - {str(e)}")
        try:
            await message.add_reaction("⚠️")
        except Exception:
            pass


# ────────────────────────────────────────────────
# START
# ────────────────────────────────────────────────

if __name__ == "__main__":
    threading.Thread(target=run_flask, daemon=True, name="Flask-KeepAlive").start()

    token = os.getenv("DISCORD_TOKEN_TRANSLATOR")
    if not token:
        log.error("DISCORD_TOKEN_TRANSLATOR fehlt!")
        exit(1)

    bot.run(token)
