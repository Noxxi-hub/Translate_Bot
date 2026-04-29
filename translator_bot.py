import discord
from discord.ext import commands
import os
import re
import time
import asyncio
import threading
import logging
from collections import deque
from datetime import datetime, timezone
from flask import Flask
from google import genai
from google.genai import types

# ────────────────────────────────────────────────
# LOGGING
# ────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        logging.FileHandler("gemini_usage.log", encoding="utf-8"),
        logging.StreamHandler()
    ]
)
log = logging.getLogger("VHABot")

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
GEMINI_MODEL = GEMINI_MODELS[0]  # für Kompatibilität
BOT_LOG_CHANNEL_ID = 1484252260614537247

# ────────────────────────────────────────────────
# GLOBALS & FLASK
# ────────────────────────────────────────────────

app = Flask(__name__)

processed_messages     = deque(maxlen=500)
processed_messages_set = set()

translate_active = True

gemini_client = genai.Client(api_key=os.getenv("GEMINI_API_KEY"))

# Semaphore: max. 4 gleichzeitige Gemini-Calls
gemini_semaphore = asyncio.Semaphore(8)

# Globale Rate-Limit-Pause
_gemini_rate_limit_until: float = 0.0

user_last_translation: dict[int, float] = {}
TRANSLATION_COOLDOWN = 2.0  # reduziert von 8.0 für Gemini

# Token-Zähler für den Tag
token_counter = {"prompt": 0, "completion": 0, "total": 0}


def run_flask():
    port = int(os.environ.get("PORT", 10000))
    app.run(host="0.0.0.0", port=port, debug=False, use_reloader=False)


@app.route("/")
def home():
    return "VHA Translator • Online"

@app.route("/ping")
def ping():
    return "pong"


# ────────────────────────────────────────────────
# GEMINI ASYNC WRAPPER mit Retry
# ────────────────────────────────────────────────

async def gemini_call(model: str, messages: list, temperature: float = 0.1,
                      max_tokens: int = 500, retries: int = 3) -> str:
    """
    Führt einen Gemini-API-Call asynchron aus mit automatischem Modell-Fallback.
    messages: OpenAI-kompatibles Format [{"role": "system"/"user", "content": "..."}]
    """
    global _gemini_rate_limit_until
    loop = asyncio.get_event_loop()

    # System-Prompt und User-Messages trennen
    system_text = None
    contents = []
    for msg in messages:
        role = msg["role"]
        content = msg["content"]
        if role == "system":
            system_text = content
        elif role == "user":
            if isinstance(content, str):
                contents.append(types.Content(
                    role="user",
                    parts=[types.Part(text=content)]
                ))
            elif isinstance(content, list):
                parts = []
                for item in content:
                    if item.get("type") == "text":
                        parts.append(types.Part(text=item["text"]))
                    elif item.get("type") == "image_url":
                        url = item["image_url"]["url"]
                        if url.startswith("data:"):
                            header, b64data = url.split(",", 1)
                            mime = header.split(":")[1].split(";")[0]
                            import base64 as _b64
                            raw = _b64.b64decode(b64data)
                            parts.append(types.Part(
                                inline_data=types.Blob(mime_type=mime, data=raw)
                            ))
                        else:
                            parts.append(types.Part(text=f"[Image URL: {url}]"))
                contents.append(types.Content(role="user", parts=parts))

    # Fallback-Kette durchprobieren
    last_error = None
    for model_name in GEMINI_MODELS:
        # thinking_config nur bei 2.5-Modellen (3.x hat es eingebaut)
        use_thinking = "2.5" in model_name
        config = types.GenerateContentConfig(
            temperature=temperature,
            max_output_tokens=max_tokens,
            system_instruction=system_text,
            thinking_config=types.ThinkingConfig(thinking_budget=0) if use_thinking else None,
        )

        wait = 4
        for attempt in range(retries):
            now = asyncio.get_event_loop().time()
            pause = _gemini_rate_limit_until - now
            if pause > 0:
                log.info(f"Rate-Limit-Pause: warte {pause:.1f}s")
                await asyncio.sleep(pause)

            async with gemini_semaphore:
                try:
                    resp = await loop.run_in_executor(
                        None,
                        lambda: gemini_client.models.generate_content(
                            model=model_name,
                            contents=contents,
                            config=config,
                        )
                    )
                    if resp.usage_metadata:
                        total = (resp.usage_metadata.prompt_token_count or 0) + \
                                (resp.usage_metadata.candidates_token_count or 0)
                        token_counter["prompt"]     += resp.usage_metadata.prompt_token_count or 0
                        token_counter["completion"] += resp.usage_metadata.candidates_token_count or 0
                        token_counter["total"]      += total
                        log.info(f"Tokens: +{total} (heute gesamt: {token_counter['total']})")
                    
                    if model_name != GEMINI_MODELS[0]:
                        log.info(f"FALLBACK OK → {model_name}")
                    return resp.text.strip()

                except Exception as e:
                    err = str(e)
                    last_error = err
                    if "429" in err or "quota" in err.lower() or "rate" in err.lower():
                        _gemini_rate_limit_until = asyncio.get_event_loop().time() + wait
                        log.warning(f"{model_name} Rate-Limit (Versuch {attempt+1}/{retries}) – warte {wait}s")
                        await asyncio.sleep(wait)
                        wait = min(wait * 2, 60)
                    elif "503" in err or "500" in err or "502" in err or "unavailable" in err.lower() or "server" in err.lower():
                        log.warning(f"{model_name} überlastet ({err[:50]}), versuche nächstes Modell...")
                        break  # sofort nächstes Modell
                    else:
                        log.error(f"Gemini-Fehler {model_name}: {e}")
                        break

        log.warning(f"Modell {model_name} fehlgeschlagen, fallback...")

    raise Exception(f"Alle Gemini-Modelle down. Letzter Fehler: {last_error}")


