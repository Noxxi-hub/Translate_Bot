import discord
from discord.ext import commands
import os
import re
import time
import asyncio
import threading
import logging
import json
from collections import deque
from flask import Flask
from google import genai
from google.genai import types
from pymongo import MongoClient, ReturnDocument

# Lokale Spracherkennung - kein API Call mehr
try:
    from langdetect import detect_langs, DetectorFactory
    DetectorFactory.seed = 0
    LANGDETECT_AVAILABLE = True
except ImportError:
    LANGDETECT_AVAILABLE = False

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

GEMINI_MODELS = [
    "gemini-2.5-flash",
    "gemini-2.5-flash-lite",
    "gemini-3-flash-preview",
]
GEMINI_MODEL = GEMINI_MODELS[0]

BOT_LOG_CHANNEL_ID = 1484252260614537247

# Feste Zielsprachen dieses Bots (PT + EN immer aktiv)
FIXED_LANGS = {"PT", "EN"}

# ────────────────────────────────────────────────
# GLOBALS & FLASK (Keep-Alive)
# ────────────────────────────────────────────────

flask_app = Flask(__name__)

processed_messages     = deque(maxlen=500)
processed_messages_set = set()

translate_active = True

gemini_client = genai.Client(api_key=os.getenv("GEMINI_API_KEY_TRANSLATOR"))

# Semaphore: max. 4 gleichzeitige Gemini-Calls
gemini_semaphore = asyncio.Semaphore(8)

import concurrent.futures as _futures
_gemini_executor = _futures.ThreadPoolExecutor(max_workers=6, thread_name_prefix="gemini_t")

user_last_translation: dict[int, float] = {}
TRANSLATION_COOLDOWN = 2.0  # reduziert von 8.0 für Gemini (höheres Rate-Limit)

token_counter = {"prompt": 0, "completion": 0, "total": 0}

# Caches
lang_cache: dict[str, str] = {}
translation_cache: dict[str, dict] = {}

# ────────────────────────────────────────────────
# MONGODB — WÜRFEL-STATISTIKEN
# ────────────────────────────────────────────────

_mongo_client = None
_dice_col = None

def _get_dice_col():
    global _mongo_client, _dice_col
    if _dice_col is None:
        uri = os.getenv("MONGODB_URI")
        if not uri:
            raise RuntimeError("MONGODB_URI fehlt!")
        _mongo_client = MongoClient(uri, serverSelectionTimeoutMS=5000)
        db = _mongo_client.get_default_database()
        _dice_col = db["dice_stats"]
        _dice_col.create_index("user_id", unique=True)
    return _dice_col


def db_update_stats(user_id: int, display_name: str, result: str):
    """result: 'win' | 'loss' | 'draw' — Name wird immer aktuell gehalten."""
    col = _get_dice_col()
    inc = {"wins": 0, "losses": 0, "draws": 0, "games": 1}
    if result == "win":
        inc["wins"] = 1
    elif result == "loss":
        inc["losses"] = 1
    else:
        inc["draws"] = 1
    col.find_one_and_update(
        {"user_id": user_id},
        {
            "$set": {"name": display_name},
            "$inc": inc,
            "$setOnInsert": {"user_id": user_id},
        },
        upsert=True,
        return_document=ReturnDocument.AFTER,
    )


def db_get_ranking(limit: int = 10) -> list:
    """Gibt Top-Spieler sortiert nach Wins zurück."""
    col = _get_dice_col()
    return list(col.find(
        {"games": {"$gt": 0}},
        {"_id": 0, "user_id": 1, "name": 1, "wins": 1, "losses": 1, "draws": 1, "games": 1}
    ).sort([("wins", -1), ("games", 1)]).limit(limit))


def db_get_player(user_id: int) -> dict | None:
    """Gibt Statistik eines einzelnen Spielers zurück."""
    col = _get_dice_col()
    return col.find_one({"user_id": user_id}, {"_id": 0})


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
# GEMINI ASYNC WRAPPER mit Retry - OPTIMIERT
# ────────────────────────────────────────────────

