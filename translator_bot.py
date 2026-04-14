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
BOT_LOG_CHANNEL_ID = 1484252260614537247

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

    import json as _json
    codes_str = ", ".join(f"{code}={lang_name}" for code, lang_name, _ in target_langs)
    json_keys = ", ".join(f'"{code}": "..."' for code, _, _ in target_langs)
    estimated = max(1500, min(6000, int(len(text) * 1.5 * len(target_langs))))

    try:
        result = await groq_call(
            model=GROQ_MODEL,
            temperature=0.1,
            max_tokens=estimated,
            messages=[
                {
                    "role": "system",
                    "content": (
                        f"Translate the user text into these languages: {codes_str}.\n"
                        f"Reply with VALID JSON ONLY — no markdown, no explanation:\n"
                        f"{{{json_keys}}}"
                    )
                },
                {"role": "user", "content": text}
            ]
        )

        # JSON parsen
        clean = result.strip()
        clean = re.sub(r"^```(?:json)?\s*", "", clean)
        clean = re.sub(r"\s*```$", "", clean)

        parsed = _json.loads(clean)
        translations = {}
        for code, _, _ in target_langs:
            val = parsed.get(code, "").strip()
            if val and val.lower() != text.lower():
                translations[code] = val
        return translations

    except Exception as e:
        log.error(f"Übersetzungsfehler (multi): {e}")
        # Fallback: einzeln übersetzen
        translations = {}
        for code, lang_name, _ in target_langs:
            try:
                result = await groq_call(
                    model=GROQ_MODEL,
                    temperature=0.1,
                    max_tokens=800,
                    messages=[
                        {"role": "system", "content": f"Translate to {lang_name}. Output ONLY the translation."},
                        {"role": "user", "content": text}
                    ]
                )
                if result and result.lower() != text.lower():
                    translations[code] = result.strip()
            except Exception:
                pass
        return translations


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
    errors = []

    try:
        await bot.load_extension("tsprachen")
        log.info("✅ tsprachen geladen")
    except Exception as e:
        errors.append(f"❌ tsprachen: {e}")
        log.error(f"❌ tsprachen: {e}")

    log.info(f"→ {bot.user}  •  ÜBERSETZER-BOT ONLINE  •  {discord.utils.utcnow():%Y-%m-%d %H:%M UTC}")
    log.info(f"Aktive Sprachen: {get_active_languages()}")

    if BOT_LOG_CHANNEL_ID:
        channel = bot.get_channel(BOT_LOG_CHANNEL_ID)
        if channel:
            if errors:
                msg = "⚠️ **Übersetzer-Bot gestartet mit Fehlern:**\n" + "\n".join(errors)
            else:
                msg = (
                    "✅ **Übersetzer-Bot erfolgreich gestartet!**\n"
                    "🔧 tsprachen.py • geladen"
                )
            await channel.send(msg)


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
        or re.search(r'https?://\S*(?:tenor\.com|giphy\.com|youtube\.com|youtu\.be|youtube-nocookie\.com|yt\.be)\S*', message.content, re.IGNORECASE)
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
        # Raum hat eigene Einstellungen → exakt diese nutzen, KEINE fixen Sprachen
        active_langs = room_setting
    else:
        # Kein Eintrag → globale Einstellungen (PT + EN immer aktiv)
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
# ════════════════════════════════════════════════
#  tsprachen.py  •  VHA Übersetzer-Bot
#  Globale Sprachen + Raumsprachen per Button
#  PT + EN immer aktiv (fix)
#  JA, ZH, KO, ES, IT, RU, AR, TR, PL, NL zuschaltbar
#  DE + FR absichtlich ausgelassen → Haupt-Bot
# ════════════════════════════════════════════════

import discord
from discord.ext import commands
from pymongo import MongoClient
import os
import logging

log = logging.getLogger("VHATranslator.Sprachen")

LOGO_URL = (
    "https://cdn.discordapp.com/attachments/1484252260614537247/"
    "1484253018533662740/Picsart_26-03-18_13-55-24-994.png"
    "?ex=69bd8dd7&is=69bc3c57&hm=de6fea399dd30f97d2a14e1515c9e7f91d81d0d9ea111f13e0757d42eb12a0e5&"
)

# Sprachen die immer aktiv sind (können nicht abgeschaltet werden)
FIXED_LANGS = {"PT", "EN"}