async def gemini_call_thinking(model: str, messages: list, temperature: float = 0.7,
                               max_tokens: int = 1000) -> str:
    """
    Gemini-Call MIT aktiviertem Thinking — nur für !ai verwendet.
    Für Übersetzungen → gemini_call() mit thinking_budget=0 verwenden.
    """
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

    config = types.GenerateContentConfig(
        temperature=temperature,
        max_output_tokens=max_tokens,
        system_instruction=system_text,
        # Thinking aktiv (kein thinking_budget gesetzt) — gut für komplexe Fragen
    )

    resp = await loop.run_in_executor(
        None,
        lambda: gemini_client.models.generate_content(
            model=model,
            contents=contents,
            config=config,
        )
    )
    return resp.text.strip()


# ────────────────────────────────────────────────
# SPRACHE ERKENNEN — regelbasiert (kein API-Call)
# ────────────────────────────────────────────────

# Cache (auch für LLM-Fallback)
lang_cache: dict[str, str] = {}

# Neutrale Wörter die keine Spracherkennung auslösen sollen
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

    if (hira + kata) / total > 0.15:  return "JA"
    if hangul / total > 0.15:         return "KO"
    if cjk / total > 0.15:            return "ZH"
    if arabic / total > 0.15:         return "AR"
    if cyril / total > 0.15:          return "RU"
    return None  # Lateinische Schrift → LLM nötig

# Nur für lateinische Texte deren Sprache unklar ist
async def detect_language_llm(text: str) -> str:
    """Lokale Erkennung – kein LLM Call, optimiert für kurze DE/FR Sätze."""
    stripped = text.strip()
    if not stripped or len(stripped) < 2:
        return "OTHER"
    
    # Neutral / Emojis → ignorieren
    if stripped.lower() in _NEUTRAL:
        return "OTHER"

    # 1. Script-Erkennung für nicht-lateinische Sprachen
    script = _script_detect(stripped)
    if script:
        return script

    t = f" {stripped.lower()} "  # Padding für Wortgrenzen

    # 2. Kurze Texte (<20 Zeichen): harte Heuristik für DE/FR
    # Das fixt "Ne bin da", "Was sagst du nicht", etc.
    if len(stripped) < 20:
        de_markers = [' ich ', ' bin ', ' da ', ' ne ', ' ja ', ' nein ', ' was ', ' du ', ' nicht ', ' mal ', ' hab ', ' habe ', ' ist ', ' ein ', ' der ', ' die ', ' das ', ' und ']
        fr_markers = [' je ', ' suis ', ' pas ', ' oui ', ' non ', ' tu ', ' vous ', ' est ', ' le ', ' la ', ' et ', ' pour ', ' quoi ']
        
        # Zähle Treffer
        de_hits = sum(1 for w in de_markers if w in t)
        fr_hits = sum(1 for w in fr_markers if w in t)
        
        if de_hits > 0 and de_hits >= fr_hits:
            return "DE"
        if fr_hits > 0 and fr_hits > de_hits:
            return "FR"
        # Wenn nichts passt, aber Text sieht deutsch aus (Umlaute)
        if any(c in stripped for c in 'äöüßÄÖÜ'):
            return "DE"

    # 3. Normale Heuristik für längere Texte
    if any(w in t for w in [' der ', ' die ', ' das ', ' und ', ' ich ', ' nicht ', ' ist ', ' ein ', ' zu ']):
        return "DE"
    if any(w in t for w in [' le ', ' la ', ' les ', ' et ', ' vous ', ' je ', ' suis ', ' pas ']):
        return "FR"
    if any(w in t for w in [' o ', ' que ', ' para ', ' com ', ' você ', ' voce ', ' não ', ' nao ']):
        return "PT"
    if any(w in t for w in [' el ', ' la ', ' y ', ' que ', ' para ', ' con ']):
        return "ES"
    if any(w in t for w in [' the ', ' and ', ' you ', ' is ', ' are ', ' i am ', ' not ']):
        return "EN"

    # 4. Fallback: bei lateinischer Schrift ohne Treffer → EN statt OTHER
    # (verhindert dass wir Nachrichten komplett skippen)
    return "EN"


