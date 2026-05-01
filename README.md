# 🌐 VHA Übersetzer-Bot — README

**Version:** Gemini 2.5 Flash  
**Hosting:** Render (Free Tier) + UptimeRobot Keep-Alive  
**Sprache:** Python 3.11+

---

## 📋 Übersicht

Der VHA Übersetzer-Bot ist der sekundäre Übersetzungsbot für die VHA Alliance Discord-Server. Er ergänzt den Haupt-Bot um zusätzliche Sprachen (JA, ZH, KO, ES, RU) und kann optional auch DE und FR pro Raum aktiviert bekommen. Beide Bots arbeiten parallel — jeder mit eigenem Gemini API-Key für maximale Performance.

---

## ⚙️ Technologie-Stack

| Komponente | Details |
|---|---|
| **KI-Modell** | Google Gemini 2.5 Flash (primär) |
| **Fallback** | Gemini 2.5 Flash Lite |
| **Datenbank** | MongoDB Atlas (`vhabot`) |
| **Web-Server** | Flask (Keep-Alive für Render) |
| **Discord-Library** | discord.py |
| **Hosting** | Render Free Tier |

---

## 🌍 Übersetzung

### Feste Sprachen (immer aktiv)
- 🇧🇷 Português
- 🇬🇧 English

### Zuschaltbare Sprachen (global via `!sprachen`)
- 🇯🇵 日本語
- 🇨🇳 中文
- 🇰🇷 한국어
- 🇪🇸 Español
- 🇷🇺 Русский

### Zusätzlich via `!traumsprachen` pro Raum
- 🇩🇪 Deutsch
- 🇫🇷 Français
- (+ alle globalen Sprachen)

### Übersetzungsregeln
- Immer **Du-Form** — niemals "Sie" oder "Vous"
- Kosenamen werden korrekt übersetzt (süße → chérie/honey, schatz → chéri/darling)
- Spielerbegriffe werden **nie** übersetzt: R1–R5, Koordinaten, Spielernamen, @mentions
- Emojis bleiben unverändert
- Natürliche, menschliche Übersetzung — kein wörtliches Wort-für-Wort

### Qualitätssicherung
Der Bot prüft jede Übersetzung automatisch auf:
- Identische Texte (nicht übersetzt) → verworfen
- Falsche Sprache im Feld → verworfen
- Wiederholungs-Loops (>15x dasselbe Wort) → verworfen
- Zu lange Ausgaben (>6x Originallänge) → abgeschnitten
- Zu ähnlich zum Original (>70% Wortüberschneidung bei 4+ Wörtern) → verworfen

---

## 📁 Dateistruktur

```
translator_bot.py   — Hauptdatei: Bot-Logik, Gemini-Calls, on_message
tsprachen.py        — Globale + Raumspracheinstellungen (MongoDB)
requirements.txt    — Python-Abhängigkeiten
```

---

## 🔑 Umgebungsvariablen (Render)

| Variable | Beschreibung |
|---|---|
| `DISCORD_TOKEN_TRANSLATOR` | Discord Bot Token (eigener Bot!) |
| `GEMINI_API_KEY_TRANSLATOR` | Google AI Studio API Key (eigener Key!) |
| `MONGODB_URI` | MongoDB Atlas Connection String (geteilt mit Haupt-Bot) |

> **Wichtig:** Jeder Bot hat einen **eigenen** Gemini API-Key damit sie sich nicht gegenseitig das Rate-Limit teilen.

---

## 💬 Befehle

### 🌐 Übersetzung
| Befehl | Beschreibung | Berechtigung |
|---|---|---|
| `!sprachen` | Globale Zielsprachen ein/ausschalten | R5, R4, DEV |
| `!traumsprachen [Kanal-ID]` | Sprachen für einen bestimmten Raum einstellen | R5, DEV |
| `!translate [Text]` | Text manuell übersetzen | Manage Messages |

### 📊 Status
| Befehl | Beschreibung | Berechtigung |
|---|---|---|
| `!ping` | Bot-Status, Latenz und Token-Verbrauch | Alle |

---

## 🗄️ MongoDB Collections

| Collection | Inhalt |
|---|---|
| `tsprachen` | Globale Spracheinstellungen des Übersetzer-Bots |
| `tsprachen_rooms` | Raumspezifische Einstellungen |

---

## 🔄 Zusammenspiel mit dem Haupt-Bot

| | Haupt-Bot | Übersetzer-Bot |
|---|---|---|
| **Feste Sprachen** | DE, FR, EN | PT, EN |
| **Optionale Sprachen** | PT, JA, ZH, KO, ES, RU | JA, ZH, KO, ES, RU |
| **Per Raum** | via `!raumsprachen` | via `!traumsprachen` (inkl. DE/FR) |
| **KI-Assistent** | ✅ (`!ai`) | ❌ |
| **Bildübersetzung** | ✅ (`!übersetze`) | ❌ |
| **Server-Tools** | ✅ | ❌ |
| **API-Key** | `GEMINI_API_KEY` | `GEMINI_API_KEY_TRANSLATOR` |

---

## 🔄 Modell-Fallback

```
gemini-2.5-flash          ← primär (beste Qualität)
    ↓ bei 503/429
gemini-2.5-flash-lite     ← schneller, leichter
```

---

## 📦 Installation (requirements.txt)

```
discord.py
flask
google-genai
pymongo
```

---

## 🚀 Deploy auf Render

1. Separates GitHub-Repo für den Übersetzer-Bot
2. Umgebungsvariablen in Render setzen
3. Start-Befehl: `python translator_bot.py`
4. UptimeRobot auf `https://[deine-render-url-translator]/ping` setzen (alle 5 Minuten)

---

## ⚠️ Bekannte Einschränkungen

- Render Free Tier: kann bei Inaktivität schlafen gehen (UptimeRobot verhindert das)
- Gemini kann bei hoher Last langsamer sein (Google-seitig)
- Beide Bots teilen dieselbe MongoDB — gleichzeitige Schreibzugriffe sind unproblematisch da verschiedene Collections
