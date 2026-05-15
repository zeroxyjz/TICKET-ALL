import os
import re
import html
import sqlite3
import logging
import asyncio
import unicodedata
from pathlib import Path
from datetime import datetime, timezone, timedelta

import discord
from discord.ext import commands
from discord import app_commands
from dotenv import load_dotenv

# =========================================================
# CONFIG GERAL
# =========================================================

BASE_DIR = Path(__file__).resolve().parent
ENV_PATH = BASE_DIR / ".env"

if ENV_PATH.exists():
    load_dotenv(dotenv_path=ENV_PATH)
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

logger = logging.getLogger("global-sales-bot")
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


def dt_to_str(dt: datetime | None) -> str | None:
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


def extract_user_id(raw: str) -> int | None:
    if not raw:
        return None
    found = re.search(r"(\d{17,22})", raw)
    if found:
        return int(found.group(1))
    return None


def format_template(template: str, member: discord.Member | discord.User, guild: discord.Guild | None = None) -> str:
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


def can_act_on_member(actor: discord.Member, target: discord.Member) -> bool:
    if actor.guild.owner_id == actor.id:
        return True
    return actor.top_role > target.top_role


def bot_can_act_on_member(guild: discord.Guild, target: discord.Member) -> bool:
    me = guild.me or guild.get_member(bot.user.id) if bot.user else None
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

    return False


def is_moderator(member: discord.Member, settings) -> bool:
    if (
        member.guild_permissions.administrator
        or member.guild_permissions.ban_members
        or member.guild_permissions.kick_members
        or member.guild_permissions.moderate_members
        or member.guild_permissions.manage_messages
    ):
        return True

    moderator_role_id = settings["moderator_role_id"]
    if moderator_role_id:
        role = member.guild.get_role(moderator_role_id)
        if role and role in member.roles:
            return True

    return False