# ────────────────────────────────────────────────
# ÜBERSETZEN — ALLE SPRACHEN IN EINEM CALL
# ────────────────────────────────────────────────

async def translate_all(text: str, target_langs: list) -> dict:
    """
    Übersetzt text in ALLE Zielsprachen in einem einzigen API-Call.
    Spart bis zu 80% der API-Requests.
    target_langs: list of (code, lang_name, label) tuples
    Gibt dict zurück: {code: übersetzter_text}
    """
    if not target_langs:
        return {}

    codes_str  = ", ".join(f"{code}={lang_name}" for code, lang_name, _ in target_langs)
    codes      = [code for code, _, _ in target_langs]
    json_keys  = ", ".join(f'"{code}": "..."' for code in codes)

    # Token-Limit dynamisch: ~1.5 Tokens/Zeichen x Anzahl Sprachen, mind. 1500, max. 6000
    estimated = max(1500, min(6000, int(len(text) * 1.5 * len(target_langs))))

    try:
        result = await gemini_call(
            model=GEMINI_MODEL,
            temperature=0.1,
            max_tokens=estimated,
            messages=[
                {
                    "role": "system",
                    "content": (
                        f"You are a human translator for Discord. Translate naturally like a bilingual friend, not word for word. Preserve tone, emotion, humor, intimacy. Adapt idioms. Keep names, mentions, emojis, game terms unchanged. Use natural spoken language. Output only translation.

"
                        f"Translate into: {codes_str}.
"

                        f"Rules:\n"
                        f"- Translate naturally and colloquially, like a real player would write\n"
                        f"- Keep game-specific terms, names, coordinates, and numbers as-is\n"
                        f"- If a word is unclear, choose the most natural game-chat interpretation\n"
                        f"- Do NOT add explanations, notes, or markdown\n"
                        f"- Reply ONLY with a valid JSON object, no extra text before or after:\n"
                        f"{{{json_keys}}}"
                    )
                },
                {"role": "user", "content": text}
            ]
        )

        # JSON parsen — robuster als Regex, löst das Chinesisch-Komma-Problem
        import json as _json
        clean = result.strip()
        if clean.startswith("```"):
            clean = clean.split("```")[1]
            if clean.startswith("json"):
                clean = clean[4:]
        clean = clean.strip()

        parsed = _json.loads(clean)
        translations = {}
        for code in codes:
            val = parsed.get(code, "").strip()
            if val and val.lower() != text.lower():
                translations[code] = val
        return translations

    except Exception as e:
        log.error(f"Übersetzungsfehler (multi): {e}")
        return {}


async def translate_text(text: str, target_lang_name: str) -> str:
    """Einzelübersetzung — nur noch für Reply-Gast-Sprachen verwendet."""
    try:
        return await gemini_call(
            model=GEMINI_MODEL,
            temperature=0.1,
            max_tokens=600,
            messages=[
                {
                    "role": "system",
                    "content": (
                        f"You are a human translator for Discord. Translate into {target_lang_name} naturally, preserving tone and meaning. Keep names and emojis. Output only translation."

                    )
                },
                {"role": "user", "content": text}
            ]
        )
    except Exception as e:
        log.error(f"Übersetzungsfehler ({target_lang_name}): {e}")
        return ""


# ────────────────────────────────────────────────
# FLAGGEN & SPRACHNAMEN
# ────────────────────────────────────────────────
# Import einmalig beim Start — nicht bei jedem on_message neu
def get_active_languages() -> set:
    try:
        from sprachen import get_active_langs
        return get_active_langs()
    except Exception:
        return {"DE", "FR"}  # Fallback Haupt-Bot


# Einmalig beim Modulstart importieren — nicht bei jeder Nachricht
try:
    from sprachen import get_active_langs as _sprachen_get_active
    from raumsprachen import get_room_langs as _raumsprachen_get_room
    def get_active_languages() -> set:
        try:
            return _sprachen_get_active()
        except Exception:
            return {"DE", "FR"}
    def _get_room_langs_safe(channel_id: int, guild_id: int = None):
        try:
            return _raumsprachen_get_room(channel_id, guild_id)
        except Exception:
            return None
except Exception:
    def get_active_languages() -> set:
        return {"DE", "FR"}
    def _get_room_langs_safe(channel_id: int):
        return None

