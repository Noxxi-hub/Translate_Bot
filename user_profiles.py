"""
user_profiles.py — Nutzerprofile (Geschlecht) für bessere Übersetzungsgrammatik.
Wird von app.py (Befehl !ich) und translator_bot.py (Lesen) genutzt.
MongoDB Collection: vhabot.user_profiles
"""

import os
import logging
from pymongo import MongoClient

log = logging.getLogger("VHABot.UserProfiles")

# ────────────────────────────────────────────────
# MongoDB — shared Connection Pool
# ────────────────────────────────────────────────

_mongo_client = None

def _get_client():
    global _mongo_client
    if _mongo_client is None:
        _mongo_client = MongoClient(
            os.getenv("MONGODB_URI"),
            serverSelectionTimeoutMS=5000,
            connectTimeoutMS=5000,
            socketTimeoutMS=5000,
        )
    return _mongo_client

def get_col():
    return _get_client()["vhabot"]["user_profiles"]


# ────────────────────────────────────────────────
# Geschlechts-Mapping
# ────────────────────────────────────────────────

GENDER_ALIASES = {
    # Weiblich
    "frau": "female", "w": "female", "f": "female", "weiblich": "female",
    "female": "female", "femme": "female", "mujer": "female", "girl": "female",
    # Männlich
    "mann": "male", "m": "male", "männlich": "male",
    "male": "male", "homme": "male", "hombre": "male", "boy": "male",
    # Neutral
    "neutral": "neutral", "n": "neutral", "divers": "neutral",
    "non-binary": "neutral", "nonbinary": "neutral",
}

GENDER_LABELS = {
    "female": "Frau / Femme / Female 👩",
    "male":   "Mann / Homme / Male 👨",
    "neutral": "Neutral / Non-binary 🧑",
}

GENDER_CONTEXT = {
    "female": (
        "The person who wrote this message is female. "
        "Use correct feminine grammar in all languages: "
        "DE: weibliche Adjektivendungen (müde, glücklich, bereit); "
        "FR: formes féminines (contente, prête, heureuse, fatiguée); "
        "PT: formas femininas (-a endings); "
        "ES: formas femeninas (-a endings)."
    ),
    "male": (
        "The person who wrote this message is male. "
        "Use correct masculine grammar in all languages: "
        "DE: männliche Adjektivendungen (müde, glücklich, bereit — same for most); "
        "FR: formes masculines (content, prêt, heureux, fatigué); "
        "PT: formas masculinas (-o endings); "
        "ES: formas masculinas (-o endings)."
    ),
    "neutral": (
        "The person who wrote this message uses gender-neutral language. "
        "Use gender-neutral forms where possible in all languages."
    ),
}


# ────────────────────────────────────────────────
# Funktionen
# ────────────────────────────────────────────────

def set_user_gender(user_id: int, username: str, gender: str) -> bool:
    """Speichert das Geschlecht eines Nutzers. Gibt True bei Erfolg zurück."""
    try:
        col = get_col()
        col.update_one(
            {"_id": str(user_id)},
            {"$set": {
                "gender": gender,
                "username": username,
            }},
            upsert=True
        )
        return True
    except Exception as e:
        log.error(f"Fehler beim Speichern des Geschlechts für {user_id}: {e}")
        return False


def get_user_gender(user_id: int) -> str | None:
    """Gibt das gespeicherte Geschlecht zurück, oder None wenn nicht gesetzt."""
    try:
        col = get_col()
        doc = col.find_one({"_id": str(user_id)})
        return doc.get("gender") if doc else None
    except Exception as e:
        log.error(f"Fehler beim Lesen des Geschlechts für {user_id}: {e}")
        return None


def get_gender_context(user_id: int) -> str | None:
    """Gibt den Gemini-Prompt-Kontext für das Geschlecht zurück, oder None."""
    gender = get_user_gender(user_id)
    if gender and gender in GENDER_CONTEXT:
        return GENDER_CONTEXT[gender]
    return None
