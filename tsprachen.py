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
        self.current = current  # State im Memory — kein DB-Call beim Button-Klick
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

            # State im Memory updaten — kein DB-Call nötig
            if code in self.current:
                self.current.discard(code)
                action = "deaktiviert"
            else:
                self.current.add(code)
                action = "aktiviert"

            # In DB speichern
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
            await interaction.response.send_message(
                "❌ Nur derjenige der den Befehl ausgeführt hat kann Änderungen vornehmen.",
                ephemeral=True
            )
            return
        # DB-Eintrag löschen → globale Einstellungen
        set_room_langs(self.channel_id, None)
        self.current = get_active_langs().copy()
        self._update_buttons()
        embed = self._make_embed()
        await interaction.response.edit_message(embed=embed, view=self)
        await interaction.followup.send(
            f"📡 #{self.channel_name} nutzt jetzt wieder die **globalen Einstellungen**.",
            ephemeral=True
        )

    async def _off_callback(self, interaction: discord.Interaction):
        if interaction.user.id != self.author.id:
            await interaction.response.send_message(
                "❌ Nur derjenige der den Befehl ausgeführt hat kann Änderungen vornehmen.",
                ephemeral=True
            )
            return
        set_room_langs(self.channel_id, set(), disabled=True)
        self.current = set()
        self._update_buttons()
        embed = self._make_embed()
        await interaction.response.edit_message(embed=embed, view=self)
        await interaction.followup.send(
            f"🚫 Übersetzung in #{self.channel_name} **deaktiviert**.",
            ephemeral=True
        )

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

        embed = discord.Embed(
            title=f"⚙️ Raumsprachen • #{self.channel_name}",
            color=color
        )
        embed.set_author(name="VHA Übersetzer-Bot", icon_url=LOGO_URL)
        embed.add_field(name="Status", value=status_text, inline=False)

        all_langs = {
            "PT": {"flag": "🇧🇷", "name": "Português"},
            "EN": {"flag": "🇬🇧", "name": "English"},
            **OPTIONAL_LANGS
        }
        status_lines = []
        for code, info in all_langs.items():
            status = "✅ Aktiv" if code in self.current else "❌ Inaktiv"
            status_lines.append(f"{info['flag']} {info['name']}: **{status}**")

        embed.add_field(
            name="🔄 Sprachen für diesen Kanal",
            value="\n".join(status_lines),
            inline=False
        )
        embed.set_footer(
            text="📡 Globale Einstellungen = Reset • 🚫 Alle aus = Übersetzung deaktivieren",
            icon_url=LOGO_URL
        )
        return embed



# ────────────────────────────────────────────────
# Cog
# ────────────────────────────────────────────────

class TSprachenCog(commands.Cog):
    def __init__(self, bot):
        self.bot = bot

    @commands.command(name="sprachen", aliases=["languages", "idiomas", "lang"])
    async def cmd_sprachen(self, ctx):
        """Globale Sprachen des Übersetzer-Bots per Button verwalten."""
        if not has_permission(ctx.author):
            await ctx.send("❌ Keine Berechtigung.", delete_after=5)
            return
        try:
            async for msg in ctx.channel.history(limit=20):
                if msg.author == ctx.guild.me and msg.embeds:
                    if "Übersetzer-Bot • Globale Sprachen" in (msg.embeds[0].title or ""):
                        await msg.delete()
        except Exception:
            pass
        view = GlobalSprachenView(ctx.author)
        embed = view._make_embed()
        await ctx.send(embed=embed, view=view)

    @commands.command(name="raumsprachen")
    async def cmd_raumsprachen(self, ctx, channel_id: int = None):
        """Raumsprachen per Button verwalten."""
        if not has_permission(ctx.author):
            await ctx.send("❌ Keine Berechtigung.", delete_after=5)
            return

        cid = channel_id if channel_id is not None else ctx.channel.id
        ch = ctx.guild.get_channel(cid)
        ch_name = ch.name if ch else str(cid)

        try:
            async for msg in ctx.channel.history(limit=20):
                if msg.author == ctx.guild.me and msg.embeds:
                    if f"Raumsprachen • #{ch_name}" in (msg.embeds[0].title or ""):
                        await msg.delete()
        except Exception:
            pass

        # State einmal beim Öffnen laden
        try:
            col = get_room_col()
            doc = col.find_one({"_id": str(cid)})
            if not doc or doc.get("disabled"):
                current = get_active_langs().copy()
            else:
                langs = set(doc.get("langs", []))
                current = langs if langs else get_active_langs().copy()
        except Exception:
            current = get_active_langs().copy()

        view = RaumSprachenView(ctx.author, cid, ch_name, current)
        embed = view._make_embed()
        await ctx.send(embed=embed, view=view)

    @commands.command(name="kanalid", aliases=["channelid"])
    async def cmd_kanalid(self, ctx):
        """Alle Textkanäle mit ID als DM."""
        if not has_permission(ctx.author):
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
            embed.set_footer(text="Für !traumsprachen [ID] verwenden")
            await ctx.author.send(embed=embed)

        await ctx.send("📬 Kanal-IDs als Direktnachricht geschickt!", delete_after=8)


async def setup(bot):
    await bot.add_cog(TSprachenCog(bot))