LANG_FLAGS = {
    "DE": "🇩🇪", "FR": "🇫🇷", "PT": "🇧🇷", "EN": "🇬🇧",
    "JA": "🇯🇵", "ES": "🇪🇸", "IT": "🇮🇹", "RU": "🇷🇺",
    "ZH": "🇨🇳", "AR": "🇸🇦", "KO": "🇰🇷", "TR": "🇹🇷",
    "PL": "🇵🇱", "NL": "🇳🇱",
}

LANG_NAMES = {
    "DE": "German", "FR": "French", "PT": "Brazilian Portuguese",
    "EN": "English", "JA": "Japanese", "ES": "Spanish",
    "IT": "Italian", "RU": "Russian", "ZH": "Chinese",
    "AR": "Arabic", "KO": "Korean", "TR": "Turkish",
    "PL": "Polish", "NL": "Dutch",
}

# ────────────────────────────────────────────────
# BOT SETUP
# ────────────────────────────────────────────────

intents = discord.Intents.default()
intents.message_content = True

bot = commands.Bot(
    command_prefix="!",
    intents=intents,
    help_command=None,
    case_insensitive=True
)


bot_ready = False  # Verhindert doppeltes Laden

@bot.event
async def on_ready():
    global bot_ready
    if bot_ready:
        return  # Bereits initialisiert → überspringen
    bot_ready = True
    errors = []

    try:
        await bot.load_extension("koordinaten")
    except Exception as e:
        errors.append(f"❌ koordinaten: {e}")

    try:
        await bot.load_extension("timer")
    except Exception as e:
        errors.append(f"❌ timer: {e}")

    try:
        from bilduebersetzer import setup as setup_bild
        await setup_bild(bot, gemini_call)
    except Exception as e:
        errors.append(f"❌ bilduebersetzer: {e}")

    try:
        await bot.load_extension("spieler")
    except Exception as e:
        errors.append(f"❌ spieler: {e}")

    try:
        from event import setup as setup_event
        await setup_event(bot, gemini_call)
    except Exception as e:
        errors.append(f"❌ event: {e}")

    try:
        await bot.load_extension("log")
    except Exception as e:
        errors.append(f"❌ log: {e}")

    try:
        await bot.load_extension("raumsprachen")
    except Exception as e:
        errors.append(f"❌ raumsprachen: {e}")

    try:
        await bot.load_extension("sprachen")
    except Exception as e:
        errors.append(f"❌ sprachen: {e}")

    try:
        await bot.load_extension("svs")
    except Exception as e:
        errors.append(f"❌ svs: {e}")

    try:
        await bot.load_extension("server")
    except Exception as e:
        errors.append(f"❌ server: {e}")

    log.info(f"→ {bot.user}  •  ONLINE  •  {discord.utils.utcnow():%Y-%m-%d %H:%M UTC}")

    if BOT_LOG_CHANNEL_ID:
        channel = bot.get_channel(BOT_LOG_CHANNEL_ID)
        if channel:
            if errors:
                msg = "⚠️ **Bot gestartet mit Fehlern:**\n" + "\n".join(errors)
            else:
                msg = (
                    "✅ **Bot erfolgreich gestartet!**\n"
                    "🔧 koordinaten.py • geladen\n"
                    "🔧 timer.py • geladen\n"
                    "🔧 bilduebersetzer.py • geladen\n"
                    "🔧 spieler.py • geladen\n"
                    "🔧 event.py • geladen\n"
                    "🔧 log.py • geladen\n"
                    "🔧 raumsprachen.py • geladen\n"
                    "🔧 sprachen.py • geladen\n"
                    "🔧 svs.py • geladen\n"
                    "🔧 server.py • geladen"
                )
            await channel.send(msg)

# ────────────────────────────────────────────────
# BEFEHLE
# ────────────────────────────────────────────────

