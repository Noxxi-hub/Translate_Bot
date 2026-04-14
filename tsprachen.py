# ════════════════════════════════════════════════
#  tsprachen.py  •  VHA Übersetzer-Bot
#  Globale Sprachen + Raumsprachen per Button
#  PT + EN immer aktiv | DE + FR nur bei Raumsprachen
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

# Sprachen die immer aktiv sind
FIXED_LANGS = {"PT", "EN"}

# Globale zuschaltbare Sprachen (ohne DE/FR)
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

# Nur bei Raumsprachen verfügbar
ROOM_ONLY_LANGS = {
    "DE": {"flag": "🇩🇪", "name": "Deutsch"},
    "FR": {"flag": "🇫🇷", "name": "Français"},
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
    try:
        col = get_col()
        doc = col.find_one({"_id": "settings"})
        if not doc:
            default = {"PT", "EN"}
            col.update_one({"_id": "settings"}, {"$set": {"active": list(default)}}, upsert=True)
            return default
        active = set(doc.get("active", []))
        active.update(FIXED_LANGS)
        return active
    except Exception as e:
        log.error(f"Fehler beim Laden der globalen Sprachen: {e}")
        return {"PT", "EN"}

def set_room_langs(channel_id: int, langs: set | None, disabled: bool = False):
    try:
        col = get_room_col()
        if langs is None and not disabled:
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

def get_room_langs(channel_id: int) -> set | None:
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

def has_permission(member: discord.Member) -> bool:
    if member.guild_permissions.administrator:
        return True
    member_roles = {r.name.upper() for r in member.roles}
    return bool(member_roles & ALLOWED_ROLES)


# ────────────────────────────────────────────────
# Globale Sprachen View
# ────────────────────────────────────────────────

class GlobalSprachenView(discord.ui.View):
    def __init__(self, author: discord.Member = None):
        super().__init__(timeout=None)          # Persistent
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
            if self.author and interaction.user.id != self.author.id:
                return await interaction.response.send_message("❌ Nur der Befehls-Ausführer darf ändern.", ephemeral=True)

            active = get_active_langs()
            if code in active:
                active.discard(code)
                action = "deaktiviert"
            else:
                active.add(code)
                action = "aktiviert"

            try:
                col = get_col()
                col.update_one({"_id": "settings"}, {"$set": {"active": list(active)}}, upsert=True)
            except Exception as e:
                return await interaction.response.send_message(f"❌ Datenbankfehler: {e}", ephemeral=True)

            self._update_buttons()
            await interaction.response.edit_message(embed=self._make_embed(), view=self)
            await interaction.followup.send(f"{OPTIONAL_LANGS[code]['flag']} **{OPTIONAL_LANGS[code]['name']}** {action}!", ephemeral=True)

        return callback

    def _make_embed(self) -> discord.Embed:
        active = get_active_langs()
        embed = discord.Embed(title="🌐 Übersetzer-Bot • Globale Sprachen", color=0x2ECC71)
        embed.set_author(name="VHA Übersetzer-Bot", icon_url=LOGO_URL)
        embed.add_field(name="🔒 Immer aktiv", value="🇧🇷 Português • 🇬🇧 English", inline=False)

        status_lines = [f"{info['flag']} {info['name']}: **{'✅ Aktiv' if code in active else '❌ Inaktiv'}**"
                        for code, info in OPTIONAL_LANGS.items()]
        embed.add_field(name="🔄 Ein-/Ausschaltbar", value="\n".join(status_lines), inline=False)
        embed.set_footer(text="Klicke auf einen Button zum Ein-/Ausschalten", icon_url=LOGO_URL)
        return embed


# ────────────────────────────────────────────────
# Raumsprachen View
# ────────────────────────────────────────────────

class RaumSprachenView(discord.ui.View):
    def __init__(self, author: discord.Member = None, channel_id: int = 0, channel_name: str = "", current: set = None):
        super().__init__(timeout=None)          # Persistent
        self.author = author
        self.channel_id = channel_id
        self.channel_name = channel_name
        self.current = current or set()
        self._update_buttons()

    def _update_buttons(self):
        self.clear_items()
        all_room_langs = {
            "PT": {"flag": "🇧🇷", "name": "Português"},
            "EN": {"flag": "🇬🇧", "name": "English"},
            **OPTIONAL_LANGS,
            **ROOM_ONLY_LANGS
        }

        for code, info in all_room_langs.items():
            is_active = code in self.current
            btn = discord.ui.Button(
                label=f"{info['flag']} {info['name']}",
                style=discord.ButtonStyle.success if is_active else discord.ButtonStyle.secondary,
                emoji="✅" if is_active else "❌",
                custom_id=f"troom_{self.channel_id}_{code}"
            )
            btn.callback = self._make_callback(code)
            self.add_item(btn)

        # Extra Buttons
        self.add_item(discord.ui.Button(
            label="📡 Globale Einstellungen",
            style=discord.ButtonStyle.primary,
            custom_id=f"troom_{self.channel_id}_reset",
            row=4
        ))
        self.add_item(discord.ui.Button(
            label="🚫 Alle aus",
            style=discord.ButtonStyle.danger,
            custom_id=f"troom_{self.channel_id}_off",
            row=4
        ))

    def _make_callback(self, code: str):
        async def callback(interaction: discord.Interaction):
            if self.author and interaction.user.id != self.author.id:
                return await interaction.response.send_message("❌ Nur der Befehls-Ausführer darf ändern.", ephemeral=True)

            if code in self.current:
                self.current.discard(code)
                action = "deaktiviert"
            else:
                self.current.add(code)
                action = "aktiviert"

            set_room_langs(self.channel_id, self.current.copy(), disabled=False)

            self._update_buttons()
            await interaction.response.edit_message(embed=self._make_embed(), view=self)

            all_langs = {"PT": {"flag": "🇧🇷", "name": "Português"}, "EN": {"flag": "🇬🇧", "name": "English"},
                         **OPTIONAL_LANGS, **ROOM_ONLY_LANGS}
            info = all_langs.get(code, {"flag": "🌐", "name": code})
            await interaction.followup.send(f"{info['flag']} **{info['name']}** in #{self.channel_name} {action}!", ephemeral=True)

        return callback

    async def _reset_callback(self, interaction: discord.Interaction):
        if self.author and interaction.user.id != self.author.id:
            return await interaction.response.send_message("❌ Keine Berechtigung.", ephemeral=True)
        set_room_langs(self.channel_id, None)
        self.current = get_active_langs().copy()
        self._update_buttons()
        await interaction.response.edit_message(embed=self._make_embed(), view=self)
        await interaction.followup.send(f"📡 #{self.channel_name} nutzt jetzt **globale Einstellungen**.", ephemeral=True)

    async def _off_callback(self, interaction: discord.Interaction):
        if self.author and interaction.user.id != self.author.id:
            return await interaction.response.send_message("❌ Keine Berechtigung.", ephemeral=True)
        set_room_langs(self.channel_id, set(), disabled=True)
        self.current = set()
        self._update_buttons()
        await interaction.response.edit_message(embed=self._make_embed(), view=self)
        await interaction.followup.send(f"🚫 Übersetzung in #{self.channel_name} **deaktiviert**.", ephemeral=True)

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

        all_room_langs = {
            "PT": {"flag": "🇧🇷", "name": "Português"},
            "EN": {"flag": "🇬🇧", "name": "English"},
            **OPTIONAL_LANGS,
            **ROOM_ONLY_LANGS
        }
        status_lines = [f"{info['flag']} {info['name']}: **{'✅ Aktiv' if code in