# Alle zuschaltbaren Sprachen — kein DE/FR, das macht der Haupt-Bot
OPTIONAL_LANGS = {
    "JA": {"flag": "🇯🇵", "name": "日本語"},
    "ZH": {"flag": "🇨🇳", "name": "中文"},
    "KO": {"flag": "🇰🇷", "name": "한국어"},
    "ES": {"flag": "🇪🇸", "name": "Español"},
    "IT": {"flag": "🇮🇹", "name": "Italiano"},
    "RU": {"flag": "🇷🇺", "name": "Русский"},
    "AR": {"flag": "🇸🇦", "name": "العربية"},
    "TR": {"flag": "🇹🇷", "name": "Türkçe"},
    "PL": {"flag": "🇵🇱", "name": "Polski"},
    "NL": {"flag": "🇳🇱", "name": "Nederlands"},
}

ALLOWED_ROLES = {"R5", "R4", "DEV"}


# ────────────────────────────────────────────────
# MongoDB Helpers
# ────────────────────────────────────────────────

def get_col():
    client = MongoClient(os.getenv("MONGODB_URI"))
    return client["vhabot"]["tsprachen"]


def get_room_col():
    client = MongoClient(os.getenv("MONGODB_URI"))
    return client["vhabot"]["tsprachen_rooms"]


def get_active_langs() -> set:
    """Globale aktive Sprachen (inkl. FIXED_LANGS)."""
    try:
        col = get_col()
        doc = col.find_one({"_id": "settings"})
        if not doc:
            default = {"PT", "EN"}
            col.update_one(
                {"_id": "settings"},
                {"$set": {"active": list(default)}},
                upsert=True
            )
            return default
        active = set(doc.get("active", list(FIXED_LANGS)))
        active.update(FIXED_LANGS)
        return active
    except Exception as e:
        log.error(f"Fehler beim Laden der Sprachen: {e}")
        return {"PT", "EN"}


def set_active_langs(langs: set):
    """Speichert globale Sprachen in MongoDB."""
    try:
        col = get_col()
        langs.update(FIXED_LANGS)
        col.update_one(
            {"_id": "settings"},
            {"$set": {"active": list(langs)}},
            upsert=True
        )
    except Exception as e:
        log.error(f"Fehler beim Speichern der Sprachen: {e}")


def get_room_langs(channel_id: int) -> set | None:
    """
    Raumsprachen für einen Kanal.
    Gibt None zurück wenn keine eigenen Einstellungen → globale nutzen.
    Gibt leeres set zurück wenn Kanal deaktiviert.
    """
    try:
        col = get_room_col()
        doc = col.find_one({"_id": str(channel_id)})
        if not doc:
            return None
        if doc.get("disabled"):
            return set()
        langs = set(doc.get("langs", []))
        return langs if langs else None
    except Exception as e:
        log.error(f"Fehler beim Laden der Raumsprachen: {e}")
        return None


def set_room_langs(channel_id: int, langs: set | None, disabled: bool = False):
    """Speichert Raumsprachen in MongoDB."""
    try:
        col = get_room_col()
        if langs is None and not disabled:
            # Reset → Eintrag löschen
            col.delete_one({"_id": str(channel_id)})
        else:
            col.update_one(
                {"_id": str(channel_id)},
                {"$set": {
                    "langs": list(langs) if langs else [],
                    "disabled": disabled
                }},
                upsert=True
            )
    except Exception as e:
        log.error(f"Fehler beim Speichern der Raumsprachen: {e}")


def has_permission(member: discord.Member) -> bool:
    if member.guild_permissions.administrator:
        return True
    member_roles = {r.name.upper() for r in member.roles}
    return bool(member_roles & ALLOWED_ROLES)


# ────────────────────────────────────────────────
# Globale Sprachen — Button View
# ────────────────────────────────────────────────