@bot.command(name="help")
async def cmd_help(ctx):
    embed = discord.Embed(
        title="VHA Bot – Befehle / Commandes / Comandos",
        color=0x5865F2
    )
    embed.set_author(name="VHA ALLIANCE", icon_url=LOGO_URL)

    embed.add_field(
        name="🌐 Übersetzer / Traducteur / Tradutor",
        value=(
            "`!translate on` / `!translate off` – An • Aus / Activer • Désactiver / Ativar • Desativar\n"
            "`!translate status` – Status / Statut / Estado\n"
            "`!ai [Text]` – KI fragen / Poser une question / Perguntar à IA\n"
            "`!übersetze` / `!traduire` – Bild übersetzen / Traduire image / Traduzir imagem"
        ),
        inline=False
    )

    embed.add_field(
        name="📍 Koordinaten / Coordonnées / Coordenadas  🔐 R5 • R4",
        value=(
            "`!koordinaten` / `!coordonnees` – Liste mit 🗑️ Delete-Buttons\n"
            "`!koordinaten add NAME R X Y` – Hinzufügen / Ajouter / Adicionar"
        ),
        inline=False
    )

    embed.add_field(
        name="👥 Spieler-IDs / Joueurs / Jogadores  🔐 R5 • R4",
        value=(
            "`!spieler` / `!joueur` – Liste mit 🗑️ Delete-Buttons\n"
            "`!spieler add NAME ID` – Hinzufügen / Ajouter / Adicionar\n"
            "`!spieler suche NAME/ID` – Suchen / Rechercher / Pesquisar"
        ),
        inline=False
    )

    embed.add_field(
        name="⏱️ Timer / Rappel / Lembrete  🔐 R5 • R4",
        value=(
            "`!timer 2h Kriegsstart` / `!rappel 2h Event` – Timer setzen\n"
            "`!timer list` – Aktive Timer mit 🗑️ Delete-Buttons\n"
            "⏳ Formate: `30m` • `2h` • `1h30m` • `3d`"
        ),
        inline=False
    )

    embed.add_field(
        name="🎮 Events  🔐 R5 • R4",
        value=(
            "`!event` – Event aus Screenshot erkennen & Timer setzen\n"
            "Als Reply auf Event-Screenshot tippen / En réponse à une capture d'écran"
        ),
        inline=False
    )

    embed.add_field(
        name="⚔️ SVS Koordinaten  🔐 R5 • R4",
        value=(
            "`!svs` – Alle Server & Koordinaten\n"
            "`!svs R77` – Server R77 mit 🗑️ Delete-Buttons\n"
            "`!svs server` – Verfügbare Server\n"
            "`!svs add SERVER NAME R X Y` – Hinzufügen"
        ),
        inline=False
    )

    embed.add_field(
        name="🌐 Sprachen / Langues / Idiomas  🔐 R5 • R4",
        value=(
            "`!sprachen` / `!languages` / `!idiomas` – Globale Sprachen ein/ausschalten mit Buttons\n"
            "`!raumsprachen [Kanal-ID]` – Sprachen nur für einen bestimmten Raum einstellen (nur Bot-Kanal, nur R5/Dev)\n"
            "`!kanalid` – Alle Kanäle mit ID als Direktnachricht (für !raumsprachen)\n"
            "💡 Kein Eintrag = globale Einstellungen • 🚫 Deaktivieren = keine Übersetzung im Raum"
        ),
        inline=False
    )

    embed.add_field(
        name="🏗️ Server-Struktur  🔐 Bot DEV",
        value=(
            "`!server export` – Aktuelle Struktur in MongoDB speichern\n"
            "`!server preview` – Gespeicherte Struktur anzeigen\n"
            "`!server import` – Struktur auf neuem Server erstellen"
        ),
        inline=False
    )

    embed.add_field(
        name="🗑️ Kanal leeren  🔐 Bot DEV",
        value=(
            "`!clean` – Alle Nachrichten im aktuellen Kanal löschen (mit Bestätigung)\n"
            "`!clean 50` – Bestimmte Anzahl Nachrichten löschen (1–1000)\n"
            "⚠️ Nur Nachrichten jünger als 14 Tage können gelöscht werden"
        ),
        inline=False
    )

    embed.add_field(
        name="📊 Status",
        value="`!ping` – Bot-Status / Latenz",
        inline=False
    )

    embed.set_thumbnail(url=LOGO_URL)
    embed.set_footer(text="VHA - Powering Communication", icon_url=LOGO_URL)
    await ctx.send(embed=embed)


@bot.command(name="ping")
async def cmd_ping(ctx):
    latency = round(bot.latency * 1000)
    embed = discord.Embed(
        title="🏓 Pong!",
        color=0x57F287 if latency < 200 else 0xF39C12
    )
    embed.add_field(name="📡 Latenz / Latence", value=f"`{latency}ms`", inline=True)
    embed.add_field(name="📊 Tokens heute / Today", value=f"`{token_counter['total']}`", inline=True)
    embed.set_footer(text="VHA Bot • Online", icon_url=LOGO_URL)
    await ctx.send(embed=embed)