async def gemini_call(model: str, messages: list, temperature: float = 0.1,
                      max_tokens: int = 500, retries: int = 3) -> str:
    loop = asyncio.get_event_loop()

    system_text = None
    contents = []
    for msg in messages:
        role = msg["role"]
        content = msg["content"]
        if role == "system":
            system_text = content
        elif role == "user":
            if isinstance(content, str):
                contents.append(types.Content(role="user", parts=[types.Part(text=content)]))

    last_error = None
    for model_name in GEMINI_MODELS:
        use_thinking = "2.5" in model_name
        config = types.GenerateContentConfig(
            temperature=temperature,
            max_output_tokens=max_tokens,
            system_instruction=system_text,
            thinking_config=types.ThinkingConfig(thinking_budget=0) if use_thinking else None,
            automatic_function_calling=types.AutomaticFunctionCallingConfig(disable=True),
        )

        wait = 4
        for attempt in range(retries):
            async with gemini_semaphore:
                try:
                    resp = await loop.run_in_executor(
                        _gemini_executor,
                        lambda: gemini_client.models.generate_content(
                            model=model_name,
                            contents=contents,
                            config=config,
                        )
                    )
                    if resp.usage_metadata:
                        total = (resp.usage_metadata.prompt_token_count or 0) + (resp.usage_metadata.candidates_token_count or 0)
                        token_counter["prompt"]     += resp.usage_metadata.prompt_token_count or 0
                        token_counter["completion"] += resp.usage_metadata.candidates_token_count or 0
                        token_counter["total"]      += total
                        log.info(f"Tokens: +{total} (heute gesamt: {token_counter['total']})")
                    
                    if model_name != GEMINI_MODELS[0]:
                        log.info(f"FALLBACK OK → {model_name}")
                    return resp.text.strip()

                except Exception as e:
                    err = str(e).lower()
                    last_error = str(e)
                    if "429" in err or "quota" in err or "resource_exhausted" in err or "rate" in err:
                        log.warning(f"{model_name} Rate-Limit (Versuch {attempt+1}/{retries}) – warte {wait}s")
                        await asyncio.sleep(wait)
                        wait = min(wait * 2, 60)
                    elif "503" in err or "500" in err or "502" in err or "unavailable" in err or "server" in err:
                        log.warning(f"{model_name} überlastet, versuche nächstes Modell...")
                        break
                    else:
                        log.error(f"Gemini-Fehler {model_name}: {e}")
                        break

        log.warning(f"Modell {model_name} fehlgeschlagen, fallback...")

    raise Exception(f"Alle Gemini-Modelle down. Letzter Fehler: {last_error}")


# ────────────────────────────────────────────────
# SPRACHE ERKENNEN — LOKAL, KEIN API-CALL
# ────────────────────────────────────────────────

_NEUTRAL = {
    "ok","okay","lol","gg","wp","xd","haha","hahaha","😂","👍","👋","gn","gm",
    "afk","brb","thx","ty","np","omg","wtf","irl","imo","btw","fyi","asap",
}

def _script_detect(text: str) -> str | None:
    """Erkennt Sprache anhand von Unicode-Blöcken."""
    cjk    = sum(1 for c in text if "一" <= c <= "鿿" or "㐀" <= c <= "䶿")
    hira   = sum(1 for c in text if "぀" <= c <= "ゟ")
    kata   = sum(1 for c in text if "゠" <= c <= "ヿ")
    hangul = sum(1 for c in text if "가" <= c <= "힣")
    arabic = sum(1 for c in text if "؀" <= c <= "ۿ")
    cyril  = sum(1 for c in text if "Ѐ" <= c <= "ӿ")
    total  = max(len(text), 1)

    if (hira + kata) / total > 0.15: return "JA"
    if hangul / total > 0.15:        return "KO"
    if cjk / total > 0.15:           return "ZH"
    if arabic / total > 0.15:        return "AR"
    if cyril / total > 0.15:         return "RU"
    return None