class GlobalSprachenView(discord.ui.View):
    def __init__(self, author: discord.Member):
        super().__init__(timeout=120)
        self.author = author
        self._update_buttons()

    def _update_buttons(self):
        self.clear_items()
        active = get_active_langs()

        for code, info in OPTIONAL_LANGS.items():
            is_active = code in active
            btn = discord.ui.Button(
                label=f"{info['flag']} {info['name']}",
                style=discord.ButtonStyle.success if is_active else discord.ButtonStyle.secondary,
                emoji="✅" if is_active else "❌",
                custom_id=f"tlang_{code}"
            )
            btn.callback = self._make_callback(code)
            self.add_item(btn)

    def _make_callback(self, code: str):
        async def callback(interaction: discord.Interaction):
            if interaction.user.id != self.author.id:
                await interaction.response.send_message(
                    "❌ Nur derjenige der den Befehl ausgeführt hat kann Änderungen vornehmen.",
                    ephemeral=True
                )
                return

            try:
                col = get_col()
                doc = col.find_one({"_id": "settings"})
                active = set(doc.get("active", list(FIXED_LANGS))) if doc else set(FIXED_LANGS)
                active.update(FIXED_LANGS)
            except Exception:
                active = set(FIXED_LANGS)

            if code in active:
                active.discard(code)
                action = "deaktiviert"
            else:
                active.add(code)
                action = "aktiviert"

            try:
                col = get_col()
                col.update_one(
                    {"_id": "settings"},
                    {"$set": {"active": list(active)}},
                    upsert=True
                )
            except Exception as e:
                await interaction.response.send_message(f"❌ Fehler: {e}", ephemeral=True)
                return

            info = OPTIONAL_LANGS[code]
            self._update_buttons()
            embed = self._make_embed()
            await interaction.response.edit_message(embed=embed, view=self)
            await interaction.followup.send(
                f"{info['flag']} **{info['name']}** {action}!",
                ephemeral=True
            )

        return callback

    def _make_embed(self) -> discord.Embed:
        active = get_active_langs()
        embed = discord.Embed(
            title="🌐 Übersetzer-Bot • Globale Sprachen",
            color=0x2ECC71
        )
        embed.set_author(name="VHA Übersetzer-Bot", icon_url=LOGO_URL)

        embed.add_field(
            name="🔒 Immer aktiv",
            value="🇧🇷 Português • 🇬🇧 English",
            inline=False
        )

        status_lines = []
        for code, info in OPTIONAL_LANGS.items():
            status = "✅ Aktiv" if code in active else "❌ Inaktiv"
            status_lines.append(f"{info['flag']} {info['name']}: **{status}**")

        embed.add_field(
            name="🔄 Ein/Ausschaltbar",
            value="\n".join(status_lines),
            inline=False
        )

        embed.set_footer(
            text="Klicke auf einen Button um eine Sprache ein/auszuschalten",
            icon_url=LOGO_URL
        )
        return embed


# ────────────────────────────────────────────────
# Raumsprachen — Button View
# ────────────────────────────────────────────────

class RaumSprachenView(discord.ui.View):
    def __init__(self, author: discord.Member, channel_id: int, channel_name: str, current: set):
        super().__init__(timeout=120)
        self.author = author
        self.channel_id = channel_id
        self.channel_name = channel_name
        self.current = current  # State im Memory
        self._update_buttons()

    def _update_buttons(self):
        self.clear_items()
        all_langs = {
            "PT": {"flag": "🇧🇷", "name": "Português"},
            "EN": {"flag": "🇬🇧", "name": "English"},
            **OPTIONAL_LANGS
        }
        for code, info in all_langs.items():
            is_active = code in self.current
            btn = discord.ui.Button(
                label=f"{info['flag']} {info['name']}",
                style=discord.ButtonStyle.success if is_active else discord.ButtonStyle.secondary,
                emoji="✅" if is_active else "❌",
                custom_id=f"troom_{self.channel_id}_{code}"
            )
            btn.callback = self._make_callback(code)
            self.add_item(btn)

        reset_btn = discord.ui.Button(
            label="📡 Globale Einstellungen",
            style=discord.ButtonStyle.primary,
            custom_id=f"troom_{self.channel_id}_reset",
            row=4
        )
        reset_btn.callback = self._reset_callback
        self.add_item(reset_btn)

        off_btn = discord.ui.Button(
            label="🚫 Alle aus",
            style=discord.ButtonStyle.danger,
            custom_id=f"troom_{self.channel_id}_off",
            row=4
        )
        off_btn.callback = self._off_callback
        self.add_item(off_btn)

    def _make_callback(self, code: str):
        async def callback(interaction: discord.Interaction):
            if interaction.user.id != self.author.id:
                await interaction.response.send_message(
                    "❌ Nur derjenige der den Befehl ausgeführt hat kann Änderungen vornehmen.",
                    ephemeral=True
                )
                return

            if code in self.current:
                self.current.discard(code)
                action = "deaktiviert"
            else:
                self.current.add(code)
                action = "aktiviert"

            set_room_langs(self.channel_id, self.current.copy(), disabled=False)

            self._update_buttons()
            embed = self._make_embed()
            await interaction.response.edit_message(embed=embed, view=self)

            all_langs = {"PT": {"flag": "🇧🇷", "name": "Português"}, "EN": {"flag": "🇬🇧", "name": "English"}, **OPTIONAL_LANGS}
            info = all_langs.get(code, {"flag": "🌐", "name": code})
            await interaction.followup.send(
                f"{info['flag']} **{info['name']}** in #{self.channel_name} {action}!",
                ephemeral=True
            )
        return callback

    async def _reset_callback(self, interaction: discord.Interaction):
        if interaction.user.id != self.author.id:
            await interaction.response.send_message("❌ Keine Berechtigung.", ephemeral=True)
            return
        set_room_langs(self.channel_id, None)
        self.current = get_active_langs().copy()
        self._update_buttons()
        embed = self._make_embed()
        await interaction.response.edit_message(embed=embed, view=self)
        await interaction.followup.send(f"📡 #{self.channel_name} nutzt jetzt globale Einstellungen.", ephemeral=True)

    async def _off_callback(self, interaction: discord.Interaction):
        if interaction.user.id != self.author.id:
            await interaction.response.send_message("❌ Keine Berechtigung.", ephemeral=True)
            return
        set_room_langs(self.channel_id, set(), disabled=True)
        self.current = set()
        self._update_buttons()
        embed = self._make_embed()
        await interaction.response.edit_message(embed=embed, view=self)
        await interaction.followup.send(f"🚫 Übersetzung in #{self.channel_name} deaktiviert.", ephemeral=True)

    def _make_embed(self) -> discord.Embed:
        room_setting = get_room_langs(self.channel_id)
        if room_setting is None:
            status_text = "📡 Nutzt globale Einstellungen"
            color = 0x3498DB
        elif len(self.current) == 0:
            status_text = "🚫 Übersetzung deaktiviert"
            color = 0xED4245
        else:
            status_text = "⚙️ Eigene Einstellungen aktiv"
            color = 0x2ECC71

        embed = discord.Embed(title=f"⚙️ Raumsprachen • #{self.channel_name}", color=color)
        embed.set_author(name="VHA Übersetzer-Bot", icon_url=LOGO_URL)
        embed.add_field(name="Status", value=status_text, inline=False)

        all_langs = {"PT": {"flag": "🇧🇷", "name": "Português"}, "EN": {"flag": "🇬🇧", "name": "English"}, **OPTIONAL_LANGS}
        status_lines = [f"{info['flag']} {info['name']}: **{'✅ Aktiv' if code in self.current else '❌ Inaktiv'}**" for code, info in all_langs.items()]

        embed.add_field(name="🔄 Sprachen für diesen Kanal", value="\n".join(status_lines), inline=False)
        embed.set_footer(text="📡 Globale Einstellungen = Reset • 🚫 Alle aus = Deaktivieren", icon_url=LOGO_URL)
        return embed