@bot.command(name="translate")
@commands.has_permissions(manage_messages=True)
async def cmd_translate(ctx, action: str = None):
    global translate_active

    if action is None:
        await ctx.send(
            "❓ Benutzung: `!translate on` / `!translate off` / `!translate status`\n"
            "Usage: `!translate on` / `!translate off` / `!translate status`"
        )
        return

    action = action.lower()

    if action == "on":
        translate_active = True
        embed = discord.Embed(title="VHA System • Übersetzung", color=0x57F287)
        embed.add_field(name="Deutsch ↔ Français ↔ Português", value="Aktiviert / Activée / Ativada", inline=False)
        await ctx.send(embed=embed)

    elif action == "off":
        translate_active = False
        embed = discord.Embed(title="VHA System • Übersetzung", color=0xED4245)
        embed.add_field(name="Deutsch ↔ Français ↔ Português", value="Deaktiviert / Désactivée / Desativada", inline=False)
        await ctx.send(embed=embed)

    elif action == "status":
        if translate_active:
            embed = discord.Embed(title="VHA System • Übersetzung", color=0x57F287)
            embed.add_field(name="Deutsch ↔ Français ↔ Português", value="Aktiviert / Activée / Ativada", inline=False)
        else:
            embed = discord.Embed(title="VHA System • Übersetzung", color=0xED4245)
            embed.add_field(name="Deutsch ↔ Français ↔ Português", value="Deaktiviert / Désactivée / Desativada", inline=False)
        await ctx.send(embed=embed)

    else:
        await ctx.send(
            "❓ Unbekannte Option. Benutze: `!translate on` / `!translate off` / `!translate status`"
        )


@cmd_translate.error
async def translate_error(ctx, error):
    if isinstance(error, commands.MissingPermissions):
        await ctx.send("❌ Du hast keine Berechtigung dafür. / Tu n'as pas la permission.")


@bot.command(name="ai")
@commands.cooldown(1, 12, commands.BucketType.user)
async def cmd_ai(ctx, *, question: str = None):
    if not question or not question.strip():
        await ctx.send("Beispiel: `!ai Qui est la VHA ?`  oder  `!ai Was ist die VHA?`")
        return

    thinking = await ctx.send("**Denke nach …** 🧠")

    lang = await detect_language_llm(question)
    flag = LANG_FLAGS.get(lang, "🌐")
    footer = f"Antwort in {lang}"

    try:
        answer = await gemini_call_thinking(
            model=GEMINI_MODEL,
            temperature=0.7,
            max_tokens=1000,
            messages=[
                {
                    "role": "system",
                    "content": (
                        "Du bist ein freundlicher VHA-Alliance Assistent. "
                        "Antworte IMMER in derselben Sprache wie die Frage. "
                        "Natürlich und direkt."
                    )
                },
                {"role": "user", "content": question.strip()}
            ]
        )
        color = 0x5865F2
    except Exception as e:
        answer = f"Fehler: {str(e)}"
        color = 0xFF0000
        footer = "Fehler"

    embed = discord.Embed(title=f"VHA KI • Antwort {flag}", description=answer, color=color)
    embed.set_author(name="VHA ALLIANCE", icon_url=LOGO_URL)
    embed.add_field(name="→ Deine Frage", value=question[:900], inline=False)
    embed.set_footer(text=f"VHA • Gemini • {GEMINI_MODEL} • {footer}", icon_url=LOGO_URL)
    await thinking.edit(embed=embed)


# ────────────────────────────────────────────────
# KANAL-IDs ANZEIGEN
# ────────────────────────────────────────────────

@bot.command(name="kanalid", aliases=["channelid", "kanalids"])
async def cmd_kanalid(ctx):
    """Zeigt alle Textkanäle mit ihrer ID — nur für den Aufrufer sichtbar."""
    if not ctx.author.guild_permissions.administrator:
        member_roles = {r.name.upper() for r in ctx.author.roles}
        if not member_roles & {"R5", "R4", "DEV"}:
            await ctx.send("❌ Keine Berechtigung.", delete_after=5)
            return

    lines = []
    for category, channels in ctx.guild.by_category():
        cat_name = category.name if category else "Ohne Kategorie"
        text_channels = [c for c in channels if isinstance(c, discord.TextChannel)]
        if not text_channels:
            continue
        lines.append(f"**{cat_name}**")
        for ch in text_channels:
            lines.append(f"• #{ch.name} — `{ch.id}`")

    # Aufteilen falls zu lang für eine Nachricht
    chunks = []
    current = []
    length = 0
    for line in lines:
        if length + len(line) > 1800:
            chunks.append("\n".join(current))
            current = [line]
            length = len(line)
        else:
            current.append(line)
            length += len(line)
    if current:
        chunks.append("\n".join(current))

    for i, chunk in enumerate(chunks):
        embed = discord.Embed(
            title=f"📋 Kanal-IDs • {ctx.guild.name}" + (f" ({i+1}/{len(chunks)})" if len(chunks) > 1 else ""),
            description=chunk,
            color=0x5865F2
        )
        embed.set_footer(text="Nur für dich sichtbar • Für !raumsprachen [ID] verwenden")
        await ctx.author.send(embed=embed)

    await ctx.send("📬 Ich habe dir die Kanal-IDs als Direktnachricht geschickt!", delete_after=8)


# ────────────────────────────────────────────────
# KANAL LEEREN
# ────────────────────────────────────────────────