async def detect_language_llm(text: str) -> str:
    """Erkennt Sprache LOKAL — 0 API-Calls, optimiert für kurze DE/FR."""
    stripped = text.strip()

    if not stripped or len(stripped) < 2:
        return "OTHER"
    words_lower = {w.strip(".,!?") for w in stripped.lower().split()}
    if words_lower <= _NEUTRAL:
        return "OTHER"
    if re.match(r"^[\d\s\W]+$", stripped):
        return "OTHER"

    # Script-Erkennung zuerst
    script_lang = _script_detect(stripped)
    if script_lang:
        return script_lang

    key = stripped.lower()[:80]
    if key in lang_cache:
        return lang_cache[key]

    t = f" {stripped.lower()} "

    # NEU: kurze Texte (<20 Zeichen) – harte Heuristik
    if len(stripped) < 20:
        de_markers = [' ich ', ' bin ', ' da ', ' ne ', ' ja ', ' nein ', ' was ', ' du ', ' nicht ', ' mal ', ' hab ', ' habe ', ' ist ', ' ein ', ' der ', ' die ', ' das ', ' und ', ' ne bin ', ' was sagst ']
        fr_markers = [' je ', ' suis ', ' pas ', ' oui ', ' non ', ' tu ', ' vous ', ' est ', ' le ', ' la ', ' et ', ' pour ', ' quoi ']
        de_hits = sum(1 for w in de_markers if w in t)
        fr_hits = sum(1 for w in fr_markers if w in t)
        if de_hits > 0 and de_hits >= fr_hits:
            lang_cache[key] = "DE"
            return "DE"
        if fr_hits > 0 and fr_hits > de_hits:
            lang_cache[key] = "FR"
            return "FR"
        if any(c in stripped for c in 'äöüßÄÖÜ'):
            lang_cache[key] = "DE"
            return "DE"

    lang = "OTHER"
    if LANGDETECT_AVAILABLE:
        try:
            langs = detect_langs(stripped)
            code = langs[0].lang.upper()
            prob = langs[0].prob
            mapping = {"PT": "PT", "EN": "EN", "DE": "DE", "FR": "FR", "ES": "ES", "RU": "RU", "JA": "JA", "ZH-CN": "ZH", "ZH": "ZH", "KO": "KO"}
            if prob > 0.7:
                lang = mapping.get(code, "OTHER")
        except:
            lang = "OTHER"
    else:
        if re.search(r'\b(der|die|das|und|ich|nicht)\b', stripped.lower()): lang = "DE"
        elif re.search(r'\b(the|and|you|for)\b', stripped.lower()): lang = "EN"
        elif re.search(r'\b(le|la|et|vous|pour)\b', stripped.lower()): lang = "FR"
        elif re.search(r'\b(o|a|e|que|para)\b', stripped.lower()): lang = "PT"

    if lang == "OTHER":
        if any(w in t for w in [' der ', ' die ', ' das ', ' und ', ' ich ', ' nicht ']):
            lang = "DE"
        elif any(w in t for w in [' le ', ' la ', ' et ', ' vous ', ' je ']):
            lang = "FR"
        elif any(w in t for w in [' the ', ' and ', ' you ']):
            lang = "EN"
        else:
            lang = "EN"

    known = {"DE","FR","PT","EN","ES","RU","JA","ZH","KO","OTHER"}
    if lang not in known:
        lang = "OTHER"

    lang_cache[key] = lang
    if len(lang_cache) > 800:
        for k in list(lang_cache.keys())[:200]:
            del lang_cache[k]
    return lang


# ────────────────────────────────────────────────
# ÜBERSETZEN - MIT CACHE
# ────────────────────────────────────────────────

