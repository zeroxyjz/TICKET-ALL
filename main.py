import os
import re
import html
import sqlite3
import logging
import asyncio
import unicodedata
from pathlib import Path
from datetime import datetime, timezone, timedelta
from typing import Optional

import discord
from discord.ext import commands
from discord import app_commands

TOKEN = os.getenv('TOKEN')

print(f"O token foi encontrado? {TOKEN is not None}")

intents = discord.Intents.default()
bot = commands.Bot(command_prefix="!", intents=intents)

try:
    from dotenv import load_dotenv
except ImportError:
    load_dotenv = None

# =========================================================
# CONFIG GERAL
# =========================================================

BASE_DIR = Path(__file__).resolve().parent
ENV_PATH = BASE_DIR / ".env"

if ENV_PATH.exists():
    if load_dotenv is not None:
        load_dotenv(dotenv_path=ENV_PATH)
    else:
        print(f"⚠️ Aviso: arquivo {ENV_PATH} encontrado, mas a biblioteca python-dotenv não está instalada.")
else:
    print(f"⚠️ Aviso: arquivo {ENV_PATH} não encontrado.")

TOKEN = os.getenv("DISCORD_TOKEN", "").strip()

if not TOKEN:
    raise RuntimeError(
        f"\n\n❌ Erro: DISCORD_TOKEN não configurado corretamente.\n"
        f"Verifique o arquivo: {ENV_PATH}\n"
        f"Formato esperado: DISCORD_TOKEN=seu_token_aqui\n"
    )

DB_PATH = BASE_DIR / "global_bot_system.db"
LOGS_DIR = BASE_DIR / "logs"
LOGS_DIR.mkdir(parents=True, exist_ok=True)

# =========================================================
# LOGGING
# =========================================================

logging.basicConfig(
    level=logging.INFO,
    format="[%(asctime)s] [%(levelname)s] %(name)s: %(message)s",
    handlers=[
        logging.FileHandler(LOGS_DIR / "bot.log", encoding="utf-8"),
        logging.StreamHandler()
    ]
)

logger = logging.getLogger("global-rp-bot")
_guild_loggers = {}


def get_guild_logger(guild_id: int) -> logging.Logger:
    if guild_id in _guild_loggers:
        return _guild_loggers[guild_id]

    guild_folder = LOGS_DIR / str(guild_id)
    guild_folder.mkdir(parents=True, exist_ok=True)

    guild_logger = logging.getLogger(f"guild-{guild_id}")
    guild_logger.setLevel(logging.INFO)

    if not guild_logger.handlers:
        handler = logging.FileHandler(guild_folder / "guild.log", encoding="utf-8")
        handler.setFormatter(logging.Formatter("[%(asctime)s] [%(levelname)s] %(message)s"))
        guild_logger.addHandler(handler)

    _guild_loggers[guild_id] = guild_logger
    return guild_logger


# =========================================================
# UTILITÁRIOS
# =========================================================

def utc_now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")


def dt_to_str(dt: Optional[datetime]) -> Optional[str]:
    if not dt:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")


def hex_to_color(value: str) -> discord.Color:
    try:
        value = (value or "#5865F2").strip().replace("#", "")
        return discord.Color(int(value, 16))
    except Exception:
        return discord.Color.blurple()


def normalize_text(text: str) -> str:
    text = unicodedata.normalize("NFKD", text or "")
    text = text.encode("ascii", "ignore").decode("ascii")
    return text


def sanitize_channel_name(name: str) -> str:
    name = normalize_text((name or "").lower())
    name = re.sub(r"[^a-z0-9\- ]", "", name)
    name = name.replace(" ", "-")
    name = re.sub(r"-+", "-", name).strip("-")
    return name[:90] if name else "ticket"


def extract_user_id(raw: str) -> Optional[int]:
    if not raw:
        return None
    found = re.search(r"(\d{17,22})", raw)
    if found:
        return int(found.group(1))
    return None


def format_template(template: str, member: discord.Member | discord.User, guild: Optional[discord.Guild] = None) -> str:
    guild_name = guild.name if guild else (member.guild.name if hasattr(member, "guild") and member.guild else "Servidor")
    display_name = getattr(member, "display_name", member.name)

    return (
        (template or "")
        .replace("{member}", getattr(member, "mention", member.name))
        .replace("{member_name}", member.name)
        .replace("{display_name}", display_name)
        .replace("{guild}", guild_name)
        .replace("{id}", str(member.id))
    )


def parse_duration_to_timedelta(text: str) -> Optional[timedelta]:
    text = (text or "").strip().lower()
    match = re.fullmatch(r"(\d+)([smhd])", text)
    if not match:
        return None

    value = int(match.group(1))
    unit = match.group(2)

    if unit == "s":
        return timedelta(seconds=value)
    if unit == "m":
        return timedelta(minutes=value)
    if unit == "h":
        return timedelta(hours=value)
    if unit == "d":
        return timedelta(days=value)
    return None


def can_act_on_member(actor: discord.Member, target: discord.Member) -> bool:
    if actor.guild.owner_id == actor.id:
        return True
    return actor.top_role > target.top_role


def bot_can_act_on_member(guild: discord.Guild, target: discord.Member) -> bool:
    if not bot.user:
        return False
    me = guild.get_member(bot.user.id)
    if me is None:
        return False
    if guild.owner_id == me.id:
        return True
    return me.top_role > target.top_role


def is_staff(member: discord.Member, settings) -> bool:
    if member.guild_permissions.administrator or member.guild_permissions.manage_channels:
        return True

    support_role_id = settings["support_role_id"]
    if support_role_id:
        role = member.guild.get_role(support_role_id)
        if role and role in member.roles:
            return True

    moderator_role_id = settings["moderator_role_id"]
    if moderator_role_id:
        role = member.guild.get_role(moderator_role_id)
        if role and role in member.roles:
            return True

    return False


def is_moderator(member: discord.Member, settings) -> bool:
    if (
        member.guild_permissions.administrator
        or member.guild_permissions.ban_members
        or member.guild_permissions.kick_members
        or member.guild_permissions.moderate_members
        or member.guild_permissions.manage_messages
        or member.guild_permissions.manage_channels
    ):
        return True

    moderator_role_id = settings["moderator_role_id"]
    if moderator_role_id:
        role = member.guild.get_role(moderator_role_id)
        if role and role in member.roles:
            return True

    return False


# =========================================================
# BANCO DE DADOS
# =========================================================