NOXXI_ID = 1464651603654086748

@bot.command(name="clean", aliases=["clear", "purge", "löschen"])
async def cmd_clean(ctx, menge: int = None):
    """Löscht Nachrichten im aktuellen Kanal. Nur für NOXXI."""

    if ctx.author.id != NOXXI_ID:
        await ctx.send("❌ Dieser Befehl ist nur für ausgewählte Personen.", delete_after=5)
        return

    # Befehlsnachricht sofort löschen
    try:
        await ctx.message.delete()
    except Exception:
        pass

    # Ohne Zahl → alles löschen (in Blöcken, Discord-Limit: 100 pro Request)
    if menge is None:
        confirm_msg = await ctx.send(
            "⚠️ **Alle Nachrichten löschen?**\n"
            "Reagiere mit ✅ zum Bestätigen oder ❌ zum Abbrechen.\n"
            "*(Nur Nachrichten jünger als 14 Tage können gelöscht werden)*",
        )
        await confirm_msg.add_reaction("✅")
        await confirm_msg.add_reaction("❌")

        def check(reaction, user):
            return (
                user == ctx.author
                and str(reaction.emoji) in ["✅", "❌"]
                and reaction.message.id == confirm_msg.id
            )

        import asyncio as _asyncio
        try:
            reaction, _ = await bot.wait_for("reaction_add", timeout=30.0, check=check)
        except _asyncio.TimeoutError:
            await confirm_msg.edit(content="⏰ Timeout — Abgebrochen.", delete_after=5)
            return

        if str(reaction.emoji) == "❌":
            await confirm_msg.edit(content="❌ Abgebrochen.", delete_after=5)
            return

        await confirm_msg.delete()
        status = await ctx.send("🗑️ Lösche alle Nachrichten...")

        deleted_total = 0
        while True:
            deleted = await ctx.channel.purge(limit=100, before=status)
            deleted_total += len(deleted)
            if len(deleted) < 100:
                break

        await status.edit(
            content=f"✅ **{deleted_total} Nachrichten gelöscht.**\n"
                    f"*(Diese Meldung verschwindet in 8 Sekunden)*"
        )
        await _asyncio.sleep(8)
        try:
            await status.delete()
        except Exception:
            pass

    else:
        # Bestimmte Anzahl löschen
        if menge < 1 or menge > 1000:
            await ctx.send("❌ Bitte eine Zahl zwischen 1 und 1000 angeben.", delete_after=6)
            return

        import asyncio as _asyncio
        deleted = await ctx.channel.purge(limit=menge)
        status = await ctx.send(
            f"✅ **{len(deleted)} Nachrichten gelöscht.**\n"
            f"*(Diese Meldung verschwindet in 6 Sekunden)*"
        )
        await _asyncio.sleep(6)
        try:
            await status.delete()
        except Exception:
            pass


@cmd_clean.error
async def clean_error(ctx, error):
    if isinstance(error, commands.BadArgument):
        await ctx.send("❌ Ungültige Zahl. Beispiel: `!clean 50`", delete_after=6)


# ────────────────────────────────────────────────
# AUTOMATISCHE ÜBERSETZUNG
# ────────────────────────────────────────────────