async def translate_all(text: str, target_langs: list) -> dict:
    if not target_langs:
        return {}

    codes = [code for code, _, _ in target_langs]
    cache_key = f"{text[:200]}_{'_'.join(codes)}"
    
    if cache_key in translation_cache:
        log.info(f"Cache-Hit für Übersetzung")
        return translation_cache[cache_key]

    codes_str = ", ".join(f"{code}={lang_name}" for code, lang_name, _ in target_langs)
    json_keys = ", ".join(f'"{code}": "..."' for code in codes)
    estimated = max(800, min(4000, int(len(text) * 2.5 * len(target_langs))))

    try:
        result = await gemini_call(
            model=GEMINI_MODEL,
            temperature=0.1,
            max_tokens=estimated,
            messages=[
                {
                    "role": "system",
                    "content": (
                        f"Du bist ein natürlicher, lockerer Übersetzer für einen Discord-Chat einer Gaming-Community.\n"
                        f"WICHTIGSTE REGELN:\n"
                        f"1. Verwende IMMER die Du-Form — niemals 'Sie' (Deutsch) oder 'Vous' (Französisch), immer 'Tu'.\n"
                        f"2. Übersetze den SINN, nicht nur Wörter — es soll natürlich und wie ein echter Mensch klingen.\n"
                        f"3. Behalte den Ton bei: Wenn ein Satz witzig, frech oder emotional ist, übersetze ihn genauso.\n"
                        f"4. Kosenamen korrekt übersetzen: 'süße/süßer'→ma chérie/mon chéri (FR), sweetie/honey (EN); 'schatz'→chéri/chérie (FR), honey/darling (EN)\n"
                        f"4b. Nur diese Kosenamen NIEMALS übersetzen: baby, babe, bby — diese bleiben in allen Sprachen unverändert\n"
                        f"5. Diese Wörter NIE übersetzen: Spielernamen, @mentions, R1/R2/R3/R4/R5, Koordinaten, Allianz-Namen\n"
                        f"6. Emojis bleiben exakt unverändert\n"
                        f"7. Jedes Sprachfeld MUSS in der richtigen Sprache sein — DE=Deutsch, FR=Französisch, EN=Englisch, PT=Portugiesisch\n"
                        f"8. WICHTIG: Alle Sprachfelder MÜSSEN immer befüllt sein — auch bei sehr kurzen Sätzen\n"
                        f"9. Antworte NUR mit diesem JSON, kein Markdown, kein Extra-Text:\n"
                        f"{{{json_keys}}}"
                    )
                },
                {"role": "user", "content": text}
            ]
        )

        clean = result.strip()
        if clean.startswith("```"):
            clean = clean.split("```")[1]
            if clean.startswith("json"):
                clean = clean[4:]
        clean = clean.strip()

        parsed = json.loads(clean)
        translations = {}
        max_len = max(len(text) * 6, 500)

        original_words = set(re.sub(r'[^\w\s]', '', text.lower()).split())

        for code in codes:
            val = parsed.get(code, "").strip()
            if not val:
                continue

            if val.lower() == text.lower():
                log.warning(f"Übersetzung identisch mit Original ({code}) — verworfen")
                continue

            # Zu ähnlich zum Original — nur bei 5+ Wörtern und nicht für EN
            if code != "EN" and len(original_words) >= 5:
                val_words = set(re.sub(r'[^\w\s]', '', val.lower()).split())
                overlap = len(original_words & val_words) / len(original_words)
                if overlap > 0.80:
                    log.warning(f"Übersetzung zu ähnlich ({code}): {overlap:.0%} — verworfen")
                    continue

            words = val.split()
            if words:
                most_common = max(set(words), key=words.count)
                if words.count(most_common) > 15:
                    log.warning(f"Loop erkannt ({code}) — verworfen")
                    continue

            if len(val) > max_len:
                val = val[:max_len]

            translations[code] = val

        # Cache speichern
        if translations:
            translation_cache[cache_key] = translations
            if len(translation_cache) > 500:
                # Alte Einträge löschen
                for k in list(translation_cache.keys())[:100]:
                    del translation_cache[k]

        return translations

    except Exception as e:
        log.error(f"Übersetzungsfehler (multi): {e}")
        return {}


# ────────────────────────────────────────────────
# FLAGGEN & SPRACHNAMEN
# ────────────────────────────────────────────────

LANG_FLAGS = {
    "DE": "🇩🇪", "FR": "🇫🇷", "PT": "🇧🇷", "EN": "🇬🇧",
    "JA": "🇯🇵", "ES": "🇪🇸", "RU": "🇷🇺",
    "ZH": "🇨🇳", "KO": "🇰🇷",
}

LANG_NAMES = {
    "DE": "German",               "FR": "French",
    "PT": "Brazilian Portuguese", "EN": "English",
    "JA": "Japanese",             "ES": "Spanish",
    "RU": "Russian",              "ZH": "Chinese",
    "KO": "Korean",
}

ALL_LANGS = [
    ("PT", "Brazilian Portuguese", "🇧🇷 Português"),
    ("EN", "English",              "🇬🇧 English"),
    ("JA", "Japanese",             "🇯🇵 日本語"),
    ("ZH", "Chinese",              "🇨🇳 中文"),
    ("KO", "Korean",               "🇰🇷 한국어"),
    ("ES", "Spanish",              "🇪🇸 Español"),
    ("RU", "Russian",              "🇷🇺 Русский"),
]

# ────────────────────────────────────────────────
# BOT SETUP
# ────────────────────────────────────────────────

intents = discord.Intents.default()
intents.message_content = True