class Database:
    def __init__(self, path: Path):
        self.conn = sqlite3.connect(path, check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        self._init_tables()
        self._migrate_schema()

    def _init_tables(self):
        cur = self.conn.cursor()

        cur.execute("""
        CREATE TABLE IF NOT EXISTS guild_settings (
            guild_id INTEGER PRIMARY KEY,
            panel_channel_id INTEGER,
            ticket_log_channel_id INTEGER,
            mod_log_channel_id INTEGER,
            welcome_channel_id INTEGER,
            goodbye_channel_id INTEGER,
            support_role_id INTEGER,
            moderator_role_id INTEGER,
            prison_role_id INTEGER,
            prison_channel_id INTEGER,
            ticket_counter INTEGER DEFAULT 0,
            welcome_enabled INTEGER DEFAULT 1,
            goodbye_enabled INTEGER DEFAULT 1,
            welcome_message TEXT DEFAULT 'Seja bem-vindo(a) ao servidor, {member}!',
            goodbye_message TEXT DEFAULT '{member_name} saiu do servidor.'
        )
        """)

        cur.execute("""
        CREATE TABLE IF NOT EXISTS tickets (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            guild_id INTEGER NOT NULL,
            channel_id INTEGER UNIQUE NOT NULL,
            creator_id INTEGER NOT NULL,
            claimed_by_id INTEGER,
            ticket_type TEXT NOT NULL,
            ticket_number INTEGER NOT NULL DEFAULT 0,
            status TEXT NOT NULL DEFAULT 'open',
            channel_name TEXT,
            internal_notes TEXT DEFAULT '',
            created_at TEXT NOT NULL,
            closed_at TEXT
        )
        """)

        cur.execute("""
        CREATE TABLE IF NOT EXISTS ticket_logs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            guild_id INTEGER NOT NULL,
            ticket_id INTEGER,
            channel_id INTEGER,
            action TEXT NOT NULL,
            moderator_id INTEGER,
            target_id INTEGER,
            details TEXT,
            created_at TEXT NOT NULL
        )
        """)

        cur.execute("""
        CREATE TABLE IF NOT EXISTS infractions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            guild_id INTEGER NOT NULL,
            user_id INTEGER NOT NULL,
            moderator_id INTEGER,
            action TEXT NOT NULL,
            reason TEXT,
            duration_seconds INTEGER,
            active INTEGER DEFAULT 1,
            created_at TEXT NOT NULL,
            expires_at TEXT
        )
        """)

        cur.execute("""
        CREATE TABLE IF NOT EXISTS ticket_panel_config (
            guild_id INTEGER PRIMARY KEY,
            title TEXT DEFAULT 'Central de Atendimento',
            description TEXT DEFAULT 'Clique no botão abaixo para abrir um ticket.',
            color TEXT DEFAULT '#5865F2',
            image_url TEXT,
            thumbnail_url TEXT,
            footer_text TEXT DEFAULT 'Sistema de Tickets'
        )
        """)

        cur.execute("""
        CREATE TABLE IF NOT EXISTS ticket_type_config (
            guild_id INTEGER NOT NULL,
            ticket_type TEXT NOT NULL,
            label TEXT NOT NULL,
            emoji TEXT NOT NULL,
            prefix TEXT NOT NULL,
            category_id INTEGER,
            panel_label TEXT,
            panel_description TEXT,
            ticket_title TEXT,
            ticket_description TEXT,
            ticket_color TEXT DEFAULT '#5865F2',
            image_url TEXT,
            thumbnail_url TEXT,
            active INTEGER DEFAULT 1,
            PRIMARY KEY (guild_id, ticket_type)
        )
        """)

        cur.execute("""
        CREATE TABLE IF NOT EXISTS gta_registration_settings (
            guild_id INTEGER PRIMARY KEY,
            registration_log_channel_id INTEGER,
            approved_role_id INTEGER,
            rejected_role_id INTEGER,
            pending_role_id INTEGER
        )
        """)

        cur.execute("""
        CREATE TABLE IF NOT EXISTS gta_registrations (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            guild_id INTEGER NOT NULL,
            user_id INTEGER NOT NULL,
            discord_name TEXT NOT NULL,
            character_name TEXT NOT NULL,
            character_age INTEGER NOT NULL,
            character_id TEXT,
            whitelisted_name TEXT,
            experience TEXT,
            availability TEXT,
            story TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT 'pending',
            reviewed_by INTEGER,
            review_reason TEXT,
            created_at TEXT NOT NULL,
            reviewed_at TEXT
        )
        """)

        cur.execute("""
        CREATE TABLE IF NOT EXISTS prison_records (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            guild_id INTEGER NOT NULL,
            user_id INTEGER NOT NULL,
            moderator_id INTEGER NOT NULL,
            reason TEXT,
            duration_seconds INTEGER,
            active INTEGER DEFAULT 1,
            saved_roles TEXT DEFAULT '',
            created_at TEXT NOT NULL,
            expires_at TEXT,
            released_at TEXT,
            released_by INTEGER
        )
        """)

        self.conn.commit()

    def _migrate_schema(self):
        self._add_column_if_missing("guild_settings", "panel_channel_id", "INTEGER")
        self._add_column_if_missing("guild_settings", "ticket_log_channel_id", "INTEGER")
        self._add_column_if_missing("guild_settings", "mod_log_channel_id", "INTEGER")
        self._add_column_if_missing("guild_settings", "welcome_channel_id", "INTEGER")
        self._add_column_if_missing("guild_settings", "goodbye_channel_id", "INTEGER")
        self._add_column_if_missing("guild_settings", "support_role_id", "INTEGER")
        self._add_column_if_missing("guild_settings", "moderator_role_id", "INTEGER")
        self._add_column_if_missing("guild_settings", "prison_role_id", "INTEGER")
        self._add_column_if_missing("guild_settings", "prison_channel_id", "INTEGER")
        self._add_column_if_missing("guild_settings", "ticket_counter", "INTEGER DEFAULT 0")
        self._add_column_if_missing("guild_settings", "welcome_enabled", "INTEGER DEFAULT 1")
        self._add_column_if_missing("guild_settings", "goodbye_enabled", "INTEGER DEFAULT 1")
        self._add_column_if_missing("guild_settings", "welcome_message", "TEXT DEFAULT 'Seja bem-vindo(a) ao servidor, {member}!'")
        self._add_column_if_missing("guild_settings", "goodbye_message", "TEXT DEFAULT '{member_name} saiu do servidor.'")

        self._add_column_if_missing("tickets", "claimed_by_id", "INTEGER")
        self._add_column_if_missing("tickets", "ticket_number", "INTEGER DEFAULT 0")
        self._add_column_if_missing("tickets", "channel_name", "TEXT")
        self._add_column_if_missing("tickets", "internal_notes", "TEXT DEFAULT ''")
        self._add_column_if_missing("tickets", "closed_at", "TEXT")

        self._add_column_if_missing("ticket_logs", "moderator_id", "INTEGER")
        self._add_column_if_missing("ticket_logs", "target_id", "INTEGER")
        self._add_column_if_missing("ticket_logs", "details", "TEXT")
        self._add_column_if_missing("ticket_logs", "created_at", "TEXT")

        self._add_column_if_missing("infractions", "reason", "TEXT")
        self._add_column_if_missing("infractions", "duration_seconds", "INTEGER")
        self._add_column_if_missing("infractions", "active", "INTEGER DEFAULT 1")
        self._add_column_if_missing("infractions", "created_at", "TEXT")
        self._add_column_if_missing("infractions", "expires_at", "TEXT")

        self._add_column_if_missing("ticket_panel_config", "title", "TEXT DEFAULT 'Central de Atendimento'")
        self._add_column_if_missing("ticket_panel_config", "description", "TEXT DEFAULT 'Clique no botão abaixo para abrir um ticket.'")
        self._add_column_if_missing("ticket_panel_config", "color", "TEXT DEFAULT '#5865F2'")
        self._add_column_if_missing("ticket_panel_config", "image_url", "TEXT")
        self._add_column_if_missing("ticket_panel_config", "thumbnail_url", "TEXT")
        self._add_column_if_missing("ticket_panel_config", "footer_text", "TEXT DEFAULT 'Sistema de Tickets'")

        self._add_column_if_missing("ticket_type_config", "label", "TEXT")
        self._add_column_if_missing("ticket_type_config", "emoji", "TEXT")
        self._add_column_if_missing("ticket_type_config", "prefix", "TEXT")
        self._add_column_if_missing("ticket_type_config", "category_id", "INTEGER")
        self._add_column_if_missing("ticket_type_config", "panel_label", "TEXT")
        self._add_column_if_missing("ticket_type_config", "panel_description", "TEXT")
        self._add_column_if_missing("ticket_type_config", "ticket_title", "TEXT")
        self._add_column_if_missing("ticket_type_config", "ticket_description", "TEXT")
        self._add_column_if_missing("ticket_type_config", "ticket_color", "TEXT DEFAULT '#5865F2'")
        self._add_column_if_missing("ticket_type_config", "image_url", "TEXT")
        self._add_column_if_missing("ticket_type_config", "thumbnail_url", "TEXT")
        self._add_column_if_missing("ticket_type_config", "active", "INTEGER DEFAULT 1")

        self._add_column_if_missing("gta_registration_settings", "registration_log_channel_id", "INTEGER")
        self._add_column_if_missing("gta_registration_settings", "approved_role_id", "INTEGER")
        self._add_column_if_missing("gta_registration_settings", "rejected_role_id", "INTEGER")
        self._add_column_if_missing("gta_registration_settings", "pending_role_id", "INTEGER")

        self._add_column_if_missing("gta_registrations", "character_id", "TEXT")
        self._add_column_if_missing("gta_registrations", "whitelisted_name", "TEXT")
        self._add_column_if_missing("gta_registrations", "experience", "TEXT")
        self._add_column_if_missing("gta_registrations", "availability", "TEXT")
        self._add_column_if_missing("gta_registrations", "status", "TEXT DEFAULT 'pending'")
        self._add_column_if_missing("gta_registrations", "reviewed_by", "INTEGER")
        self._add_column_if_missing("gta_registrations", "review_reason", "TEXT")
        self._add_column_if_missing("gta_registrations", "created_at", "TEXT")
        self._add_column_if_missing("gta_registrations", "reviewed_at", "TEXT")

        self._add_column_if_missing("prison_records", "saved_roles", "TEXT DEFAULT ''")
        self._add_column_if_missing("prison_records", "released_at", "TEXT")
        self._add_column_if_missing("prison_records", "released_by", "INTEGER")

    def _column_exists(self, table_name: str, column_name: str) -> bool:
        cur = self.conn.cursor()
        cur.execute(f"PRAGMA table_info({table_name})")
        columns = cur.fetchall()
        return any(col["name"] == column_name for col in columns)

    def _add_column_if_missing(self, table_name: str, column_name: str, definition: str):
        if not self._column_exists(table_name, column_name):
            cur = self.conn.cursor()
            cur.execute(f"ALTER TABLE {table_name} ADD COLUMN {column_name} {definition}")
            self.conn.commit()

    def fetchone(self, query: str, params: tuple = ()):
        cur = self.conn.cursor()
        cur.execute(query, params)
        return cur.fetchone()

    def fetchall(self, query: str, params: tuple = ()):
        cur = self.conn.cursor()
        cur.execute(query, params)
        return cur.fetchall()

    def execute(self, query: str, params: tuple = ()):
        cur = self.conn.cursor()
        cur.execute(query, params)
        self.conn.commit()
        return cur

    # ---------- guild settings ----------
    def get_settings(self, guild_id: int):
        row = self.fetchone("SELECT * FROM guild_settings WHERE guild_id = ?", (guild_id,))
        if row:
            return row
        self.execute("INSERT INTO guild_settings (guild_id) VALUES (?)", (guild_id,))
        return self.fetchone("SELECT * FROM guild_settings WHERE guild_id = ?", (guild_id,))

    def update_settings(self, guild_id: int, **kwargs):
        if not kwargs:
            return
        fields = ", ".join([f"{k} = ?" for k in kwargs.keys()])
        values = list(kwargs.values()) + [guild_id]
        self.execute(f"UPDATE guild_settings SET {fields} WHERE guild_id = ?", tuple(values))

    def next_ticket_counter(self, guild_id: int) -> int:
        settings = self.get_settings(guild_id)
        current = settings["ticket_counter"] or 0
        new_value = current + 1
        self.update_settings(guild_id, ticket_counter=new_value)
        return new_value

    # ---------- panel config ----------
    def get_panel_config(self, guild_id: int):
        row = self.fetchone("SELECT * FROM ticket_panel_config WHERE guild_id = ?", (guild_id,))
        if row:
            return row
        self.execute("INSERT INTO ticket_panel_config (guild_id) VALUES (?)", (guild_id,))
        return self.fetchone("SELECT * FROM ticket_panel_config WHERE guild_id = ?", (guild_id,))

    def update_panel_config(self, guild_id: int, **kwargs):
        if not kwargs:
            return
        self.get_panel_config(guild_id)
        fields = ", ".join([f"{k} = ?" for k in kwargs.keys()])
        values = list(kwargs.values()) + [guild_id]
        self.execute(f"UPDATE ticket_panel_config SET {fields} WHERE guild_id = ?", tuple(values))

    # ---------- ticket type config ----------
    def seed_default_ticket_types(self, guild_id: int):
        defaults = {
            "compras": {
                "label": "COMPRAS", "emoji": "🛒", "prefix": "compras",
                "panel_label": "Compras", "panel_description": "Planos, pagamentos e contratação",
                "ticket_title": "🛒 Atendimento • Compras",
                "ticket_description": "Olá! Envie aqui sua dúvida sobre planos, pagamentos ou contratação.",
                "ticket_color": "#57F287"
            },
            "suporte": {
                "label": "SUPORTE", "emoji": "🛠️", "prefix": "suporte",
                "panel_label": "Suporte", "panel_description": "Ajuda técnica e problemas gerais",
                "ticket_title": "🛠️ Atendimento • Suporte",
                "ticket_description": "Olá! Descreva seu problema com o máximo de detalhes possível.",
                "ticket_color": "#5865F2"
            },
            "parcerias": {
                "label": "PARCERIAS", "emoji": "🤝", "prefix": "parceria",
                "panel_label": "Parcerias", "panel_description": "Propostas e colaborações",
                "ticket_title": "🤝 Atendimento • Parcerias",
                "ticket_description": "Olá! Envie sua proposta de parceria ou colaboração.",
                "ticket_color": "#FEE75C"
            },
            "doacoes": {
                "label": "DOAÇÕES", "emoji": "💎", "prefix": "doacao",
                "panel_label": "Doações", "panel_description": "Apoio, benefícios e contribuições",
                "ticket_title": "💎 Atendimento • Doações",
                "ticket_description": "Olá! Envie sua dúvida ou solicitação relacionada a doações.",
                "ticket_color": "#EB459E"
            }
        }

        for key, data in defaults.items():
            exists = self.fetchone(
                "SELECT 1 FROM ticket_type_config WHERE guild_id = ? AND ticket_type = ?",
                (guild_id, key)
            )
            if not exists:
                self.execute("""
                    INSERT INTO ticket_type_config (
                        guild_id, ticket_type, label, emoji, prefix, category_id, panel_label,
                        panel_description, ticket_title, ticket_description, ticket_color, image_url,
                        thumbnail_url, active
                    ) VALUES (?, ?, ?, ?, ?, NULL, ?, ?, ?, ?, ?, NULL, NULL, 1)
                """, (
                    guild_id, key, data["label"], data["emoji"], data["prefix"],
                    data["panel_label"], data["panel_description"],
                    data["ticket_title"], data["ticket_description"], data["ticket_color"]
                ))

    def get_ticket_types(self, guild_id: int):
        return self.fetchall("""
            SELECT * FROM ticket_type_config
            WHERE guild_id = ? AND active = 1
            ORDER BY rowid ASC
        """, (guild_id,))

    def get_ticket_type(self, guild_id: int, ticket_type: str):
        return self.fetchone("""
            SELECT * FROM ticket_type_config
            WHERE guild_id = ? AND ticket_type = ?
        """, (guild_id, ticket_type))

    def upsert_ticket_type(self, guild_id: int, ticket_type: str, **kwargs):
        exists = self.get_ticket_type(guild_id, ticket_type)
        if not exists:
            self.execute("""
                INSERT INTO ticket_type_config (
                    guild_id, ticket_type, label, emoji, prefix, active
                ) VALUES (?, ?, ?, ?, ?, 1)
            """, (
                guild_id, ticket_type,
                kwargs.get("label", ticket_type.upper()),
                kwargs.get("emoji", "🎫"),
                kwargs.get("prefix", sanitize_channel_name(ticket_type))
            ))

        if kwargs:
            fields = ", ".join([f"{k} = ?" for k in kwargs.keys()])
            values = list(kwargs.values()) + [guild_id, ticket_type]
            self.execute(
                f"UPDATE ticket_type_config SET {fields} WHERE guild_id = ? AND ticket_type = ?",
                tuple(values)
            )

    # ---------- tickets ----------
    def get_ticket_by_channel(self, channel_id: int):
        return self.fetchone("SELECT * FROM tickets WHERE channel_id = ?", (channel_id,))

    def get_open_ticket_by_user(self, guild_id: int, user_id: int):
        return self.fetchone("""
            SELECT * FROM tickets
            WHERE guild_id = ? AND creator_id = ? AND status = 'open'
            ORDER BY id DESC LIMIT 1
        """, (guild_id, user_id))

    def create_ticket(self, guild_id: int, channel_id: int, creator_id: int, ticket_type: str, ticket_number: int, channel_name: str):
        cur = self.execute("""
            INSERT INTO tickets (
                guild_id, channel_id, creator_id, claimed_by_id, ticket_type,
                ticket_number, status, channel_name, internal_notes, created_at, closed_at
            ) VALUES (?, ?, ?, NULL, ?, ?, 'open', ?, '', ?, NULL)
        """, (guild_id, channel_id, creator_id, ticket_type, ticket_number, channel_name, utc_now()))
        return cur.lastrowid

    def update_ticket(self, channel_id: int, **kwargs):
        if not kwargs:
            return
        fields = ", ".join([f"{k} = ?" for k in kwargs.keys()])
        values = list(kwargs.values()) + [channel_id]
        self.execute(f"UPDATE tickets SET {fields} WHERE channel_id = ?", tuple(values))

    def add_log(self, guild_id: int, ticket_id: Optional[int], channel_id: Optional[int],
                action: str, moderator_id: Optional[int] = None, target_id: Optional[int] = None,
                details: Optional[str] = None):
        self.execute("""
            INSERT INTO ticket_logs (
                guild_id, ticket_id, channel_id, action, moderator_id, target_id, details, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """, (guild_id, ticket_id, channel_id, action, moderator_id, target_id, details, utc_now()))

    # ---------- infractions ----------
    def add_infraction(self, guild_id: int, user_id: int, moderator_id: Optional[int],
                       action: str, reason: Optional[str] = None, duration_seconds: Optional[int] = None,
                       expires_at: Optional[str] = None, active: int = 1):
        self.execute("""
            INSERT INTO infractions (
                guild_id, user_id, moderator_id, action, reason,
                duration_seconds, active, created_at, expires_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (guild_id, user_id, moderator_id, action, reason, duration_seconds, active, utc_now(), expires_at))

    def get_user_infractions(self, guild_id: int, user_id: int):
        return self.fetchall("""
            SELECT * FROM infractions
            WHERE guild_id = ? AND user_id = ?
            ORDER BY id DESC
        """, (guild_id, user_id))

    # ---------- registration ----------
    def get_registration_settings(self, guild_id: int):
        row = self.fetchone("SELECT * FROM gta_registration_settings WHERE guild_id = ?", (guild_id,))
        if row:
            return row
        self.execute("INSERT INTO gta_registration_settings (guild_id) VALUES (?)", (guild_id,))
        return self.fetchone("SELECT * FROM gta_registration_settings WHERE guild_id = ?", (guild_id,))

    def update_registration_settings(self, guild_id: int, **kwargs):
        if not kwargs:
            return
        self.get_registration_settings(guild_id)
        fields = ", ".join([f"{k} = ?" for k in kwargs.keys()])
        values = list(kwargs.values()) + [guild_id]
        self.execute(f"UPDATE gta_registration_settings SET {fields} WHERE guild_id = ?", tuple(values))

    def create_registration(self, guild_id: int, user_id: int, discord_name: str, character_name: str,
                            character_age: int, character_id: str, whitelisted_name: str,
                            experience: str, availability: str, story: str):
        cur = self.execute("""
            INSERT INTO gta_registrations (
                guild_id, user_id, discord_name, character_name, character_age,
                character_id, whitelisted_name, experience, availability, story,
                status, reviewed_by, review_reason, created_at, reviewed_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'pending', NULL, NULL, ?, NULL)
        """, (guild_id, user_id, discord_name, character_name, character_age,
              character_id, whitelisted_name, experience, availability, story, utc_now()))
        return cur.lastrowid

    def get_registration(self, registration_id: int):
        return self.fetchone("SELECT * FROM gta_registrations WHERE id = ?", (registration_id,))

    def get_pending_registration_by_user(self, guild_id: int, user_id: int):
        return self.fetchone("""
            SELECT * FROM gta_registrations
            WHERE guild_id = ? AND user_id = ? AND status = 'pending'
            ORDER BY id DESC LIMIT 1
        """, (guild_id, user_id))

    def update_registration(self, registration_id: int, **kwargs):
        if not kwargs:
            return
        fields = ", ".join([f"{k} = ?" for k in kwargs.keys()])
        values = list(kwargs.values()) + [registration_id]
        self.execute(f"UPDATE gta_registrations SET {fields} WHERE id = ?", tuple(values))

    def get_pending_registrations(self, guild_id: int, limit: int = 15):
        return self.fetchall("""
            SELECT * FROM gta_registrations
            WHERE guild_id = ? AND status = 'pending'
            ORDER BY id DESC LIMIT ?
        """, (guild_id, limit))

    # ---------- prison ----------
    def create_prison_record(self, guild_id: int, user_id: int, moderator_id: int,
                             reason: Optional[str], duration_seconds: Optional[int],
                             expires_at: Optional[str], saved_roles: str) -> int:
        cur = self.execute("""
            INSERT INTO prison_records (
                guild_id, user_id, moderator_id, reason, duration_seconds,
                active, saved_roles, created_at, expires_at, released_at, released_by
            ) VALUES (?, ?, ?, ?, ?, 1, ?, ?, ?, NULL, NULL)
        """, (guild_id, user_id, moderator_id, reason, duration_seconds, saved_roles, utc_now(), expires_at))
        return cur.lastrowid

    def get_active_prison(self, guild_id: int, user_id: int):
        return self.fetchone("""
            SELECT * FROM prison_records
            WHERE guild_id = ? AND user_id = ? AND active = 1
            ORDER BY id DESC LIMIT 1
        """, (guild_id, user_id))

    def release_prison(self, record_id: int, released_by: int):
        self.execute("""
            UPDATE prison_records
            SET active = 0, released_at = ?, released_by = ?
            WHERE id = ?
        """, (utc_now(), released_by, record_id))

    def get_prison_history(self, guild_id: int, user_id: int):
        return self.fetchall("""
            SELECT * FROM prison_records
            WHERE guild_id = ? AND user_id = ?
            ORDER BY id DESC
        """, (guild_id, user_id))


db = Database(DB_PATH)

# =========================================================
# EMBEDS
# =========================================================

def build_panel_embed(guild: discord.Guild) -> discord.Embed:
    panel = db.get_panel_config(guild.id)
    ticket_types = db.get_ticket_types(guild.id)
    color = hex_to_color(panel["color"] or "#5865F2")

    description_lines = [panel["description"] or "Clique no botão abaixo para abrir um ticket.", ""]

    if ticket_types:
        for row in ticket_types:
            description_lines.append(
                f"{row['emoji']} **{row['panel_label'] or row['label']}** — {row['panel_description'] or 'Abrir atendimento'}"
            )
    else:
        description_lines.append("Nenhum setor ativo configurado.")

    embed = discord.Embed(
        title=panel["title"] or "Central de Atendimento",
        description="\n".join(description_lines),
        color=color
    )

    embed.add_field(
        name="Regras rápidas",
        value=(
            "• Cada usuário pode manter **1 ticket aberto por vez**\n"
            "• Explique sua solicitação com o máximo de detalhes\n"
            "• Aguarde a equipe assumir o atendimento"
        ),
        inline=False
    )

    if panel["thumbnail_url"]:
        embed.set_thumbnail(url=panel["thumbnail_url"])
    elif guild.icon:
        embed.set_thumbnail(url=guild.icon.url)

    if panel["image_url"]:
        embed.set_image(url=panel["image_url"])
    elif guild.banner:
        embed.set_image(url=guild.banner.url)

    embed.set_footer(text=panel["footer_text"] or f"{guild.name} • Sistema de Tickets")
    return embed


def build_ticket_embed(guild: discord.Guild, member: discord.Member, ticket,
                       claimed_by: Optional[discord.Member] = None) -> discord.Embed:
    cfg = db.get_ticket_type(guild.id, ticket["ticket_type"])

    if not cfg:
        title = f"🎫 Ticket #{ticket['ticket_number']}"
        desc = "Descreva sua solicitação abaixo."
        color = discord.Color.blurple()
        image_url = None
        thumb_url = None
        label = ticket["ticket_type"].upper()
        emoji = "🎫"
    else:
        title = cfg["ticket_title"] or f"{cfg['emoji']} Ticket #{ticket['ticket_number']} • {cfg['label']}"
        desc = cfg["ticket_description"] or "Descreva sua solicitação abaixo."
        color = hex_to_color(cfg["ticket_color"] or "#5865F2")
        image_url = cfg["image_url"]
        thumb_url = cfg["thumbnail_url"]
        label = cfg["label"]
        emoji = cfg["emoji"]

    status_text = "Aberto"
    if ticket["claimed_by_id"]:
        status_text = "Em atendimento"
    if ticket["status"] == "closed":
        status_text = "Fechado"

    embed = discord.Embed(title=title, description=f"{member.mention}\n\n{desc}", color=color)
    embed.add_field(name="Usuário", value=f"{member.mention}\n`{member.id}`", inline=True)
    embed.add_field(name="Setor", value=f"{emoji} {label}", inline=True)
    embed.add_field(name="Status", value=status_text, inline=True)

    embed.add_field(
        name="Ações disponíveis",
        value=(
            "• Cancelar ticket\n• Ajuda de DM\n• Chamar membro\n• Adicionar membro\n"
            "• Remover membro\n• Mover ticket\n• Renomear canal\n• Nota interna\n"
            "• Assumir\n• Saudar\n• Fechar"
        ),
        inline=False
    )

    if claimed_by:
        embed.add_field(name="Atendente", value=claimed_by.mention, inline=False)
    else:
        embed.add_field(name="Atendente", value="Aguardando equipe.", inline=False)

    notes = ticket["internal_notes"] or ""
    if notes:
        embed.add_field(name="Observação interna", value=notes[-1024:], inline=False)

    if thumb_url:
        embed.set_thumbnail(url=thumb_url)
    elif guild.icon:
        embed.set_thumbnail(url=guild.icon.url)
    if image_url:
        embed.set_image(url=image_url)

    embed.set_footer(text=f"{guild.name} • Ticket #{ticket['ticket_number']}")
    return embed


def build_registration_panel_embed(guild: discord.Guild) -> discord.Embed:
    embed = discord.Embed(
        title="📋 Registro GTA RP",
        description=(
            "Clique no botão abaixo para enviar seu registro.\n\n"
            "**Informações solicitadas:**\n"
            "• Nome do personagem\n"
            "• Idade do personagem\n"
            "• ID / passaporte\n"
            "• Experiência em RP\n"
            "• História do personagem\n\n"
            "Após o envio, a equipe vai analisar e aprovar ou reprovar."
        ),
        color=discord.Color.blurple()
    )
    if guild.icon:
        embed.set_thumbnail(url=guild.icon.url)
    embed.set_footer(text=f"{guild.name} • Registro RP")
    return embed


# =========================================================
# FUNÇÕES DE SUPORTE
# =========================================================

async def send_ticket_log(guild: discord.Guild, title: str, description: str, color: discord.Color):
    settings = db.get_settings(guild.id)
    channel_id = settings["ticket_log_channel_id"]
    if not channel_id:
        return
    channel = guild.get_channel(channel_id)
    if not isinstance(channel, discord.TextChannel):
        return
    embed = discord.Embed(title=title, description=description, color=color, timestamp=datetime.now(timezone.utc))
    try:
        await channel.send(embed=embed)
    except Exception as e:
        logger.warning(f"Falha ao enviar ticket log no servidor {guild.id}: {e}")


async def send_mod_log(guild: discord.Guild, title: str, description: str, color: discord.Color):
    settings = db.get_settings(guild.id)
    channel_id = settings["mod_log_channel_id"]
    if not channel_id:
        return
    channel = guild.get_channel(channel_id)
    if not isinstance(channel, discord.TextChannel):
        return
    embed = discord.Embed(title=title, description=description, color=color, timestamp=datetime.now(timezone.utc))
    try:
        await channel.send(embed=embed)
    except Exception as e:
        logger.warning(f"Falha ao enviar mod log no servidor {guild.id}: {e}")


async def ensure_ticket_categories(guild: discord.Guild):
    db.seed_default_ticket_types(guild.id)
    rows = db.get_ticket_types(guild.id)

    for row in rows:
        category = guild.get_channel(row["category_id"]) if row["category_id"] else None
        if isinstance(category, discord.CategoryChannel):
            continue

        category_name = f"📩 {row['label']}"
        category = discord.utils.get(guild.categories, name=category_name)

        if category is None:
            try:
                category = await guild.create_category(category_name, reason="Configuração automática do sistema de tickets")
            except discord.Forbidden:
                category = None

        if category:
            db.upsert_ticket_type(guild.id, row["ticket_type"], category_id=category.id)


def get_ticket_category(guild: discord.Guild, ticket_type: str) -> Optional[discord.CategoryChannel]:
    cfg = db.get_ticket_type(guild.id, ticket_type)
    if not cfg or not cfg["category_id"]:
        return None
    ch = guild.get_channel(cfg["category_id"])
    return ch if isinstance(ch, discord.CategoryChannel) else None


async def generate_transcript(channel: discord.TextChannel, guild_id: int) -> Path:
    guild_folder = LOGS_DIR / str(guild_id) / "transcripts"
    guild_folder.mkdir(parents=True, exist_ok=True)

    filename = f"{channel.id}_{int(datetime.now().timestamp())}.html"
    path = guild_folder / filename

    messages = []
    async for msg in channel.history(limit=None, oldest_first=True):
        attachments = ""
        if msg.attachments:
            attachments = "<br>".join(
                f'<a href="{html.escape(a.url)}" target="_blank">📎 {html.escape(a.filename)}</a>'
                for a in msg.attachments
            )
        embeds_info = ""
        if msg.embeds:
            embeds_info = "<br>".join(
                f"Embed: {html.escape(embed.title or 'Sem título')}"
                for embed in msg.embeds
            )
        content = html.escape(msg.content) if msg.content else ""
        timestamp = msg.created_at.strftime("%d/%m/%Y %H:%M:%S UTC")
        avatar = msg.author.display_avatar.url if msg.author.display_avatar else ""

        messages.append(f"""
        <div style="padding:12px; margin:10px 0; background:#1e1f22; border-radius:10px;">
            <div style="display:flex; align-items:center; gap:10px; margin-bottom:8px;">
                <img src="{avatar}" width="40" height="40" style="border-radius:50%;">
                <div>
                    <strong>{html.escape(str(msg.author))}</strong><br>
                    <span style="font-size:12px; color:#aaa;">{timestamp}</span>
                </div>
            </div>
            <div style="white-space:pre-wrap;">{content or '<i>Sem texto</i>'}</div>
            {"<div style='margin-top:8px;'>" + attachments + "</div>" if attachments else ""}
            {"<div style='margin-top:8px; color:#89b4fa;'>" + embeds_info + "</div>" if embeds_info else ""}
        </div>
        """)

    html_content = f"""<!DOCTYPE html>
<html lang="pt-br">
<head>
  <meta charset="UTF-8">
  <title>Transcript - {html.escape(channel.name)}</title>
</head>
<body style="background:#111214; color:#f2f3f5; font-family:Arial, sans-serif; padding:20px;">
  <h1>Transcript do Ticket</h1>
  <p><strong>Canal:</strong> #{html.escape(channel.name)}</p>
  <p><strong>Servidor:</strong> {html.escape(channel.guild.name)}</p>
  <hr style="border:1px solid #2b2d31;">
  {''.join(messages) if messages else '<p>Nenhuma mensagem registrada.</p>'}
</body>
</html>"""

    path.write_text(html_content, encoding="utf-8")
    return path


async def close_ticket(channel: discord.TextChannel, closer: discord.Member, reason: str = "Finalizado"):
    ticket = db.get_ticket_by_channel(channel.id)
    if not ticket or ticket["status"] != "open":
        return False, "Este ticket já está fechado ou não existe no banco."

    guild = channel.guild
    guild_logger = get_guild_logger(guild.id)
    transcript_path = await generate_transcript(channel, guild.id)

    db.update_ticket(channel.id, status="closed", closed_at=utc_now())
    db.add_log(guild.id, ticket["id"], channel.id, "ticket_closed", moderator_id=closer.id, details=reason)
    guild_logger.info(f"Ticket fechado | canal={channel.id} | por={closer.id} | motivo={reason}")

    embed = discord.Embed(
        title="🔒 Ticket Finalizado",
        description=(
            f"**Canal:** {channel.mention}\n"
            f"**Ticket interno:** `{ticket['id']}`\n"
            f"**Número:** `{ticket['ticket_number']}`\n"
            f"**Fechado por:** {closer.mention}\n"
            f"**Motivo:** {reason}"
        ),
        color=discord.Color.red(),
        timestamp=datetime.now(timezone.utc)
    )

    try:
        settings = db.get_settings(guild.id)
        log_channel = guild.get_channel(settings["ticket_log_channel_id"]) if settings["ticket_log_channel_id"] else None
        if isinstance(log_channel, discord.TextChannel):
            file = discord.File(transcript_path, filename=transcript_path.name)
            await log_channel.send(embed=embed, file=file)
    except Exception as e:
        guild_logger.warning(f"Falha ao enviar transcript do ticket: {e}")

    try:
        await channel.send(embed=discord.Embed(
            title="🔒 Ticket encerrado",
            description=f"Este ticket será apagado em **5 segundos**.\n**Motivo:** {reason}",
            color=discord.Color.red()
        ))
    except Exception:
        pass

    await asyncio.sleep(5)

    try:
        await channel.delete(reason=f"Ticket finalizado por {closer} | {reason}")
    except Exception as e:
        guild_logger.warning(f"Erro ao deletar canal do ticket {channel.id}: {e}")

    return True, "Ticket encerrado com sucesso."


async def refresh_ticket_message(channel: discord.TextChannel):
    ticket = db.get_ticket_by_channel(channel.id)
    if not ticket or not bot.user:
        return

    creator = channel.guild.get_member(ticket["creator_id"])
    if not creator:
        return

    claimed_by = channel.guild.get_member(ticket["claimed_by_id"]) if ticket["claimed_by_id"] else None
    embed = build_ticket_embed(channel.guild, creator, ticket, claimed_by=claimed_by)

    try:
        async for msg in channel.history(limit=25, oldest_first=True):
            if msg.author.id == bot.user.id and msg.embeds:
                view = TicketControlView()
                await msg.edit(embed=embed, view=view)
                return
    except Exception:
        pass

    view = TicketControlView()
    await channel.send(embed=embed, view=view)


# =========================================================
# BOT
# =========================================================

intents = discord.Intents.default()
intents.members = True
intents.message_content = True
intents.guilds = True

bot = commands.Bot(command_prefix="!", intents=intents)


# =========================================================
# MODALS
# =========================================================

class TicketTypeModal(discord.ui.Modal, title="Abrir Ticket"):
    assunto = discord.ui.TextInput(
        label="Assunto",
        placeholder="Descreva brevemente o motivo do ticket",
        max_length=200,
        required=True
    )

    def __init__(self, ticket_type: str):
        super().__init__()
        self.ticket_type = ticket_type

    async def on_submit(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)
        guild = interaction.guild
        member = interaction.user

        if not guild or not isinstance(member, discord.Member):
            await interaction.followup.send("❌ Erro interno.", ephemeral=True)
            return

        existing = db.get_open_ticket_by_user(guild.id, member.id)
        if existing:
            ch = guild.get_channel(existing["channel_id"])
            if ch:
                await interaction.followup.send(
                    f"❌ Você já tem um ticket aberto: {ch.mention}", ephemeral=True
                )
                return

        settings = db.get_settings(guild.id)
        cfg = db.get_ticket_type(guild.id, self.ticket_type)

        if not cfg:
            await interaction.followup.send("❌ Tipo de ticket inválido.", ephemeral=True)
            return

        ticket_number = db.next_ticket_counter(guild.id)
        prefix = cfg["prefix"] or sanitize_channel_name(self.ticket_type)
        channel_name = f"{prefix}-{ticket_number:04d}"

        category = get_ticket_category(guild, self.ticket_type)

        overwrites = {
            guild.default_role: discord.PermissionOverwrite(read_messages=False),
            member: discord.PermissionOverwrite(read_messages=True, send_messages=True, attach_files=True),
            guild.me: discord.PermissionOverwrite(read_messages=True, send_messages=True, manage_channels=True)
        }

        if settings["support_role_id"]:
            role = guild.get_role(settings["support_role_id"])
            if role:
                overwrites[role] = discord.PermissionOverwrite(read_messages=True, send_messages=True)

        if settings["moderator_role_id"]:
            role = guild.get_role(settings["moderator_role_id"])
            if role:
                overwrites[role] = discord.PermissionOverwrite(read_messages=True, send_messages=True)

        try:
            channel = await guild.create_text_channel(
                channel_name,
                category=category,
                overwrites=overwrites,
                reason=f"Ticket criado por {member}"
            )
        except discord.Forbidden:
            await interaction.followup.send("❌ Sem permissão para criar o canal.", ephemeral=True)
            return

        ticket_id = db.create_ticket(guild.id, channel.id, member.id, self.ticket_type, ticket_number, channel_name)
        ticket = db.get_ticket_by_channel(channel.id)

        db.add_log(guild.id, ticket_id, channel.id, "ticket_created", target_id=member.id,
                   details=f"Assunto: {self.assunto.value}")

        embed = build_ticket_embed(guild, member, ticket)
        view = TicketControlView()

        await channel.send(
            content=f"{member.mention} — Ticket aberto com sucesso!",
            embed=embed,
            view=view
        )

        await interaction.followup.send(f"✅ Ticket criado: {channel.mention}", ephemeral=True)

        await send_ticket_log(
            guild,
            "🎫 Novo Ticket Aberto",
            f"**Usuário:** {member.mention}\n**Canal:** {channel.mention}\n**Tipo:** {cfg['label']}\n**Assunto:** {self.assunto.value}",
            discord.Color.green()
        )


class NoteModal(discord.ui.Modal, title="Nota Interna"):
    note = discord.ui.TextInput(
        label="Nota",
        style=discord.TextStyle.paragraph,
        placeholder="Escreva a nota interna aqui...",
        max_length=500,
        required=True
    )

    async def on_submit(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)
        ticket = db.get_ticket_by_channel(interaction.channel.id)
        if not ticket:
            await interaction.followup.send("❌ Ticket não encontrado.", ephemeral=True)
            return

        existing = ticket["internal_notes"] or ""
        timestamp = datetime.now(timezone.utc).strftime("%d/%m %H:%M")
        new_note = f"[{timestamp} | {interaction.user.display_name}]: {self.note.value}"
        updated = f"{existing}\n{new_note}".strip()[-2000:]

        db.update_ticket(interaction.channel.id, internal_notes=updated)
        await refresh_ticket_message(interaction.channel)
        await interaction.followup.send("✅ Nota adicionada.", ephemeral=True)


class RenameModal(discord.ui.Modal, title="Renomear Canal"):
    new_name = discord.ui.TextInput(
        label="Novo nome",
        placeholder="Novo nome do canal (sem espaços)",
        max_length=90,
        required=True
    )

    async def on_submit(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)
        name = sanitize_channel_name(self.new_name.value)
        if not name:
            await interaction.followup.send("❌ Nome inválido.", ephemeral=True)
            return
        try:
            await interaction.channel.edit(name=name)
            db.update_ticket(interaction.channel.id, channel_name=name)
            await interaction.followup.send(f"✅ Canal renomeado para `{name}`.", ephemeral=True)
        except discord.Forbidden:
            await interaction.followup.send("❌ Sem permissão para renomear.", ephemeral=True)


class AddMemberModal(discord.ui.Modal, title="Adicionar Membro"):
    user_id = discord.ui.TextInput(
        label="ID ou menção do usuário",
        placeholder="Ex: 123456789012345678 ou @usuario",
        max_length=30,
        required=True
    )

    async def on_submit(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)
        uid = extract_user_id(self.user_id.value)
        if not uid:
            await interaction.followup.send("❌ ID inválido.", ephemeral=True)
            return

        member = interaction.guild.get_member(uid)
        if not member:
            await interaction.followup.send("❌ Membro não encontrado.", ephemeral=True)
            return

        try:
            await interaction.channel.set_permissions(
                member, read_messages=True, send_messages=True, attach_files=True
            )
            await interaction.followup.send(f"✅ {member.mention} adicionado ao ticket.", ephemeral=True)
            await interaction.channel.send(f"🔓 {member.mention} foi adicionado ao ticket por {interaction.user.mention}.")
        except discord.Forbidden:
            await interaction.followup.send("❌ Sem permissão.", ephemeral=True)


class RemoveMemberModal(discord.ui.Modal, title="Remover Membro"):
    user_id = discord.ui.TextInput(
        label="ID ou menção do usuário",
        placeholder="Ex: 123456789012345678",
        max_length=30,
        required=True
    )

    async def on_submit(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)
        uid = extract_user_id(self.user_id.value)
        if not uid:
            await interaction.followup.send("❌ ID inválido.", ephemeral=True)
            return

        member = interaction.guild.get_member(uid)
        if not member:
            await interaction.followup.send("❌ Membro não encontrado.", ephemeral=True)
            return

        ticket = db.get_ticket_by_channel(interaction.channel.id)
        if ticket and ticket["creator_id"] == uid:
            await interaction.followup.send("❌ Não é possível remover o criador do ticket.", ephemeral=True)
            return

        try:
            await interaction.channel.set_permissions(member, overwrite=None)
            await interaction.followup.send(f"✅ {member.mention} removido do ticket.", ephemeral=True)
            await interaction.channel.send(f"🔒 {member.mention} foi removido do ticket por {interaction.user.mention}.")
        except discord.Forbidden:
            await interaction.followup.send("❌ Sem permissão.", ephemeral=True)


class MoveTicketModal(discord.ui.Modal, title="Mover Ticket"):
    ticket_type = discord.ui.TextInput(
        label="Tipo de ticket (ex: suporte, compras)",
        placeholder="suporte",
        max_length=50,
        required=True
    )

    async def on_submit(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)
        guild = interaction.guild
        ttype = self.ticket_type.value.strip().lower()
        cfg = db.get_ticket_type(guild.id, ttype)

        if not cfg:
            await interaction.followup.send(f"❌ Tipo `{ttype}` não encontrado.", ephemeral=True)
            return

        category = get_ticket_category(guild, ttype)
        if not category:
            await interaction.followup.send("❌ Categoria não configurada.", ephemeral=True)
            return

        try:
            await interaction.channel.edit(category=category)
            db.update_ticket(interaction.channel.id, ticket_type=ttype)
            await interaction.followup.send(f"✅ Ticket movido para **{cfg['label']}**.", ephemeral=True)
            await interaction.channel.send(f"📁 Ticket movido para **{cfg['emoji']} {cfg['label']}** por {interaction.user.mention}.")
        except discord.Forbidden:
            await interaction.followup.send("❌ Sem permissão.", ephemeral=True)


class GreetModal(discord.ui.Modal, title="Saudar Usuário"):
    message = discord.ui.TextInput(
        label="Mensagem de saudação",
        style=discord.TextStyle.paragraph,
        default="Olá! Seja bem-vindo(a). Em que posso ajudar?",
        max_length=500,
        required=True
    )

    async def on_submit(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)
        ticket = db.get_ticket_by_channel(interaction.channel.id)
        creator = interaction.guild.get_member(ticket["creator_id"]) if ticket else None

        mention = creator.mention if creator else ""
        await interaction.channel.send(
            embed=discord.Embed(
                description=f"{mention}\n\n{self.message.value}",
                color=discord.Color.green()
            ).set_author(
                name=interaction.user.display_name,
                icon_url=interaction.user.display_avatar.url
            )
        )
        await interaction.followup.send("✅ Saudação enviada.", ephemeral=True)


class RegistrationModal(discord.ui.Modal, title="📋 Registro GTA RP"):
    character_name = discord.ui.TextInput(label="Nome do Personagem", max_length=100, required=True)
    character_age = discord.ui.TextInput(label="Idade do Personagem", max_length=3, required=True)
    character_id = discord.ui.TextInput(label="ID / Passaporte do Personagem", max_length=50, required=False, default="")
    experience = discord.ui.TextInput(
        label="Experiência em RP",
        style=discord.TextStyle.paragraph,
        placeholder="Conte sua experiência com roleplay...",
        max_length=500,
        required=True
    )
    story = discord.ui.TextInput(
        label="História do Personagem",
        style=discord.TextStyle.paragraph,
        placeholder="Descreva a história e personalidade do seu personagem...",
        max_length=1000,
        required=True
    )

    async def on_submit(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)
        guild = interaction.guild
        member = interaction.user

        if not guild or not isinstance(member, discord.Member):
            return

        pending = db.get_pending_registration_by_user(guild.id, member.id)
        if pending:
            await interaction.followup.send("❌ Você já possui um registro pendente de análise.", ephemeral=True)
            return

        try:
            age = int(self.character_age.value)
        except ValueError:
            await interaction.followup.send("❌ A idade deve ser um número.", ephemeral=True)
            return

        if age < 1 or age > 150:
            await interaction.followup.send("❌ Idade inválida para o personagem.", ephemeral=True)
            return

        reg_id = db.create_registration(
            guild_id=guild.id,
            user_id=member.id,
            discord_name=str(member),
            character_name=self.character_name.value,
            character_age=age,
            character_id=self.character_id.value or "",
            whitelisted_name="",
            experience=self.experience.value,
            availability="",
            story=self.story.value
        )

        reg_settings = db.get_registration_settings(guild.id)

        if reg_settings["pending_role_id"]:
            role = guild.get_role(reg_settings["pending_role_id"])
            if role:
                try:
                    await member.add_roles(role, reason="Registro pendente")
                except Exception:
                    pass

        embed = discord.Embed(
            title=f"📋 Novo Registro #{reg_id}",
            description=f"**Usuário:** {member.mention} (`{member.id}`)",
            color=discord.Color.orange(),
            timestamp=datetime.now(timezone.utc)
        )
        embed.add_field(name="Nome do Personagem", value=self.character_name.value, inline=True)
        embed.add_field(name="Idade", value=str(age), inline=True)
        embed.add_field(name="ID/Passaporte", value=self.character_id.value or "Não informado", inline=True)
        embed.add_field(name="Experiência em RP", value=self.experience.value, inline=False)
        embed.add_field(name="História", value=self.story.value[:1024], inline=False)
        embed.set_footer(text=f"ID do Registro: {reg_id}")

        if member.display_avatar:
            embed.set_thumbnail(url=member.display_avatar.url)

        view = RegistrationReviewView(reg_id)

        if reg_settings["registration_log_channel_id"]:
            log_channel = guild.get_channel(reg_settings["registration_log_channel_id"])
            if isinstance(log_channel, discord.TextChannel):
                try:
                    await log_channel.send(embed=embed, view=view)
                except Exception:
                    pass

        await interaction.followup.send(
            embed=discord.Embed(
                title="✅ Registro enviado!",
                description="Seu registro foi enviado para análise da equipe. Aguarde a resposta.",
                color=discord.Color.green()
            ),
            ephemeral=True
        )


# =========================================================
# VIEWS
# =========================================================

class TicketSelectView(discord.ui.View):
    def __init__(self, ticket_types):
        super().__init__(timeout=None)
        options = []
        for row in ticket_types[:25]:
            options.append(discord.SelectOption(
                label=row["panel_label"] or row["label"],
                value=row["ticket_type"],
                description=(row["panel_description"] or "")[:100],
                emoji=row["emoji"]
            ))

        if options:
            select = discord.ui.Select(
                placeholder="Selecione o tipo de atendimento...",
                options=options,
                custom_id="ticket_type_select"
            )
            select.callback = self.select_callback
            self.add_item(select)

    async def select_callback(self, interaction: discord.Interaction):
        ticket_type = interaction.data["values"][0]
        await interaction.response.send_modal(TicketTypeModal(ticket_type))


class TicketControlView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)

    async def _check_staff(self, interaction: discord.Interaction) -> bool:
        settings = db.get_settings(interaction.guild.id)
        if not is_staff(interaction.user, settings):
            await interaction.response.send_message("❌ Você não tem permissão.", ephemeral=True)
            return False
        return True

    @discord.ui.button(label="✅ Assumir", style=discord.ButtonStyle.success, custom_id="ticket_claim", row=0)
    async def claim(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not await self._check_staff(interaction):
            return
        ticket = db.get_ticket_by_channel(interaction.channel.id)
        if not ticket:
            await interaction.response.send_message("❌ Ticket não encontrado.", ephemeral=True)
            return
        db.update_ticket(interaction.channel.id, claimed_by_id=interaction.user.id)
        await refresh_ticket_message(interaction.channel)
        await interaction.response.send_message(
            embed=discord.Embed(
                description=f"✅ {interaction.user.mention} assumiu o atendimento.",
                color=discord.Color.green()
            )
        )

    @discord.ui.button(label="👋 Saudar", style=discord.ButtonStyle.primary, custom_id="ticket_greet", row=0)
    async def greet(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not await self._check_staff(interaction):
            return
        await interaction.response.send_modal(GreetModal())

    @discord.ui.button(label="📝 Nota", style=discord.ButtonStyle.secondary, custom_id="ticket_note", row=0)
    async def note(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not await self._check_staff(interaction):
            return
        await interaction.response.send_modal(NoteModal())

    @discord.ui.button(label="➕ Adicionar", style=discord.ButtonStyle.secondary, custom_id="ticket_add", row=1)
    async def add_member(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not await self._check_staff(interaction):
            return
        await interaction.response.send_modal(AddMemberModal())

    @discord.ui.button(label="➖ Remover", style=discord.ButtonStyle.secondary, custom_id="ticket_remove", row=1)
    async def remove_member(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not await self._check_staff(interaction):
            return
        await interaction.response.send_modal(RemoveMemberModal())

    @discord.ui.button(label="📁 Mover", style=discord.ButtonStyle.secondary, custom_id="ticket_move", row=1)
    async def move(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not await self._check_staff(interaction):
            return
        await interaction.response.send_modal(MoveTicketModal())

    @discord.ui.button(label="✏️ Renomear", style=discord.ButtonStyle.secondary, custom_id="ticket_rename", row=2)
    async def rename(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not await self._check_staff(interaction):
            return
        await interaction.response.send_modal(RenameModal())

    @discord.ui.button(label="📩 DM Help", style=discord.ButtonStyle.secondary, custom_id="ticket_dm", row=2)
    async def dm_help(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not await self._check_staff(interaction):
            return
        ticket = db.get_ticket_by_channel(interaction.channel.id)
        if not ticket:
            await interaction.response.send_message("❌ Ticket não encontrado.", ephemeral=True)
            return
        creator = interaction.guild.get_member(ticket["creator_id"])
        if not creator:
            await interaction.response.send_message("❌ Criador do ticket não encontrado.", ephemeral=True)
            return
        try:
            await creator.send(embed=discord.Embed(
                title="📩 Mensagem do Atendimento",
                description=(
                    f"Olá {creator.mention}!\n\n"
                    f"A equipe do servidor **{interaction.guild.name}** está entrando em contato sobre seu ticket.\n"
                    f"Por favor, acesse o canal {interaction.channel.mention} para continuar o atendimento."
                ),
                color=discord.Color.blurple()
            ))
            await interaction.response.send_message("✅ DM enviada ao usuário.", ephemeral=True)
        except discord.Forbidden:
            await interaction.response.send_message("❌ Não foi possível enviar DM (DMs bloqueadas).", ephemeral=True)

    @discord.ui.button(label="🔒 Fechar", style=discord.ButtonStyle.danger, custom_id="ticket_close", row=2)
    async def close(self, interaction: discord.Interaction, button: discord.ui.Button):
        settings = db.get_settings(interaction.guild.id)
        ticket = db.get_ticket_by_channel(interaction.channel.id)

        if not ticket:
            await interaction.response.send_message("❌ Ticket não encontrado.", ephemeral=True)
            return

        is_owner = ticket["creator_id"] == interaction.user.id
        is_staff_member = is_staff(interaction.user, settings)

        if not is_owner and not is_staff_member:
            await interaction.response.send_message("❌ Apenas o criador ou a equipe pode fechar o ticket.", ephemeral=True)
            return

        await interaction.response.send_message(
            embed=discord.Embed(
                title="🔒 Fechar Ticket",
                description="Você confirma o encerramento deste ticket?",
                color=discord.Color.red()
            ),
            view=CloseConfirmView(),
            ephemeral=True
        )

    @discord.ui.button(label="❌ Cancelar", style=discord.ButtonStyle.danger, custom_id="ticket_cancel", row=3)
    async def cancel(self, interaction: discord.Interaction, button: discord.ui.Button):
        ticket = db.get_ticket_by_channel(interaction.channel.id)
        if not ticket:
            await interaction.response.send_message("❌ Ticket não encontrado.", ephemeral=True)
            return

        settings = db.get_settings(interaction.guild.id)
        is_owner = ticket["creator_id"] == interaction.user.id
        is_staff_member = is_staff(interaction.user, settings)

        if not is_owner and not is_staff_member:
            await interaction.response.send_message("❌ Sem permissão.", ephemeral=True)
            return

        await interaction.response.send_message(
            embed=discord.Embed(
                title="❌ Cancelar Ticket",
                description="Você confirma o **cancelamento** deste ticket?",
                color=discord.Color.orange()
            ),
            view=CancelConfirmView(),
            ephemeral=True
        )


class CloseConfirmView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=30)

    @discord.ui.button(label="✅ Confirmar", style=discord.ButtonStyle.danger)
    async def confirm(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.defer()
        await close_ticket(interaction.channel, interaction.user, reason="Fechado pela equipe")

    @discord.ui.button(label="❌ Cancelar", style=discord.ButtonStyle.secondary)
    async def cancel(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.send_message("Ação cancelada.", ephemeral=True)


class CancelConfirmView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=30)

    @discord.ui.button(label="✅ Confirmar Cancelamento", style=discord.ButtonStyle.danger)
    async def confirm(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.defer()
        await close_ticket(interaction.channel, interaction.user, reason="Cancelado pelo usuário")

    @discord.ui.button(label="Voltar", style=discord.ButtonStyle.secondary)
    async def cancel(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.send_message("Ação cancelada.", ephemeral=True)


class RegistrationPanelView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)

    @discord.ui.button(label="📋 Enviar Registro", style=discord.ButtonStyle.primary, custom_id="open_registration")
    async def open_registration(self, interaction: discord.Interaction, button: discord.ui.Button):
        pending = db.get_pending_registration_by_user(interaction.guild.id, interaction.user.id)
        if pending:
            await interaction.response.send_message(
                "❌ Você já possui um registro pendente. Aguarde a análise da equipe.", ephemeral=True
            )
            return
        await interaction.response.send_modal(RegistrationModal())


class RegistrationReviewView(discord.ui.View):
    def __init__(self, registration_id: int):
        super().__init__(timeout=None)
        self.registration_id = registration_id

    @discord.ui.button(label="✅ Aprovar", style=discord.ButtonStyle.success, custom_id="reg_approve")
    async def approve(self, interaction: discord.Interaction, button: discord.ui.Button):
        settings = db.get_settings(interaction.guild.id)
        if not is_moderator(interaction.user, settings):
            await interaction.response.send_message("❌ Sem permissão.", ephemeral=True)
            return

        reg = db.get_registration(self.registration_id)
        if not reg or reg["status"] != "pending":
            await interaction.response.send_message("❌ Registro não encontrado ou já revisado.", ephemeral=True)
            return

        db.update_registration(self.registration_id, status="approved", reviewed_by=interaction.user.id, reviewed_at=utc_now())

        reg_settings = db.get_registration_settings(interaction.guild.id)
        member = interaction.guild.get_member(reg["user_id"])

        if member:
            if reg_settings["approved_role_id"]:
                role = interaction.guild.get_role(reg_settings["approved_role_id"])
                if role:
                    try:
                        await member.add_roles(role, reason="Registro aprovado")
                    except Exception:
                        pass

            if reg_settings["pending_role_id"]:
                role = interaction.guild.get_role(reg_settings["pending_role_id"])
                if role and role in member.roles:
                    try:
                        await member.remove_roles(role, reason="Registro aprovado")
                    except Exception:
                        pass

            try:
                await member.send(embed=discord.Embed(
                    title="✅ Registro Aprovado!",
                    description=(
                        f"Olá **{member.display_name}**!\n\n"
                        f"Seu registro no servidor **{interaction.guild.name}** foi **aprovado**!\n"
                        f"Personagem: **{reg['character_name']}**\n\n"
                        f"Bem-vindo(a) ao roleplay! 🎉"
                    ),
                    color=discord.Color.green()
                ))
            except discord.Forbidden:
                pass

        embed = interaction.message.embeds[0] if interaction.message.embeds else discord.Embed()
        embed.color = discord.Color.green()
        embed.add_field(name="✅ Status", value=f"Aprovado por {interaction.user.mention}", inline=False)

        await interaction.response.edit_message(embed=embed, view=None)

    @discord.ui.button(label="❌ Reprovar", style=discord.ButtonStyle.danger, custom_id="reg_reject")
    async def reject(self, interaction: discord.Interaction, button: discord.ui.Button):
        settings = db.get_settings(interaction.guild.id)
        if not is_moderator(interaction.user, settings):
            await interaction.response.send_message("❌ Sem permissão.", ephemeral=True)
            return

        await interaction.response.send_modal(RejectRegistrationModal(self.registration_id, interaction.message))


class RejectRegistrationModal(discord.ui.Modal, title="Reprovar Registro"):
    reason = discord.ui.TextInput(
        label="Motivo da reprovação",
        style=discord.TextStyle.paragraph,
        placeholder="Explique o motivo...",
        max_length=500,
        required=True
    )

    def __init__(self, registration_id: int, message: discord.Message):
        super().__init__()
        self.registration_id = registration_id
        self.message = message

    async def on_submit(self, interaction: discord.Interaction):
        await interaction.response.defer()
        reg = db.get_registration(self.registration_id)
        if not reg or reg["status"] != "pending":
            return

        db.update_registration(
            self.registration_id,
            status="rejected",
            reviewed_by=interaction.user.id,
            review_reason=self.reason.value,
            reviewed_at=utc_now()
        )

        reg_settings = db.get_registration_settings(interaction.guild.id)
        member = interaction.guild.get_member(reg["user_id"])

        if member:
            if reg_settings["rejected_role_id"]:
                role = interaction.guild.get_role(reg_settings["rejected_role_id"])
                if role:
                    try:
                        await member.add_roles(role, reason="Registro reprovado")
                    except Exception:
                        pass

            if reg_settings["pending_role_id"]:
                role = interaction.guild.get_role(reg_settings["pending_role_id"])
                if role and role in member.roles:
                    try:
                        await member.remove_roles(role, reason="Registro reprovado")
                    except Exception:
                        pass

            try:
                await member.send(embed=discord.Embed(
                    title="❌ Registro Reprovado",
                    description=(
                        f"Olá **{member.display_name}**!\n\n"
                        f"Seu registro no servidor **{interaction.guild.name}** foi **reprovado**.\n\n"
                        f"**Motivo:** {self.reason.value}\n\n"
                        f"Caso tenha dúvidas, abra um ticket no servidor."
                    ),
                    color=discord.Color.red()
                ))
            except discord.Forbidden:
                pass

        if self.message.embeds:
            embed = self.message.embeds[0]
            embed.color = discord.Color.red()
            embed.add_field(
                name="❌ Status",
                value=f"Reprovado por {interaction.user.mention}\n**Motivo:** {self.reason.value}",
                inline=False
            )
            try:
                await self.message.edit(embed=embed, view=None)
            except Exception:
                pass


# =========================================================
# EVENTOS
# =========================================================

@bot.event
async def on_ready():
    logger.info(f"Bot conectado como {bot.user} (ID: {bot.user.id})")

    bot.add_view(TicketControlView())
    bot.add_view(RegistrationPanelView())

    try:
        synced = await bot.tree.sync()
        logger.info(f"Sincronizados {len(synced)} comandos slash.")
    except Exception as e:
        logger.error(f"Erro ao sincronizar comandos: {e}")


@bot.event
async def on_member_join(member: discord.Member):
    settings = db.get_settings(member.guild.id)
    if not settings["welcome_enabled"]:
        return

    channel_id = settings["welcome_channel_id"]
    if not channel_id:
        return

    channel = member.guild.get_channel(channel_id)
    if not isinstance(channel, discord.TextChannel):
        return

    message = format_template(settings["welcome_message"], member)

    embed = discord.Embed(
        title="👋 Bem-vindo(a)!",
        description=message,
        color=discord.Color.green(),
        timestamp=datetime.now(timezone.utc)
    )

    if member.display_avatar:
        embed.set_thumbnail(url=member.display_avatar.url)

    embed.set_footer(text=f"{member.guild.name} • {member.guild.member_count} membros")

    try:
        await channel.send(embed=embed)
    except Exception:
        pass


@bot.event
async def on_member_remove(member: discord.Member):
    settings = db.get_settings(member.guild.id)
    if not settings["goodbye_enabled"]:
        return

    channel_id = settings["goodbye_channel_id"]
    if not channel_id:
        return

    channel = member.guild.get_channel(channel_id)
    if not isinstance(channel, discord.TextChannel):
        return

    message = format_template(settings["goodbye_message"], member)

    embed = discord.Embed(
        description=message,
        color=discord.Color.red(),
        timestamp=datetime.now(timezone.utc)
    )

    embed.set_footer(text=f"{member.guild.name}")

    try:
        await channel.send(embed=embed)
    except Exception:
        pass


# =========================================================
# COMANDOS SLASH — SETUP
# =========================================================

@bot.tree.command(name="setup-ticket", description="[ADMIN] Envia o painel de tickets no canal atual")
@app_commands.default_permissions(administrator=True)
async def setup_ticket(interaction: discord.Interaction):
    await interaction.response.defer(ephemeral=True)
    guild = interaction.guild

    await ensure_ticket_categories(guild)

    ticket_types = db.get_ticket_types(guild.id)
    embed = build_panel_embed(guild)

    if ticket_types:
        view = TicketSelectView(ticket_types)
    else:
        view = discord.ui.View()

    msg = await interaction.channel.send(embed=embed, view=view)
    db.update_settings(guild.id, panel_channel_id=interaction.channel.id)
    await interaction.followup.send(f"✅ Painel enviado em {interaction.channel.mention}.", ephemeral=True)


@bot.tree.command(name="setup-registro", description="[ADMIN] Envia o painel de registro GTA RP")
@app_commands.default_permissions(administrator=True)
async def setup_registro(interaction: discord.Interaction):
    await interaction.response.defer(ephemeral=True)
    embed = build_registration_panel_embed(interaction.guild)
    view = RegistrationPanelView()
    await interaction.channel.send(embed=embed, view=view)
    await interaction.followup.send(f"✅ Painel de registro enviado.", ephemeral=True)


@bot.tree.command(name="config", description="[ADMIN] Configura canais e cargos do bot")
@app_commands.describe(
    ticket_log="Canal de logs de tickets",
    mod_log="Canal de logs de moderação",
    welcome="Canal de boas-vindas",
    goodbye="Canal de despedida",
    support_role="Cargo de suporte",
    moderator_role="Cargo de moderador",
    prison_role="Cargo de preso (GTA RP)",
    prison_channel="Canal de preso (GTA RP)"
)
@app_commands.default_permissions(administrator=True)
async def config(
    interaction: discord.Interaction,
    ticket_log: Optional[discord.TextChannel] = None,
    mod_log: Optional[discord.TextChannel] = None,
    welcome: Optional[discord.TextChannel] = None,
    goodbye: Optional[discord.TextChannel] = None,
    support_role: Optional[discord.Role] = None,
    moderator_role: Optional[discord.Role] = None,
    prison_role: Optional[discord.Role] = None,
    prison_channel: Optional[discord.TextChannel] = None
):
    await interaction.response.defer(ephemeral=True)
    updates = {}

    if ticket_log:
        updates["ticket_log_channel_id"] = ticket_log.id
    if mod_log:
        updates["mod_log_channel_id"] = mod_log.id
    if welcome:
        updates["welcome_channel_id"] = welcome.id
    if goodbye:
        updates["goodbye_channel_id"] = goodbye.id
    if support_role:
        updates["support_role_id"] = support_role.id
    if moderator_role:
        updates["moderator_role_id"] = moderator_role.id
    if prison_role:
        updates["prison_role_id"] = prison_role.id
    if prison_channel:
        updates["prison_channel_id"] = prison_channel.id

    if updates:
        db.update_settings(interaction.guild.id, **updates)

    settings = db.get_settings(interaction.guild.id)

    def ch(cid):
        return f"<#{cid}>" if cid else "Não configurado"

    def rl(rid):
        return f"<@&{rid}>" if rid else "Não configurado"

    embed = discord.Embed(title="⚙️ Configurações Atuais", color=discord.Color.blurple())
    embed.add_field(name="📋 Log de Tickets", value=ch(settings["ticket_log_channel_id"]), inline=True)
    embed.add_field(name="🔨 Log de Moderação", value=ch(settings["mod_log_channel_id"]), inline=True)
    embed.add_field(name="👋 Boas-vindas", value=ch(settings["welcome_channel_id"]), inline=True)
    embed.add_field(name="👋 Despedida", value=ch(settings["goodbye_channel_id"]), inline=True)
    embed.add_field(name="🎧 Cargo Suporte", value=rl(settings["support_role_id"]), inline=True)
    embed.add_field(name="🔨 Cargo Moderador", value=rl(settings["moderator_role_id"]), inline=True)
    embed.add_field(name="⛓️ Cargo Preso", value=rl(settings["prison_role_id"]), inline=True)
    embed.add_field(name="🏠 Canal Prisão", value=ch(settings["prison_channel_id"]), inline=True)

    await interaction.followup.send(embed=embed, ephemeral=True)


@bot.tree.command(name="config-registro", description="[ADMIN] Configura o sistema de registro GTA RP")
@app_commands.describe(
    log_channel="Canal de log de registros",
    approved_role="Cargo dos aprovados",
    rejected_role="Cargo dos reprovados",
    pending_role="Cargo dos pendentes"
)
@app_commands.default_permissions(administrator=True)
async def config_registro(
    interaction: discord.Interaction,
    log_channel: Optional[discord.TextChannel] = None,
    approved_role: Optional[discord.Role] = None,
    rejected_role: Optional[discord.Role] = None,
    pending_role: Optional[discord.Role] = None
):
    await interaction.response.defer(ephemeral=True)
    updates = {}

    if log_channel:
        updates["registration_log_channel_id"] = log_channel.id
    if approved_role:
        updates["approved_role_id"] = approved_role.id
    if rejected_role:
        updates["rejected_role_id"] = rejected_role.id
    if pending_role:
        updates["pending_role_id"] = pending_role.id

    if updates:
        db.update_registration_settings(interaction.guild.id, **updates)

    settings = db.get_registration_settings(interaction.guild.id)

    embed = discord.Embed(title="📋 Config de Registro", color=discord.Color.blurple())
    embed.add_field(name="Canal de Log", value=f"<#{settings['registration_log_channel_id']}>" if settings['registration_log_channel_id'] else "Não configurado", inline=False)
    embed.add_field(name="Cargo Aprovado", value=f"<@&{settings['approved_role_id']}>" if settings['approved_role_id'] else "Não configurado", inline=True)
    embed.add_field(name="Cargo Reprovado", value=f"<@&{settings['rejected_role_id']}>" if settings['rejected_role_id'] else "Não configurado", inline=True)
    embed.add_field(name="Cargo Pendente", value=f"<@&{settings['pending_role_id']}>" if settings['pending_role_id'] else "Não configurado", inline=True)

    await interaction.followup.send(embed=embed, ephemeral=True)


@bot.tree.command(name="config-panel", description="[ADMIN] Personaliza o embed do painel de tickets")
@app_commands.describe(
    titulo="Título do painel",
    descricao="Descrição do painel",
    cor="Cor hex do painel (ex: #5865F2)",
    imagem_url="URL da imagem do painel",
    thumbnail_url="URL do thumbnail",
    rodape="Texto do rodapé"
)
@app_commands.default_permissions(administrator=True)
async def config_panel(
    interaction: discord.Interaction,
    titulo: Optional[str] = None,
    descricao: Optional[str] = None,
    cor: Optional[str] = None,
    imagem_url: Optional[str] = None,
    thumbnail_url: Optional[str] = None,
    rodape: Optional[str] = None
):
    await interaction.response.defer(ephemeral=True)
    updates = {}

    if titulo:
        updates["title"] = titulo
    if descricao:
        updates["description"] = descricao
    if cor:
        updates["color"] = cor
    if imagem_url:
        updates["image_url"] = imagem_url
    if thumbnail_url:
        updates["thumbnail_url"] = thumbnail_url
    if rodape:
        updates["footer_text"] = rodape

    if updates:
        db.update_panel_config(interaction.guild.id, **updates)

    await interaction.followup.send("✅ Painel atualizado! Use `/setup-ticket` para reenviar.", ephemeral=True)


@bot.tree.command(name="config-tipo", description="[ADMIN] Cria ou edita um tipo de ticket")
@app_commands.describe(
    tipo="Identificador do tipo (ex: suporte)",
    emoji="Emoji do tipo",
    label="Nome exibido",
    descricao="Descrição curta",
    titulo_ticket="Título do embed do ticket",
    descricao_ticket="Descrição do embed do ticket",
    cor="Cor hex (ex: #5865F2)",
    ativar="Ativar ou desativar este tipo"
)
@app_commands.default_permissions(administrator=True)
async def config_tipo(
    interaction: discord.Interaction,
    tipo: str,
    emoji: Optional[str] = None,
    label: Optional[str] = None,
    descricao: Optional[str] = None,
    titulo_ticket: Optional[str] = None,
    descricao_ticket: Optional[str] = None,
    cor: Optional[str] = None,
    ativar: Optional[bool] = None
):
    await interaction.response.defer(ephemeral=True)
    tipo = tipo.lower().strip()
    updates = {}

    if emoji:
        updates["emoji"] = emoji
    if label:
        updates["label"] = label
        updates["panel_label"] = label
    if descricao:
        updates["panel_description"] = descricao
    if titulo_ticket:
        updates["ticket_title"] = titulo_ticket
    if descricao_ticket:
        updates["ticket_description"] = descricao_ticket
    if cor:
        updates["ticket_color"] = cor
    if ativar is not None:
        updates["active"] = 1 if ativar else 0

    db.upsert_ticket_type(interaction.guild.id, tipo, **updates)
    await interaction.followup.send(f"✅ Tipo `{tipo}` configurado com sucesso!", ephemeral=True)


# =========================================================
# COMANDOS SLASH — MODERAÇÃO
# =========================================================

@bot.tree.command(name="ban", description="[MOD] Banir um usuário")
@app_commands.describe(usuario="Usuário a banir", motivo="Motivo do ban", dias_mensagens="Dias de mensagens a deletar (0-7)")
@app_commands.default_permissions(ban_members=True)
async def ban(interaction: discord.Interaction, usuario: discord.Member, motivo: str = "Sem motivo", dias_mensagens: int = 0):
    await interaction.response.defer(ephemeral=True)

    if not can_act_on_member(interaction.user, usuario):
        await interaction.followup.send("❌ Você não pode banir este usuário (hierarquia).", ephemeral=True)
        return

    if not bot_can_act_on_member(interaction.guild, usuario):
        await interaction.followup.send("❌ O bot não pode banir este usuário.", ephemeral=True)
        return

    try:
        await usuario.send(embed=discord.Embed(
            title="🔨 Você foi banido",
            description=f"**Servidor:** {interaction.guild.name}\n**Motivo:** {motivo}",
            color=discord.Color.red()
        ))
    except Exception:
        pass

    await interaction.guild.ban(usuario, reason=f"{motivo} | Por: {interaction.user}", delete_message_days=max(0, min(7, dias_mensagens)))
    db.add_infraction(interaction.guild.id, usuario.id, interaction.user.id, "ban", reason=motivo)

    await interaction.followup.send(embed=discord.Embed(
        title="🔨 Usuário Banido",
        description=f"**Usuário:** {usuario.mention}\n**Motivo:** {motivo}\n**Por:** {interaction.user.mention}",
        color=discord.Color.red()
    ), ephemeral=True)

    await send_mod_log(
        interaction.guild,
        "🔨 Ban",
        f"**Usuário:** {usuario} (`{usuario.id}`)\n**Motivo:** {motivo}\n**Moderador:** {interaction.user.mention}",
        discord.Color.red()
    )


@bot.tree.command(name="unban", description="[MOD] Desbanir um usuário pelo ID")
@app_commands.describe(user_id="ID do usuário a desbanir", motivo="Motivo do unban")
@app_commands.default_permissions(ban_members=True)
async def unban(interaction: discord.Interaction, user_id: str, motivo: str = "Sem motivo"):
    await interaction.response.defer(ephemeral=True)
    uid = extract_user_id(user_id)
    if not uid:
        await interaction.followup.send("❌ ID inválido.", ephemeral=True)
        return

    try:
        user = await bot.fetch_user(uid)
        await interaction.guild.unban(user, reason=f"{motivo} | Por: {interaction.user}")
        db.add_infraction(interaction.guild.id, uid, interaction.user.id, "unban", reason=motivo, active=0)
        await interaction.followup.send(embed=discord.Embed(
            title="✅ Usuário Desbanido",
            description=f"**Usuário:** {user} (`{uid}`)\n**Motivo:** {motivo}",
            color=discord.Color.green()
        ), ephemeral=True)
        await send_mod_log(interaction.guild, "✅ Unban", f"**Usuário:** {user} (`{uid}`)\n**Moderador:** {interaction.user.mention}", discord.Color.green())
    except discord.NotFound:
        await interaction.followup.send("❌ Usuário não encontrado ou não está banido.", ephemeral=True)


@bot.tree.command(name="kick", description="[MOD] Expulsar um usuário")
@app_commands.describe(usuario="Usuário a expulsar", motivo="Motivo do kick")
@app_commands.default_permissions(kick_members=True)
async def kick(interaction: discord.Interaction, usuario: discord.Member, motivo: str = "Sem motivo"):
    await interaction.response.defer(ephemeral=True)

    if not can_act_on_member(interaction.user, usuario):
        await interaction.followup.send("❌ Hierarquia inválida.", ephemeral=True)
        return

    if not bot_can_act_on_member(interaction.guild, usuario):
        await interaction.followup.send("❌ Bot não pode kickar este usuário.", ephemeral=True)
        return

    try:
        await usuario.send(embed=discord.Embed(
            title="👢 Você foi expulso",
            description=f"**Servidor:** {interaction.guild.name}\n**Motivo:** {motivo}",
            color=discord.Color.orange()
        ))
    except Exception:
        pass

    await usuario.kick(reason=f"{motivo} | Por: {interaction.user}")
    db.add_infraction(interaction.guild.id, usuario.id, interaction.user.id, "kick", reason=motivo)

    await interaction.followup.send(embed=discord.Embed(
        title="👢 Usuário Expulso",
        description=f"**Usuário:** {usuario.mention}\n**Motivo:** {motivo}\n**Por:** {interaction.user.mention}",
        color=discord.Color.orange()
    ), ephemeral=True)

    await send_mod_log(interaction.guild, "👢 Kick", f"**Usuário:** {usuario}\n**Motivo:** {motivo}\n**Moderador:** {interaction.user.mention}", discord.Color.orange())


@bot.tree.command(name="mute", description="[MOD] Silenciar um usuário temporariamente (timeout)")
@app_commands.describe(usuario="Usuário a silenciar", duracao="Duração (ex: 10m, 1h, 2d)", motivo="Motivo")
@app_commands.default_permissions(moderate_members=True)
async def mute(interaction: discord.Interaction, usuario: discord.Member, duracao: str, motivo: str = "Sem motivo"):
    await interaction.response.defer(ephemeral=True)

    delta = parse_duration_to_timedelta(duracao)
    if not delta:
        await interaction.followup.send("❌ Formato de duração inválido. Use: `10s`, `5m`, `2h`, `1d`", ephemeral=True)
        return

    if delta.total_seconds() > 2419200:
        await interaction.followup.send("❌ Duração máxima: 28 dias.", ephemeral=True)
        return

    if not can_act_on_member(interaction.user, usuario):
        await interaction.followup.send("❌ Hierarquia inválida.", ephemeral=True)
        return

    until = datetime.now(timezone.utc) + delta
    await usuario.timeout(until, reason=f"{motivo} | Por: {interaction.user}")

    db.add_infraction(interaction.guild.id, usuario.id, interaction.user.id, "mute", reason=motivo,
                      duration_seconds=int(delta.total_seconds()),
                      expires_at=dt_to_str(until))

    await interaction.followup.send(embed=discord.Embed(
        title="🔇 Usuário Silenciado",
        description=f"**Usuário:** {usuario.mention}\n**Duração:** {duracao}\n**Motivo:** {motivo}",
        color=discord.Color.yellow()
    ), ephemeral=True)

    await send_mod_log(interaction.guild, "🔇 Mute",
                       f"**Usuário:** {usuario}\n**Duração:** {duracao}\n**Motivo:** {motivo}\n**Moderador:** {interaction.user.mention}",
                       discord.Color.yellow())


@bot.tree.command(name="unmute", description="[MOD] Remover silêncio de um usuário")
@app_commands.describe(usuario="Usuário a desmutar", motivo="Motivo")
@app_commands.default_permissions(moderate_members=True)
async def unmute(interaction: discord.Interaction, usuario: discord.Member, motivo: str = "Sem motivo"):
    await interaction.response.defer(ephemeral=True)

    await usuario.timeout(None, reason=f"{motivo} | Por: {interaction.user}")
    db.add_infraction(interaction.guild.id, usuario.id, interaction.user.id, "unmute", reason=motivo, active=0)

    await interaction.followup.send(embed=discord.Embed(
        title="🔊 Silêncio Removido",
        description=f"**Usuário:** {usuario.mention}\n**Por:** {interaction.user.mention}",
        color=discord.Color.green()
    ), ephemeral=True)

    await send_mod_log(interaction.guild, "🔊 Unmute",
                       f"**Usuário:** {usuario}\n**Motivo:** {motivo}\n**Moderador:** {interaction.user.mention}",
                       discord.Color.green())


@bot.tree.command(name="warn", description="[MOD] Advertir um usuário")
@app_commands.describe(usuario="Usuário a advertir", motivo="Motivo da advertência")
@app_commands.default_permissions(moderate_members=True)
async def warn(interaction: discord.Interaction, usuario: discord.Member, motivo: str):
    await interaction.response.defer(ephemeral=True)

    db.add_infraction(interaction.guild.id, usuario.id, interaction.user.id, "warn", reason=motivo)

    try:
        await usuario.send(embed=discord.Embed(
            title="⚠️ Você recebeu uma advertência",
            description=f"**Servidor:** {interaction.guild.name}\n**Motivo:** {motivo}",
            color=discord.Color.yellow()
        ))
    except Exception:
        pass

    await interaction.followup.send(embed=discord.Embed(
        title="⚠️ Advertência Aplicada",
        description=f"**Usuário:** {usuario.mention}\n**Motivo:** {motivo}\n**Por:** {interaction.user.mention}",
        color=discord.Color.yellow()
    ), ephemeral=True)

    await send_mod_log(interaction.guild, "⚠️ Warn",
                       f"**Usuário:** {usuario}\n**Motivo:** {motivo}\n**Moderador:** {interaction.user.mention}",
                       discord.Color.yellow())


@bot.tree.command(name="infractions", description="[MOD] Ver histórico de infrações de um usuário")
@app_commands.describe(usuario="Usuário a consultar")
@app_commands.default_permissions(moderate_members=True)
async def infractions(interaction: discord.Interaction, usuario: discord.Member):
    await interaction.response.defer(ephemeral=True)

    records = db.get_user_infractions(interaction.guild.id, usuario.id)

    if not records:
        await interaction.followup.send(f"✅ {usuario.mention} não possui infrações.", ephemeral=True)
        return

    embed = discord.Embed(
        title=f"📋 Infrações de {usuario.display_name}",
        color=discord.Color.orange(),
        timestamp=datetime.now(timezone.utc)
    )

    for r in records[:10]:
        moderator = interaction.guild.get_member(r["moderator_id"]) if r["moderator_id"] else None
        mod_name = moderator.display_name if moderator else f"ID: {r['moderator_id']}"
        embed.add_field(
            name=f"#{r['id']} — {r['action'].upper()} — {r['created_at']}",
            value=f"**Motivo:** {r['reason'] or 'Não informado'}\n**Moderador:** {mod_name}",
            inline=False
        )

    embed.set_footer(text=f"Total: {len(records)} infração(ões) | Mostrando até 10")

    await interaction.followup.send(embed=embed, ephemeral=True)


@bot.tree.command(name="clear", description="[MOD] Deletar mensagens do canal")
@app_commands.describe(quantidade="Número de mensagens a deletar (1-100)")
@app_commands.default_permissions(manage_messages=True)
async def clear(interaction: discord.Interaction, quantidade: int):
    await interaction.response.defer(ephemeral=True)

    if quantidade < 1 or quantidade > 100:
        await interaction.followup.send("❌ Quantidade deve ser entre 1 e 100.", ephemeral=True)
        return

    deleted = await interaction.channel.purge(limit=quantidade)
    await interaction.followup.send(f"✅ {len(deleted)} mensagem(ns) deletada(s).", ephemeral=True)

    await send_mod_log(interaction.guild, "🗑️ Clear",
                       f"**Canal:** {interaction.channel.mention}\n**Qtd:** {len(deleted)}\n**Por:** {interaction.user.mention}",
                       discord.Color.greyple())


@bot.tree.command(name="slowmode", description="[MOD] Configurar modo lento no canal")
@app_commands.describe(segundos="Intervalo em segundos (0 para desativar)")
@app_commands.default_permissions(manage_channels=True)
async def slowmode(interaction: discord.Interaction, segundos: int):
    await interaction.response.defer(ephemeral=True)

    if segundos < 0 or segundos > 21600:
        await interaction.followup.send("❌ Valor inválido (0 a 21600 segundos).", ephemeral=True)
        return

    await interaction.channel.edit(slowmode_delay=segundos)

    msg = f"✅ Modo lento desativado." if segundos == 0 else f"✅ Modo lento definido para `{segundos}s`."
    await interaction.followup.send(msg, ephemeral=True)


@bot.tree.command(name="lock", description="[MOD] Bloquear o canal para membros")
@app_commands.default_permissions(manage_channels=True)
async def lock(interaction: discord.Interaction):
    await interaction.response.defer(ephemeral=True)
    await interaction.channel.set_permissions(interaction.guild.default_role, send_messages=False)
    await interaction.channel.send(embed=discord.Embed(
        description="🔒 Canal bloqueado pela moderação.",
        color=discord.Color.red()
    ))
    await interaction.followup.send("✅ Canal bloqueado.", ephemeral=True)


@bot.tree.command(name="unlock", description="[MOD] Desbloquear o canal para membros")
@app_commands.default_permissions(manage_channels=True)
async def unlock(interaction: discord.Interaction):
    await interaction.response.defer(ephemeral=True)
    await interaction.channel.set_permissions(interaction.guild.default_role, send_messages=True)
    await interaction.channel.send(embed=discord.Embed(
        description="🔓 Canal desbloqueado pela moderação.",
        color=discord.Color.green()
    ))
    await interaction.followup.send("✅ Canal desbloqueado.", ephemeral=True)


@bot.tree.command(name="userinfo", description="Exibe informações de um usuário")
@app_commands.describe(usuario="Usuário a consultar")
async def userinfo(interaction: discord.Interaction, usuario: Optional[discord.Member] = None):
    await interaction.response.defer()
    member = usuario or interaction.user

    if not isinstance(member, discord.Member):
        await interaction.followup.send("❌ Membro não encontrado.", ephemeral=True)
        return

    infractions_count = len(db.get_user_infractions(interaction.guild.id, member.id))

    embed = discord.Embed(
        title=f"👤 {member.display_name}",
        color=member.color if member.color.value else discord.Color.blurple(),
        timestamp=datetime.now(timezone.utc)
    )

    embed.set_thumbnail(url=member.display_avatar.url)
    embed.add_field(name="Nome", value=str(member), inline=True)
    embed.add_field(name="ID", value=f"`{member.id}`", inline=True)
    embed.add_field(name="Bot", value="Sim" if member.bot else "Não", inline=True)
    embed.add_field(name="Entrou no servidor", value=f"<t:{int(member.joined_at.timestamp())}:R>" if member.joined_at else "Desconhecido", inline=True)
    embed.add_field(name="Conta criada", value=f"<t:{int(member.created_at.timestamp())}:R>", inline=True)
    embed.add_field(name="Infrações", value=str(infractions_count), inline=True)

    roles = [r.mention for r in member.roles if r.name != "@everyone"]
    if roles:
        embed.add_field(name=f"Cargos ({len(roles)})", value=" ".join(roles[-10:]), inline=False)

    await interaction.followup.send(embed=embed)


# =========================================================
# COMANDOS SLASH — PRISÃO GTA RP
# =========================================================

@bot.tree.command(name="prender", description="[MOD RP] Prender um jogador no GTA RP")
@app_commands.describe(
    usuario="Usuário a prender",
    motivo="Motivo da prisão",
    duracao="Duração (ex: 10m, 1h, 2d) — deixe vazio para indeterminado"
)
@app_commands.default_permissions(moderate_members=True)
async def prender(interaction: discord.Interaction, usuario: discord.Member, motivo: str = "Sem motivo", duracao: Optional[str] = None):
    await interaction.response.defer(ephemeral=True)

    settings = db.get_settings(interaction.guild.id)

    if not is_moderator(interaction.user, settings):
        await interaction.followup.send("❌ Sem permissão.", ephemeral=True)
        return

    prison_role_id = settings["prison_role_id"]
    if not prison_role_id:
        await interaction.followup.send("❌ Cargo de preso não configurado. Use `/config`.", ephemeral=True)
        return

    prison_role = interaction.guild.get_role(prison_role_id)
    if not prison_role:
        await interaction.followup.send("❌ Cargo de preso não encontrado.", ephemeral=True)
        return

    active = db.get_active_prison(interaction.guild.id, usuario.id)
    if active:
        await interaction.followup.send(f"❌ {usuario.mention} já está preso.", ephemeral=True)
        return

    delta = None
    expires_at = None
    duration_seconds = None

    if duracao:
        delta = parse_duration_to_timedelta(duracao)
        if not delta:
            await interaction.followup.send("❌ Formato de duração inválido. Use: `10m`, `1h`, `2d`", ephemeral=True)
            return
        expires_at = dt_to_str(datetime.now(timezone.utc) + delta)
        duration_seconds = int(delta.total_seconds())

    saved_roles = ",".join([str(r.id) for r in usuario.roles if r.name != "@everyone" and not r.managed])

    try:
        roles_to_remove = [r for r in usuario.roles if r.name != "@everyone" and not r.managed and r != prison_role]
        await usuario.remove_roles(*roles_to_remove, reason=f"Preso: {motivo}")
        await usuario.add_roles(prison_role, reason=f"Preso: {motivo}")
    except discord.Forbidden:
        await interaction.followup.send("❌ Sem permissão para modificar os cargos.", ephemeral=True)
        return

    prison_channel_id = settings["prison_channel_id"]
    if prison_channel_id:
        prison_channel = interaction.guild.get_channel(prison_channel_id)
        if isinstance(prison_channel, discord.TextChannel):
            try:
                await prison_channel.set_permissions(usuario, read_messages=True, send_messages=True)
            except Exception:
                pass

    record_id = db.create_prison_record(
        guild_id=interaction.guild.id,
        user_id=usuario.id,
        moderator_id=interaction.user.id,
        reason=motivo,
        duration_seconds=duration_seconds,
        expires_at=expires_at,
        saved_roles=saved_roles
    )

    db.add_infraction(interaction.guild.id, usuario.id, interaction.user.id, "prison",
                      reason=motivo, duration_seconds=duration_seconds, expires_at=expires_at)

    duration_text = duracao if duracao else "Indeterminado"

    embed = discord.Embed(
        title="⛓️ Preso",
        description=(
            f"**Usuário:** {usuario.mention}\n"
            f"**Motivo:** {motivo}\n"
            f"**Duração:** {duration_text}\n"
            f"**Preso por:** {interaction.user.mention}"
        ),
        color=discord.Color.dark_red(),
        timestamp=datetime.now(timezone.utc)
    )

    try:
        await usuario.send(embed=discord.Embed(
            title="⛓️ Você foi preso no GTA RP",
            description=f"**Servidor:** {interaction.guild.name}\n**Motivo:** {motivo}\n**Duração:** {duration_text}",
            color=discord.Color.red()
        ))
    except Exception:
        pass

    if prison_channel_id:
        prison_ch = interaction.guild.get_channel(prison_channel_id)
        if isinstance(prison_ch, discord.TextChannel):
            try:
                await prison_ch.send(embed=embed)
            except Exception:
                pass

    await interaction.followup.send(embed=embed, ephemeral=True)

    await send_mod_log(interaction.guild, "⛓️ Prisão",
                       f"**Usuário:** {usuario}\n**Motivo:** {motivo}\n**Duração:** {duration_text}\n**Moderador:** {interaction.user.mention}",
                       discord.Color.dark_red())

    if delta:
        await asyncio.sleep(delta.total_seconds())
        still_active = db.get_active_prison(interaction.guild.id, usuario.id)
        if still_active and still_active["id"] == record_id:
            await _release_prisoner(interaction.guild, usuario.id, record_id, None, "Pena cumprida automaticamente")


async def _release_prisoner(guild: discord.Guild, user_id: int, record_id: int,
                             released_by: Optional[discord.Member], reason: str):
    record = db.fetchone("SELECT * FROM prison_records WHERE id = ?", (record_id,))
    if not record or not record["active"]:
        return

    settings = db.get_settings(guild.id)
    prison_role_id = settings["prison_role_id"]
    prison_role = guild.get_role(prison_role_id) if prison_role_id else None

    member = guild.get_member(user_id)
    if not member:
        db.release_prison(record_id, released_by.id if released_by else 0)
        return

    try:
        if prison_role and prison_role in member.roles:
            await member.remove_roles(prison_role, reason=reason)

        saved = record["saved_roles"] or ""
        if saved:
            roles_to_restore = []
            for rid in saved.split(","):
                rid = rid.strip()
                if rid:
                    role = guild.get_role(int(rid))
                    if role and role not in member.roles:
                        roles_to_restore.append(role)
            if roles_to_restore:
                await member.add_roles(*roles_to_restore, reason="Saindo da prisão")
    except discord.Forbidden:
        pass

    prison_channel_id = settings["prison_channel_id"]
    if prison_channel_id:
        prison_ch = guild.get_channel(prison_channel_id)
        if isinstance(prison_ch, discord.TextChannel):
            try:
                await prison_ch.set_permissions(member, overwrite=None)
            except Exception:
                pass

    db.release_prison(record_id, released_by.id if released_by else 0)

    try:
        await member.send(embed=discord.Embed(
            title="✅ Você foi solto!",
            description=f"**Servidor:** {guild.name}\n**Motivo:** {reason}",
            color=discord.Color.green()
        ))
    except Exception:
        pass

    await send_mod_log(guild, "✅ Solto",
                       f"**Usuário:** {member}\n**Motivo:** {reason}\n**Por:** {released_by.mention if released_by else 'Sistema'}",
                       discord.Color.green())


@bot.tree.command(name="soltar", description="[MOD RP] Soltar um jogador da prisão")
@app_commands.describe(usuario="Usuário a soltar", motivo="Motivo da soltura")
@app_commands.default_permissions(moderate_members=True)
async def soltar(interaction: discord.Interaction, usuario: discord.Member, motivo: str = "Solto pela moderação"):
    await interaction.response.defer(ephemeral=True)

    settings = db.get_settings(interaction.guild.id)
    if not is_moderator(interaction.user, settings):
        await interaction.followup.send("❌ Sem permissão.", ephemeral=True)
        return

    record = db.get_active_prison(interaction.guild.id, usuario.id)
    if not record:
        await interaction.followup.send(f"❌ {usuario.mention} não está preso.", ephemeral=True)
        return

    await _release_prisoner(interaction.guild, usuario.id, record["id"], interaction.user, motivo)

    await interaction.followup.send(embed=discord.Embed(
        title="✅ Jogador Solto",
        description=f"**Usuário:** {usuario.mention}\n**Motivo:** {motivo}\n**Por:** {interaction.user.mention}",
        color=discord.Color.green()
    ), ephemeral=True)


@bot.tree.command(name="ficha", description="[MOD RP] Ver ficha criminal de um jogador")
@app_commands.describe(usuario="Usuário a consultar")
@app_commands.default_permissions(moderate_members=True)
async def ficha(interaction: discord.Interaction, usuario: discord.Member):
    await interaction.response.defer(ephemeral=True)

    records = db.get_prison_history(interaction.guild.id, usuario.id)
    infractions_list = db.get_user_infractions(interaction.guild.id, usuario.id)

    embed = discord.Embed(
        title=f"📋 Ficha Criminal — {usuario.display_name}",
        color=discord.Color.dark_red(),
        timestamp=datetime.now(timezone.utc)
    )
    embed.set_thumbnail(url=usuario.display_avatar.url)
    embed.add_field(name="Discord", value=str(usuario), inline=True)
    embed.add_field(name="ID", value=f"`{usuario.id}`", inline=True)

    active_prison = db.get_active_prison(interaction.guild.id, usuario.id)
    embed.add_field(name="Status", value="⛓️ Preso" if active_prison else "✅ Livre", inline=True)

    if active_prison:
        mod = interaction.guild.get_member(active_prison["moderator_id"])
        embed.add_field(
            name="Prisão Atual",
            value=f"**Motivo:** {active_prison['reason']}\n**Preso por:** {mod.mention if mod else 'Desconhecido'}\n**Em:** {active_prison['created_at']}",
            inline=False
        )

    total_prisons = len(records)
    total_infractions = len(infractions_list)
    embed.add_field(name="Total de Prisões", value=str(total_prisons), inline=True)
    embed.add_field(name="Total de Infrações", value=str(total_infractions), inline=True)

    for r in records[:5]:
        mod = interaction.guild.get_member(r["moderator_id"])
        status = "⛓️ Ativo" if r["active"] else "✅ Cumprido"
        embed.add_field(
            name=f"Prisão #{r['id']} — {status}",
            value=f"**Motivo:** {r['reason'] or 'N/A'}\n**Em:** {r['created_at']}\n**Preso por:** {mod.mention if mod else 'N/A'}",
            inline=False
        )

    embed.set_footer(text="Mostrando até 5 prisões mais recentes")
    await interaction.followup.send(embed=embed, ephemeral=True)


@bot.tree.command(name="registros-pendentes", description="[MOD] Lista registros GTA RP pendentes")
@app_commands.default_permissions(moderate_members=True)
async def registros_pendentes(interaction: discord.Interaction):
    await interaction.response.defer(ephemeral=True)

    records = db.get_pending_registrations(interaction.guild.id, limit=10)

    if not records:
        await interaction.followup.send("✅ Nenhum registro pendente.", ephemeral=True)
        return

    embed = discord.Embed(
        title="📋 Registros Pendentes",
        description=f"Total de {len(records)} registro(s) pendente(s).",
        color=discord.Color.orange()
    )

    for r in records:
        member = interaction.guild.get_member(r["user_id"])
        name = member.mention if member else f"ID: {r['user_id']}"
        embed.add_field(
            name=f"#{r['id']} — {r['character_name']}",
            value=f"**Discord:** {name}\n**Idade:** {r['character_age']}\n**Enviado:** {r['created_at']}",
            inline=False
        )

    await interaction.followup.send(embed=embed, ephemeral=True)


@bot.tree.command(name="ticket-add", description="[STAFF] Adicionar usuário ao ticket atual")
@app_commands.describe(usuario="Usuário a adicionar")
@app_commands.default_permissions(manage_channels=True)
async def ticket_add(interaction: discord.Interaction, usuario: discord.Member):
    await interaction.response.defer(ephemeral=True)
    ticket = db.get_ticket_by_channel(interaction.channel.id)
    if not ticket:
        await interaction.followup.send("❌ Este canal não é um ticket.", ephemeral=True)
        return

    try:
        await interaction.channel.set_permissions(usuario, read_messages=True, send_messages=True, attach_files=True)
        await interaction.followup.send(f"✅ {usuario.mention} adicionado.", ephemeral=True)
        await interaction.channel.send(f"🔓 {usuario.mention} foi adicionado ao ticket por {interaction.user.mention}.")
    except discord.Forbidden:
        await interaction.followup.send("❌ Sem permissão.", ephemeral=True)


@bot.tree.command(name="ticket-remove", description="[STAFF] Remover usuário do ticket atual")
@app_commands.describe(usuario="Usuário a remover")
@app_commands.default_permissions(manage_channels=True)
async def ticket_remove(interaction: discord.Interaction, usuario: discord.Member):
    await interaction.response.defer(ephemeral=True)
    ticket = db.get_ticket_by_channel(interaction.channel.id)
    if not ticket:
        await interaction.followup.send("❌ Este canal não é um ticket.", ephemeral=True)
        return

    if ticket["creator_id"] == usuario.id:
        await interaction.followup.send("❌ Não é possível remover o criador.", ephemeral=True)
        return

    try:
        await interaction.channel.set_permissions(usuario, overwrite=None)
        await interaction.followup.send(f"✅ {usuario.mention} removido.", ephemeral=True)
        await interaction.channel.send(f"🔒 {usuario.mention} foi removido do ticket por {interaction.user.mention}.")
    except discord.Forbidden:
        await interaction.followup.send("❌ Sem permissão.", ephemeral=True)


@bot.tree.command(name="ticket-fechar", description="[STAFF] Fechar o ticket atual")
@app_commands.describe(motivo="Motivo do fechamento")
@app_commands.default_permissions(manage_channels=True)
async def ticket_fechar(interaction: discord.Interaction, motivo: str = "Finalizado pela equipe"):
    await interaction.response.defer(ephemeral=True)
    ticket = db.get_ticket_by_channel(interaction.channel.id)
    if not ticket:
        await interaction.followup.send("❌ Este canal não é um ticket.", ephemeral=True)
        return

    await interaction.followup.send("🔒 Fechando ticket...", ephemeral=True)
    await close_ticket(interaction.channel, interaction.user, reason=motivo)


@bot.tree.command(name="ticket-info", description="[STAFF] Ver informações do ticket atual")
@app_commands.default_permissions(manage_channels=True)
async def ticket_info(interaction: discord.Interaction):
    await interaction.response.defer(ephemeral=True)
    ticket = db.get_ticket_by_channel(interaction.channel.id)
    if not ticket:
        await interaction.followup.send("❌ Este canal não é um ticket.", ephemeral=True)
        return

    creator = interaction.guild.get_member(ticket["creator_id"])
    claimed = interaction.guild.get_member(ticket["claimed_by_id"]) if ticket["claimed_by_id"] else None

    embed = discord.Embed(
        title=f"🎫 Info do Ticket #{ticket['ticket_number']}",
        color=discord.Color.blurple(),
        timestamp=datetime.now(timezone.utc)
    )
    embed.add_field(name="ID Interno", value=str(ticket["id"]), inline=True)
    embed.add_field(name="Tipo", value=ticket["ticket_type"], inline=True)
    embed.add_field(name="Status", value=ticket["status"], inline=True)
    embed.add_field(name="Criador", value=creator.mention if creator else f"ID: {ticket['creator_id']}", inline=True)
    embed.add_field(name="Atendente", value=claimed.mention if claimed else "Nenhum", inline=True)
    embed.add_field(name="Criado em", value=ticket["created_at"], inline=True)

    await interaction.followup.send(embed=embed, ephemeral=True)


@bot.tree.command(name="ajuda", description="Exibe a lista de comandos disponíveis")
async def ajuda(interaction: discord.Interaction):
    await interaction.response.defer(ephemeral=True)

    embed = discord.Embed(
        title="📖 Comandos do Bot",
        description="Lista completa de comandos disponíveis.",
        color=discord.Color.blurple(),
        timestamp=datetime.now(timezone.utc)
    )

    embed.add_field(
        name="⚙️ Administração",
        value=(
            "`/setup-ticket` — Envia o painel de tickets\n"
            "`/setup-registro` — Envia o painel de registro\n"
            "`/config` — Configura canais e cargos\n"
            "`/config-registro` — Configura o sistema de registro\n"
            "`/config-panel` — Personaliza o painel de tickets\n"
            "`/config-tipo` — Cria/edita tipos de ticket"
        ),
        inline=False
    )

    embed.add_field(
        name="🔨 Moderação",
        value=(
            "`/ban` — Banir usuário\n"
            "`/unban` — Desbanir usuário\n"
            "`/kick` — Expulsar usuário\n"
            "`/mute` — Silenciar (timeout)\n"
            "`/unmute` — Remover silêncio\n"
            "`/warn` — Advertir usuário\n"
            "`/infractions` — Ver infrações\n"
            "`/clear` — Limpar mensagens\n"
            "`/slowmode` — Modo lento\n"
            "`/lock` / `/unlock` — Bloquear/desbloquear canal"
        ),
        inline=False
    )

    embed.add_field(
        name="⛓️ GTA RP — Prisão",
        value=(
            "`/prender` — Prender jogador\n"
            "`/soltar` — Soltar jogador\n"
            "`/ficha` — Ficha criminal do jogador"
        ),
        inline=False
    )

    embed.add_field(
        name="🎫 Tickets",
        value=(
            "`/ticket-add` — Adicionar membro ao ticket\n"
            "`/ticket-remove` — Remover membro do ticket\n"
            "`/ticket-fechar` — Fechar ticket\n"
            "`/ticket-info` — Info do ticket"
        ),
        inline=False
    )

    embed.add_field(
        name="📋 Registro",
        value=(
            "`/registros-pendentes` — Ver registros pendentes"
        ),
        inline=False
    )

    embed.add_field(
        name="👤 Geral",
        value=(
            "`/userinfo` — Info de um usuário\n"
            "`/ajuda` — Esta mensagem"
        ),
        inline=False
    )

    if interaction.guild and interaction.guild.icon:
        embed.set_thumbnail(url=interaction.guild.icon.url)

    embed.set_footer(text="Use os comandos com /")
    await interaction.followup.send(embed=embed, ephemeral=True)


# =========================================================
# INICIALIZAÇÃO
# =========================================================

@bot.event
async def on_ready():
    print(f"Bot online como {bot.user}")
bot.run(TOKEN)