@bot.event
async def on_message(message: discord.Message):
    global processed_messages, processed_messages_set, translate_active

    if message.author.bot:
        return

    # Forum-Raum: Haupt-Bot schweigt hier komplett
    FORUM_CHANNEL_ID = 1478065008960077866
    channel_id = message.channel.id
    parent_id = getattr(message.channel, 'parent_id', None)
    if channel_id == FORUM_CHANNEL_ID or parent_id == FORUM_CHANNEL_ID:
        return

    # ── GIF & YOUTUBE SPERRE (nur ignorieren, keine API-Calls) ───────────────
    _SKIP_URL_PATTERN = re.compile(
        r'https?://\S*(?:tenor\.com|giphy\.com|youtube\.com|youtu\.be|youtube-nocookie\.com|yt\.be)\S*',
        re.IGNORECASE
    )
    if (
        any(a.filename.lower().endswith(".gif") or (a.content_type and "gif" in a.content_type.lower())
            for a in message.attachments)
        or _SKIP_URL_PATTERN.search(message.content)
        or message.stickers
    ):
        return
    # ── ENDE GIF & YOUTUBE SPERRE ────────────────────────────────────────────

    if message.id in processed_messages_set:
        return
    if len(processed_messages) == processed_messages.maxlen:
        oldest = processed_messages[0]
        processed_messages_set.discard(oldest)
    processed_messages.append(message.id)
    processed_messages_set.add(message.id)

    if message.content.startswith(bot.command_prefix):
        await bot.process_commands(message)
        return

    if not translate_active:
        return

    content = message.content.strip()

    # Kein Text-Inhalt (nur Anhänge, GIFs, Sticker, Embeds) → skip
    if not content:
        return

    # Zu kurz → skip
    if len(content) < 2:
        return

    # Nur ein Link → skip (inkl. Tenor/Giphy GIFs)
    if re.match(r'^https?://\S+$', content):
        return

    # Tenor / Giphy GIF-Links rausfiltern (auch wenn Text dabei)
    content_cleaned = re.sub(r'https?://\S+', '', content).strip()
    if not content_cleaned or len(content_cleaned) < 2:
        return
    content = content_cleaned

    # Cooldown pro User
    now = time.time()
    last = user_last_translation.get(message.author.id, 0)
    if now - last < TRANSLATION_COOLDOWN:
        return
    user_last_translation[message.author.id] = now

    # Sprache erkennen
    lang = await detect_language_llm(content)
    if lang == "OTHER":
        return

    # Reply-Ziel Sprache prüfen
    reply_target_lang = None
    if message.reference and message.reference.resolved:
        ref = message.reference.resolved
        if isinstance(ref, discord.Message) and not ref.author.bot:
            ref_lang = await detect_language_llm(ref.content.strip())
            if ref_lang not in ("DE", "FR", "PT", "EN", "OTHER"):
                reply_target_lang = ref_lang

    author_name = message.author.display_name

    def make_multi_embed(fields: list, color: int = 0x3498DB) -> discord.Embed:
        embed = discord.Embed(title=f"💬 • {author_name}", color=color)
        for flag, text in fields:
            # Discord Embed Felder max. 1024 Zeichen - aufteilen wenn nötig
            if len(text) <= 1000:
                embed.add_field(name=flag, value=text, inline=False)
            else:
                # Ersten Teil mit Flagge, Rest als Fortsetzung
                chunks = [text[i:i+1000] for i in range(0, len(text), 1000)]
                embed.add_field(name=flag, value=chunks[0], inline=False)
                for chunk in chunks[1:]:
                    embed.add_field(name="↳", value=chunk, inline=False)
        embed.set_footer(text="VHA Translator", icon_url=LOGO_URL)
        return embed

    try:
        fields = []

        # ── Raum-spezifische Sprachen prüfen ──
        # Raum hat eigene Einstellungen → diese nutzen (überschreibt globale)
        # Raum hat KEINE Einstellungen → globale Sprachen nutzen (normales Verhalten)
        # Raum wurde explizit deaktiviert (leere Liste) → gar nicht übersetzen
        # ── Raum-spezifische Sprachen prüfen ──
        room_langs = _get_room_langs_safe(message.channel.id, message.guild.id if message.guild else None)

        if room_langs is not None:
            if len(room_langs) == 0:
                return  # Explizit deaktiviert
            active_langs = room_langs
        else:
            active_langs = get_active_languages()  # Globale Einstellungen

        # Haupt-Bot: feste Zielsprachen DE+FR, Rest zuschaltbar
        # PT/EN/JA/ZH/KO werden vom Übersetzer-Bot übernommen
        ALL_LANGS = [
            ("DE", "German",               "🇩🇪 Deutsch"),
            ("FR", "French",               "🇫🇷 Français"),
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

        # Haupt-Bot übersetzt NUR in seine aktiven Sprachen
        # Nachrichten die bereits DE oder FR sind → auch übersetzen
        # Nachrichten in PT/EN/JA/ZH/KO → Übersetzer-Bot macht das
        target_langs = [
            t for t in ALL_LANGS
            if t[0] != lang and t[0] in active_langs
        ]

        # Wenn keine Zielsprachen → skip (Übersetzer-Bot übernimmt)
        if not target_langs:
            return

        # Ein einziger API-Call für alle Sprachen → spart 80% der Requests
        translations = await translate_all(content, target_langs)
        for code, lang_name, label in target_langs:
            translation = translations.get(code, "")
            if translation:
                fields.append((label, translation))

        # Reply auf Gast → auch in Gastsprache übersetzen
        if reply_target_lang and reply_target_lang not in active_langs:
            guest_text = await translate_text(content, LANG_NAMES.get(reply_target_lang, reply_target_lang))
            guest_flag = LANG_FLAGS.get(reply_target_lang, "🌐")
            if guest_text and guest_text.lower() != content.lower():
                fields.append((guest_flag, guest_text))

        if fields:
            color = 0x9B59B6 if lang not in ("DE", "FR", "PT", "EN", "JA", "ZH", "KO") else 0x3498DB
            await message.reply(embed=make_multi_embed(fields, color), mention_author=False)

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

    token = os.getenv("DISCORD_TOKEN")
    if not token:
        log.error("DISCORD_TOKEN fehlt!")
        exit(1)

    bot.run(token)
