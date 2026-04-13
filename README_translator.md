# 🌐 VHA Übersetzer-Bot — Mecha Fire

[![Python](https://img.shields.io/badge/Python-3.10%2B-blue)](https://python.org)
[![discord.py](https://img.shields.io/badge/discord.py-latest-5865F2)](https://discordpy.readthedocs.io)
[![MongoDB](https://img.shields.io/badge/MongoDB-Atlas-47A248)](https://mongodb.com/atlas)
[![Hosted on](https://img.shields.io/badge/Hosted-Render-46E3B7)](https://render.com)

Zweiter Bot im VHA-System — zuständig für **PT, EN und alle weiteren Sprachen**.  
Arbeitet parallel zum Haupt-Bot (`app.py`), der DE + FR übernimmt.  
Gleiche Infrastruktur · Eigener API-Key · Eigener Discord-Token

---

## 📁 Projektstruktur

```
├── translator_bot.py    # Bot-Einstiegspunkt, Groq-Wrapper, Auto-Translation, Flask-Keepalive
├── tsprachen.py         # Globale Sprachen + Raumsprachen per Button-UI (Cog)
├── requirements.txt     # Python-Abhängigkeiten
```

---

## 🔀 Aufgabenteilung im VHA-System

Der VHA-Server läuft mit **zwei Bots gleichzeitig**:

| Bot | Datei | Feste Sprachen | Zuschaltbar |
|---|---|---|---|
| **Haupt-Bot** | `app.py` | 🇩🇪 DE · 🇫🇷 FR | PT, EN, JA, ZH, KO, … |
| **Übersetzer-Bot** | `translator_bot.py` | 🇧🇷 PT · 🇬🇧 EN | JA, ZH, KO, ES, IT, RU, AR, TR, PL, NL |

Nachrichten auf DE oder FR werden **nicht** vom Übersetzer-Bot verarbeitet — der Haupt-Bot übernimmt diese.  
Nachrichten auf PT oder EN werden **nicht** vom Haupt-Bot verarbeitet — der Übersetzer-Bot übernimmt diese.  
So werden Doppelübersetzungen und gegenseitige Übersetzung von Bot-Nachrichten verhindert.

---

## ⚙️ Setup & Deployment

### Umgebungsvariablen

| Variable | Beschreibung |
|---|---|
| `DISCORD_TOKEN_TRANSLATOR` | Discord Bot Token (eigener Bot, **nicht** der Haupt-Bot) |
| `GROQ_API_KEY_TRANSLATOR` | Groq API Key (eigener Key für Rate-Limit-Trennung) |
| `MONGODB_URI` | MongoDB Atlas Connection String (gleiche DB wie Haupt-Bot) |
| `PORT` | Server-Port (Standard: `10000`, von Render gesetzt) |

> ⚠️ Der Bot verwendet **eigene Env-Variablen** (`DISCORD_TOKEN_TRANSLATOR`, `GROQ_API_KEY_TRANSLATOR`), damit Haupt-Bot und Übersetzer-Bot unabhängige Rate-Limits haben.

### Lokale Installation

```bash
pip install -r requirements.txt
python translator_bot.py
```

### Deployment auf Render

1. Repository auf GitHub pushen
2. Neuen **Web Service** auf [render.com](https://render.com) erstellen
3. Build Command: `pip install -r requirements.txt`
4. Start Command: `python translator_bot.py`
5. Umgebungsvariablen in den Render-Settings setzen
6. Der eingebaute Flask-Server (`/ping`) hält den Bot am Leben

### Requirements

```
discord.py
groq
flask
pymongo
dnspython
```

---

## 🌐 Auto-Translation

Der Bot übersetzt Nachrichten automatisch in alle aktivierten Sprachen (PT + EN immer, weitere optional).

**Funktionsweise:**
- Sprache wird zuerst regelbasiert per Unicode-Block erkannt (kein API-Call)
- Nur für lateinische Schriften wird ein LLaMA-Call ausgelöst
- Nachrichten unter 2 Zeichen, neutrale Wörter (`ok`, `lol`, `gg`…) und reine Links werden übersprungen
- GIFs, YouTube-Links und Sticker werden ignoriert
- Bot-Nachrichten werden grundsätzlich ignoriert (verhindert Übersetzungs-Loops)
- Pro User gilt ein Cooldown von **8 Sekunden**
- Max. **2 gleichzeitige** Groq-Calls (Semaphore), mit automatischem Retry und Backoff bei Rate-Limits
- Ein einziger API-Call übersetzt in **alle** Zielsprachen gleichzeitig

**Feste Sprachen (immer aktiv):** 🇧🇷 Português · 🇬🇧 English  
**Zuschaltbar:** 🇯🇵 日本語 · 🇨🇳 中文 · 🇰🇷 한국어 · 🇪🇸 Español · 🇮🇹 Italiano · 🇷🇺 Русский · 🇸🇦 العربية · 🇹🇷 Türkçe · 🇵🇱 Polski · 🇳🇱 Nederlands

---

## 📋 Befehle

**Präfix:** `!t`  
*(Abweichend vom Haupt-Bot `!` — verhindert Befehlskonflikte)*

---

### 🌍 Sprachen

#### `!tsprachen`
*(aliases: `!tlanguages`, `!tidiomas`, `!tlang`)*

Öffnet ein interaktives Button-Menü zur globalen Sprachsteuerung des Übersetzer-Bots.  
PT + EN sind immer aktiv und können nicht abgeschaltet werden.  
JA, ZH, KO, ES, IT, RU, AR, TR, PL, NL können per Button ein/ausgeschaltet werden.

Duplikate werden automatisch gelöscht (vorherige `!tsprachen`-Nachricht im Kanal).

**Berechtigung:** Administrator · R5 · R4 · DEV

---

#### `!traumsprachen [Kanal-ID]`

Öffnet ein Button-Menü für raum-spezifische Spracheinstellungen des Übersetzer-Bots.  
Überschreibt die globalen Einstellungen für den jeweiligen Kanal.

```
!traumsprachen 1234567890123456789
```

- Alle Sprachen (PT, EN + optional) können pro Kanal individuell ein/ausgeschaltet werden
- 📡 **Globale Einstellungen** — setzt den Kanal zurück (Eintrag in MongoDB wird gelöscht)
- 🚫 **Alle aus** — deaktiviert die Übersetzung für diesen Kanal dauerhaft

**Berechtigung:** Administrator · R5 · R4 · DEV

**Tipp:** Mit `!tkanalid` werden alle Kanal-IDs des Servers per DM zugeschickt.

---

#### `!tkanalid`
*(alias: `!tchannelid`)*

Schickt alle Textkanäle mit ihrer ID als Direktnachricht.  
Hilfreich für den Einsatz mit `!traumsprachen`.

**Berechtigung:** Administrator · R5 · R4 · DEV

---

### 🔧 Verwaltung

#### `!tping`

Zeigt den Status des Übersetzer-Bots: Latenz, heutigen Token-Verbrauch und aktive Sprachen.

---

#### `!ttranslate [on|off|status]`

Schaltet die Auto-Translation des Bots global ein oder aus.

```
!ttranslate on      # Aktivieren
!ttranslate off     # Deaktivieren
!ttranslate status  # Aktuellen Status anzeigen
```

**Berechtigung:** `manage_messages`-Berechtigung

---

## 🗄️ MongoDB Datenstruktur

**Datenbank:** `vhabot`  
*(Gleiche Datenbank wie der Haupt-Bot — separate Collections)*

| Collection | Inhalt |
|---|---|
| `tsprachen` | Globale Spracheinstellungen (`_id: "settings"`, Feld: `active`) |
| `tsprachen_rooms` | Raum-spezifische Sprachen (`_id: channel_id`, Felder: `langs`, `disabled`) |

> Die Collections `tsprachen` und `tsprachen_rooms` sind vollständig getrennt von `sprachen` und `raumsprachen` des Haupt-Bots. Beide Bots teilen sich nur die MongoDB-Verbindung, nicht die Sprachdaten.

---

## 🔐 Rollenberechtigungen

| Rolle | Berechtigungen |
|---|---|
| **Administrator** | Alle Befehle |
| **R5** | `!tsprachen`, `!traumsprachen`, `!tkanalid` |
| **R4** | `!tsprachen`, `!traumsprachen`, `!tkanalid` |
| **DEV** | `!tsprachen`, `!traumsprachen`, `!tkanalid` |

---

## 🤖 KI-Modell

| Modell | Verwendung |
|---|---|
| `llama-3.3-70b-versatile` | Spracherkennung + Text-Übersetzung |

**Rate-Limit-Handling:**
- Semaphore (max. 2 gleichzeitige Calls)
- Globale Pause bei 429 (exponentieller Backoff, max. 60s)
- Token-Verbrauch wird täglich in `translator_bot.log` geloggt und ist per `!tping` einsehbar

---

## 🌐 Flask Keep-Alive

Eingebetteter Flask-Server für Render:

- `GET /` → `"VHA Translator-Bot • Online"`
- `GET /ping` → `"pong"`

---

## 📝 Lizenz & Credits

Entwickelt für die **VHA Alliance** (Mecha Fire).  
Maintainer: **Noxxi-hub**