async def recent_audit_entry(
    guild: discord.Guild,
    action: discord.AuditLogAction,
    target_id: int,
    seconds: int = 20
):
    try:
        now = discord.utils.utcnow()
        async for entry in guild.audit_logs(limit=10, action=action):
            target = getattr(entry, "target", None)
            if target and getattr(target, "id", None) == target_id:
                created_at = entry.created_at
                if created_at.tzinfo is None:
                    created_at = created_at.replace(tzinfo=timezone.utc)

                if (now - created_at).total_seconds() <= seconds:
                    return entry
    except Exception:
        pass
    return None


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
        cursor = self.conn.cursor()

        cursor.execute("""
        CREATE TABLE IF NOT EXISTS guild_settings (
            guild_id INTEGER PRIMARY KEY,
            panel_channel_id INTEGER,
            ticket_log_channel_id INTEGER,
            mod_log_channel_id INTEGER,
            welcome_channel_id INTEGER,
            goodbye_channel_id INTEGER,
            support_role_id INTEGER,
            moderator_role_id INTEGER,
            ticket_counter INTEGER DEFAULT 0,

            embed_color TEXT DEFAULT '#5865F2',
            embed_title TEXT DEFAULT 'Central de Atendimento',
            embed_description TEXT DEFAULT 'Escolha abaixo o setor que deseja falar.',
            image_url TEXT,
            thumbnail_url TEXT,

            welcome_enabled INTEGER DEFAULT 1,
            goodbye_enabled INTEGER DEFAULT 1,
            welcome_message TEXT DEFAULT 'Seja bem-vindo(a) ao servidor, {member}!',
            goodbye_message TEXT DEFAULT '{member_name} saiu do servidor.',

            compras_category_id INTEGER,
            suporte_category_id INTEGER,
            parcerias_category_id INTEGER,
            doacoes_category_id INTEGER
        )
        """)

        cursor.execute("""
        CREATE TABLE IF NOT EXISTS tickets (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            guild_id INTEGER NOT NULL,
            channel_id INTEGER UNIQUE NOT NULL,
            creator_id INTEGER NOT NULL,
            claimed_by_id INTEGER,
            ticket_type TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT 'open',
            channel_name TEXT,
            internal_notes TEXT DEFAULT '',
            created_at TEXT NOT NULL,
            closed_at TEXT
        )
        """)

        cursor.execute("""
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

        cursor.execute("""
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

        self.conn.commit()

    def _migrate_schema(self):
        self._add_column_if_missing("guild_settings", "panel_channel_id", "INTEGER")
        self._add_column_if_missing("guild_settings", "ticket_log_channel_id", "INTEGER")
        self._add_column_if_missing("guild_settings", "mod_log_channel_id", "INTEGER")
        self._add_column_if_missing("guild_settings", "welcome_channel_id", "INTEGER")
        self._add_column_if_missing("guild_settings", "goodbye_channel_id", "INTEGER")
        self._add_column_if_missing("guild_settings", "support_role_id", "INTEGER")
        self._add_column_if_missing("guild_settings", "moderator_role_id", "INTEGER")
        self._add_column_if_missing("guild_settings", "ticket_counter", "INTEGER DEFAULT 0")
        self._add_column_if_missing("guild_settings", "embed_color", "TEXT DEFAULT '#5865F2'")
        self._add_column_if_missing("guild_settings", "embed_title", "TEXT DEFAULT 'Central de Atendimento'")
        self._add_column_if_missing("guild_settings", "embed_description", "TEXT DEFAULT 'Escolha abaixo o setor que deseja falar.'")
        self._add_column_if_missing("guild_settings", "image_url", "TEXT")
        self._add_column_if_missing("guild_settings", "thumbnail_url", "TEXT")
        self._add_column_if_missing("guild_settings", "welcome_enabled", "INTEGER DEFAULT 1")
        self._add_column_if_missing("guild_settings", "goodbye_enabled", "INTEGER DEFAULT 1")
        self._add_column_if_missing("guild_settings", "welcome_message", "TEXT DEFAULT 'Seja bem-vindo(a) ao servidor, {member}!'")
        self._add_column_if_missing("guild_settings", "goodbye_message", "TEXT DEFAULT '{member_name} saiu do servidor.'")
        self._add_column_if_missing("guild_settings", "compras_category_id", "INTEGER")
        self._add_column_if_missing("guild_settings", "suporte_category_id", "INTEGER")
        self._add_column_if_missing("guild_settings", "parcerias_category_id", "INTEGER")
        self._add_column_if_missing("guild_settings", "doacoes_category_id", "INTEGER")

        self._add_column_if_missing("tickets", "claimed_by_id", "INTEGER")
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

    def get_ticket_by_channel(self, channel_id: int):
        return self.fetchone("SELECT * FROM tickets WHERE channel_id = ?", (channel_id,))

    def get_open_ticket_by_user(self, guild_id: int, user_id: int):
        return self.fetchone("""
            SELECT * FROM tickets
            WHERE guild_id = ? AND creator_id = ? AND status = 'open'
        """, (guild_id, user_id))

    def create_ticket(self, guild_id: int, channel_id: int, creator_id: int, ticket_type: str, channel_name: str):
        cur = self.execute("""
            INSERT INTO tickets (guild_id, channel_id, creator_id, ticket_type, status, channel_name, created_at)
            VALUES (?, ?, ?, ?, 'open', ?, ?)
        """, (guild_id, channel_id, creator_id, ticket_type, channel_name, utc_now()))
        return cur.lastrowid

    def update_ticket(self, channel_id: int, **kwargs):
        if not kwargs:
            return
        fields = ", ".join([f"{k} = ?" for k in kwargs.keys()])
        values = list(kwargs.values()) + [channel_id]
        self.execute(f"UPDATE tickets SET {fields} WHERE channel_id = ?", tuple(values))

    def add_log(
        self,
        guild_id: int,
        ticket_id: int | None,
        channel_id: int | None,
        action: str,
        moderator_id: int | None = None,
        target_id: int | None = None,
        details: str | None = None
    ):
        self.execute("""
            INSERT INTO ticket_logs (guild_id, ticket_id, channel_id, action, moderator_id, target_id, details, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """, (guild_id, ticket_id, channel_id, action, moderator_id, target_id, details, utc_now()))

    def add_infraction(
        self,
        guild_id: int,
        user_id: int,
        moderator_id: int | None,
        action: str,
        reason: str | None = None,
        duration_seconds: int | None = None,
        expires_at: str | None = None,
        active: int = 1
    ):
        self.execute("""
            INSERT INTO infractions (guild_id, user_id, moderator_id, action, reason, duration_seconds, active, created_at, expires_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (
            guild_id, user_id, moderator_id, action, reason, duration_seconds, active, utc_now(), expires_at
        ))

    def get_user_infractions(self, guild_id: int, user_id: int):
        return self.fetchall("""
            SELECT * FROM infractions
            WHERE guild_id = ? AND user_id = ?
            ORDER BY id DESC
        """, (guild_id, user_id))


db = Database(DB_PATH)

# =========================================================
# CONFIG DOS TICKETS
# =========================================================

TICKET_TYPES = {
    "compras": {
        "label": "COMPRAS",
        "emoji": "🛒",
        "prefix": "compras",
        "db_category_field": "compras_category_id"
    },
    "suporte": {
        "label": "SUPORTE",
        "emoji": "🛠️",
        "prefix": "suporte",
        "db_category_field": "suporte_category_id"
    },
    "parcerias": {
        "label": "PARCERIAS",
        "emoji": "🤝",
        "prefix": "parceria",
        "db_category_field": "parcerias_category_id"
    },
    "doacoes": {
        "label": "DOAÇÕES",
        "emoji": "💎",
        "prefix": "doacao",
        "db_category_field": "doacoes_category_id"
    }
}

MOVE_ALIASES = {
    "compras": "compras",
    "compra": "compras",
    "suporte": "suporte",
    "parcerias": "parcerias",
    "parceria": "parcerias",
    "doacoes": "doacoes",
    "doações": "doacoes",
    "doacao": "doacoes",
    "doação": "doacoes"
}

# =========================================================
# EMBEDS
# =========================================================

def build_panel_embed(guild: discord.Guild, settings) -> discord.Embed:
    color = hex_to_color(settings["embed_color"] or "#5865F2")

    embed = discord.Embed(
        title=settings["embed_title"] or "Central de Atendimento",
        description=settings["embed_description"] or (
            "Escolha abaixo o setor ideal para falar com nossa equipe.\n\n"
            "🛒 **Compras** — planos, pagamento e contratação\n"
            "🛠️ **Suporte** — ajuda técnica e atendimento\n"
            "🤝 **Parcerias** — propostas e colaborações\n"
            "💎 **Doações** — apoio e benefícios"
        ),
        color=color
    )

    embed.add_field(
        name="Setores disponíveis",
        value=(
            "🛒 **COMPRAS**\n"
            "🛠️ **SUPORTE**\n"
            "🤝 **PARCERIAS**\n"
            "💎 **DOAÇÕES**"
        ),
        inline=False
    )

    embed.add_field(
        name="Observação",
        value="Cada usuário pode manter **1 ticket aberto por vez** neste servidor.",
        inline=False
    )

    if settings["thumbnail_url"]:
        embed.set_thumbnail(url=settings["thumbnail_url"])
    elif guild.icon:
        embed.set_thumbnail(url=guild.icon.url)

    if settings["image_url"]:
        embed.set_image(url=settings["image_url"])
    elif guild.banner:
        embed.set_image(url=guild.banner.url)

    embed.set_footer(text=f"{guild.name} • Atendimento")
    return embed


def build_ticket_embed(
    guild: discord.Guild,
    member: discord.Member,
    settings,
    ticket_type: str,
    ticket_number: int,
    claimed_by: discord.Member | None = None
) -> discord.Embed:
    data = TICKET_TYPES[ticket_type]
    color = hex_to_color(settings["embed_color"] or "#5865F2")

    descriptions = {
        "compras": "Olá! Seja bem-vindo ao setor de **Compras**. Envie sua dúvida sobre planos, valores, formas de pagamento ou contratação.",
        "suporte": "Olá! Seja bem-vindo ao setor de **Suporte**. Explique seu problema com o máximo de detalhes possível para acelerar o atendimento.",
        "parcerias": "Olá! Seja bem-vindo ao setor de **Parcerias**. Envie sua proposta, mídia kit ou ideia de colaboração.",
        "doacoes": "Olá! Seja bem-vindo ao setor de **Doações**. Envie sua dúvida, intenção de apoio ou solicitação relacionada a benefícios."
    }

    embed = discord.Embed(
        title=f"{data['emoji']} Ticket #{ticket_number} • {data['label']}",
        description=f"{member.mention}\n\n{descriptions.get(ticket_type, 'Descreva sua solicitação abaixo.')}",
        color=color
    )

    embed.add_field(name="Usuário", value=f"{member.mention}\n`{member.id}`", inline=True)
    embed.add_field(name="Setor", value=data["label"], inline=True)
    embed.add_field(name="Status", value="Aberto", inline=True)

    embed.add_field(
        name="Ações disponíveis",
        value=(
            "• Desejo sair ou cancelar esse ticket\n"
            "• Como libero minha DM?\n"
            "• Chamar membro\n"
            "• Adicionar membro\n"
            "• Remover membro\n"
            "• Mover ticket\n"
            "• Trocar nome do canal\n"
            "• Adicionar observação interna\n"
            "• Assumir atendimento\n"
            "• Saudar atendimento\n"
            "• Finalizar ticket"
        ),
        inline=False
    )

    if claimed_by:
        embed.add_field(name="Atendido por", value=claimed_by.mention, inline=False)
    else:
        embed.add_field(name="Atendimento", value="Aguardando um membro da equipe.", inline=False)

    if settings["thumbnail_url"]:
        embed.set_thumbnail(url=settings["thumbnail_url"])
    elif guild.icon:
        embed.set_thumbnail(url=guild.icon.url)

    if settings["image_url"]:
        embed.set_image(url=settings["image_url"])
    elif guild.banner:
        embed.set_image(url=guild.banner.url)
    elif guild.icon:
        embed.set_image(url=guild.icon.url)

    embed.set_footer(text=f"{guild.name} • Sistema Comercial")
    return embed


# =========================================================
# FUNÇÕES DE LOG E TICKETS
# =========================================================

async def send_ticket_log(guild: discord.Guild, title: str, description: str, color: discord.Color):
    settings = db.get_settings(guild.id)
    channel_id = settings["ticket_log_channel_id"]
    if not channel_id:
        return

    channel = guild.get_channel(channel_id)
    if not isinstance(channel, discord.TextChannel):
        return

    embed = discord.Embed(
        title=title,
        description=description,
        color=color,
        timestamp=datetime.now(timezone.utc)
    )
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

    embed = discord.Embed(
        title=title,
        description=description,
        color=color,
        timestamp=datetime.now(timezone.utc)
    )

    try:
        await channel.send(embed=embed)
    except Exception as e:
        logger.warning(f"Falha ao enviar mod log no servidor {guild.id}: {e}")


async def ensure_ticket_categories(guild: discord.Guild):
    settings = db.get_settings(guild.id)
    updates = {}

    for _, data in TICKET_TYPES.items():
        field = data["db_category_field"]
        category_id = settings[field]
        category = guild.get_channel(category_id) if category_id else None

        if not isinstance(category, discord.CategoryChannel):
            category_name = f"📩 {data['label']}"
            category = discord.utils.get(guild.categories, name=category_name)
            if category is None:
                try:
                    category = await guild.create_category(
                        category_name,
                        reason="Configuração automática do sistema de tickets"
                    )
                except discord.Forbidden:
                    category = None
            if category:
                updates[field] = category.id

    if updates:
        db.update_settings(guild.id, **updates)

    return db.get_settings(guild.id)


def get_ticket_category_from_settings(guild: discord.Guild, settings, ticket_type: str):
    field = TICKET_TYPES[ticket_type]["db_category_field"]
    category_id = settings[field]
    if not category_id:
        return None
    return guild.get_channel(category_id)


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

    html_content = f"""
    <!DOCTYPE html>
    <html lang="pt-br">
    <head>
      <meta charset="UTF-8">
      <title>Transcript - {channel.name}</title>
    </head>
    <body style="background:#111214; color:#f2f3f5; font-family:Arial, sans-serif; padding:20px;">
      <h1>Transcript do Ticket</h1>
      <p><strong>Canal:</strong> #{html.escape(channel.name)}</p>
      <p><strong>Servidor:</strong> {html.escape(channel.guild.name)}</p>
      <hr style="border:1px solid #2b2d31;">
      {''.join(messages) if messages else '<p>Nenhuma mensagem registrada.</p>'}
    </body>
    </html>
    """

    path.write_text(html_content, encoding="utf-8")
    return path


async def close_ticket(channel: discord.TextChannel, closer: discord.Member, reason: str = "Finalizado"):
    ticket = db.get_ticket_by_channel(channel.id)
    if not ticket or ticket["status"] != "open":
        return False, "Este ticket já está fechado ou não existe no banco."

    guild = channel.guild
    guild_logger = get_guild_logger(guild.id)

    transcript_path = await generate_transcript(channel, guild.id)

    db.update_ticket(
        channel.id,
        status="closed",
        closed_at=utc_now()
    )
    db.add_log(
        guild.id,
        ticket["id"],
        channel.id,
        "ticket_closed",
        moderator_id=closer.id,
        details=reason
    )

    guild_logger.info(f"Ticket fechado | canal={channel.id} | por={closer.id} | motivo={reason}")

    embed = discord.Embed(
        title="🔒 Ticket Finalizado",
        description=(
            f"**Canal:** {channel.mention}\n"
            f"**ID do ticket:** `{ticket['id']}`\n"
            f"**Fechado por:** {closer.mention}\n"
            f"**Motivo:** {reason}"
        ),
        color=discord.Color.red(),
        timestamp=datetime.now(timezone.utc)
    )

    try:
        file = discord.File(transcript_path, filename=transcript_path.name)
        settings = db.get_settings(guild.id)
        log_channel = guild.get_channel(settings["ticket_log_channel_id"]) if settings["ticket_log_channel_id"] else None

        if isinstance(log_channel, discord.TextChannel):
            await log_channel.send(embed=embed, file=file)
    except Exception as e:
        guild_logger.warning(f"Falha ao enviar transcript do ticket: {e}")

    try:
        await channel.send(
            embed=discord.Embed(
                title="🔒 Ticket encerrado",
                description=f"Este ticket será apagado em **5 segundos**.\n**Motivo:** {reason}",
                color=discord.Color.red()
            )
        )
    except Exception:
        pass

    await asyncio.sleep(5)

    try:
        await channel.delete(reason=f"Ticket finalizado por {closer} | {reason}")
    except Exception as e:
        guild_logger.warning(f"Erro ao deletar canal do ticket {channel.id}: {e}")

    return True, "Ticket encerrado com sucesso."


# =========================================================
# BOT
# =========================================================

intents = discord.Intents.default()
intents.guilds = True
# IMPORTANT: intents.members / message_content são privilegiadas.
# Se você não habilitou no Developer Portal, o bot vai falhar.
intents.members = False
intents.messages = True
intents.message_content = False

bot = commands.Bot(command_prefix="!", intents=intents)


# =========================================================
# MODAIS
# =========================================================

class MemberActionModal(discord.ui.Modal):
    def __init__(self, action_type: str):
        self.action_type = action_type

        title_map = {
            "call": "Chamar membro",
            "add": "Adicionar membro",
            "remove": "Remover membro"
        }

        super().__init__(title=title_map.get(action_type, "Gerenciar membro"))

        self.member_input = discord.ui.TextInput(
            label="ID ou menção do membro",
            placeholder="Ex.: 123456789012345678 ou @usuario",
            required=True,
            max_length=64
        )
        self.reason_input = discord.ui.TextInput(
            label="Motivo / Observação",
            placeholder="Opcional, mas recomendado",
            required=False,
            style=discord.TextStyle.paragraph,
            max_length=500
        )

        self.add_item(self.member_input)
        self.add_item(self.reason_input)

    async def on_submit(self, interaction: discord.Interaction):
        if not interaction.guild or not isinstance(interaction.channel, discord.TextChannel):
            return await interaction.response.send_message("Esse modal só pode ser usado em um ticket.", ephemeral=True)

        settings = db.get_settings(interaction.guild.id)
        ticket = db.get_ticket_by_channel(interaction.channel.id)

        if not ticket:
            return await interaction.response.send_message("Este canal não é um ticket válido.", ephemeral=True)

        if not is_staff(interaction.user, settings):
            return await interaction.response.send_message("Você não tem permissão para fazer isso.", ephemeral=True)

        user_id = extract_user_id(str(self.member_input.value))
        if not user_id:
            return await interaction.response.send_message("Não consegui identificar o membro.", ephemeral=True)

        member = interaction.guild.get_member(user_id)
        if not member:
            return await interaction.response.send_message("Esse membro não está no servidor.", ephemeral=True)

        reason = self.reason_input.value or "Sem observação."

        if self.action_type == "add":
            await interaction.channel.set_permissions(member, view_channel=True, send_messages=True, read_message_history=True, attach_files=True, embed_links=True)
            db.add_log(interaction.guild.id, ticket["id"], interaction.channel.id, "member_added", moderator_id=interaction.user.id, target_id=member.id, details=reason)

            await send_ticket_log(
                interaction.guild,
                "➕ Membro adicionado ao ticket",
                f"**Ticket:** {interaction.channel.mention}\n**Membro:** {member.mention}\n**Por:** {interaction.user.mention}\n**Motivo:** {reason}",
                discord.Color.green()
            )
            await interaction.response.send_message(f"✅ {member.mention} foi adicionado ao ticket.", ephemeral=False)

        elif self.action_type == "remove":
            if member.id == ticket["creator_id"]:
                return await interaction.response.send_message("Você não pode remover o criador do ticket por aqui.", ephemeral=True)

            await interaction.channel.set_permissions(member, overwrite=None)
            db.add_log(interaction.guild.id, ticket["id"], interaction.channel.id, "member_removed", moderator_id=interaction.user.id, target_id=member.id, details=reason)

            await send_ticket_log(
                interaction.guild,
                "➖ Membro removido do ticket",
                f"**Ticket:** {interaction.channel.mention}\n**Membro:** {member.mention}\n**Por:** {interaction.user.mention}\n**Motivo:** {reason}",
                discord.Color.orange()
            )
            await interaction.response.send_message(f"✅ {member.mention} foi removido do ticket.", ephemeral=False)

        elif self.action_type == "call":
            await interaction.channel.set_permissions(member, view_channel=True, send_messages=True, read_message_history=True, attach_files=True, embed_links=True)
            db.add_log(interaction.guild.id, ticket["id"], interaction.channel.id, "member_called", moderator_id=interaction.user.id, target_id=member.id, details=reason)

            await send_ticket_log(
                interaction.guild,
                "📣 Membro chamado ao ticket",
                f"**Ticket:** {interaction.channel.mention}\n**Membro:** {member.mention}\n**Por:** {interaction.user.mention}\n**Motivo:** {reason}",
                discord.Color.blurple()
            )
            await interaction.response.send_message(
                f"📣 {member.mention}, você foi chamado para este ticket.\n**Motivo:** {reason}",
                allowed_mentions=discord.AllowedMentions(users=True)
            )


class MoveTicketModal(discord.ui.Modal, title="Mover ticket"):
    destination = discord.ui.TextInput(
        label="Novo setor",
        placeholder="compras / suporte / parcerias / doacoes",
        required=True,
        max_length=50
    )

    async def on_submit(self, interaction: discord.Interaction):
        if not interaction.guild or not isinstance(interaction.channel, discord.TextChannel):
            return await interaction.response.send_message("Canal inválido.", ephemeral=True)

        settings = db.get_settings(interaction.guild.id)
        if not is_staff(interaction.user, settings):
            return await interaction.response.send_message("Você não tem permissão.", ephemeral=True)

        ticket = db.get_ticket_by_channel(interaction.channel.id)
        if not ticket:
            return await interaction.response.send_message("Esse canal não é um ticket.", ephemeral=True)

        raw = self.destination.value.strip().lower()
        if raw not in MOVE_ALIASES:
            return await interaction.response.send_message("Setor inválido.", ephemeral=True)

        new_type = MOVE_ALIASES[raw]
        settings = await ensure_ticket_categories(interaction.guild)
        new_category = get_ticket_category_from_settings(interaction.guild, settings, new_type)

        if not new_category:
            return await interaction.response.send_message("Não encontrei a categoria de destino.", ephemeral=True)

        old_type = ticket["ticket_type"]
        creator = interaction.guild.get_member(ticket["creator_id"])
        display_name = creator.display_name if creator else "usuario"
        prefix = TICKET_TYPES[new_type]["prefix"]

        new_name = sanitize_channel_name(f"{prefix}-{ticket['id']}-{display_name}")

        await interaction.channel.edit(
            category=new_category,
            name=new_name,
            reason=f"Ticket movido por {interaction.user}"
        )

        db.update_ticket(interaction.channel.id, ticket_type=new_type, channel_name=new_name)
        db.add_log(interaction.guild.id, ticket["id"], interaction.channel.id, "ticket_moved", moderator_id=interaction.user.id, details=f"{old_type} -> {new_type}")

        await send_ticket_log(
            interaction.guild,
            "🔀 Ticket movido",
            f"**Ticket:** {interaction.channel.mention}\n**De:** {TICKET_TYPES[old_type]['label']}\n**Para:** {TICKET_TYPES[new_type]['label']}\n**Por:** {interaction.user.mention}",
            discord.Color.orange()
        )

        await interaction.response.send_message(
            f"✅ Ticket movido para **{TICKET_TYPES[new_type]['label']}** com sucesso.",
            ephemeral=False
        )


class RenameTicketModal(discord.ui.Modal, title="Trocar nome do canal"):
    new_name = discord.ui.TextInput(
        label="Novo nome do canal",
        placeholder="Ex.: compras-plano-vip",
        required=True,
        max_length=90
    )

    async def on_submit(self, interaction: discord.Interaction):
        if not interaction.guild or not isinstance(interaction.channel, discord.TextChannel):
            return await interaction.response.send_message("Canal inválido.", ephemeral=True)

        settings = db.get_settings(interaction.guild.id)
        if not is_staff(interaction.user, settings):
            return await interaction.response.send_message("Você não tem permissão.", ephemeral=True)

        ticket = db.get_ticket_by_channel(interaction.channel.id)
        if not ticket:
            return await interaction.response.send_message("Esse canal não é um ticket.", ephemeral=True)

        new_name = sanitize_channel_name(self.new_name.value)
        old_name = interaction.channel.name

        await interaction.channel.edit(name=new_name, reason=f"Nome alterado por {interaction.user}")
        db.update_ticket(interaction.channel.id, channel_name=new_name)
        db.add_log(interaction.guild.id, ticket["id"], interaction.channel.id, "channel_renamed", moderator_id=interaction.user.id, details=f"{old_name} -> {new_name}")

        await send_ticket_log(
            interaction.guild,
            "✏️ Nome do ticket alterado",
            f"**Canal antigo:** `{old_name}`\n**Canal novo:** `{new_name}`\n**Por:** {interaction.user.mention}",
            discord.Color.blurple()
        )

        await interaction.response.send_message(f"✅ Nome do canal alterado para `{new_name}`.", ephemeral=False)


class InternalNoteModal(discord.ui.Modal, title="Adicionar observação interna"):
    note = discord.ui.TextInput(
        label="Observação interna",
        placeholder="Digite aqui a observação interna do ticket...",
        required=True,
        style=discord.TextStyle.paragraph,
        max_length=1500
    )

    async def on_submit(self, interaction: discord.Interaction):
        if not interaction.guild or not isinstance(interaction.channel, discord.TextChannel):
            return await interaction.response.send_message("Canal inválido.", ephemeral=True)

        settings = db.get_settings(interaction.guild.id)
        if not is_staff(interaction.user, settings):
            return await interaction.response.send_message("Você não tem permissão.", ephemeral=True)

        ticket = db.get_ticket_by_channel(interaction.channel.id)
        if not ticket:
            return await interaction.response.send_message("Esse canal não é um ticket.", ephemeral=True)

        old_notes = ticket["internal_notes"] or ""
        separator = "\n\n" if old_notes else ""
        new_notes = f"{old_notes}{separator}[{utc_now()}] {interaction.user}:\n{self.note.value}"
        db.update_ticket(interaction.channel.id, internal_notes=new_notes)
        db.add_log(interaction.guild.id, ticket["id"], interaction.channel.id, "internal_note_added", moderator_id=interaction.user.id, details=self.note.value)

        await send_ticket_log(
            interaction.guild,
            "📝 Observação interna adicionada",
            f"**Ticket:** {interaction.channel.mention}\n**Por:** {interaction.user.mention}\n\n**Observação:**\n{self.note.value}",
            discord.Color.orange()
        )

        await interaction.response.send_message("✅ Observação interna salva e enviada para os logs.", ephemeral=True)


# =========================================================
# VIEWS
# =========================================================

class TicketPanelView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)

    async def create_ticket_flow(self, interaction: discord.Interaction, ticket_type: str):
        if not interaction.guild or not isinstance(interaction.user, discord.Member):
            return await interaction.response.send_message("Esse painel só funciona em servidor.", ephemeral=True)

        await interaction.response.defer(ephemeral=True, thinking=True)

        settings = await ensure_ticket_categories(interaction.guild)
        existing = db.get_open_ticket_by_user(interaction.guild.id, interaction.user.id)

        if existing:
            channel = interaction.guild.get_channel(existing["channel_id"])
            if channel:
                return await interaction.followup.send(
                    f"Você já possui um ticket aberto: {channel.mention}",
                    ephemeral=True
                )

        category = get_ticket_category_from_settings(interaction.guild, settings, ticket_type)
        if not category:
            return await interaction.followup.send("A categoria desse ticket não está configurada.", ephemeral=True)

        support_role = interaction.guild.get_role(settings["support_role_id"]) if settings["support_role_id"] else None
        me = interaction.guild.me or interaction.guild.get_member(bot.user.id)

        counter = db.next_ticket_counter(interaction.guild.id)
        channel_name = sanitize_channel_name(
            f"{TICKET_TYPES[ticket_type]['prefix']}-{counter}-{interaction.user.display_name}"
        )

        overwrites = {
            interaction.guild.default_role: discord.PermissionOverwrite(view_channel=False),
            interaction.user: discord.PermissionOverwrite(
                view_channel=True,
                send_messages=True,
                read_message_history=True,
                attach_files=True,
                embed_links=True
            )
        }

        if me:
            overwrites[me] = discord.PermissionOverwrite(
                view_channel=True,
                send_messages=True,
                read_message_history=True,
                manage_channels=True,
                manage_messages=True,
                attach_files=True,
                embed_links=True
            )

        if support_role:
            overwrites[support_role] = discord.PermissionOverwrite(
                view_channel=True,
                send_messages=True,
                read_message_history=True,
                attach_files=True,
                embed_links=True,
                manage_messages=True
            )

        try:
            channel = await interaction.guild.create_text_channel(
                name=channel_name,
                category=category,
                overwrites=overwrites,
                topic=f"ticket_owner={interaction.user.id} | type={ticket_type} | number={counter}",
                reason=f"Novo ticket aberto por {interaction.user}"
            )
        except discord.Forbidden:
            return await interaction.followup.send("Não tenho permissão para criar o ticket.", ephemeral=True)
        except discord.HTTPException as e:
            return await interaction.followup.send(f"Erro ao criar ticket: {e}", ephemeral=True)

        ticket_db_id = db.create_ticket(
            interaction.guild.id,
            channel.id,
            interaction.user.id,
            ticket_type,
            channel.name
        )
        db.add_log(interaction.guild.id, ticket_db_id, channel.id, "ticket_created", moderator_id=interaction.user.id, details=ticket_type)

        guild_logger = get_guild_logger(interaction.guild.id)
        guild_logger.info(f"Ticket criado | canal={channel.id} | user={interaction.user.id} | tipo={ticket_type}")

        embed = build_ticket_embed(interaction.guild, interaction.user, settings, ticket_type, ticket_db_id)

        mention_text = f"{interaction.user.mention}"
        if support_role:
            mention_text += f" {support_role.mention}"

        await channel.send(
            content=mention_text,
            embed=embed,
            view=TicketControlView(),
            allowed_mentions=discord.AllowedMentions(users=True, roles=True)
        )

        await send_ticket_log(
            interaction.guild,
            "📩 Novo ticket aberto",
            f"**Canal:** {channel.mention}\n**Usuário:** {interaction.user.mention}\n**Setor:** {TICKET_TYPES[ticket_type]['label']}\n**Ticket ID:** `{ticket_db_id}`",
            discord.Color.green()
        )

        await interaction.followup.send(f"✅ Seu ticket foi criado com sucesso: {channel.mention}", ephemeral=True)

    @discord.ui.button(label="Compras", emoji="🛒", style=discord.ButtonStyle.success, custom_id="ticket_button_compras", row=0)
    async def compras_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self.create_ticket_flow(interaction, "compras")

    @discord.ui.button(label="Suporte", emoji="🛠️", style=discord.ButtonStyle.primary, custom_id="ticket_button_suporte", row=0)
    async def suporte_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self.create_ticket_flow(interaction, "suporte")

    @discord.ui.button(label="Parcerias", emoji="🤝", style=discord.ButtonStyle.secondary, custom_id="ticket_button_parcerias", row=1)
    async def parcerias_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self.create_ticket_flow(interaction, "parcerias")

    @discord.ui.button(label="Doações", emoji="💎", style=discord.ButtonStyle.secondary, custom_id="ticket_button_doacoes", row=1)
    async def doacoes_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self.create_ticket_flow(interaction, "doacoes")


class TicketControlView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)

    async def _get_context(self, interaction: discord.Interaction):
        if not interaction.guild or not isinstance(interaction.channel, discord.TextChannel) or not isinstance(interaction.user, discord.Member):
            return None, None, None
        ticket = db.get_ticket_by_channel(interaction.channel.id)
        settings = db.get_settings(interaction.guild.id)
        return ticket, settings, interaction.channel

    async def _refresh_main_ticket_message(self, channel: discord.TextChannel, ticket, settings, claimed_by: discord.Member | None = None):
        creator = channel.guild.get_member(ticket["creator_id"])
        if not creator:
            return

        embed = build_ticket_embed(channel.guild, creator, settings, ticket["ticket_type"], ticket["id"], claimed_by=claimed_by)

        try:
            async for msg in channel.history(limit=15, oldest_first=True):
                if msg.author.id == bot.user.id and msg.components:
                    await msg.edit(embed=embed, view=TicketControlView())
                    break
        except Exception:
            pass

    @discord.ui.button(label="Desejo sair ou cancelar esse ticket", style=discord.ButtonStyle.secondary, custom_id="ticket_cancel", row=0)
    async def cancel_ticket(self, interaction: discord.Interaction, button: discord.ui.Button):
        ticket, settings, channel = await self._get_context(interaction)
        if not ticket:
            return await interaction.response.send_message("Este canal não é um ticket.", ephemeral=True)

        if interaction.user.id != ticket["creator_id"] and not is_staff(interaction.user, settings):
            return await interaction.response.send_message("Somente o criador do ticket ou a equipe pode usar isso.", ephemeral=True)

        await interaction.response.defer(ephemeral=True, thinking=True)
        ok, msg = await close_ticket(channel, interaction.user, reason="Cancelado pelo usuário/equipe")
        await interaction.followup.send(msg, ephemeral=True)

    @discord.ui.button(label="Como libero minha DM?", style=discord.ButtonStyle.primary, custom_id="ticket_dm_help", row=0)
    async def dm_help(self, interaction: discord.Interaction, button: discord.ui.Button):
        embed = discord.Embed(
            title="📩 Como liberar sua DM",
            description=(
                "Se a equipe precisar falar com você no privado, faça isso:\n\n"
                "**1.** Abra as configurações do Discord\n"
                "**2.** Vá no servidor\n"
                "**3.** Ative a opção para receber mensagens diretas dos membros do servidor\n"
                "**4.** Confira também se você não bloqueou a equipe\n\n"
                "Se preferir, continue o atendimento diretamente por este ticket."
            ),
            color=discord.Color.blurple()
        )
        await interaction.response.send_message(embed=embed, ephemeral=True)

    @discord.ui.button(label="Chamar membro", style=discord.ButtonStyle.secondary, custom_id="ticket_call_member", row=1)
    async def call_member(self, interaction: discord.Interaction, button: discord.ui.Button):
        ticket, settings, _ = await self._get_context(interaction)
        if not ticket:
            return await interaction.response.send_message("Este canal não é um ticket.", ephemeral=True)
        if not is_staff(interaction.user, settings):
            return await interaction.response.send_message("Você não tem permissão.", ephemeral=True)
        await interaction.response.send_modal(MemberActionModal("call"))

    @discord.ui.button(label="Adicionar membro", style=discord.ButtonStyle.success, custom_id="ticket_add_member", row=1)
    async def add_member(self, interaction: discord.Interaction, button: discord.ui.Button):
        ticket, settings, _ = await self._get_context(interaction)
        if not ticket:
            return await interaction.response.send_message("Este canal não é um ticket.", ephemeral=True)
        if not is_staff(interaction.user, settings):
            return await interaction.response.send_message("Você não tem permissão.", ephemeral=True)
        await interaction.response.send_modal(MemberActionModal("add"))

    @discord.ui.button(label="Remover membro", style=discord.ButtonStyle.danger, custom_id="ticket_remove_member", row=2)
    async def remove_member(self, interaction: discord.Interaction, button: discord.ui.Button):
        ticket, settings, _ = await self._get_context(interaction)
        if not ticket:
            return await interaction.response.send_message("Este canal não é um ticket.", ephemeral=True)
        if not is_staff(interaction.user, settings):
            return await interaction.response.send_message("Você não tem permissão.", ephemeral=True)
        await interaction.response.send_modal(MemberActionModal("remove"))

    @discord.ui.button(label="Mover ticket", style=discord.ButtonStyle.secondary, custom_id="ticket_move", row=2)
    async def move_ticket(self, interaction: discord.Interaction, button: discord.ui.Button):
        ticket, settings, _ = await self._get_context(interaction)
        if not ticket:
            return await interaction.response.send_message("Este canal não é um ticket.", ephemeral=True)
        if not is_staff(interaction.user, settings):
            return await interaction.response.send_message("Você não tem permissão.", ephemeral=True)
        await interaction.response.send_modal(MoveTicketModal())

    @discord.ui.button(label="Trocar nome do canal", style=discord.ButtonStyle.secondary, custom_id="ticket_rename", row=3)
    async def rename_channel(self, interaction: discord.Interaction, button: discord.ui.Button):
        ticket, settings, _ = await self._get_context(interaction)
        if not ticket:
            return await interaction.response.send_message("Este canal não é um ticket.", ephemeral=True)
        if not is_staff(interaction.user, settings):
            return await interaction.response.send_message("Você não tem permissão.", ephemeral=True)
        await interaction.response.send_modal(RenameTicketModal())

    @discord.ui.button(label="Adicionar observação interna", style=discord.ButtonStyle.secondary, custom_id="ticket_note", row=3)
    async def internal_note(self, interaction: discord.Interaction, button: discord.ui.Button):
        ticket, settings, _ = await self._get_context(interaction)
        if not ticket:
            return await interaction.response.send_message("Este canal não é um ticket.", ephemeral=True)
        if not is_staff(interaction.user, settings):
            return await interaction.response.send_message("Você não tem permissão.", ephemeral=True)
        await interaction.response.send_modal(InternalNoteModal())

    @discord.ui.button(label="Assumir atendimento", style=discord.ButtonStyle.success, custom_id="ticket_claim", row=4)
    async def claim_ticket(self, interaction: discord.Interaction, button: discord.ui.Button):
        ticket, settings, channel = await self._get_context(interaction)
        if not ticket:
            return await interaction.response.send_message("Este canal não é um ticket.", ephemeral=True)
        if not is_staff(interaction.user, settings):
            return await interaction.response.send_message("Você não tem permissão.", ephemeral=True)

        db.update_ticket(channel.id, claimed_by_id=interaction.user.id)
        db.add_log(interaction.guild.id, ticket["id"], channel.id, "ticket_claimed", moderator_id=interaction.user.id)

        await send_ticket_log(
            interaction.guild,
            "✅ Ticket assumido",
            f"**Ticket:** {channel.mention}\n**Atendente:** {interaction.user.mention}",
            discord.Color.green()
        )

        await self._refresh_main_ticket_message(channel, ticket, settings, claimed_by=interaction.user)
        await interaction.response.send_message(f"✅ {interaction.user.mention} assumiu este atendimento.", ephemeral=False)

    @discord.ui.button(label="Saudar atendimento", style=discord.ButtonStyle.primary, custom_id="ticket_greet", row=4)
    async def greet_ticket(self, interaction: discord.Interaction, button: discord.ui.Button):
        ticket, settings, channel = await self._get_context(interaction)
        if not ticket:
            return await interaction.response.send_message("Este canal não é um ticket.", ephemeral=True)
        if not is_staff(interaction.user, settings):
                        return await interaction.response.send_message("Você não tem permissão.", ephemeral=True)

        creator = interaction.guild.get_member(ticket["creator_id"])

        embed = discord.Embed(
            title="👋 Atendimento iniciado",
            description=(
                f"Olá {creator.mention if creator else 'usuário'}!\n\n"
                f"Meu nome é {interaction.user.mention} e irei atender você neste ticket.\n"
                f"Por favor, envie todas as informações necessárias para agilizar o atendimento."
            ),
            color=discord.Color.blurple()
        )

        db.add_log(
            interaction.guild.id,
            ticket["id"],
            channel.id,
            "ticket_greeted",
            moderator_id=interaction.user.id
        )

        await interaction.response.send_message(embed=embed)

    @discord.ui.button(label="Finalizar ticket", style=discord.ButtonStyle.danger, custom_id="ticket_close", row=4)
    async def close_ticket_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        ticket, settings, channel = await self._get_context(interaction)

        if not ticket:
            return await interaction.response.send_message(
                "Este canal não é um ticket.",
                ephemeral=True
            )

        if not is_staff(interaction.user, settings):
            return await interaction.response.send_message(
                "Você não tem permissão.",
                ephemeral=True
            )

        await interaction.response.defer(ephemeral=True, thinking=True)

        ok, msg = await close_ticket(
            channel,
            interaction.user,
            reason="Finalizado pela equipe"
        )

        await interaction.followup.send(msg, ephemeral=True)


# =========================================================
# EVENTOS
# =========================================================

@bot.event
async def on_ready():
    logger.info(f"Bot conectado como {bot.user}")

    bot.add_view(TicketPanelView())
    bot.add_view(TicketControlView())

    try:
        synced = await bot.tree.sync()
        logger.info(f"Slash commands sincronizados: {len(synced)}")
    except Exception as e:
        logger.error(f"Erro ao sincronizar comandos: {e}")

    activity = discord.Activity(
        type=discord.ActivityType.watching,
        name="Sistema Global de Tickets"
    )

    await bot.change_presence(activity=activity)


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

    message = format_template(
        settings["welcome_message"],
        member,
        member.guild
    )

    embed = discord.Embed(
        title="🎉 Novo membro",
        description=message,
        color=discord.Color.green(),
        timestamp=datetime.now(timezone.utc)
    )

    if member.display_avatar:
        embed.set_thumbnail(url=member.display_avatar.url)

    try:
        await channel.send(embed=embed)
    except Exception as e:
        logger.warning(f"Erro ao enviar mensagem de boas-vindas: {e}")


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

    message = format_template(
        settings["goodbye_message"],
        member,
        member.guild
    )

    embed = discord.Embed(
        title="👋 Membro saiu",
        description=message,
        color=discord.Color.red(),
        timestamp=datetime.now(timezone.utc)
    )

    if member.display_avatar:
        embed.set_thumbnail(url=member.display_avatar.url)

    try:
        await channel.send(embed=embed)
    except Exception as e:
        logger.warning(f"Erro ao enviar mensagem de saída: {e}")


# =========================================================
# SLASH COMMANDS
# =========================================================

@bot.tree.command(name="setup", description="Configura o sistema principal do bot")
@app_commands.checks.has_permissions(administrator=True)
async def setup(interaction: discord.Interaction):
    if not interaction.guild:
        return await interaction.response.send_message(
            "Esse comando só funciona em servidores.",
            ephemeral=True
        )

    await interaction.response.defer(ephemeral=True)

    settings = await ensure_ticket_categories(interaction.guild)

    panel_channel = None

    if settings["panel_channel_id"]:
        panel_channel = interaction.guild.get_channel(settings["panel_channel_id"])

    if not isinstance(panel_channel, discord.TextChannel):
        panel_channel = discord.utils.get(
            interaction.guild.text_channels,
            name="tickets"
        )

        if panel_channel is None:
            try:
                panel_channel = await interaction.guild.create_text_channel(
                    "tickets",
                    reason="Canal automático do sistema de tickets"
                )
            except discord.Forbidden:
                return await interaction.followup.send(
                    "Não tenho permissão para criar canais.",
                    ephemeral=True
                )

        db.update_settings(
            interaction.guild.id,
            panel_channel_id=panel_channel.id
        )

    embed = build_panel_embed(interaction.guild, settings)

    await panel_channel.send(
        embed=embed,
        view=TicketPanelView()
    )

    await interaction.followup.send(
        f"✅ Sistema configurado com sucesso em {panel_channel.mention}",
        ephemeral=True
    )


@bot.tree.command(name="setlogs", description="Define canais de logs")
@app_commands.describe(
    ticket_logs="Canal para logs de tickets",
    mod_logs="Canal para logs de moderação"
)
@app_commands.checks.has_permissions(administrator=True)
async def setlogs(
    interaction: discord.Interaction,
    ticket_logs: discord.TextChannel,
    mod_logs: discord.TextChannel
):
    db.update_settings(
        interaction.guild.id,
        ticket_log_channel_id=ticket_logs.id,
        mod_log_channel_id=mod_logs.id
    )

    await interaction.response.send_message(
        "✅ Canais de logs configurados.",
        ephemeral=True
    )


@bot.tree.command(name="setwelcome", description="Configura o canal de boas-vindas")
@app_commands.checks.has_permissions(administrator=True)
async def setwelcome(
    interaction: discord.Interaction,
    channel: discord.TextChannel
):
    db.update_settings(
        interaction.guild.id,
        welcome_channel_id=channel.id
    )

    await interaction.response.send_message(
        f"✅ Canal de boas-vindas definido para {channel.mention}",
        ephemeral=True
    )


@bot.tree.command(name="setgoodbye", description="Configura o canal de saída")
@app_commands.checks.has_permissions(administrator=True)
async def setgoodbye(
    interaction: discord.Interaction,
    channel: discord.TextChannel
):
    db.update_settings(
        interaction.guild.id,
        goodbye_channel_id=channel.id
    )

    await interaction.response.send_message(
        f"✅ Canal de saída definido para {channel.mention}",
        ephemeral=True
    )


@bot.tree.command(name="setsupportrole", description="Define o cargo da equipe")
@app_commands.checks.has_permissions(administrator=True)
async def setsupportrole(
    interaction: discord.Interaction,
    role: discord.Role
):
    db.update_settings(
        interaction.guild.id,
        support_role_id=role.id
    )

    await interaction.response.send_message(
        f"✅ Cargo da equipe definido para {role.mention}",
        ephemeral=True
    )


@bot.tree.command(name="setmoderatorrole", description="Define o cargo de moderador")
@app_commands.checks.has_permissions(administrator=True)
async def setmoderatorrole(
    interaction: discord.Interaction,
    role: discord.Role
):
    db.update_settings(
        interaction.guild.id,
        moderator_role_id=role.id
    )

    await interaction.response.send_message(
        f"✅ Cargo moderador definido para {role.mention}",
        ephemeral=True
    )


@bot.tree.command(name="painel", description="Envia novamente o painel de tickets")
@app_commands.checks.has_permissions(administrator=True)
async def painel(interaction: discord.Interaction):
    settings = db.get_settings(interaction.guild.id)

    embed = build_panel_embed(interaction.guild, settings)

    await interaction.channel.send(
        embed=embed,
        view=TicketPanelView()
    )

    await interaction.response.send_message(
        "✅ Painel enviado.",
        ephemeral=True
    )


@bot.tree.command(name="userinfo", description="Mostra informações de um usuário")
@app_commands.checks.has_permissions(manage_messages=True)
async def userinfo(
    interaction: discord.Interaction,
    member: discord.Member
):
    infractions = db.get_user_infractions(
        interaction.guild.id,
        member.id
    )

    embed = discord.Embed(
        title=f"👤 Informações de {member}",
        color=discord.Color.blurple(),
        timestamp=datetime.now(timezone.utc)
    )

    embed.set_thumbnail(url=member.display_avatar.url)

    embed.add_field(
        name="ID",
        value=f"`{member.id}`",
        inline=True
    )

    embed.add_field(
        name="Conta criada",
        value=discord.utils.format_dt(member.created_at, style="R"),
        inline=True
    )

    embed.add_field(
        name="Entrou no servidor",
        value=discord.utils.format_dt(member.joined_at, style="R") if member.joined_at else "Desconhecido",
        inline=True
    )

    embed.add_field(
        name="Infrações",
        value=str(len(infractions)),
        inline=False
    )

    if infractions:
        text = []

        for inf in infractions[:10]:
            text.append(
                f"• `{inf['action']}` — {inf['reason'] or 'Sem motivo'}"
            )

        embed.add_field(
            name="Histórico",
            value="\n".join(text),
            inline=False
        )

    await interaction.response.send_message(embed=embed)


@bot.tree.error
async def on_app_command_error(interaction: discord.Interaction, error):
    if isinstance(error, app_commands.MissingPermissions):
        return await interaction.response.send_message(
            "❌ Você não possui permissão para usar esse comando.",
            ephemeral=True
        )

    logger.error(f"Erro em slash command: {error}")

    try:
        await interaction.response.send_message(
            "❌ Ocorreu um erro ao executar este comando.",
            ephemeral=True
        )
    except Exception:
        pass


# =========================================================
# INICIAR BOT
# =========================================================

if __name__ == "__main__":
    try:
        logger.info("Iniciando bot...")
        bot.run(TOKEN, log_handler=None)
    except discord.LoginFailure:
        logger.error("Token inválido.")
    except Exception as e:
        logger.exception(f"Erro fatal ao iniciar bot: {e}")

class NoteTicketModal(discord.ui.Modal, title="Adicionar Observação Interna"):
    note = discord.ui.TextInput(
        label="Observação", 
        style=discord.TextStyle.paragraph, 
        required=True, 
        max_length=500
    )

    async def on_submit(self, interaction: discord.Interaction):
        # O método precisa de "def" após o "async"
        ticket = db.get_ticket_by_channel(interaction.channel.id)
        
        if not ticket:
            return await interaction.response.send_message("❌ Erro: Este canal não consta como um ticket ativo no banco de dados.", ephemeral=True)

        existing_notes = ticket["internal_notes"] if ticket["internal_notes"] else ""
        new_notes = existing_notes + f"\n[{utc_now()}] {interaction.user}: {self.note.value}"
        
        db.update_ticket(interaction.channel.id, internal_notes=new_notes)
        await interaction.response.send_message("📝 Observação salva no banco de dados.", ephemeral=True)
# =========================================================
# VIEWS (BOTÕES)
# =========================================================

class TicketControlView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)

    @discord.ui.button(label="Assumir", style=discord.ButtonStyle.green, custom_id="ticket_claim")
    async def claim(self, interaction: discord.Interaction, button: discord.ui.Button):
        settings = db.get_settings(interaction.guild.id)
        if not is_staff(interaction.user, settings):
            return await interaction.response.send_message("Apenas a equipe pode assumir tickets.", ephemeral=True)
        
        db.update_ticket(interaction.channel.id, claimed_by_id=interaction.user.id)
        await interaction.response.send_message(f"🙋‍♂️ {interaction.user.mention} assumiu este atendimento!")
        
    @discord.ui.button(label="Ações", style=discord.ButtonStyle.secondary, custom_id="ticket_actions")
    async def actions(self, interaction: discord.Interaction, button: discord.ui.Button):
        settings = db.get_settings(interaction.guild.id)
        if not is_staff(interaction.user, settings):
            return await interaction.response.send_message("Sem permissão.", ephemeral=True)

        select = discord.ui.Select(
            placeholder="Escolha uma ação...",
            options=[
                discord.SelectOption(label="Adicionar Membro", value="add", emoji="➕"),
                discord.SelectOption(label="Remover Membro", value="remove", emoji="➖"),
                discord.SelectOption(label="Renomear Canal", value="rename", emoji="📝"),
                discord.SelectOption(label="Nota Interna", value="note", emoji="📌"),
                discord.SelectOption(label="Mover Setor", value="move", emoji="📂")
            ]
        )

        async def select_callback(it: discord.Interaction):
            val = select.values[0]
            if val in ["add", "remove"]:
                await it.response.send_modal(MemberActionModal(val))
            elif val == "rename":
                await it.response.send_modal(RenameTicketModal())
            elif val == "note":
                await it.response.send_modal(NoteTicketModal())

        select.callback = select_callback
        view = discord.ui.View()
        view.add_item(select)
        await interaction.response.send_message("Gerenciamento de Ticket:", view=view, ephemeral=True)

    @discord.ui.button(label="Fechar", style=discord.ButtonStyle.danger, custom_id="ticket_close")
    async def close(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.defer()
        await close_ticket(interaction.channel, interaction.user)

class PersistentTicketView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)

    @discord.ui.select(
        placeholder="Selecione o setor para abrir um ticket",
        custom_id="ticket_select",
        options=[
            discord.SelectOption(label=v["label"], emoji=v["emoji"], value=k) 
            for k, v in TICKET_TYPES.items()
        ]
    )
    async def select_callback(self, interaction: discord.Interaction, select: discord.ui.Select):
        guild = interaction.guild
        user = interaction.user
        ticket_type = select.values[0]
        
        existing = db.get_open_ticket_by_user(guild.id, user.id)
        if existing:
            return await interaction.response.send_message(f"❌ Você já possui um ticket aberto: <#{existing['channel_id']}>", ephemeral=True)

        await interaction.response.defer(ephemeral=True)
        
        settings = await ensure_ticket_categories(guild)
        category = get_ticket_category_from_settings(guild, settings, ticket_type)
        
        num = db.next_ticket_counter(guild.id)
        overwrites = {
            guild.default_role: discord.PermissionOverwrite(read_messages=False),
            user: discord.PermissionOverwrite(read_messages=True, send_messages=True, attach_files=True, embed_links=True),
            guild.me: discord.PermissionOverwrite(read_messages=True, send_messages=True, manage_channels=True)
        }
        
        # Adiciona cargo de suporte se existir
        if settings["support_role_id"]:
            role = guild.get_role(settings["support_role_id"])
            if role: overwrites[role] = discord.PermissionOverwrite(read_messages=True, send_messages=True)

        channel_name = f"{TICKET_TYPES[ticket_type]['prefix']}-{num:04d}"
        channel = await guild.create_text_channel(name=channel_name, category=category, overwrites=overwrites)
        
        db.create_ticket(guild.id, channel.id, user.id, ticket_type, channel_name)
        
        embed = build_ticket_embed(guild, user, settings, ticket_type, num)
        await channel.send(embed=embed, view=TicketControlView())
        
        await interaction.followup.send(f"✅ Ticket criado com sucesso: {channel.mention}", ephemeral=True)

# =========================================================
# COMANDOS E EVENTOS
# =========================================================

@bot.event
async def on_ready():
    logger.info(f"Bot logado como {bot.user}")
    bot.add_view(PersistentTicketView())
    bot.add_view(TicketControlView())
    try:
        synced = await bot.tree.sync()
        logger.info(f"Synced {len(synced)} comandos.")
    except Exception as e:
        logger.error(f"Erro ao sincronizar: {e}")

@bot.tree.command(name="painel_setup", description="Envia o painel de tickets no canal atual.")
@app_commands.checks.has_permissions(administrator=True)
async def painel_setup(interaction: discord.Interaction):
    settings = db.get_settings(interaction.guild.id)
    embed = build_panel_embed(interaction.guild, settings)
    await interaction.channel.send(embed=embed, view=PersistentTicketView())
    db.update_settings(interaction.guild.id, panel_channel_id=interaction.channel.id)
    await interaction.response.send_message("Painel configurado!", ephemeral=True)

@bot.event
async def on_member_join(member: discord.Member):
    settings = db.get_settings(member.guild.id)
    if settings["welcome_enabled"] and settings["welcome_channel_id"]:
        channel = member.guild.get_channel(settings["welcome_channel_id"])
        if channel:
            msg = format_template(settings["welcome_message"], member, member.guild)
            await channel.send(msg)

@bot.event
async def on_member_remove(member: discord.Member):
    settings = db.get_settings(member.guild.id)
    if settings["goodbye_enabled"] and settings["goodbye_channel_id"]:
        channel = member.guild.get_channel(settings["goodbye_channel_id"])
        if channel:
            msg = format_template(settings["goodbye_message"], member, member.guild)
            await channel.send(msg)