# ────────────────────────────────────────────────
# Cog
# ────────────────────────────────────────────────

class TSprachenCog(commands.Cog):
    def __init__(self, bot):
        self.bot = bot

    @commands.command(name="sprachen", aliases=["lang"])
    async def cmd_sprachen(self, ctx):
        if not has_permission(ctx.author):
            return await ctx.send("❌ Keine Berechtigung.", delete_after=5)
        view = GlobalSprachenView(ctx.author)
        await ctx.send(embed=view._make_embed(), view=view)

    @commands.command(name="raumsprachen")
    async def cmd_raumsprachen(self, ctx, channel_id: int = None):
        if not has_permission(ctx.author):
            return await ctx.send("❌ Keine Berechtigung.", delete_after=5)

        cid = channel_id if channel_id is not None else ctx.channel.id
        ch = ctx.guild.get_channel(cid)
        ch_name = ch.name if ch else str(cid)

        try:
            col = get_room_col()
            doc = col.find_one({"_id": str(cid)})
            if not doc or doc.get("disabled"):
                current = get_active_langs().copy()
            else:
                current = set(doc.get("langs", [])) if doc.get("langs") else get_active_langs().copy()
        except Exception:
            current = get_active_langs().copy()

        view = RaumSprachenView(ctx.author, cid, ch_name, current)
        await ctx.send(embed=view._make_embed(), view=view)

    @commands.command(name="kanalid", aliases=["channelid"])
    async def cmd_kanalid(self, ctx):
        """Sendet alle Kanal-IDs inklusive Foren als DM."""
        if not has_permission(ctx.author):
            return await ctx.send("❌ Keine Berechtigung.", delete_after=5)

        lines = []
        for category, channels in ctx.guild.by_category():
            cat_name = category.name if category else "Ohne Kategorie"
            
            # WICHTIG: Prüft auf Text- UND Forum-Kanäle
            relevant = [c for c in channels if isinstance(c, (discord.TextChannel, discord.ForumChannel))]
            if not relevant: continue

            lines.append(f"\n**📂 {cat_name}**")
            for ch in relevant:
                icon = "📝 Forum" if isinstance(ch, discord.ForumChannel) else "💬 Text"
                lines.append(f"• {icon} | **{ch.name}** — `{ch.id}`")

        full_text = "__**KOMPLETTE KANAL-LISTE**__\n" + "\n".join(lines)
        
        try:
            if len(full_text) > 1900:
                for i in range(0, len(full_text), 1900):
                    await ctx.author.send(full_text[i:i+1900])
            else:
                await ctx.author.send(full_text)
            await ctx.send("📬 Liste (inkl. Foren) wurde privat verschickt!")
        except discord.Forbidden:
            await ctx.send("❌ DMs blockiert!")

async def setup(bot):
    await bot.add_cog(TSprachenCog(bot))