bot = commands.Bot(
    command_prefix=["!t", "!"],
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
    if not LANGDETECT_AVAILABLE:
        log.warning("langdetect nicht installiert - nutze Fallback-Heuristik. Installiere mit: pip install langdetect")

    if BOT_LOG_CHANNEL_ID:
        channel = bot.get_channel(BOT_LOG_CHANNEL_ID)
        if channel:
            if errors:
                msg = "⚠️ **Übersetzer-Bot gestartet mit Fehlern:**\n" + "\n".join(errors)
            else:
                msg = (
                    "✅ **Übersetzer-Bot erfolgreich gestartet!**\n"
                    "🔧 tsprachen.py • geladen\n"
                    "⚡ Optimiert: Lokale Spracherkennung, AFC deaktiviert, Cache aktiv"
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
    embed.add_field(name="💾 Cache", value=f"Lang: {len(lang_cache)} | Trans: {len(translation_cache)}", inline=True)
    embed.set_footer(text="VHA Übersetzer-Bot • Optimiert", icon_url=LOGO_URL)
    await ctx.send(embed=embed)


# ────────────────────────────────────────────────
# WÜRFELSPIEL 🎲
# ────────────────────────────────────────────────

import random as _random

DICE_FACES = {1: "1️⃣", 2: "2️⃣", 3: "3️⃣", 4: "4️⃣", 5: "5️⃣", 6: "6️⃣"}

# Aktive Würfelduell-Herausforderungen: {channel_id: {challenger_id, challenger_name, roll}}
_dice_challenges: dict = {}


@bot.command(name="würfel", aliases=["dice", "roll", "dé", "dado", "кубик", "w6"])
async def cmd_wuerfel(ctx, seiten: int = 6):
    """
    Würfelspiel — mehrere Modi:
    !würfel          → wirf einen W6
    !würfel 20       → wirf einen W20 (oder beliebige Seitenzahl)
    !würfel duell    → fordere den nächsten Spieler zum Duell heraus
    """
    # Sonderfall: "duell" als Argument
    if ctx.invoked_with in ("würfel", "dice", "roll", "dé", "dado", "кубик", "w6"):
        # Prüfe ob erstes Argument "duell" ist — aber seiten wäre dann kein int
        pass

    if not (2 <= seiten <= 1000):
        await ctx.send("❌ Seitenzahl muss zwischen 2 und 1000 liegen.", delete_after=6)
        return

    result = _random.randint(1, seiten)
    face = DICE_FACES.get(result, "🎲")

    # Bewertung
    pct = result / seiten
    big_dice = DICE_FACES.get(result, "") if seiten == 6 else ""

    if pct == 1.0:
        de, fr, en = "🏆 **MAXIMUM!** Unglaublich!", "🏆 **MAXIMUM!** Incroyable!", "🏆 **MAXIMUM!** Incredible!"
        color = 0xF1C40F
    elif pct >= 0.8:
        de, fr, en = "🔥 Starker Wurf!", "🔥 Beau lancer!", "🔥 Strong roll!"
        color = 0x2ECC71
    elif pct >= 0.5:
        de, fr, en = "👍 Solider Wurf.", "👍 Lancer correct.", "👍 Solid roll."
        color = 0x3498DB
    elif pct >= 0.2:
        de, fr, en = "😬 Könnte besser sein...", "😬 Peut mieux faire...", "😬 Could be better..."
        color = 0xF39C12
    else:
        de, fr, en = "💀 Kritischer Misserfolg!", "💀 Échec critique!", "💀 Critical failure!"
        color = 0xE74C3C

    dice_line = f"# {big_dice} {result} {big_dice}" if big_dice else f"# {result}"
    embed = discord.Embed(title=f"{face} W{seiten}-Wurf / Lancer W{seiten} / W{seiten} Roll", color=color)
    embed.add_field(name=f"🎲 {ctx.author.display_name}", value=dice_line, inline=False)
    embed.add_field(name="🇩🇪", value=de, inline=True)
    embed.add_field(name="🇫🇷", value=fr, inline=True)
    embed.add_field(name="🇬🇧", value=en, inline=True)
    embed.set_footer(text=f"1–{seiten} möglich / possible", icon_url=LOGO_URL)
    await ctx.send(embed=embed)


@bot.command(name="duell", aliases=["duel", "duel🎲"])
async def cmd_duell(ctx):
    """
    Würfelduell gegen einen anderen Spieler.
    Erster Spieler tippt !duell → fordert heraus
    Zweiter Spieler tippt !duell → nimmt an und das Ergebnis wird ermittelt
    """
    channel_id = ctx.channel.id
    user_id = ctx.author.id

    # Läuft bereits eine Herausforderung in diesem Kanal?
    if channel_id in _dice_challenges:
        challenge = _dice_challenges[channel_id]

        # Derselbe User kann nicht gegen sich selbst spielen
        if challenge["challenger_id"] == user_id:
            await ctx.send(
                f"⏳ Du hast bereits ein Duell gestartet, {ctx.author.display_name}! "
                "Warte auf einen anderen Spieler.",
                delete_after=8
            )
            return

        # Herausforderer wird angenommen!
        del _dice_challenges[channel_id]

        roll1 = challenge["roll"]
        roll2 = _random.randint(1, 6)

        challenger_id   = challenge["challenger_id"]
        challenger_name = challenge["challenger_name"]
        opponent_id     = ctx.author.id
        opponent_name   = ctx.author.display_name

        if roll1 > roll2:
            winner_de = f"🏆 **{challenger_name}** gewinnt!"
            winner_fr = f"🏆 **{challenger_name}** gagne !"
            winner_en = f"🏆 **{challenger_name}** wins!"
            color = 0xF1C40F
            c_result, o_result = "win", "loss"
        elif roll2 > roll1:
            winner_de = f"🏆 **{opponent_name}** gewinnt!"
            winner_fr = f"🏆 **{opponent_name}** gagne !"
            winner_en = f"🏆 **{opponent_name}** wins!"
            color = 0xF1C40F
            c_result, o_result = "loss", "win"
        else:
            winner_de = "🤝 **Unentschieden!** Nochmal würfeln?"
            winner_fr = "🤝 **Égalité !** Rejouer ?"
            winner_en = "🤝 **Draw!** Roll again?"
            color = 0x9B59B6
            c_result, o_result = "draw", "draw"

        # Statistiken in MongoDB speichern (Name wird dabei aktualisiert)
        try:
            db_update_stats(challenger_id, challenger_name, c_result)
            db_update_stats(opponent_id, opponent_name, o_result)
        except Exception as e:
            log.error(f"DB stats error: {e}")

        embed = discord.Embed(
            title="🎲 Würfelduell — Ergebnis! / Résultat ! / Result!",
            color=color
        )
        embed.add_field(name=challenger_name, value=f"# {roll1}", inline=True)
        embed.add_field(name="VS", value="⚔️", inline=True)
        embed.add_field(name=opponent_name, value=f"# {roll2}", inline=True)
        embed.add_field(
            name="Ergebnis / Résultat / Result",
            value=f"🇩🇪 {winner_de}\n🇫🇷 {winner_fr}\n🇬🇧 {winner_en}",
            inline=False
        )
        embed.set_footer(text="VHA Würfelduell / Duel de dés / Dice Duel", icon_url=LOGO_URL)
        await ctx.send(embed=embed)

    else:
        # Neue Herausforderung starten
        roll = _random.randint(1, 6)
        _dice_challenges[channel_id] = {
            "challenger_id": user_id,
            "challenger_name": ctx.author.display_name,
            "roll": roll,
        }

        embed = discord.Embed(
            title="🎲 Würfelduell / Duel de dés / Dice Duel",
            color=0x9B59B6
        )
        embed.add_field(
            name=ctx.author.display_name,
            value=(
                f"🇩🇪 **{ctx.author.display_name}** fordert zum Würfelduell heraus! Tippe `!duell` um mitzuspielen!\n"
                f"🇫🇷 **{ctx.author.display_name}** lance un duel de dés ! Tape `!duell` pour jouer !\n"
                f"🇬🇧 **{ctx.author.display_name}** challenges you to a dice duel! Type `!duell` to join!\n\n"
                "🇩🇪 *(Die Würfel werden erst am Ende aufgedeckt)*\n"
                "🇫🇷 *(Les dés seront révélés à la fin)*\n"
                "🇬🇧 *(Dice are revealed at the end)*"
            ),
            inline=False
        )
        embed.set_footer(text="Herausforderung läuft... / Défi en cours... / Challenge running...", icon_url=LOGO_URL)
        await ctx.send(embed=embed)

        # Nach 5 Minuten automatisch ablaufen lassen
        await asyncio.sleep(300)
        if channel_id in _dice_challenges and _dice_challenges[channel_id]["challenger_id"] == user_id:
            del _dice_challenges[channel_id]
            try:
                await ctx.send(
                    f"⏰ **{ctx.author.display_name}**: "
                    f"🇩🇪 Würfelduell abgelaufen — niemand hat angenommen. "
                    f"🇫🇷 Duel expiré — personne n'a accepté. "
                    f"🇬🇧 Duel expired — nobody joined.",
                    delete_after=10
                )
            except Exception:
                pass



# ────────────────────────────────────────────────
# WÜRFEL-RANKING
# ────────────────────────────────────────────────

MEDALS = ["🥇", "🥈", "🥉"]

@bot.command(name="ranking", aliases=["rank", "stats", "leaderboard", "top", "classement", "rang"])
async def cmd_ranking(ctx, member: discord.Member = None):
    """
    !ranking          → Top-10 Würfelduelle
    !ranking @User    → Statistik eines bestimmten Spielers
    """
    if member is not None:
        # Einzelspieler-Statistik
        try:
            data = db_get_player(member.id)
        except Exception as e:
            await ctx.send(f"❌ DB-Fehler: {e}", delete_after=8)
            return

        if not data:
            embed = discord.Embed(
                description=(
                    f"🇩🇪 **{member.display_name}** hat noch keine Duelle gespielt.\n"
                    f"🇫🇷 **{member.display_name}** n'a pas encore joué.\n"
                    f"🇬🇧 **{member.display_name}** has not played yet."
                ),
                color=0x95A5A6
            )
            await ctx.send(embed=embed)
            return

        wins   = data.get("wins",   0)
        losses = data.get("losses", 0)
        draws  = data.get("draws",  0)
        games  = data.get("games",  0)
        wr     = round(wins / games * 100) if games else 0

        embed = discord.Embed(
            title=f"🎲 {data.get('name', member.display_name)}",
            color=0xF1C40F if wins > losses else 0x3498DB
        )
        embed.add_field(
            name="🇩🇪 Statistik  🇫🇷 Statistiques  🇬🇧 Stats",
            value=(
                f"🏆 **{wins}** Siege / Victoires / Wins\n"
                f"💀 **{losses}** Niederlagen / Défaites / Losses\n"
                f"🤝 **{draws}** Unentschieden / Égalités / Draws\n"
                f"🎲 **{games}** Spiele / Parties / Games\n"
                f"📊 **{wr}%** Winrate"
            ),
            inline=False
        )
        embed.set_footer(text="VHA Würfelranking", icon_url=LOGO_URL)
        await ctx.send(embed=embed)
        return

    # Top-10 Ranking
    try:
        top = db_get_ranking(10)
    except Exception as e:
        await ctx.send(f"❌ DB-Fehler: {e}", delete_after=8)
        return

    if not top:
        await ctx.send(
            "🇩🇪 Noch keine Daten vorhanden.  🇫🇷 Aucune donnée.  🇬🇧 No data yet.",
            delete_after=8
        )
        return

    lines = []
    for i, p in enumerate(top):
        medal  = MEDALS[i] if i < 3 else f"`{i+1}.`"
        wins   = p.get("wins",   0)
        losses = p.get("losses", 0)
        draws  = p.get("draws",  0)
        games  = p.get("games",  0)
        wr     = round(wins / games * 100) if games else 0
        lines.append(
            f"{medal} **{p['name']}** — 🏆 {wins}W / 💀 {losses}L / 🤝 {draws}D  *(📊 {wr}%)*"
        )

    embed = discord.Embed(
        title="🎲 Würfel-Ranking / Classement / Leaderboard",
        description="\n".join(lines),
        color=0xF1C40F
    )
    embed.set_footer(text="VHA Würfelranking  •  !ranking @User für Details", icon_url=LOGO_URL)
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
# SPRACHEN & RAUMSPRACHEN
# ────────────────────────────────────────────────

@bot.event
async def on_message(message: discord.Message):
    global processed_messages, processed_messages_set, translate_active

    if message.author.bot:
        return

    if (
        any(a.filename.lower().endswith(".gif") or (a.content_type and "gif" in a.content_type.lower())
            for a in message.attachments)
        or re.search(r'https?://\S*(?:tenor\.com|giphy\.com|youtube\.com|youtu\.be|youtube-nocookie\.com|yt\.be)\S*', message.content, re.IGNORECASE)
        or message.stickers
    ):
        return

    if message.id in processed_messages_set:
        return
    if len(processed_messages) == processed_messages.maxlen:
        processed_messages_set.discard(processed_messages[0])
    processed_messages.append(message.id)
    processed_messages_set.add(message.id)

    # Befehle (!t... und !...) niemals übersetzen
    msg_stripped = message.content.strip()
    if msg_stripped and msg_stripped.startswith("!"):
        await bot.process_commands(message)
        return

    if not translate_active:
        return

    content = message.content.strip()
    if not content or len(content) < 2:
        return

    if re.match(r'^https?://\S+$', content):
        return

    content_cleaned = re.sub(r'https?://\S+', '', content).strip()
    if not content_cleaned or len(content_cleaned) < 2:
        return
    content = content_cleaned

    now = time.time()
    if now - user_last_translation.get(message.author.id, 0) < TRANSLATION_COOLDOWN:
        log.info(f"SKIP cooldown [{message.channel.name}] user:{message.author.display_name} ({now - user_last_translation.get(message.author.id, 0):.1f}s < {TRANSLATION_COOLDOWN}s)")
        return
    user_last_translation[message.author.id] = now

    lang = await detect_language_llm(content)
    if lang == "OTHER":
        log.info(f"SKIP OTHER [{message.channel.name}] '{content[:30]}'")
        return

    FORUM_CHANNEL_ID = 1478065008960077866
    channel_id = message.channel.id
    parent_id = getattr(message.channel, 'parent_id', None)
    if channel_id == FORUM_CHANNEL_ID or parent_id == FORUM_CHANNEL_ID:
        room_setting = {"PT", "EN", "DE", "FR"}
    else:
        try:
            from tsprachen import get_room_langs
            room_setting = get_room_langs(message.channel.id)
            if room_setting is None and hasattr(message.channel, "parent_id") and message.channel.parent_id:
                room_setting = get_room_langs(message.channel.parent_id)
        except Exception:
            room_setting = None

    if room_setting is not None:
        if len(room_setting) == 0:
            return
        active_langs = room_setting
    else:
        active_langs = get_active_languages()

    ALL_LANGS_FULL = [
        ("DE", "German",               "🇩🇪 Deutsch"),
        ("FR", "French",               "🇫🇷 Français"),
        ("PT", "Brazilian Portuguese", "🇧🇷 Português"),
        ("EN", "English",              "🇬🇧 English"),
        ("JA", "Japanese",             "🇯🇵 日本語"),
        ("ZH", "Chinese",              "🇨🇳 中文"),
        ("KO", "Korean",               "🇰🇷 한국어"),
        ("ES", "Spanish",              "🇪🇸 Español"),
        ("RU", "Russian",              "🇷🇺 Русский"),
    ]

    lang_pool = ALL_LANGS_FULL

    target_langs = [
        t for t in lang_pool
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

    # ── PERFORMANCE LOGGING START ──
    import time as _time
    perf_start = _time.perf_counter()
    discord_delay_ms = int((_time.time() - message.created_at.timestamp()) * 1000)
    
    # Cache-Key vorhersagen (wie in translate_all)
    codes = [c for c, _, _ in target_langs]
    cache_key = f"{content[:200]}_{'_'.join(codes)}"
    cache_hit = cache_key in translation_cache

    try:
        translations = await translate_all(content, target_langs)
        fields = []
        for code, _, label in target_langs:
            translation = translations.get(code, "")
            if translation:
                fields.append((label, translation))

        if fields:
            await message.reply(embed=make_embed(fields), mention_author=False)

        total_ms = int((_time.perf_counter() - perf_start) * 1000)
        
        # Log mit allen Details
        log.info(
            f"PERF [{message.guild.name if message.guild else 'DM'}] "
            f"#{message.channel.name} | user:{message.author.display_name} | "
            f"lang:{lang}->{','.join(codes)} | "
            f"discord:{discord_delay_ms}ms | cache:{'HIT' if cache_hit else 'MISS'} | "
            f"total:{total_ms}ms | len:{len(content)}"
        )

    except Exception as e:
        total_ms = int((_time.perf_counter() - perf_start) * 1000)
        log.error(f"Übersetzungsfehler nach {total_ms}ms: {type(e).__name__} - {str(e)}")
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
