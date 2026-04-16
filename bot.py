import os
import re
import sys
import json
import gzip
import asyncio
import logging
import hashlib
import datetime
import unicodedata
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Optional, Set, Tuple

import aiomysql
import telethon
from dotenv import load_dotenv
from logging.handlers import RotatingFileHandler
from rapidfuzz import fuzz
from telethon import TelegramClient, Button, events, types
from telethon.errors import (
    AuthKeyDuplicatedError,
    FloodWaitError,
    MessageTooLongError,
    PasswordHashInvalidError,
    PhoneCodeExpiredError,
    PhoneCodeInvalidError,
    SessionPasswordNeededError,
    UserAlreadyParticipantError,
    UserIsBlockedError,
)
from telethon.tl.functions.channels import GetParticipantRequest, JoinChannelRequest
from telethon.tl.types import (
    ChannelParticipant,
    ChannelParticipantAdmin,
    ChannelParticipantCreator,
    ChannelParticipantSelf,
    InputPeerUser,
    User,
)
from urllib.parse import urlparse


# =============== Infra & Config ===============


class AppLogger:
    @staticmethod
    def build(name: str = "telegram-bot") -> logging.Logger:
        logger = logging.getLogger(name)
        logger.setLevel(logging.DEBUG)
        fmt = logging.Formatter("%(asctime)s | %(levelname)-7s | %(message)s")
        fh = RotatingFileHandler("bot.log", maxBytes=10**8, backupCount=3, encoding="utf-8")
        fh.setFormatter(fmt)
        sh = logging.StreamHandler(sys.stdout)
        sh.setFormatter(fmt)
        if not logger.handlers:
            logger.addHandler(fh)
            logger.addHandler(sh)
        return logger


class Config:
    VALID_MODES = {"forward", "reply", "both", "self"}

    def __init__(self) -> None:
        load_dotenv()
        self.logger = AppLogger.build()

        self.DEFAULT_EXCLUDED_GROUPS: Set[int] = {
            -1002272546210, -1002405645012, -1002353780992, -1002311800895
        }
        self.DEFAULT_FALLBACK_GROUP_ID: int = int(os.getenv("FALLBACK_GROUP_ID", "-1002353780992"))
        self.DEFAULT_COMMAND_GROUP_ID: int = int(os.getenv("COMMAND_GROUP_ID", "-1002311800895"))
        self.command_bot_index = int(os.getenv("COMMAND_BOT_INDEX", "2"))

        self.DEFAULT_KEYWORDS = [
            "ابي مساعده", "يسوي", "يحل", "خصوصي", "شاطر", "تحل", "تسوي", "يعرف", "تعرف", "واجب", "بروجكت",
            "فاهم", "سكليف", "بحث", "مشروع", "يساعد", "اسايمنت",
            "ابي مساعده", "ابغى مساعده", "ابغا مساعده", "محتاج مساعده", "حد يساعدني", "احد يساعدني",
            "ابي حد يحضر عني", "ابغا حد يحضر عني", "يحضر عني", "يحظر", "يحضر",
            "عندي اختبار", "احد عنده خصوصي", "احد يعرف مختص",
            "اسايمنت", "بروجكت", "مشروع", "س ك ل ي ف", "case study", "كيس ستدي",
            "بوربوينت", "بووربوينت", "عذر طبي", "اجازة مرضية",
        ]

        self.COMMANDS = {
            "help", "cancel", "unblock",
            "add", "del", "list", "find",
            "blkadd", "blkdel", "blklist", "blkfind",
            "autoadd", "autodel", "autolist", "autofind",
            "groupadd", "groupdel", "groupupdate", "grouplist", "joingroups",
            "stopjoin", "groupcount", "usergroups", "usergroups_notin",
            "dbbackup", "dbrestore",
            "blkuser_add", "blkuser_del", "blkuser_list", "blkuser_find",
            "autoreplies_count", "autoreplies_list", "autoreplies_clear",
            "stats",
            "accadd", "acccode", "accpass", "acclist", "accstart", "accstop",
            "accrestart", "accmode", "acctarget", "accdel", "accsetcmd", "accstatus",
            "kwadd", "kwdel", "kwlist", "kwfind",
            "exgroupadd", "exgroupdel", "exgrouplist", "exgroupfind",
            "fallback", "fallbackset", "cmdgroupset", "configshow",
        }

        self.EXCLUDED_GROUPS: Set[int] = set(self.DEFAULT_EXCLUDED_GROUPS)
        self.FALLBACK_GROUP_ID: int = self.DEFAULT_FALLBACK_GROUP_ID
        self.COMMAND_GROUP_ID: int = self.DEFAULT_COMMAND_GROUP_ID
        self.KEYWORDS: List[str] = list(dict.fromkeys(self.DEFAULT_KEYWORDS))
        self.KW_RE: re.Pattern[str] = re.compile(r"$^")
        self.compile_keywords()

        self.LINK_RE = re.compile(r"(https://t\.me/(?:c/)?(?:\d+|[A-Za-z0-9_]+)/?\d*)(?:\?comment=\d+)?")

    def compile_keywords(self) -> None:
        safe = [k for k in self.KEYWORDS if k]
        self.KW_RE = re.compile("|".join(map(re.escape, safe)), re.IGNORECASE) if safe else re.compile(r"$^")


# =============== Utilities ===============


class TextUtils:
    @staticmethod
    def normalize_text(text: str) -> str:
        s = unicodedata.normalize("NFD", text)
        s = "".join(ch for ch in s if unicodedata.category(ch) != "Mn")
        return re.sub(r"[\s\W_]+", "", s).lower()

    @staticmethod
    def fuzzy_match(a: str, b: str, threshold: int = 80) -> bool:
        return fuzz.ratio(TextUtils.normalize_text(a), TextUtils.normalize_text(b)) >= threshold

    @staticmethod
    def split_long(text: str, chunk: int = 4000) -> List[str]:
        return [text[i:i + chunk] for i in range(0, len(text), chunk)]

    @staticmethod
    def make_dedupe_key(key: Tuple[Any, ...]) -> str:
        raw = "|".join(map(str, key))
        return hashlib.sha1(raw.encode("utf-8")).hexdigest()

    @staticmethod
    def normalize_phone(phone: str) -> str:
        phone = phone.strip()
        if phone.startswith("+"):
            return "+" + re.sub(r"\D+", "", phone[1:])
        return re.sub(r"\D+", "", phone)

    @staticmethod
    def parse_bool_flag(value: str) -> bool:
        return value.strip().lower() in {"1", "true", "yes", "y", "on", "cmd", "command", "نعم", "اي", "صح"}


@dataclass
class PendingAuth:
    phone: str
    api_id: int
    api_hash: str
    target_group_id: int
    mode: str
    is_command_bot: bool
    session_name: str
    phone_code_hash: str
    client: TelegramClient
    created_at: datetime.datetime
    needs_password: bool = False


# =============== DB Layer ===============


class DB:
    def __init__(self, logger: logging.Logger) -> None:
        self.logger = logger
        self.pool: Optional[aiomysql.Pool] = None

    async def init(self) -> None:
        url = urlparse(os.getenv("JAWSDB_URL", ""))
        if not url.hostname:
            self.logger.error("JAWSDB_URL not set")
            sys.exit(1)

        self.pool = await aiomysql.create_pool(
            host=url.hostname,
            port=url.port or 3306,
            user=url.username,
            password=url.password,
            db=url.path.lstrip("/"),
            autocommit=True,
            charset="utf8mb4",
            cursorclass=aiomysql.DictCursor,
            pool_recycle=300,
        )

        async with self.pool.acquire() as conn:
            async with conn.cursor() as cur:
                await cur.execute("SET sql_notes=0")

                await cur.execute("""
                    CREATE TABLE IF NOT EXISTS direct_reply_messages (
                        id INT AUTO_INCREMENT PRIMARY KEY,
                        message_text VARCHAR(255) NOT NULL
                    )
                """)
                await cur.execute("""
                    CREATE TABLE IF NOT EXISTS blocked_reply_messages (
                        id INT AUTO_INCREMENT PRIMARY KEY,
                        message_text VARCHAR(255) NOT NULL
                    )
                """)
                await cur.execute("""
                    CREATE TABLE IF NOT EXISTS auto_reply_responses (
                        id INT AUTO_INCREMENT PRIMARY KEY,
                        message_text VARCHAR(255) NOT NULL
                    )
                """)
                await cur.execute("""
                    CREATE TABLE IF NOT EXISTS join_groups (
                        id INT AUTO_INCREMENT PRIMARY KEY,
                        group_link VARCHAR(255) NOT NULL UNIQUE
                    )
                """)
                await cur.execute("""
                    CREATE TABLE IF NOT EXISTS blocked_users (
                        user_id BIGINT PRIMARY KEY,
                        username VARCHAR(64) NULL,
                        display_name VARCHAR(255) NULL,
                        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                    )
                """)
                await cur.execute("""
                    CREATE TABLE IF NOT EXISTS auto_reply_log (
                        id BIGINT AUTO_INCREMENT PRIMARY KEY,
                        user_id BIGINT NOT NULL,
                        username VARCHAR(64) NULL,
                        display_name VARCHAR(255) NULL,
                        bot_phone VARCHAR(32) NULL,
                        message_id BIGINT NULL,
                        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                        INDEX (user_id),
                        INDEX (created_at)
                    )
                """)
                await cur.execute("""
                    CREATE TABLE IF NOT EXISTS keyword_rules (
                        id INT AUTO_INCREMENT PRIMARY KEY,
                        keyword_text VARCHAR(255) NOT NULL UNIQUE
                    )
                """)
                await cur.execute("""
                    CREATE TABLE IF NOT EXISTS excluded_groups (
                        group_id BIGINT PRIMARY KEY,
                        title VARCHAR(255) NULL,
                        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                    )
                """)
                await cur.execute("""
                    CREATE TABLE IF NOT EXISTS app_settings (
                        setting_key VARCHAR(64) PRIMARY KEY,
                        setting_value TEXT NULL,
                        updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP
                    )
                """)
                await cur.execute("""
                    CREATE TABLE IF NOT EXISTS bot_accounts (
                        id INT AUTO_INCREMENT PRIMARY KEY,
                        api_id INT NOT NULL,
                        api_hash VARCHAR(128) NOT NULL,
                        phone VARCHAR(32) NOT NULL UNIQUE,
                        target_group_id BIGINT NOT NULL,
                        mode VARCHAR(16) NOT NULL DEFAULT 'both',
                        enabled TINYINT(1) NOT NULL DEFAULT 1,
                        is_command_bot TINYINT(1) NOT NULL DEFAULT 0,
                        session_name VARCHAR(128) NOT NULL,
                        last_error TEXT NULL,
                        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                        updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP
                    )
                """)

                try:
                    await cur.execute("ALTER TABLE auto_reply_log ADD COLUMN dedupe_key VARCHAR(64) NOT NULL DEFAULT '' AFTER user_id")
                except Exception:
                    pass
                try:
                    await cur.execute("ALTER TABLE auto_reply_log ADD COLUMN src_chat_id BIGINT NULL AFTER dedupe_key")
                except Exception:
                    pass
                try:
                    await cur.execute("ALTER TABLE auto_reply_log ADD COLUMN src_msg_id BIGINT NULL AFTER src_chat_id")
                except Exception:
                    pass
                try:
                    await cur.execute("CREATE UNIQUE INDEX uq_auto_user_dedupe ON auto_reply_log (user_id, dedupe_key)")
                except Exception:
                    pass

                for table in ("direct_reply_messages", "blocked_reply_messages", "auto_reply_responses"):
                    await self._dedupe_text_table(cur, table)
                for table, index_name in (
                    ("direct_reply_messages", "uq_direct_reply_messages_text"),
                    ("blocked_reply_messages", "uq_blocked_reply_messages_text"),
                    ("auto_reply_responses", "uq_auto_reply_responses_text"),
                ):
                    try:
                        await cur.execute(f"CREATE UNIQUE INDEX {index_name} ON {table} (message_text)")
                    except Exception:
                        pass

                await cur.execute("SET sql_notes=1")

        self.logger.info("Database initialized / upgraded")

    async def _dedupe_text_table(self, cur: aiomysql.DictCursor, table: str) -> None:
        await cur.execute(f"""
            DELETE t1 FROM {table} t1
            INNER JOIN {table} t2
            WHERE t1.id > t2.id AND t1.message_text = t2.message_text
        """)

    async def scalar_table_values(self, table: str, col: str) -> List[str]:
        assert self.pool
        async with self.pool.acquire() as conn:
            await conn.ping(reconnect=True)
            async with conn.cursor() as cur:
                await cur.execute(f"SELECT {col} FROM {table} ORDER BY id ASC")
                rows = await cur.fetchall()
                return [r[col] for r in rows]

    async def insert_scalar_value(self, table: str, col: str, value: str) -> None:
        assert self.pool
        async with self.pool.acquire() as conn:
            await conn.ping(reconnect=True)
            async with conn.cursor() as cur:
                await cur.execute(f"INSERT IGNORE INTO {table}({col}) VALUES(%s)", (value,))

    async def delete_scalar_value(self, table: str, col: str, value: str) -> None:
        assert self.pool
        async with self.pool.acquire() as conn:
            await conn.ping(reconnect=True)
            async with conn.cursor() as cur:
                await cur.execute(f"DELETE FROM {table} WHERE {col}=%s", (value,))

    async def count_rows(self, table: str) -> int:
        assert self.pool
        async with self.pool.acquire() as conn:
            await conn.ping(reconnect=True)
            async with conn.cursor() as cur:
                await cur.execute(f"SELECT COUNT(*) AS c FROM {table}")
                row = await cur.fetchone()
                return int(row["c"] if row else 0)

    async def get_stats(self) -> Dict[str, int]:
        return {
            "direct": await self.count_rows("direct_reply_messages"),
            "blocked_text": await self.count_rows("blocked_reply_messages"),
            "blocked_users": await self.count_rows("blocked_users"),
            "groups": await self.count_rows("join_groups"),
            "keywords": await self.count_rows("keyword_rules"),
            "excluded_groups": await self.count_rows("excluded_groups"),
            "accounts": await self.count_rows("bot_accounts"),
        }

    # ===== Blocked users =====

    async def blocked_users_map(self) -> Dict[int, Tuple[str, str]]:
        assert self.pool
        async with self.pool.acquire() as conn:
            await conn.ping(reconnect=True)
            async with conn.cursor() as cur:
                await cur.execute("""
                    SELECT user_id, COALESCE(username,'') AS username, COALESCE(display_name,'') AS display_name
                    FROM blocked_users
                """)
                rows = await cur.fetchall()
                return {int(r["user_id"]): (r["username"], r["display_name"]) for r in rows}

    async def add_blocked_user(self, user_id: int, username: Optional[str], display_name: Optional[str]) -> None:
        assert self.pool
        async with self.pool.acquire() as conn:
            await conn.ping(reconnect=True)
            async with conn.cursor() as cur:
                await cur.execute("""
                    INSERT INTO blocked_users (user_id, username, display_name, created_at)
                    VALUES (%s, %s, %s, NOW())
                    ON DUPLICATE KEY UPDATE
                        username=VALUES(username),
                        display_name=VALUES(display_name)
                """, (user_id, username or "", display_name or ""))

    async def del_blocked_user(self, user_id: int) -> int:
        assert self.pool
        async with self.pool.acquire() as conn:
            await conn.ping(reconnect=True)
            async with conn.cursor() as cur:
                await cur.execute("DELETE FROM blocked_users WHERE user_id=%s", (user_id,))
                return cur.rowcount

    async def list_blocked_users(self) -> List[Dict[str, Any]]:
        assert self.pool
        async with self.pool.acquire() as conn:
            await conn.ping(reconnect=True)
            async with conn.cursor() as cur:
                await cur.execute("""
                    SELECT user_id, username, display_name, created_at
                    FROM blocked_users ORDER BY created_at DESC
                """)
                return await cur.fetchall()

    async def find_blocked_users(self, pattern: str) -> List[Dict[str, Any]]:
        like = f"%{pattern}%"
        assert self.pool
        async with self.pool.acquire() as conn:
            await conn.ping(reconnect=True)
            async with conn.cursor() as cur:
                await cur.execute("""
                    SELECT user_id, username, display_name, created_at
                    FROM blocked_users
                    WHERE CAST(user_id AS CHAR) LIKE %s OR username LIKE %s OR display_name LIKE %s
                    ORDER BY created_at DESC
                """, (like, like, like))
                return await cur.fetchall()

    # ===== Auto replies log =====

    async def count_auto_replies(self, user_id: int) -> int:
        assert self.pool
        async with self.pool.acquire() as conn:
            await conn.ping(reconnect=True)
            async with conn.cursor() as cur:
                await cur.execute("SELECT COUNT(*) AS c FROM auto_reply_log WHERE user_id=%s", (user_id,))
                row = await cur.fetchone()
                return int(row["c"] if row else 0)

    async def count_auto_replies_distinct(self, user_id: int, hours: int = 24) -> int:
        assert self.pool
        async with self.pool.acquire() as conn:
            await conn.ping(reconnect=True)
            async with conn.cursor() as cur:
                await cur.execute("""
                    SELECT COUNT(DISTINCT dedupe_key) AS c
                    FROM auto_reply_log
                    WHERE user_id=%s AND created_at >= NOW() - INTERVAL %s HOUR
                """, (user_id, hours))
                row = await cur.fetchone()
                return int(row["c"] if row else 0)

    async def log_auto_reply_pending(
        self,
        user_id: int,
        username: Optional[str],
        display_name: Optional[str],
        dedupe_key: str,
        src_chat_id: Optional[int],
        src_msg_id: Optional[int],
    ) -> int:
        assert self.pool
        async with self.pool.acquire() as conn:
            await conn.ping(reconnect=True)
            async with conn.cursor() as cur:
                await cur.execute("""
                    INSERT IGNORE INTO auto_reply_log
                        (user_id, dedupe_key, username, display_name, bot_phone, message_id, src_chat_id, src_msg_id)
                    VALUES (%s, %s, %s, %s, NULL, NULL, %s, %s)
                """, (user_id, dedupe_key, username or "", display_name or "", src_chat_id, src_msg_id))
                return int(cur.lastrowid or 0)

    async def update_auto_reply_log(self, log_id: int, bot_phone: Optional[str] = None, message_id: Optional[int] = None) -> None:
        if not log_id:
            return
        sets: List[str] = []
        vals: List[Any] = []
        if bot_phone is not None:
            sets.append("bot_phone=%s")
            vals.append(bot_phone)
        if message_id is not None:
            sets.append("message_id=%s")
            vals.append(message_id)
        if not sets:
            return
        q = "UPDATE auto_reply_log SET " + ", ".join(sets) + " WHERE id=%s"
        vals.append(log_id)
        assert self.pool
        async with self.pool.acquire() as conn:
            await conn.ping(reconnect=True)
            async with conn.cursor() as cur:
                await cur.execute(q, tuple(vals))

    async def list_auto_replies(self, limit: int = 50) -> List[Dict[str, Any]]:
        limit = max(1, min(limit, 500))
        assert self.pool
        async with self.pool.acquire() as conn:
            await conn.ping(reconnect=True)
            async with conn.cursor() as cur:
                await cur.execute(f"""
                    SELECT id, user_id, username, display_name, bot_phone, message_id, created_at
                    FROM auto_reply_log
                    ORDER BY id DESC
                    LIMIT {limit}
                """)
                return await cur.fetchall()

    async def list_auto_replies_for_user(self, user_id: int, limit: int = 50) -> List[Dict[str, Any]]:
        limit = max(1, min(limit, 500))
        assert self.pool
        async with self.pool.acquire() as conn:
            await conn.ping(reconnect=True)
            async with conn.cursor() as cur:
                await cur.execute(f"""
                    SELECT id, user_id, username, display_name, bot_phone, message_id, created_at
                    FROM auto_reply_log
                    WHERE user_id=%s
                    ORDER BY id DESC
                    LIMIT {limit}
                """, (user_id,))
                return await cur.fetchall()

    async def clear_auto_reply_log(self, user_id: Optional[int] = None) -> int:
        assert self.pool
        async with self.pool.acquire() as conn:
            await conn.ping(reconnect=True)
            async with conn.cursor() as cur:
                if user_id is None:
                    await cur.execute("SELECT COUNT(*) AS c FROM auto_reply_log")
                    c = int((await cur.fetchone() or {}).get("c", 0))
                    await cur.execute("TRUNCATE TABLE auto_reply_log")
                    return c
                await cur.execute("DELETE FROM auto_reply_log WHERE user_id=%s", (user_id,))
                return cur.rowcount

    # ===== Runtime config =====

    async def seed_keyword_if_missing(self, keyword: str) -> None:
        await self.insert_scalar_value("keyword_rules", "keyword_text", keyword)

    async def load_keywords(self) -> List[str]:
        return await self.scalar_table_values("keyword_rules", "keyword_text")

    async def delete_keyword(self, keyword: str) -> None:
        await self.delete_scalar_value("keyword_rules", "keyword_text", keyword)

    async def list_excluded_groups(self) -> List[Dict[str, Any]]:
        assert self.pool
        async with self.pool.acquire() as conn:
            await conn.ping(reconnect=True)
            async with conn.cursor() as cur:
                await cur.execute("SELECT group_id, title, created_at FROM excluded_groups ORDER BY created_at DESC")
                return await cur.fetchall()

    async def add_excluded_group(self, group_id: int, title: str = "") -> None:
        assert self.pool
        async with self.pool.acquire() as conn:
            await conn.ping(reconnect=True)
            async with conn.cursor() as cur:
                await cur.execute("""
                    INSERT INTO excluded_groups (group_id, title)
                    VALUES (%s, %s)
                    ON DUPLICATE KEY UPDATE title=VALUES(title)
                """, (group_id, title))

    async def del_excluded_group(self, group_id: int) -> int:
        assert self.pool
        async with self.pool.acquire() as conn:
            await conn.ping(reconnect=True)
            async with conn.cursor() as cur:
                await cur.execute("DELETE FROM excluded_groups WHERE group_id=%s", (group_id,))
                return cur.rowcount

    async def find_excluded_groups(self, pattern: str) -> List[Dict[str, Any]]:
        like = f"%{pattern}%"
        assert self.pool
        async with self.pool.acquire() as conn:
            await conn.ping(reconnect=True)
            async with conn.cursor() as cur:
                await cur.execute("""
                    SELECT group_id, title, created_at
                    FROM excluded_groups
                    WHERE CAST(group_id AS CHAR) LIKE %s OR title LIKE %s
                    ORDER BY created_at DESC
                """, (like, like))
                return await cur.fetchall()

    async def get_setting(self, key: str) -> Optional[str]:
        assert self.pool
        async with self.pool.acquire() as conn:
            await conn.ping(reconnect=True)
            async with conn.cursor() as cur:
                await cur.execute("SELECT setting_value FROM app_settings WHERE setting_key=%s", (key,))
                row = await cur.fetchone()
                return row["setting_value"] if row else None

    async def set_setting(self, key: str, value: str) -> None:
        assert self.pool
        async with self.pool.acquire() as conn:
            await conn.ping(reconnect=True)
            async with conn.cursor() as cur:
                await cur.execute("""
                    INSERT INTO app_settings (setting_key, setting_value)
                    VALUES (%s, %s)
                    ON DUPLICATE KEY UPDATE setting_value=VALUES(setting_value)
                """, (key, value))

    async def list_settings(self) -> List[Dict[str, Any]]:
        assert self.pool
        async with self.pool.acquire() as conn:
            await conn.ping(reconnect=True)
            async with conn.cursor() as cur:
                await cur.execute("SELECT setting_key, setting_value, updated_at FROM app_settings ORDER BY setting_key ASC")
                return await cur.fetchall()

    # ===== Bot accounts =====

    async def upsert_account(
        self,
        api_id: int,
        api_hash: str,
        phone: str,
        target_group_id: int,
        mode: str,
        enabled: bool,
        is_command_bot: bool,
        session_name: str,
        last_error: Optional[str] = None,
    ) -> None:
        assert self.pool
        async with self.pool.acquire() as conn:
            await conn.ping(reconnect=True)
            async with conn.cursor() as cur:
                await cur.execute("""
                    INSERT INTO bot_accounts
                        (api_id, api_hash, phone, target_group_id, mode, enabled, is_command_bot, session_name, last_error)
                    VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
                    ON DUPLICATE KEY UPDATE
                        api_id=VALUES(api_id),
                        api_hash=VALUES(api_hash),
                        target_group_id=VALUES(target_group_id),
                        mode=VALUES(mode),
                        enabled=VALUES(enabled),
                        is_command_bot=VALUES(is_command_bot),
                        session_name=VALUES(session_name),
                        last_error=VALUES(last_error)
                """, (api_id, api_hash, phone, target_group_id, mode, int(enabled), int(is_command_bot), session_name, last_error))

    async def list_accounts(self) -> List[Dict[str, Any]]:
        assert self.pool
        async with self.pool.acquire() as conn:
            await conn.ping(reconnect=True)
            async with conn.cursor() as cur:
                await cur.execute("""
                    SELECT id, api_id, api_hash, phone, target_group_id, mode, enabled, is_command_bot, session_name,
                           last_error, created_at, updated_at
                    FROM bot_accounts
                    ORDER BY is_command_bot DESC, created_at ASC
                """)
                return await cur.fetchall()

    async def get_account(self, phone: str) -> Optional[Dict[str, Any]]:
        assert self.pool
        async with self.pool.acquire() as conn:
            await conn.ping(reconnect=True)
            async with conn.cursor() as cur:
                await cur.execute("""
                    SELECT id, api_id, api_hash, phone, target_group_id, mode, enabled, is_command_bot, session_name,
                           last_error, created_at, updated_at
                    FROM bot_accounts
                    WHERE phone=%s
                    LIMIT 1
                """, (phone,))
                return await cur.fetchone()

    async def delete_account(self, phone: str) -> int:
        assert self.pool
        async with self.pool.acquire() as conn:
            await conn.ping(reconnect=True)
            async with conn.cursor() as cur:
                await cur.execute("DELETE FROM bot_accounts WHERE phone=%s", (phone,))
                return cur.rowcount

    async def set_account_enabled(self, phone: str, enabled: bool) -> None:
        assert self.pool
        async with self.pool.acquire() as conn:
            await conn.ping(reconnect=True)
            async with conn.cursor() as cur:
                await cur.execute("UPDATE bot_accounts SET enabled=%s WHERE phone=%s", (int(enabled), phone))

    async def set_account_mode(self, phone: str, mode: str) -> int:
        assert self.pool
        async with self.pool.acquire() as conn:
            await conn.ping(reconnect=True)
            async with conn.cursor() as cur:
                await cur.execute("UPDATE bot_accounts SET mode=%s WHERE phone=%s", (mode, phone))
                return cur.rowcount

    async def set_account_target_group(self, phone: str, target_group_id: int) -> int:
        assert self.pool
        async with self.pool.acquire() as conn:
            await conn.ping(reconnect=True)
            async with conn.cursor() as cur:
                await cur.execute("UPDATE bot_accounts SET target_group_id=%s WHERE phone=%s", (target_group_id, phone))
                return cur.rowcount

    async def set_account_error(self, phone: str, error: str) -> None:
        assert self.pool
        async with self.pool.acquire() as conn:
            await conn.ping(reconnect=True)
            async with conn.cursor() as cur:
                await cur.execute("UPDATE bot_accounts SET last_error=%s WHERE phone=%s", (error[:4000], phone))

    async def set_command_bot(self, phone: str) -> None:
        assert self.pool
        async with self.pool.acquire() as conn:
            await conn.ping(reconnect=True)
            async with conn.cursor() as cur:
                await cur.execute("UPDATE bot_accounts SET is_command_bot=0")
                await cur.execute("UPDATE bot_accounts SET is_command_bot=1 WHERE phone=%s", (phone,))

    async def get_command_bot_phone(self) -> Optional[str]:
        assert self.pool
        async with self.pool.acquire() as conn:
            await conn.ping(reconnect=True)
            async with conn.cursor() as cur:
                await cur.execute("SELECT phone FROM bot_accounts WHERE is_command_bot=1 LIMIT 1")
                row = await cur.fetchone()
                return row["phone"] if row else None


# =============== Backup / Restore Manager ===============


class DbBackupManager:
    BACKUP_TABLES = {
        "direct_reply_messages": {"kind": "scalar", "col": "message_text"},
        "blocked_reply_messages": {"kind": "scalar", "col": "message_text"},
        "auto_reply_responses": {"kind": "scalar", "col": "message_text"},
        "join_groups": {"kind": "scalar", "col": "group_link"},
        "keyword_rules": {"kind": "scalar", "col": "keyword_text"},
        "blocked_users": {"kind": "full"},
        "excluded_groups": {"kind": "full"},
        "app_settings": {"kind": "full"},
        "bot_accounts": {"kind": "full"},
    }

    def __init__(self, db: DB, logger: logging.Logger):
        self.db = db
        self.logger = logger

    async def export_backup(self, out_path: str) -> Dict[str, int]:
        assert self.db.pool
        payload: Dict[str, Any] = {
            "meta": {"version": 3, "created_at": datetime.datetime.utcnow().isoformat() + "Z"},
            "tables": {},
        }
        summary: Dict[str, int] = {}
        async with self.db.pool.acquire() as conn:
            async with conn.cursor() as cur:
                for table, spec in self.BACKUP_TABLES.items():
                    if spec["kind"] == "scalar":
                        col = spec["col"]
                        await cur.execute(f"SELECT {col} FROM {table}")
                        rows = await cur.fetchall()
                        payload["tables"][table] = [r[col] for r in rows]
                        summary[table] = len(rows)
                    else:
                        await cur.execute(f"SELECT * FROM {table}")
                        rows = await cur.fetchall()
                        payload["tables"][table] = rows
                        summary[table] = len(rows)
        Path(out_path).parent.mkdir(parents=True, exist_ok=True)
        raw = json.dumps(payload, ensure_ascii=False, indent=2)
        if out_path.endswith(".gz"):
            with gzip.open(out_path, "wt", encoding="utf-8") as f:
                f.write(raw)
        else:
            Path(out_path).write_text(raw, encoding="utf-8")
        self.logger.info(f"Backup exported -> {out_path}")
        return summary

    def _load_payload(self, in_path: str) -> Dict[str, Any]:
        path = Path(in_path)
        first_two = b""
        if path.exists():
            with path.open("rb") as f:
                first_two = f.read(2)
        is_gzip = path.suffix.lower() == ".gz" or first_two == b"\x1f\x8b"
        if is_gzip:
            with gzip.open(in_path, "rt", encoding="utf-8") as f:
                return json.load(f)
        with open(in_path, "r", encoding="utf-8") as f:
            return json.load(f)

    async def import_backup(self, in_path: str) -> Dict[str, Dict[str, int]]:
        assert self.db.pool
        data = self._load_payload(in_path)
        tables = data.get("tables", {})
        summary: Dict[str, Dict[str, int]] = {}

        async with self.db.pool.acquire() as conn:
            await conn.begin()
            try:
                async with conn.cursor() as cur:
                    for table, spec in self.BACKUP_TABLES.items():
                        rows = tables.get(table, [])
                        inserted = 0
                        skipped = 0
                        if not rows:
                            summary[table] = {"inserted": 0, "skipped": 0}
                            continue

                        if spec["kind"] == "scalar":
                            col = spec["col"]
                            await cur.execute(f"SELECT {col} FROM {table}")
                            existing = {r[col] for r in await cur.fetchall()}
                            for row in rows:
                                if row in existing:
                                    skipped += 1
                                    continue
                                await cur.execute(f"INSERT IGNORE INTO {table}({col}) VALUES(%s)", (row,))
                                inserted += 1
                                existing.add(row)
                        elif table == "blocked_users":
                            await cur.execute("SELECT user_id FROM blocked_users")
                            existing = {int(r["user_id"]) for r in await cur.fetchall()}
                            for r in rows:
                                uid = int(r.get("user_id") or 0)
                                if not uid:
                                    skipped += 1
                                    continue
                                if uid in existing:
                                    await cur.execute("""
                                        UPDATE blocked_users
                                        SET username=%s, display_name=%s
                                        WHERE user_id=%s
                                    """, (r.get("username", "") or "", r.get("display_name", "") or "", uid))
                                    skipped += 1
                                else:
                                    await cur.execute("""
                                        INSERT INTO blocked_users (user_id, username, display_name, created_at)
                                        VALUES (%s, %s, %s, %s)
                                    """, (uid, r.get("username", "") or "", r.get("display_name", "") or "", r.get("created_at") or datetime.datetime.utcnow()))
                                    inserted += 1
                                    existing.add(uid)
                        elif table == "excluded_groups":
                            await cur.execute("SELECT group_id FROM excluded_groups")
                            existing = {int(r["group_id"]) for r in await cur.fetchall()}
                            for r in rows:
                                gid = int(r.get("group_id") or 0)
                                if not gid:
                                    skipped += 1
                                    continue
                                if gid in existing:
                                    skipped += 1
                                    continue
                                await cur.execute(
                                    "INSERT IGNORE INTO excluded_groups (group_id, title) VALUES (%s, %s)",
                                    (gid, r.get("title", "") or ""),
                                )
                                inserted += 1
                                existing.add(gid)
                        elif table == "app_settings":
                            await cur.execute("SELECT setting_key FROM app_settings")
                            existing = {r["setting_key"] for r in await cur.fetchall()}
                            for r in rows:
                                key = (r.get("setting_key") or "").strip()
                                if not key:
                                    skipped += 1
                                    continue
                                if key in existing:
                                    skipped += 1
                                    continue
                                await cur.execute(
                                    "INSERT IGNORE INTO app_settings (setting_key, setting_value) VALUES (%s, %s)",
                                    (key, r.get("setting_value", "")),
                                )
                                inserted += 1
                                existing.add(key)
                        elif table == "bot_accounts":
                            await cur.execute("SELECT phone FROM bot_accounts")
                            existing = {r["phone"] for r in await cur.fetchall()}
                            for r in rows:
                                phone = TextUtils.normalize_phone(str(r.get("phone") or ""))
                                if not phone:
                                    skipped += 1
                                    continue
                                if phone in existing:
                                    skipped += 1
                                    continue
                                await cur.execute("""
                                    INSERT IGNORE INTO bot_accounts
                                        (api_id, api_hash, phone, target_group_id, mode, enabled, is_command_bot, session_name, last_error)
                                    VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
                                """, (
                                    int(r.get("api_id") or 0),
                                    str(r.get("api_hash") or ""),
                                    phone,
                                    int(r.get("target_group_id") or 0),
                                    str(r.get("mode") or "both").lower(),
                                    int(r.get("enabled", 1) or 0),
                                    int(r.get("is_command_bot", 0) or 0),
                                    str(r.get("session_name") or f"session_{phone.replace('+', '')}"),
                                    r.get("last_error"),
                                ))
                                inserted += 1
                                existing.add(phone)

                        summary[table] = {"inserted": inserted, "skipped": skipped}
                await conn.commit()
                self.logger.info(f"Restore (merge) finished from {in_path}")
                return summary
            except Exception as exc:
                await conn.rollback()
                self.logger.error(f"Restore failed, rolled back. {exc}", exc_info=True)
                raise


# =============== Messaging / Repo / Formatter ===============


class Messenger:
    def __init__(self, logger: logging.Logger) -> None:
        self.logger = logger

    async def safe_send(
        self,
        client: TelegramClient,
        dst: int | types.User | types.InputPeerUser,
        text: str,
        tag: str = "SEND",
        buttons=None,
    ) -> Optional[types.Message]:
        if len(text) > 4000:
            last = None
            for part in TextUtils.split_long(text, 4000):
                last = await self.safe_send(client, dst, part, tag=tag, buttons=buttons)
                buttons = None
            return last

        delay = 1
        for _ in range(3):
            try:
                return await client.send_message(dst, text, parse_mode="Markdown", buttons=buttons)
            except FloodWaitError as e:
                self.logger.warning(f"{tag} wait {e.seconds}s")
                await asyncio.sleep(delay)
                delay *= 2
            except MessageTooLongError:
                for part in TextUtils.split_long(text, 4000):
                    await client.send_message(dst, part, parse_mode="Markdown")
                return None
            except UserIsBlockedError:
                self.logger.warning(f"{tag} blocked by {dst}")
                return None
            except Exception as ex:
                self.logger.error(f"{tag} error: {ex}", exc_info=True)
                return None
        return None

    async def safe_send_file(
        self,
        client: TelegramClient,
        dst: int | types.User | types.InputPeerUser,
        file_path: str,
        caption: str = "",
        tag: str = "FILE",
    ) -> Optional[types.Message]:
        delay = 1
        for _ in range(3):
            try:
                return await client.send_file(dst, file_path, caption=caption, force_document=True)
            except FloodWaitError as e:
                self.logger.warning(f"{tag} wait {e.seconds}s")
                await asyncio.sleep(delay)
                delay *= 2
            except UserIsBlockedError:
                self.logger.warning(f"{tag} blocked by {dst}")
                return None
            except Exception as ex:
                self.logger.error(f"{tag} error: {ex}", exc_info=True)
                return None
        return None


class GroupRepo:
    def __init__(self, db: DB) -> None:
        self.db = db

    async def add(self, link: str) -> None:
        await self.db.insert_scalar_value("join_groups", "group_link", link)

    async def delete(self, link: str) -> None:
        await self.db.delete_scalar_value("join_groups", "group_link", link)

    async def update(self, old_link: str, new_link: str) -> None:
        assert self.db.pool
        async with self.db.pool.acquire() as conn:
            async with conn.cursor() as cur:
                await cur.execute("UPDATE join_groups SET group_link=%s WHERE group_link=%s", (new_link, old_link))

    async def all(self) -> List[str]:
        return await self.db.scalar_table_values("join_groups", "group_link")


class MessageFormatter:
    def __init__(self, cfg: Config) -> None:
        self.cfg = cfg

    def message_key(self, ev: events.NewMessage.Event) -> Tuple[Any, ...]:
        t = ev.message.message or ""
        m = self.cfg.LINK_RE.search(t)
        return ("link", m.group(1)) if m else ("id", ev.chat_id, ev.message.id)

    async def build_forward_text(self, ev: events.NewMessage.Event) -> str:
        text = ev.message.message or "—"
        sender = await ev.get_sender()
        if sender:
            username = getattr(sender, "username", None)
            fn = getattr(sender, "first_name", "") or ""
            ln = getattr(sender, "last_name", "") or ""
            disp = f"{fn} {ln}".strip() or f"مستخدم (ID: {sender.id})"
            if username:
                sender_line = f"👤 **المرسل:** [@{username}](https://t.me/{username})"
                if disp != f"مستخدم (ID: {sender.id})":
                    sender_line += f" ({disp})"
            else:
                sender_line = f"👤 **المرسل:** {disp}"
            dm_line = f"🔗 **مراسلة مباشرة:** [اضغط هنا للمراسلة](tg://user?id={sender.id})"
        else:
            sender_line = "👤 **المرسل:** غير معروف"
            dm_line = "🔗 **مراسلة مباشرة:** غير متاحة"

        chat = ev.chat or await ev.get_chat()
        if chat:
            chat_username = getattr(chat, "username", None)
            chat_title = getattr(chat, "title", None)
            if chat_username:
                group_line = f"📍 **المجموعة/القناة:** @{chat_username}"
            elif chat_title:
                group_line = f"📍 **المجموعة/القناة:** {chat_title}"
            else:
                group_line = "📍 **المجموعة/القناة:** غير معروفة"
        else:
            group_line = "📍 **المجموعة/القناة:** غير معروفة"

        link_line = "📜 **رابط الرسالة:** غير متاح"
        if ev.chat_id:
            chat_username_for_link = getattr(chat, "username", None)
            if str(ev.chat_id).startswith("-100"):
                channel_id_raw = str(ev.chat_id)[4:]
                link_line = f"📜 **رابط الرسالة:** [اضغط هنا للرسالة](https://t.me/c/{channel_id_raw}/{ev.message.id})"
            elif chat_username_for_link:
                link_line = f"📜 **رابط الرسالة:** [اضغط هنا للرسالة](https://t.me/{chat_username_for_link}/{ev.message.id})"

        return f"`{text}`\n\n{sender_line}\n{dm_line}\n{group_line}\n{link_line}"


# =============== Shared State ===============


class State:
    def __init__(self) -> None:
        self.bots: List["Bot"] = []
        self.FORWARD_DONE: Set[Tuple[Any, ...]] = set()
        self.REPLY_DONE: Set[Tuple[Any, ...]] = set()
        self.PROCESS_LOCK = asyncio.Lock()

        self.direct_triggers: List[str] = []
        self.blocked_phrases: List[str] = []
        self.auto_replies: List[str] = []
        self._auto_index = 0

        self.pending_ops: Dict[Tuple[int, int], Dict[str, Any]] = {}
        self.pending_auth: Dict[str, PendingAuth] = {}
        self.COMMAND_USER_ID: Optional[int] = None

        self.stop_joining_flags: Dict[str, bool] = {}
        self.joining_now: Dict[str, asyncio.Task] = {}

        self._fallback_entity_cache: Dict[int, Any] = {}
        self._fallback_member_cache: Dict[int, bool] = {}
        self.REPLY_LOCKS: Dict[Tuple[Any, ...], asyncio.Lock] = {}

        self.blocked_users: Dict[int, Tuple[str, str]] = {}

    def next_auto_reply(self) -> str:
        if not self.auto_replies:
            return "ارسلت ذي في الجروب 😇\n\nابشر/ي اساعدك"
        msg = self.auto_replies[self._auto_index]
        self._auto_index = (self._auto_index + 1) % len(self.auto_replies)
        return msg

    def get_reply_lock(self, key: Tuple[Any, ...]) -> asyncio.Lock:
        lock = self.REPLY_LOCKS.get(key)
        if lock is None:
            lock = asyncio.Lock()
            self.REPLY_LOCKS[key] = lock
        return lock


# =============== Fallback Router ===============


class FallbackRouter:
    def __init__(self, cfg: Config, state: State, messenger: Messenger, formatter: MessageFormatter, logger: logging.Logger) -> None:
        self.cfg = cfg
        self.state = state
        self.messenger = messenger
        self.formatter = formatter
        self.logger = logger

    def reset_cache(self) -> None:
        self.state._fallback_entity_cache.clear()
        self.state._fallback_member_cache.clear()

    async def _get_fallback_entity(self, client: TelegramClient):
        cid = id(client)
        if cid in self.state._fallback_entity_cache:
            return self.state._fallback_entity_cache[cid]
        try:
            entity = await client.get_entity(self.cfg.FALLBACK_GROUP_ID)
            self.state._fallback_entity_cache[cid] = entity
            return entity
        except Exception:
            return None

    async def _is_member_of_fallback(self, client: TelegramClient) -> bool:
        cid = id(client)
        if cid in self.state._fallback_member_cache:
            return self.state._fallback_member_cache[cid]
        try:
            me = await client.get_me()
            entity = await self._get_fallback_entity(client)
            if not entity:
                self.state._fallback_member_cache[cid] = False
                return False
            res = await client(GetParticipantRequest(entity, me.id))
            is_in = isinstance(
                res.participant,
                (ChannelParticipant, ChannelParticipantSelf, ChannelParticipantAdmin, ChannelParticipantCreator),
            )
            self.state._fallback_member_cache[cid] = is_in
            return is_in
        except Exception:
            self.state._fallback_member_cache[cid] = False
            return False

    async def _try_forward_normal(self, client: TelegramClient, ev: events.NewMessage.Event) -> bool:
        if not await self._is_member_of_fallback(client):
            return False
        try:
            await client.forward_messages(self.cfg.FALLBACK_GROUP_ID, ev.message)
            return True
        except Exception:
            return False

    async def _try_forward_textual(self, client: TelegramClient, ev: events.NewMessage.Event, prefix: str = "") -> bool:
        if not await self._is_member_of_fallback(client):
            return False
        try:
            fwd_txt = await self.formatter.build_forward_text(ev)
            if prefix:
                fwd_txt = f"{prefix}\n\n{fwd_txt}"
            await self.messenger.safe_send(client, self.cfg.FALLBACK_GROUP_ID, fwd_txt, tag="FALLBACK_TXT")
            return True
        except Exception:
            return False

    async def forward_any(self, ev: events.NewMessage.Event, warn_prefix: str = "") -> None:
        src_bot = next((b for b in self.state.bots if b.client is ev.client), None)
        if not src_bot:
            return

        if await self._try_forward_normal(src_bot.client, ev):
            return
        for b in self.state.bots:
            if b.client is src_bot.client:
                continue
            if await self._try_forward_normal(b.client, ev):
                return
        if await self._try_forward_textual(src_bot.client, ev, prefix=warn_prefix):
            return
        for b in self.state.bots:
            if b.client is src_bot.client:
                continue
            if await self._try_forward_textual(b.client, ev, prefix=warn_prefix):
                return


# =============== Bot ===============


class Bot:
    def __init__(
        self,
        cfg: Config,
        db: DB,
        state: State,
        messenger: Messenger,
        group_repo: GroupRepo,
        formatter: MessageFormatter,
        fallback: FallbackRouter,
        client: TelegramClient,
        target_group_id: int,
        phone: str,
        mode: str,
        manager: "BotManager",
        is_command_bot: bool = False,
        logger: Optional[logging.Logger] = None,
        backup: Optional[DbBackupManager] = None,
    ) -> None:
        self.cfg = cfg
        self.db = db
        self.state = state
        self.messenger = messenger
        self.group_repo = group_repo
        self.formatter = formatter
        self.fallback = fallback
        self.client = client
        self.target_group_id = target_group_id
        self.phone = phone
        self.mode = mode.lower()
        self.is_command_bot = is_command_bot
        self.logger = logger or cfg.logger
        self.backup = backup
        self.manager = manager

        self.command_pattern = r"(?i)^/(?:" + "|".join(map(re.escape, sorted(self.cfg.COMMANDS))) + r")\b"
        client.add_event_handler(self._safe_on_message, events.NewMessage(incoming=True))
        client.add_event_handler(self._safe_on_message, events.NewMessage(outgoing=True))
        client.add_event_handler(self._safe_on_command, events.NewMessage(incoming=True, pattern=self.command_pattern))
        client.add_event_handler(self._safe_on_command, events.NewMessage(outgoing=True, pattern=self.command_pattern))

    async def _handle_auth_key_duplicated(self, where: str, exc: Exception) -> None:
        self.logger.error(f"AuthKeyDuplicatedError on {where} for {self.phone}: {exc}", exc_info=True)
        await self.manager.handle_auth_key_duplicated(self.phone, where=where, exc=exc)

    async def _safe_on_message(self, ev: events.NewMessage.Event) -> None:
        try:
            await self.on_message(ev)
        except AuthKeyDuplicatedError as exc:
            await self._handle_auth_key_duplicated("on_message", exc)
        except Exception as exc:
            self.logger.error(f"Unhandled on_message error for {self.phone}: {exc}", exc_info=True)

    async def _safe_on_command(self, ev: events.NewMessage.Event) -> None:
        try:
            await self.on_command(ev)
        except AuthKeyDuplicatedError as exc:
            await self._handle_auth_key_duplicated("on_command", exc)
        except Exception as exc:
            self.logger.error(f"Unhandled on_command error for {self.phone}: {exc}", exc_info=True)

    async def join_sleep(self, phone: str, seconds: int) -> None:
        for _ in range(seconds):
            if self.state.stop_joining_flags.get(phone):
                break
            await asyncio.sleep(1)

    async def join_groups_with_account(self, start_index: int = 0) -> None:
        links = await self.group_repo.all()
        joined = 0
        total = len(links)
        await self.messenger.safe_send(self.client, self.cfg.COMMAND_GROUP_ID, f"🚀 بدء انضمام الحساب {self.phone} من {start_index + 1} إلى {total}...", tag="JOIN_GROUPS")
        try:
            for idx, link in enumerate(links[start_index:], start=start_index):
                if self.state.stop_joining_flags.get(self.phone):
                    await self.messenger.safe_send(self.client, self.cfg.COMMAND_GROUP_ID, f"⏹ إيقاف يدوي عند {idx + 1}/{total}.", tag="JOIN_GROUPS")
                    return
                try:
                    entity = await self.client.get_entity(link)
                    await self.client(JoinChannelRequest(entity))
                    joined += 1
                    await self.messenger.safe_send(self.client, self.cfg.COMMAND_GROUP_ID, f"✅ [{idx + 1}/{total}] انضم: {link}", tag="JOIN_GROUPS")
                    await self.join_sleep(self.phone, 250)
                except AuthKeyDuplicatedError as ex:
                    await self._handle_auth_key_duplicated("join_groups_with_account", ex)
                    return
                except UserAlreadyParticipantError:
                    await self.messenger.safe_send(self.client, self.cfg.COMMAND_GROUP_ID, f"ℹ️ [{idx + 1}/{total}] عضو مسبقاً: {link}", tag="JOIN_GROUPS")
                except FloodWaitError as e:
                    await self.messenger.safe_send(self.client, self.cfg.COMMAND_GROUP_ID, f"⏸ توقف {e.seconds}s عند [{idx + 1}/{total}]", tag="JOIN_GROUPS")
                    await self.join_sleep(self.phone, e.seconds + 2)
                except Exception as ex:
                    await self.messenger.safe_send(self.client, self.cfg.COMMAND_GROUP_ID, f"❌ [{idx + 1}/{total}] فشل: {link}\n{ex}", tag="JOIN_GROUPS")
        finally:
            self.state.joining_now.pop(self.phone, None)
            self.state.stop_joining_flags.pop(self.phone, None)
        await self.messenger.safe_send(self.client, self.cfg.COMMAND_GROUP_ID, f"🏁 {self.phone}: انضم {joined}/{total}.", tag="JOIN_GROUPS")

    async def user_groups_status(self) -> Optional[Tuple[List[str], List[str]]]:
        links = await self.group_repo.all()
        in_groups, not_in = [], []
        try:
            me = await self.client.get_me()
            for link in links:
                try:
                    entity = await self.client.get_entity(link)
                    result = await self.client(GetParticipantRequest(entity, me.id))
                    if isinstance(result.participant, (ChannelParticipant, ChannelParticipantSelf, ChannelParticipantAdmin, ChannelParticipantCreator)):
                        in_groups.append(link)
                    else:
                        not_in.append(link)
                except AuthKeyDuplicatedError as ex:
                    await self._handle_auth_key_duplicated("user_groups_status", ex)
                    return None
                except Exception:
                    not_in.append(link)
            return in_groups, not_in
        except AuthKeyDuplicatedError as ex:
            await self._handle_auth_key_duplicated("user_groups_status", ex)
            return None

    async def unified_dispatch(self, ev: events.NewMessage.Event) -> None:
        key = self.formatter.message_key(ev)
        text = ev.message.message or ""

        if self.cfg.KW_RE.search(text) and key not in self.state.FORWARD_DONE:
            self.state.FORWARD_DONE.add(key)
            fwd_txt = await self.formatter.build_forward_text(ev)
            await asyncio.gather(*[
                self.messenger.safe_send(b.client, b.target_group_id, fwd_txt, tag=f"FWD:{b.phone}")
                for b in self.state.bots if b.mode in ("forward", "both")
            ], return_exceptions=True)

        src_bot = next((b for b in self.state.bots if b.client is ev.client), None)
        if not src_bot:
            return

        sender = await ev.get_sender()
        sender_id = getattr(sender, "id", None)
        if sender_id and sender_id in self.state.blocked_users:
            return

        wants_reply = (
            self.cfg.KW_RE.search(text)
            and any(TextUtils.fuzzy_match(text, trg) for trg in self.state.direct_triggers)
            and TextUtils.normalize_text(text) not in {TextUtils.normalize_text(p) for p in self.state.blocked_phrases}
        )
        if not wants_reply:
            return

        lock = self.state.get_reply_lock(key)
        async with lock:
            if key in self.state.REPLY_DONE:
                return

            dedupe_key = TextUtils.make_dedupe_key(key)
            pending_log_id = 0
            try:
                uname = getattr(sender, "username", "") or ""
                disp_name = f"{(getattr(sender, 'first_name', '') or '').strip()} {(getattr(sender, 'last_name', '') or '').strip()}".strip()
                pending_log_id = await self.db.log_auto_reply_pending(
                    sender_id or 0, uname, disp_name, dedupe_key, ev.chat_id, ev.message.id
                )
            except Exception as ex:
                self.logger.warning(f"log_auto_reply_pending failed: {ex}")

            THRESHOLD = 4
            if sender_id:
                try:
                    prev = await self.db.count_auto_replies_distinct(sender_id, hours=24)
                    if prev >= THRESHOLD:
                        uname = getattr(sender, "username", "") or ""
                        disp_name = f"{(getattr(sender, 'first_name', '') or '').strip()} {(getattr(sender, 'last_name', '') or '').strip()}".strip()
                        await self.db.add_blocked_user(sender_id, uname, disp_name)
                        self.state.blocked_users[sender_id] = (uname, disp_name)
                        self.state.REPLY_DONE.add(key)
                        return
                except Exception as ex:
                    self.logger.warning(f"auto-replies threshold check failed: {ex}")

            self.state.REPLY_DONE.add(key)
            await self.fallback.forward_any(ev)

            any_ok = False
            for b in self.state.bots:
                if b.mode not in ("reply", "both"):
                    continue
                try:
                    tgt = sender if b.client is ev.client else await b.client.get_entity(sender.id)
                except Exception:
                    tgt = getattr(sender, "id", None)
                if not tgt:
                    continue
                sent_orig = await self.messenger.safe_send(b.client, tgt, text, tag=f"ORIG_REPLY:{b.phone}")
                if not sent_orig:
                    continue
                sent_auto = await self.messenger.safe_send(b.client, tgt, self.state.next_auto_reply(), tag=f"AUTO_REPLY:{b.phone}")
                if sent_auto:
                    try:
                        await self.db.update_auto_reply_log(pending_log_id, bot_phone=b.phone, message_id=getattr(sent_auto, "id", None))
                    except Exception as ex:
                        self.logger.warning(f"update_auto_reply_log failed: {ex}")
                    any_ok = True
                    break

            if not any_ok:
                warn = "⚠️ لم يتم الرد تلقائيًا – سيتم المتابعة يدويًا:"
                await self.fallback.forward_any(ev, warn_prefix=warn)

    async def handle_pending(self, ev: events.NewMessage.Event) -> bool:
        chat_id = ev.chat_id
        sender_id = ev.message.sender_id
        key = (chat_id, sender_id)
        pending = self.state.pending_ops.get(key)
        if not pending:
            return False
        if chat_id != self.cfg.COMMAND_GROUP_ID or not self.is_command_bot:
            return False

        text = (ev.message.message or "").strip()
        if text.lower() == "/cancel":
            self.state.pending_ops.pop(key, None)
            await self.messenger.safe_send(self.client, chat_id, "✅ تم إلغاء العملية المعلّقة.", tag="CMD")
            return True

        op = pending.get("op")

        if op == "dbrestore_upload":
            if not ev.message.document:
                await self.messenger.safe_send(self.client, chat_id, "⚠️ أرسل ملف النسخة بصيغة `.json` أو `.json.gz` أو استخدم /cancel.", tag="CMD")
                return True
            path = await self.client.download_media(ev.message, file="backups/")
            if not path:
                await self.messenger.safe_send(self.client, chat_id, "❌ تعذر تنزيل الملف.", tag="CMD")
                return True
            self.state.pending_ops.pop(key, None)
            await self._do_restore(chat_id, str(path))
            return True

        if op == "accadd_form":
            lines = [l.strip() for l in text.splitlines() if l.strip()]
            if len(lines) < 4:
                await self.messenger.safe_send(self.client, chat_id, "⚠️ أرسل على الأقل 4 أسطر: api_id ثم api_hash ثم phone ثم target_group_id. ويمكن سطر خامس mode وسادس command flag.", tag="CMD")
                return True
            try:
                api_id = int(lines[0])
                api_hash = lines[1]
                phone = TextUtils.normalize_phone(lines[2])
                target_group_id = int(lines[3])
                mode = lines[4].lower() if len(lines) > 4 else "both"
                is_cmd = TextUtils.parse_bool_flag(lines[5]) if len(lines) > 5 else False
            except Exception:
                await self.messenger.safe_send(self.client, chat_id, "❌ صيغة الإدخال غير صحيحة.", tag="CMD")
                return True
            self.state.pending_ops.pop(key, None)
            await self._begin_account_add(chat_id, api_id, api_hash, phone, target_group_id, mode, is_cmd)
            return True

        if op == "acccode_input":
            phone = pending["phone"]
            self.state.pending_ops.pop(key, None)
            await self._finish_account_code(chat_id, phone, text)
            return True

        if op == "accpass_input":
            phone = pending["phone"]
            self.state.pending_ops.pop(key, None)
            await self._finish_account_password(chat_id, phone, text)
            return True

        if op in {"groupadd", "groupdel", "groupupdate", "joingroups", "usergroups", "stopjoin"}:
            self.state.pending_ops.pop(key, None)
            return await self._handle_legacy_pending(chat_id, op, text)

        if op in {"add", "del", "find", "blkadd", "blkdel", "blkfind", "autoadd", "autodel", "autofind", "kwadd", "kwdel", "kwfind", "exgroupadd", "exgroupdel", "exgroupfind"}:
            self.state.pending_ops.pop(key, None)
            await self._handle_command_payload(chat_id, op, text)
            return True

        return False

    async def _handle_legacy_pending(self, chat_id: int, op: str, text: str) -> bool:
        lines = [l.strip() for l in text.splitlines() if l.strip()]
        if op == "stopjoin":
            choice = lines[0].strip().lower() if lines else ""
            if choice == "all":
                for phone in list(self.state.joining_now):
                    self.state.stop_joining_flags[phone] = True
                await self.messenger.safe_send(self.client, chat_id, "⏹ تم إيقاف كل عمليات الانضمام الجارية.", tag="CMD")
                return True
            if choice in self.state.joining_now:
                self.state.stop_joining_flags[choice] = True
                await self.messenger.safe_send(self.client, chat_id, f"⏹ أوقفنا {choice}.", tag="CMD")
            else:
                await self.messenger.safe_send(self.client, chat_id, f"⚠️ لا توجد عملية نشطة للحساب: {choice}", tag="CMD")
            return True

        if op in ("groupadd", "groupdel", "groupupdate"):
            if op == "groupadd":
                existing = await self.group_repo.all()
                results = []
                for link in lines:
                    if link not in existing:
                        await self.group_repo.add(link)
                        results.append(f"✓ أضفنا: {link}")
                    else:
                        results.append(f"⚠️ موجود: {link}")
                await self.messenger.safe_send(self.client, chat_id, "نتيجة الإضافة:\n" + "\n".join(results), tag="CMD")
                return True
            if op == "groupdel":
                existing = await self.group_repo.all()
                results = []
                for link in lines:
                    if link in existing:
                        await self.group_repo.delete(link)
                        results.append(f"✓ حذفنا: {link}")
                    else:
                        results.append(f"⚠️ غير موجود: {link}")
                await self.messenger.safe_send(self.client, chat_id, "نتيجة الحذف:\n" + "\n".join(results), tag="CMD")
                return True
            if len(lines) == 2:
                old_link, new_link = lines
                await self.group_repo.update(old_link, new_link)
                await self.messenger.safe_send(self.client, chat_id, f"تم التحديث:\n{old_link} → {new_link}", tag="CMD")
            else:
                await self.messenger.safe_send(self.client, chat_id, "⚠️ أرسل سطرين: الرابط القديم ثم الجديد.", tag="CMD")
            return True

        if op == "joingroups":
            parts = lines[0].split() if lines else []
            if not parts:
                await self.messenger.safe_send(self.client, chat_id, "⚠️ أرسل: <رقم الجوال> [بداية رقمية]", tag="CMD")
                return True
            phone = TextUtils.normalize_phone(parts[0])
            start_index = int(parts[1]) - 1 if len(parts) > 1 and parts[1].isdigit() else 0
            target_bot = self.manager.active_bots.get(phone)
            if not target_bot:
                await self.messenger.safe_send(self.client, chat_id, f"⚠️ لا يوجد حساب شغال: {phone}", tag="CMD")
                return True
            t = asyncio.create_task(target_bot.join_groups_with_account(start_index))
            self.state.joining_now[phone] = t
            self.state.stop_joining_flags[phone] = False
            await self.messenger.safe_send(self.client, chat_id, f"⏳ بدأنا انضمام {phone} من رقم {start_index + 1}.", tag="CMD")
            return True

        if op == "usergroups":
            phone = TextUtils.normalize_phone(lines[0]) if lines else ""
            target_bot = self.manager.active_bots.get(phone)
            if not target_bot:
                await self.messenger.safe_send(self.client, chat_id, f"⚠️ الحساب غير موجود أو غير شغال: {phone}", tag="CMD")
                return True
            status = await target_bot.user_groups_status()
            if status is None:
                await self.messenger.safe_send(self.client, chat_id, f"⛔️ تم إيقاف الحساب {phone} لأن الجلسة صارت غير صالحة بسبب استخدامها من IP آخر. أعد إنشاء الجلسة لهذا الرقم.", tag="CMD")
                return True
            in_g, not_in = status
            msg = f"🔢 **{phone} عضو في {len(in_g)} من {len(in_g) + len(not_in)} جروب.**\n❌ خارجها: {len(not_in)}"
            await self.messenger.safe_send(self.client, chat_id, msg, tag="CMD")
            return True

        return True

    async def _handle_command_payload(self, chat_id: int, cmd: str, arg: str) -> None:
        stores = {
            "add": ("direct_reply_messages", self.state.direct_triggers, "message_text"),
            "del": ("direct_reply_messages", self.state.direct_triggers, "message_text"),
            "find": ("direct_reply_messages", self.state.direct_triggers, "message_text"),
            "blkadd": ("blocked_reply_messages", self.state.blocked_phrases, "message_text"),
            "blkdel": ("blocked_reply_messages", self.state.blocked_phrases, "message_text"),
            "blkfind": ("blocked_reply_messages", self.state.blocked_phrases, "message_text"),
            "autoadd": ("auto_reply_responses", self.state.auto_replies, "message_text"),
            "autodel": ("auto_reply_responses", self.state.auto_replies, "message_text"),
            "autofind": ("auto_reply_responses", self.state.auto_replies, "message_text"),
        }
        if cmd in stores:
            table, store_list, col = stores[cmd]
            lines = [l.strip() for l in arg.splitlines() if l.strip()]
            if cmd in ("add", "blkadd", "autoadd"):
                results = []
                for line in lines:
                    if line not in store_list:
                        await self.db.insert_scalar_value(table, col, line)
                        store_list.append(line)
                        results.append(f"✓ أضفنا: {line}")
                    else:
                        results.append(f"⚠️ موجود: {line}")
                await self.messenger.safe_send(self.client, chat_id, "نتيجة الإضافة:\n" + "\n".join(results), tag="CMD")
                return
            if cmd in ("del", "blkdel", "autodel"):
                results = []
                for line in lines:
                    if line in store_list:
                        await self.db.delete_scalar_value(table, col, line)
                        store_list.remove(line)
                        results.append(f"✓ حذفنا: {line}")
                    else:
                        results.append(f"⚠️ غير موجود: {line}")
                await self.messenger.safe_send(self.client, chat_id, "نتيجة الحذف:\n" + "\n".join(results), tag="CMD")
                return
            thresh = 100 if cmd.startswith("blk") else 80
            patterns = [l.strip() for l in arg.splitlines() if l.strip()]
            msg_lines = []
            for pat in patterns:
                matches = [m for m in store_list if fuzz.ratio(TextUtils.normalize_text(pat), TextUtils.normalize_text(m)) >= thresh]
                if matches:
                    msg_lines.append(f"🔎 **نتائج `{pat}`:**")
                    msg_lines.extend([f"```\n{m}\n```" for m in matches])
                else:
                    msg_lines.append(f"🔎 **نتائج `{pat}`:**\n— لا توجد مطابقات —")
            await self.messenger.safe_send(self.client, chat_id, "\n".join(msg_lines) if msg_lines else "— لا توجد مطابقات —", tag="CMD")
            return

        if cmd in {"kwadd", "kwdel", "kwfind"}:
            lines = [l.strip() for l in arg.splitlines() if l.strip()]
            keywords = self.cfg.KEYWORDS[:]
            if cmd == "kwadd":
                res = []
                for line in lines:
                    if line not in keywords:
                        await self.db.insert_scalar_value("keyword_rules", "keyword_text", line)
                        res.append(f"✓ أضفنا: {line}")
                    else:
                        res.append(f"⚠️ موجود: {line}")
                await self.manager.reload_runtime_state()
                await self.messenger.safe_send(self.client, chat_id, "نتيجة إضافة الكلمات:\n" + "\n".join(res), tag="CMD")
                return
            if cmd == "kwdel":
                res = []
                for line in lines:
                    if line in keywords:
                        await self.db.delete_keyword(line)
                        res.append(f"✓ حذفنا: {line}")
                    else:
                        res.append(f"⚠️ غير موجود: {line}")
                await self.manager.reload_runtime_state()
                await self.messenger.safe_send(self.client, chat_id, "نتيجة حذف الكلمات:\n" + "\n".join(res), tag="CMD")
                return
            patterns = lines
            msg_lines = []
            for pat in patterns:
                matches = [m for m in keywords if fuzz.ratio(TextUtils.normalize_text(pat), TextUtils.normalize_text(m)) >= 80]
                if matches:
                    msg_lines.append(f"🔎 **نتائج `{pat}`:**")
                    msg_lines.extend([f"`{m}`" for m in matches])
                else:
                    msg_lines.append(f"🔎 **نتائج `{pat}`:**\n— لا توجد مطابقات —")
            await self.messenger.safe_send(self.client, chat_id, "\n".join(msg_lines) if msg_lines else "— لا توجد مطابقات —", tag="CMD")
            return

        if cmd in {"exgroupadd", "exgroupdel", "exgroupfind"}:
            lines = [l.strip() for l in arg.splitlines() if l.strip()]
            if cmd == "exgroupadd":
                res = []
                existing = {int(r["group_id"]) for r in await self.db.list_excluded_groups()}
                for line in lines:
                    try:
                        gid = int(line)
                    except Exception:
                        res.append(f"⚠️ قيمة غير صالحة: {line}")
                        continue
                    if gid in existing:
                        res.append(f"⚠️ موجود: {gid}")
                        continue
                    await self.db.add_excluded_group(gid)
                    res.append(f"✓ أضفنا: {gid}")
                await self.manager.reload_runtime_state()
                await self.messenger.safe_send(self.client, chat_id, "نتيجة إضافة الجروبات المستبعدة:\n" + "\n".join(res), tag="CMD")
                return
            if cmd == "exgroupdel":
                res = []
                existing = {int(r["group_id"]) for r in await self.db.list_excluded_groups()}
                for line in lines:
                    try:
                        gid = int(line)
                    except Exception:
                        res.append(f"⚠️ قيمة غير صالحة: {line}")
                        continue
                    if gid not in existing:
                        res.append(f"⚠️ غير موجود: {gid}")
                        continue
                    await self.db.del_excluded_group(gid)
                    res.append(f"✓ حذفنا: {gid}")
                await self.manager.reload_runtime_state()
                await self.messenger.safe_send(self.client, chat_id, "نتيجة حذف الجروبات المستبعدة:\n" + "\n".join(res), tag="CMD")
                return
            rows = await self.db.find_excluded_groups(arg.strip())
            if not rows:
                await self.messenger.safe_send(self.client, chat_id, "— لا توجد مطابقة —", tag="CMD")
                return
            msg = "\n".join([f"- `{r['group_id']}` | {r['title'] or '—'} | {r['created_at']}" for r in rows[:200]])
            await self.messenger.safe_send(self.client, chat_id, msg, tag="CMD")
            return

    async def _do_restore(self, chat_id: int, in_path: str) -> None:
        try:
            await self.messenger.safe_send(self.client, chat_id, f"⏳ نسترجع من `{in_path}`...", tag="CMD")
            summary = await self.manager.restore_backup(in_path)
            lines = ["✅ تم الاسترجاع الدمجي وتحديث الذاكرة."]
            for table, stat in summary.items():
                if stat["inserted"] or stat["skipped"]:
                    lines.append(f"• **{table}**: new={stat['inserted']} | skipped={stat['skipped']}")
            await self.messenger.safe_send(self.client, chat_id, "\n".join(lines), tag="CMD")
        except Exception as e:
            await self.messenger.safe_send(self.client, chat_id, f"❌ فشل الاسترجاع:\n`{e}`", tag="CMD")

    async def _begin_account_add(self, chat_id: int, api_id: int, api_hash: str, phone: str, target_group_id: int, mode: str, is_cmd: bool) -> None:
        if mode not in self.cfg.VALID_MODES:
            await self.messenger.safe_send(self.client, chat_id, f"❌ المود غير صالح. المتاح: {', '.join(sorted(self.cfg.VALID_MODES))}", tag="CMD")
            return
        if await self.db.get_account(phone):
            await self.messenger.safe_send(self.client, chat_id, f"⚠️ الحساب {phone} موجود مسبقًا. استخدم أوامر التحكم بدل الإضافة.", tag="CMD")
            return
        if phone in self.state.pending_auth:
            await self.messenger.safe_send(self.client, chat_id, f"⚠️ يوجد طلب توثيق معلّق لهذا الرقم: {phone}", tag="CMD")
            return
        session_name = self.manager.session_name(phone)
        client = TelegramClient(session_name, api_id, api_hash)
        try:
            await client.connect()
            sent = await client.send_code_request(phone)
            self.state.pending_auth[phone] = PendingAuth(
                phone=phone,
                api_id=api_id,
                api_hash=api_hash,
                target_group_id=target_group_id,
                mode=mode,
                is_command_bot=is_cmd,
                session_name=session_name,
                phone_code_hash=sent.phone_code_hash,
                client=client,
                created_at=datetime.datetime.utcnow(),
                needs_password=False,
            )
            await self.messenger.safe_send(
                self.client,
                chat_id,
                f"📩 تم إرسال كود التحقق إلى `{phone}`.\nأرسل الآن `/acccode {phone} 12345` أو استخدم `/acccode` ثم أرسل الكود فقط.",
                tag="CMD",
            )
        except Exception as exc:
            try:
                await client.disconnect()
            except Exception:
                pass
            await self.messenger.safe_send(self.client, chat_id, f"❌ فشل إرسال الكود:\n`{exc}`", tag="CMD")

    async def _finish_account_code(self, chat_id: int, phone: str, code: str) -> None:
        pending = self.state.pending_auth.get(phone)
        if not pending:
            await self.messenger.safe_send(self.client, chat_id, f"⚠️ لا يوجد طلب توثيق معلّق للحساب {phone}.", tag="CMD")
            return
        try:
            if not pending.client.is_connected():
                await pending.client.connect()
            await pending.client.sign_in(phone=phone, code=code.strip(), phone_code_hash=pending.phone_code_hash)
            await self.manager.complete_account_login(pending)
            self.state.pending_auth.pop(phone, None)
            await self.messenger.safe_send(self.client, chat_id, f"✅ تم إنشاء الجلسة وتشغيل الحساب `{phone}` بنجاح.", tag="CMD")
        except SessionPasswordNeededError:
            pending.needs_password = True
            await self.messenger.safe_send(
                self.client,
                chat_id,
                f"🔐 الحساب `{phone}` عليه كلمة سر ثنائية. أرسل `/accpass {phone} كلمة_السر` أو استخدم `/accpass` ثم أرسلها فقط.",
                tag="CMD",
            )
        except (PhoneCodeInvalidError, PhoneCodeExpiredError) as exc:
            await self.messenger.safe_send(self.client, chat_id, f"❌ الكود غير صالح أو منتهي: `{exc}`", tag="CMD")
        except Exception as exc:
            await self.messenger.safe_send(self.client, chat_id, f"❌ فشل التحقق من الكود:\n`{exc}`", tag="CMD")

    async def _finish_account_password(self, chat_id: int, phone: str, password: str) -> None:
        pending = self.state.pending_auth.get(phone)
        if not pending:
            await self.messenger.safe_send(self.client, chat_id, f"⚠️ لا يوجد طلب توثيق معلّق للحساب {phone}.", tag="CMD")
            return
        try:
            if not pending.client.is_connected():
                await pending.client.connect()
            await pending.client.sign_in(password=password)
            await self.manager.complete_account_login(pending)
            self.state.pending_auth.pop(phone, None)
            await self.messenger.safe_send(self.client, chat_id, f"✅ تم اعتماد كلمة السر وتشغيل الحساب `{phone}`.", tag="CMD")
        except PasswordHashInvalidError:
            await self.messenger.safe_send(self.client, chat_id, "❌ كلمة السر الثنائية غير صحيحة.", tag="CMD")
        except Exception as exc:
            await self.messenger.safe_send(self.client, chat_id, f"❌ فشل اعتماد كلمة السر:\n`{exc}`", tag="CMD")

    async def on_message(self, ev: events.NewMessage.Event) -> None:
        chat_id, sender_id = ev.chat_id, ev.message.sender_id
        text = ev.message.message or ""

        if await self.handle_pending(ev):
            return

        if chat_id == self.cfg.COMMAND_GROUP_ID and text.startswith("/"):
            return

        if text.startswith("✉") or ev.is_private or ev.out or chat_id in self.cfg.EXCLUDED_GROUPS:
            return
        if any([re.search(r"@\w{5,}", text), re.search(r"https?://\S+", text), len(text.split()) > 17, re.search(r"\d", text)]):
            return
        sender = ev.message.sender
        if getattr(sender, "bot", False):
            return
        try:
            part = await ev.client.get_participant(chat_id, sender_id)
            if isinstance(part, (ChannelParticipantAdmin, ChannelParticipantCreator)):
                return
        except Exception:
            pass

        if self.mode == "self":
            if self.cfg.KW_RE.search(text):
                fwd = await self.formatter.build_forward_text(ev)
                await self.messenger.safe_send(self.client, self.target_group_id, fwd, tag="SELFFWD")
            await self.unified_dispatch(ev)
        else:
            await self.unified_dispatch(ev)

    async def on_command(self, ev: events.NewMessage.Event) -> None:
        chat_id, sender_id = ev.chat_id, ev.message.sender_id
        raw = (ev.message.message or "").strip()
        parts = raw.split(maxsplit=1)
        cmd = parts[0].lstrip("/").lower()
        arg = parts[1] if len(parts) > 1 else ""

        if chat_id != self.cfg.COMMAND_GROUP_ID:
            return
        if not self.is_command_bot:
            return
        if self.state.COMMAND_USER_ID and sender_id != self.state.COMMAND_USER_ID:
            await self.messenger.safe_send(self.client, chat_id, "⚠️ غير مصرح.", tag="CMD")
            return
        if cmd not in self.cfg.COMMANDS:
            await self.messenger.safe_send(self.client, chat_id, "⚠️ أمر غير معروف. اكتب /help.", tag="CMD")
            return

        if cmd == "cancel":
            self.state.pending_ops.pop((chat_id, sender_id), None)
            await self.messenger.safe_send(self.client, chat_id, "✅ تم إلغاء أي عملية معلّقة لهذا الحساب.", tag="CMD")
            return

        # ===== Backup / restore =====
        if cmd == "dbbackup":
            ts = datetime.datetime.utcnow().strftime("%Y%m%d_%H%M%S")
            out_path = f"backups/db_{ts}.json.gz"
            try:
                await self.messenger.safe_send(self.client, chat_id, "⏳ نُنشئ نسخة احتياطية شاملة الآن...", tag="CMD")
                summary = await self.backup.export_backup(out_path)
                caption = "✅ تم إنشاء النسخة الاحتياطية.\n" + "\n".join([f"• {k}: {v}" for k, v in summary.items() if v])
                await self.messenger.safe_send_file(self.client, chat_id, out_path, caption=caption, tag="DBFILE")
            except Exception as e:
                await self.messenger.safe_send(self.client, chat_id, f"❌ فشل النسخ الاحتياطي:\n`{e}`", tag="CMD")
            return

        if cmd == "dbrestore":
            if ev.is_reply:
                rep = await ev.get_reply_message()
                if rep and rep.document:
                    path = await self.client.download_media(rep, file="backups/")
                    if not path:
                        await self.messenger.safe_send(self.client, chat_id, "❌ تعذر تنزيل الملف.", tag="CMD")
                        return
                    await self._do_restore(chat_id, str(path))
                    return
            if arg:
                await self._do_restore(chat_id, arg.strip())
                return
            self.state.pending_ops[(chat_id, sender_id)] = {"op": "dbrestore_upload"}
            await self.messenger.safe_send(self.client, chat_id, "📎 أرسل الآن ملف النسخة `.json` أو `.json.gz` داخل المجموعة، أو استخدم /cancel للإلغاء.", tag="CMD")
            return

        # ===== Stats =====
        if cmd == "stats":
            try:
                s = await self.db.get_stats()
                msg = (
                    "📊 **ملخص التخزين الحالي:**\n\n"
                    f"• 🟢 الجُمل للرد المباشر: **{s['direct']}**\n"
                    f"• ⛔️ الجُمل المحظورة للنص: **{s['blocked_text']}**\n"
                    f"• 🚫 المستخدمون المحظورون: **{s['blocked_users']}**\n"
                    f"• 🔗 روابط الجروبات: **{s['groups']}**\n"
                    f"• 🔎 الكلمات الأساسية: **{s['keywords']}**\n"
                    f"• 🚷 الجروبات المستبعدة: **{s['excluded_groups']}**\n"
                    f"• 🤖 الحسابات المسجلة: **{s['accounts']}**"
                )
                await self.messenger.safe_send(self.client, chat_id, msg, tag="CMD")
            except Exception as e:
                await self.messenger.safe_send(self.client, chat_id, f"❌ تعذّر جلب الإحصاءات: `{e}`", tag="CMD")
            return

        # ===== Accounts =====
        if cmd == "accadd":
            if not arg:
                self.state.pending_ops[(chat_id, sender_id)] = {"op": "accadd_form"}
                await self.messenger.safe_send(
                    self.client,
                    chat_id,
                    "✍️ أرسل بيانات الحساب بهذا الترتيب، كل قيمة في سطر:\n1) api_id\n2) api_hash\n3) phone\n4) target_group_id\n5) mode اختياري\n6) command flag اختياري (yes/no)",
                    tag="CMD",
                )
                return
            parts2 = arg.split()
            if len(parts2) < 4:
                await self.messenger.safe_send(self.client, chat_id, "استخدام: /accadd <api_id> <api_hash> <phone> <target_group_id> [mode] [command_flag]", tag="CMD")
                return
            try:
                api_id = int(parts2[0])
                api_hash = parts2[1]
                phone = TextUtils.normalize_phone(parts2[2])
                target_group_id = int(parts2[3])
                mode = parts2[4].lower() if len(parts2) > 4 else "both"
                is_cmd = TextUtils.parse_bool_flag(parts2[5]) if len(parts2) > 5 else False
            except Exception:
                await self.messenger.safe_send(self.client, chat_id, "❌ صيغة الأمر غير صحيحة.", tag="CMD")
                return
            await self._begin_account_add(chat_id, api_id, api_hash, phone, target_group_id, mode, is_cmd)
            return

        if cmd == "acccode":
            if arg:
                parts2 = arg.split(maxsplit=1)
                if len(parts2) == 2:
                    await self._finish_account_code(chat_id, TextUtils.normalize_phone(parts2[0]), parts2[1].strip())
                    return
            pendings = list(self.state.pending_auth.keys())
            if not pendings:
                await self.messenger.safe_send(self.client, chat_id, "⚠️ لا يوجد حساب بانتظار كود تحقق.", tag="CMD")
                return
            phone = pendings[0] if len(pendings) == 1 else None
            if not phone:
                await self.messenger.safe_send(self.client, chat_id, "استخدم: /acccode <phone> <code>", tag="CMD")
                return
            self.state.pending_ops[(chat_id, sender_id)] = {"op": "acccode_input", "phone": phone}
            await self.messenger.safe_send(self.client, chat_id, f"✍️ أرسل الآن كود التحقق للحساب {phone} فقط.", tag="CMD")
            return

        if cmd == "accpass":
            if arg:
                parts2 = arg.split(maxsplit=1)
                if len(parts2) == 2:
                    await self._finish_account_password(chat_id, TextUtils.normalize_phone(parts2[0]), parts2[1])
                    return
            pendings = [p.phone for p in self.state.pending_auth.values() if p.needs_password]
            if not pendings:
                await self.messenger.safe_send(self.client, chat_id, "⚠️ لا يوجد حساب بانتظار كلمة سر ثنائية.", tag="CMD")
                return
            phone = pendings[0] if len(pendings) == 1 else None
            if not phone:
                await self.messenger.safe_send(self.client, chat_id, "استخدم: /accpass <phone> <password>", tag="CMD")
                return
            self.state.pending_ops[(chat_id, sender_id)] = {"op": "accpass_input", "phone": phone}
            await self.messenger.safe_send(self.client, chat_id, f"✍️ أرسل الآن كلمة السر الثنائية للحساب {phone} فقط.", tag="CMD")
            return

        if cmd in {"acclist", "accstatus"}:
            rows = await self.db.list_accounts()
            if not rows:
                await self.messenger.safe_send(self.client, chat_id, "— لا توجد حسابات مسجلة —", tag="CMD")
                return
            msg_lines = []
            for i, r in enumerate(rows, start=1):
                phone = r["phone"]
                runtime = "🟢 شغال" if phone in self.manager.active_bots else "⚪️ متوقف"
                pending = " | ⏳ بانتظار توثيق" if phone in self.state.pending_auth else ""
                msg_lines.append(
                    f"**#{i}** {phone}{' | 👑 command' if r['is_command_bot'] else ''}\n"
                    f"• الحالة: {runtime}{pending}\n"
                    f"• mode: `{r['mode']}` | enabled: `{int(r['enabled'])}`\n"
                    f"• target_group_id: `{r['target_group_id']}`\n"
                    f"• session: `{r['session_name']}`\n"
                    f"• last_error: `{(r['last_error'] or '—')[:120]}`"
                )
            await self.messenger.safe_send(self.client, chat_id, "\n\n".join(msg_lines), tag="CMD")
            return

        if cmd == "accstart":
            if not arg:
                await self.messenger.safe_send(self.client, chat_id, "استخدم: /accstart <phone>", tag="CMD")
                return
            phone = TextUtils.normalize_phone(arg.strip())
            ok, msg = await self.manager.start_account(phone)
            await self.messenger.safe_send(self.client, chat_id, msg if ok else f"⚠️ {msg}", tag="CMD")
            return

        if cmd == "accstop":
            if not arg:
                await self.messenger.safe_send(self.client, chat_id, "استخدم: /accstop <phone>", tag="CMD")
                return
            phone = TextUtils.normalize_phone(arg.strip())
            ok, msg = await self.manager.stop_account(phone, disable=True)
            await self.messenger.safe_send(self.client, chat_id, msg if ok else f"⚠️ {msg}", tag="CMD")
            return

        if cmd == "accrestart":
            if not arg:
                await self.messenger.safe_send(self.client, chat_id, "استخدم: /accrestart <phone>", tag="CMD")
                return
            phone = TextUtils.normalize_phone(arg.strip())
            ok, msg = await self.manager.restart_account(phone)
            await self.messenger.safe_send(self.client, chat_id, msg if ok else f"⚠️ {msg}", tag="CMD")
            return

        if cmd == "accmode":
            parts2 = arg.split()
            if len(parts2) != 2:
                await self.messenger.safe_send(self.client, chat_id, "استخدم: /accmode <phone> <forward|reply|both|self>", tag="CMD")
                return
            phone = TextUtils.normalize_phone(parts2[0])
            mode = parts2[1].lower()
            ok, msg = await self.manager.update_account_mode(phone, mode)
            await self.messenger.safe_send(self.client, chat_id, msg if ok else f"⚠️ {msg}", tag="CMD")
            return

        if cmd == "acctarget":
            parts2 = arg.split()
            if len(parts2) != 2:
                await self.messenger.safe_send(self.client, chat_id, "استخدم: /acctarget <phone> <target_group_id>", tag="CMD")
                return
            try:
                phone = TextUtils.normalize_phone(parts2[0])
                target_group_id = int(parts2[1])
            except Exception:
                await self.messenger.safe_send(self.client, chat_id, "❌ صيغة غير صحيحة.", tag="CMD")
                return
            ok, msg = await self.manager.update_account_target(phone, target_group_id)
            await self.messenger.safe_send(self.client, chat_id, msg if ok else f"⚠️ {msg}", tag="CMD")
            return

        if cmd == "accsetcmd":
            if not arg:
                await self.messenger.safe_send(self.client, chat_id, "استخدم: /accsetcmd <phone>", tag="CMD")
                return
            phone = TextUtils.normalize_phone(arg.strip())
            ok, msg = await self.manager.promote_command_bot(phone)
            await self.messenger.safe_send(self.client, chat_id, msg if ok else f"⚠️ {msg}", tag="CMD")
            return

        if cmd == "accdel":
            parts2 = arg.split()
            if not parts2:
                await self.messenger.safe_send(self.client, chat_id, "استخدم: /accdel <phone> [purge]", tag="CMD")
                return
            phone = TextUtils.normalize_phone(parts2[0])
            purge = len(parts2) > 1 and TextUtils.parse_bool_flag(parts2[1])
            ok, msg = await self.manager.delete_account(phone, purge_session=purge)
            await self.messenger.safe_send(self.client, chat_id, msg if ok else f"⚠️ {msg}", tag="CMD")
            return

        # ===== Dynamic keywords =====
        if cmd in {"kwadd", "kwdel", "kwfind"} and not arg:
            self.state.pending_ops[(chat_id, sender_id)] = {"op": cmd}
            await self.messenger.safe_send(self.client, chat_id, f"✍️ أرسل الآن محتوى **{cmd}**، كل سطر عنصر مستقل.", tag="CMD")
            return
        if cmd in {"kwadd", "kwdel", "kwfind"} and arg:
            await self._handle_command_payload(chat_id, cmd, arg)
            return
        if cmd == "kwlist":
            raw_flag = arg.strip().lower() in {"raw", "بدون", "no", "بدون ترقيم"}
            kws = self.cfg.KEYWORDS
            if not kws:
                await self.messenger.safe_send(self.client, chat_id, "— لا توجد كلمات أساسية —", tag="CMD")
                return
            msg = "\n".join(kws) if raw_flag else "\n".join([f"{i + 1}. `{k}`" for i, k in enumerate(kws)])
            await self.messenger.safe_send(self.client, chat_id, msg, tag="CMD")
            return

        # ===== Excluded groups =====
        if cmd in {"exgroupadd", "exgroupdel", "exgroupfind"} and not arg:
            self.state.pending_ops[(chat_id, sender_id)] = {"op": cmd}
            await self.messenger.safe_send(self.client, chat_id, f"✍️ أرسل الآن محتوى **{cmd}**، كل سطر group_id مستقل.", tag="CMD")
            return
        if cmd in {"exgroupadd", "exgroupdel", "exgroupfind"} and arg:
            await self._handle_command_payload(chat_id, cmd, arg)
            return
        if cmd == "exgrouplist":
            raw_flag = arg.strip().lower() in {"raw", "بدون", "no", "بدون ترقيم"}
            rows = await self.db.list_excluded_groups()
            if not rows:
                await self.messenger.safe_send(self.client, chat_id, "— لا توجد جروبات مستبعدة —", tag="CMD")
                return
            if raw_flag:
                msg = "\n".join([str(r["group_id"]) for r in rows])
            else:
                msg = "\n".join([f"{i + 1}. `{r['group_id']}` | {r['title'] or '—'}" for i, r in enumerate(rows)])
            await self.messenger.safe_send(self.client, chat_id, msg, tag="CMD")
            return

        # ===== Fallback / command group =====
        if cmd == "fallback":
            await self.messenger.safe_send(self.client, chat_id, f"📌 fallback_group_id الحالي: `{self.cfg.FALLBACK_GROUP_ID}`", tag="CMD")
            return
        if cmd == "fallbackset":
            if not arg:
                await self.messenger.safe_send(self.client, chat_id, "استخدم: /fallbackset <group_id>", tag="CMD")
                return
            try:
                gid = int(arg.strip())
            except Exception:
                await self.messenger.safe_send(self.client, chat_id, "❌ group_id غير صالح.", tag="CMD")
                return
            await self.db.set_setting("fallback_group_id", str(gid))
            await self.manager.reload_runtime_state()
            await self.messenger.safe_send(self.client, chat_id, f"✅ تم تحديث fallback_group_id إلى `{gid}`", tag="CMD")
            return

        if cmd == "cmdgroupset":
            if not arg:
                await self.messenger.safe_send(self.client, chat_id, "استخدم: /cmdgroupset <group_id>", tag="CMD")
                return
            try:
                gid = int(arg.strip())
            except Exception:
                await self.messenger.safe_send(self.client, chat_id, "❌ group_id غير صالح.", tag="CMD")
                return
            await self.db.set_setting("command_group_id", str(gid))
            await self.manager.reload_runtime_state()
            await self.messenger.safe_send(self.client, chat_id, f"✅ تم تحديث command_group_id إلى `{gid}`", tag="CMD")
            return

        if cmd == "configshow":
            await self.messenger.safe_send(
                self.client,
                chat_id,
                (
                    "⚙️ **الإعدادات الحالية:**\n"
                    f"• fallback_group_id: `{self.cfg.FALLBACK_GROUP_ID}`\n"
                    f"• command_group_id: `{self.cfg.COMMAND_GROUP_ID}`\n"
                    f"• keywords_count: `{len(self.cfg.KEYWORDS)}`\n"
                    f"• excluded_groups_count: `{len(self.cfg.EXCLUDED_GROUPS)}`\n"
                    f"• command_user_id: `{self.state.COMMAND_USER_ID or '—'}`"
                ),
                tag="CMD",
            )
            return

        # ===== Blocked users =====
        if cmd == "blkuser_add":
            if ev.is_reply:
                rep = await ev.get_reply_message()
                snd = await rep.get_sender()
                uid = getattr(snd, "id", None)
                uname = getattr(snd, "username", "") or ""
                dname = f"{(getattr(snd, 'first_name', '') or '').strip()} {(getattr(snd, 'last_name', '') or '').strip()}".strip()
                if not uid:
                    await self.messenger.safe_send(self.client, chat_id, "❌ لا أستطيع استخراج user_id من الرد.", tag="CMD")
                    return
                await self.db.add_blocked_user(uid, uname, dname)
                self.state.blocked_users[uid] = (uname, dname)
                await self.messenger.safe_send(self.client, chat_id, f"✅ أُضيف للمحظورين: {uid} @{uname or '—'} | {dname or '—'}", tag="CMD")
                return
            if not arg:
                await self.messenger.safe_send(self.client, chat_id, "استخدم: /blkuser_add <user_id> [username] [display name...] أو بالرد على رسالة.", tag="CMD")
                return
            parts2 = arg.split()
            try:
                uid = int(parts2[0])
                uname = parts2[1] if len(parts2) > 1 else ""
                dname = " ".join(parts2[2:]) if len(parts2) > 2 else ""
            except Exception:
                await self.messenger.safe_send(self.client, chat_id, "صيغة غير صحيحة.", tag="CMD")
                return
            await self.db.add_blocked_user(uid, uname, dname)
            self.state.blocked_users[uid] = (uname, dname)
            await self.messenger.safe_send(self.client, chat_id, f"✅ أُضيف للمحظورين: {uid} @{uname or '—'} | {dname or '—'}", tag="CMD")
            return

        if cmd == "blkuser_del":
            if not arg and not ev.is_reply:
                await self.messenger.safe_send(self.client, chat_id, "استخدم: /blkuser_del <user_id> أو بالرد على رسالة.", tag="CMD")
                return
            if ev.is_reply:
                snd = await (await ev.get_reply_message()).get_sender()
                uid = int(getattr(snd, "id", 0) or 0)
            else:
                try:
                    uid = int(arg.strip())
                except Exception:
                    uid = 0
            if not uid:
                await self.messenger.safe_send(self.client, chat_id, "❌ لم أتمكن من تحديد user_id.", tag="CMD")
                return
            c = await self.db.del_blocked_user(uid)
            self.state.blocked_users.pop(uid, None)
            await self.messenger.safe_send(self.client, chat_id, f"✅ أُزيل من المحظورين (حُذف {c}).", tag="CMD")
            return

        if cmd == "blkuser_list":
            raw_flag = arg.strip().lower() in {"raw", "بدون", "no"}
            rows = await self.db.list_blocked_users()
            if not rows:
                await self.messenger.safe_send(self.client, chat_id, "— لا يوجد محظورون —", tag="CMD")
                return
            if raw_flag:
                msg = "\n".join([f"{r['user_id']} @{r['username'] or '—'} | {r['display_name'] or '—'} | {r['created_at']}" for r in rows[:200]])
            else:
                msg = "\n\n".join([
                    f"🔹 **#{i + 1}**\n👤 **User ID:** `{r['user_id']}`{' | @' + r['username'] if r['username'] else ''}\n📝 **Name:** `{r['display_name'] or '—'}`\n🕒 **Blocked At:** `{r['created_at']}`"
                    for i, r in enumerate(rows[:200])
                ])
            await self.messenger.safe_send(self.client, chat_id, msg, tag="CMD")
            return

        if cmd == "blkuser_find":
            if not arg:
                await self.messenger.safe_send(self.client, chat_id, "اكتب: /blkuser_find <pattern>", tag="CMD")
                return
            rows = await self.db.find_blocked_users(arg)
            if not rows:
                await self.messenger.safe_send(self.client, chat_id, "— لا توجد مطابقة —", tag="CMD")
                return
            msg = "\n".join([f"- {r['user_id']} @{r['username'] or '—'} | {r['display_name'] or '—'} | {r['created_at']}" for r in rows[:200]])
            await self.messenger.safe_send(self.client, chat_id, msg, tag="CMD")
            return

        # ===== Auto replies log =====
        if cmd == "autoreplies_count":
            if not arg:
                await self.messenger.safe_send(self.client, chat_id, "استخدام: /autoreplies_count <user_id>", tag="CMD")
                return
            try:
                uid = int(arg.strip())
            except Exception:
                await self.messenger.safe_send(self.client, chat_id, "صيغة غير صحيحة.", tag="CMD")
                return
            c = await self.db.count_auto_replies(uid)
            await self.messenger.safe_send(self.client, chat_id, f"🔢 عدد محاولات/ردود المستخدم {uid}: {c}", tag="CMD")
            return

        if cmd == "autoreplies_list":
            limit = int(arg.strip()) if arg.strip().isdigit() else 50
            if ev.is_reply:
                snd = await (await ev.get_reply_message()).get_sender()
                uid = getattr(snd, "id", None)
                if not uid:
                    await self.messenger.safe_send(self.client, chat_id, "❌ لا أستطيع استخراج user_id من الرد.", tag="CMD")
                    return
                rows = await self.db.list_auto_replies_for_user(uid, limit=limit)
                title = f"📒 آخر {len(rows)} سجل للمستخدم {uid}:"
            else:
                rows = await self.db.list_auto_replies(limit=limit)
                title = f"📒 آخر {len(rows)} سجل عام:"
            if not rows:
                await self.messenger.safe_send(self.client, chat_id, "— لا يوجد سجلات —", tag="CMD")
                return
            lines = [
                (
                    f"🔹 **#{r['id']}**\n"
                    f"👤 **User:** `{r['user_id']}`{' | @' + r['username'] if r['username'] else ''}\n"
                    f"📝 **Name:** `{r['display_name'] or '—'}`\n"
                    f"🤖 **Bot:** `{r['bot_phone'] or '—'}`\n"
                    f"✉️ **Msg ID:** `{r['message_id'] or '—'}`\n"
                    f"🕒 **Time:** `{r['created_at']}`"
                )
                for r in rows
            ]
            await self.messenger.safe_send(self.client, chat_id, title + "\n" + "\n\n".join(lines), tag="CMD")
            return

        if cmd == "autoreplies_clear":
            if not arg and not ev.is_reply:
                await self.messenger.safe_send(self.client, chat_id, "استخدام:\n/autoreplies_clear all\n/autoreplies_clear <user_id>\nأو بالرد على رسالة.", tag="CMD")
                return
            if arg.strip().lower() == "all":
                c = await self.db.clear_auto_reply_log(None)
                await self.messenger.safe_send(self.client, chat_id, f"🧹 مسحنا {c} سجل من السجل العام.", tag="CMD")
                return
            if ev.is_reply and not arg:
                snd = await (await ev.get_reply_message()).get_sender()
                uid = getattr(snd, "id", None)
            else:
                try:
                    uid = int(arg.strip())
                except Exception:
                    uid = None
            if not uid:
                await self.messenger.safe_send(self.client, chat_id, "استخدم: /autoreplies_clear <user_id> أو all", tag="CMD")
                return
            c = await self.db.clear_auto_reply_log(uid)
            await self.messenger.safe_send(self.client, chat_id, f"🧹 مسحنا سجلات المستخدم {uid}: {c}", tag="CMD")
            return

        # ===== Text store commands =====
        stores = {
            "add": self.state.direct_triggers,
            "del": self.state.direct_triggers,
            "find": self.state.direct_triggers,
            "blkadd": self.state.blocked_phrases,
            "blkdel": self.state.blocked_phrases,
            "blkfind": self.state.blocked_phrases,
            "autoadd": self.state.auto_replies,
            "autodel": self.state.auto_replies,
            "autofind": self.state.auto_replies,
        }
        if cmd in stores and not arg:
            self.state.pending_ops[(chat_id, sender_id)] = {"op": cmd}
            await self.messenger.safe_send(self.client, chat_id, f"✍️ أرسل الآن العناصر الخاصة بـ **{cmd}**، كل سطر عنصر مستقل.", tag="CMD")
            return
        if cmd in stores and arg:
            await self._handle_command_payload(chat_id, cmd, arg)
            return
        if cmd in ("list", "blklist", "autolist"):
            store = {
                "list": self.state.direct_triggers,
                "blklist": self.state.blocked_phrases,
                "autolist": self.state.auto_replies,
            }[cmd]
            raw_flag = arg.strip().lower() in {"raw", "بدون", "no", "بدون ترقيم"}
            if not store:
                await self.messenger.safe_send(self.client, chat_id, "— لا يوجد —", tag="CMD")
                return
            msg = "\n".join([f"`{s}`" for s in store]) if raw_flag else "\n".join([f"{i + 1}. `{s}`" for i, s in enumerate(store)])
            await self.messenger.safe_send(self.client, chat_id, msg, tag="CMD")
            return

        # ===== Group management =====
        if cmd in ("groupadd", "groupdel", "groupupdate") and not arg:
            self.state.pending_ops[(chat_id, sender_id)] = {"op": cmd}
            await self.messenger.safe_send(self.client, chat_id, f"✍️ أرسل الآن لكل أمر **{cmd}**. في التحديث: سطر1=القديم، سطر2=الجديد.", tag="CMD")
            return
        if cmd in ("groupadd", "groupdel", "groupupdate") and arg:
            await self._handle_legacy_pending(chat_id, cmd, arg)
            return
        if cmd == "grouplist":
            links = await self.group_repo.all()
            raw_flag = arg.strip().lower() in {"raw", "بدون", "no", "بدون ترقيم"}
            msg = "\n".join(links) if raw_flag else "\n".join([f"{i + 1}. {lnk}" for i, lnk in enumerate(links)]) if links else "لا يوجد روابط جروبات مخزنة."
            await self.messenger.safe_send(self.client, chat_id, msg, tag="CMD")
            return
        if cmd == "groupcount":
            links = await self.group_repo.all()
            await self.messenger.safe_send(self.client, chat_id, f"📊 العدد: {len(links)}", tag="CMD")
            return

        if cmd == "usergroups":
            if not arg:
                accs = [f"- {b.phone}" for b in self.state.bots]
                msg = "**الحسابات المتوفرة:**\n" + ("\n".join(accs) if accs else "—") + "\n\n✍️ أرسل رقم الحساب."
                self.state.pending_ops[(chat_id, sender_id)] = {"op": "usergroups"}
                await self.messenger.safe_send(self.client, chat_id, msg, tag="CMD")
                return
            await self._handle_legacy_pending(chat_id, "usergroups", arg)
            return

        if cmd == "usergroups_notin":
            phone = TextUtils.normalize_phone(arg.strip())
            target_bot = self.manager.active_bots.get(phone)
            if not target_bot:
                await self.messenger.safe_send(self.client, chat_id, f"⚠️ الحساب غير موجود أو غير شغال: {phone}", tag="CMD")
                return
            _, not_in = await target_bot.user_groups_status()
            msg = "✅ عضو في كل الجروبات المخزنة!" if not not_in else "❗️الجروبات **غير المنتسب لها**:\n" + "\n".join(not_in)
            await self.messenger.safe_send(self.client, chat_id, msg, tag="CMD")
            return

        if cmd == "joingroups":
            if not arg:
                accs = [f"- {b.phone}" for b in self.state.bots]
                msg = "**الحسابات المتوفرة:**\n" + ("\n".join(accs) if accs else "—") + "\n\n✍️ أرسل: <رقم الجوال> [بداية رقمية]"
                self.state.pending_ops[(chat_id, sender_id)] = {"op": "joingroups"}
                await self.messenger.safe_send(self.client, chat_id, msg, tag="CMD")
                return
            await self._handle_legacy_pending(chat_id, "joingroups", arg)
            return

        if cmd == "stopjoin":
            if not self.state.joining_now:
                await self.messenger.safe_send(self.client, chat_id, "لا توجد عمليات انضمام حالية.", tag="CMD")
                return
            accs = "\n".join(f"- {p}" for p in self.state.joining_now)
            msg = f"**عمليات نشطة:**\n{accs}\n\n✍️ أرسل رقم الحساب لإيقافه أو all لإيقاف الكل."
            self.state.pending_ops[(chat_id, sender_id)] = {"op": "stopjoin"}
            await self.messenger.safe_send(self.client, chat_id, msg, tag="CMD")
            return

        # ===== Help / unblock =====
        if cmd == "help":
            help_text = (
                "✨ **أوامر البوت المطوّرة** ✨\n\n"
                "**الإدارة العامة**\n"
                "• /stats\n• /configshow\n• /cancel\n\n"
                "**إدارة الحسابات**\n"
                "• /accadd\n• /acccode\n• /accpass\n• /acclist\n• /accstart\n• /accstop\n• /accrestart\n• /accmode\n• /acctarget\n• /accsetcmd\n• /accdel\n\n"
                "**الكلمات الأساسية والجروبات المستبعدة**\n"
                "• /kwadd /kwdel /kwlist /kwfind\n"
                "• /exgroupadd /exgroupdel /exgrouplist /exgroupfind\n"
                "• /fallback /fallbackset\n• /cmdgroupset\n\n"
                "**النسخ الاحتياطي**\n"
                "• /dbbackup\n• /dbrestore\n\n"
                "**مخازن النص الحالية**\n"
                "• /add /del /list /find\n"
                "• /blkadd /blkdel /blklist /blkfind\n"
                "• /autoadd /autodel /autolist /autofind\n\n"
                "**الجروبات والانتساب**\n"
                "• /groupadd /groupdel /groupupdate /grouplist /groupcount\n"
                "• /usergroups /usergroups_notin /joingroups /stopjoin\n\n"
                "**السجل والحظر**\n"
                "• /blkuser_add /blkuser_del /blkuser_list /blkuser_find\n"
                "• /autoreplies_count /autoreplies_list /autoreplies_clear\n\n"
                "**ملاحظة:** لم يتم المساس بمنطق التحويل والرد التلقائي؛ الإضافات ركزت على الإدارة والتحكم والنسخ الاحتياطي."
            )
            await self.messenger.safe_send(self.client, chat_id, help_text, tag="CMD")
            return

        if cmd == "unblock":
            for b in self.state.bots:
                asyncio.create_task(self._start_spambot(b.client))
                asyncio.create_task(self._start_spambot(b.client))
            await self.messenger.safe_send(self.client, chat_id, "✓ أرسلنا /start إلى @SpamBot مرتين لكل حساب شغال.", tag="CMD")
            return

    async def _start_spambot(self, cli: TelegramClient) -> None:
        try:
            async with cli.conversation("@SpamBot") as conv:
                await conv.send_message("/start")
                await conv.get_response(timeout=10)
        except Exception:
            pass


# =============== Bot Manager (App) ===============


class BotManager:
    def __init__(self) -> None:
        self.cfg = Config()
        self.db = DB(self.cfg.logger)
        self.state = State()
        self.messenger = Messenger(self.cfg.logger)
        self.group_repo = GroupRepo(self.db)
        self.formatter = MessageFormatter(self.cfg)
        self.fallback = FallbackRouter(self.cfg, self.state, self.messenger, self.formatter, self.cfg.logger)
        self.backup = DbBackupManager(self.db, self.cfg.logger)
        self.active_bots: Dict[str, Bot] = {}
        self.run_tasks: Dict[str, asyncio.Task] = {}
        self.account_me_ids: Dict[str, int] = {}
        self._duplicate_auth_locks: Dict[str, asyncio.Lock] = {}

    def session_name(self, phone: str) -> str:
        digits = re.sub(r"\D+", "", phone)
        return f"session_{digits}"

    def session_files_exist(self, session_name: str) -> bool:
        return Path(session_name + ".session").exists() or Path(session_name).exists()

    def purge_session_files(self, session_name: str) -> None:
        for suffix in (".session", ".session-journal"):
            p = Path(session_name + suffix)
            if p.exists():
                p.unlink(missing_ok=True)

    async def seed_defaults(self) -> None:
        if await self.db.count_rows("keyword_rules") == 0:
            for kw in self.cfg.DEFAULT_KEYWORDS:
                await self.db.seed_keyword_if_missing(kw)
        if await self.db.count_rows("excluded_groups") == 0:
            for gid in self.cfg.DEFAULT_EXCLUDED_GROUPS:
                await self.db.add_excluded_group(gid)
        if await self.db.get_setting("fallback_group_id") is None:
            await self.db.set_setting("fallback_group_id", str(self.cfg.DEFAULT_FALLBACK_GROUP_ID))
        if await self.db.get_setting("command_group_id") is None:
            await self.db.set_setting("command_group_id", str(self.cfg.DEFAULT_COMMAND_GROUP_ID))

    async def sync_env_accounts_to_db(self) -> None:
        existing_cmd = await self.db.get_command_bot_phone()
        env_cmd_phone = None
        env_rows: List[Dict[str, Any]] = []
        for i in range(1, 100):
            aid = os.getenv(f"TELEGRAM_API_ID_{i}")
            ah = os.getenv(f"TELEGRAM_API_HASH_{i}")
            ph = os.getenv(f"TELEGRAM_PHONE_{i}")
            tg = os.getenv(f"TELEGRAM_TARGET_GROUP_ID_{i}")
            md = os.getenv(f"TELEGRAM_MODE_{i}", "both")
            if not all((aid, ah, ph, tg)):
                break
            phone = TextUtils.normalize_phone(ph)
            env_rows.append({
                "api_id": int(aid),
                "api_hash": ah,
                "phone": phone,
                "target_group_id": int(tg),
                "mode": md.lower(),
                "enabled": True,
                "is_command_bot": False,
                "session_name": self.session_name(phone),
            })
            if i == self.cfg.command_bot_index:
                env_cmd_phone = phone
        chosen_cmd = existing_cmd or env_cmd_phone
        for row in env_rows:
            await self.db.upsert_account(
                api_id=row["api_id"],
                api_hash=row["api_hash"],
                phone=row["phone"],
                target_group_id=row["target_group_id"],
                mode=row["mode"] if row["mode"] in self.cfg.VALID_MODES else "both",
                enabled=True,
                is_command_bot=(row["phone"] == chosen_cmd),
                session_name=row["session_name"],
            )
        if not chosen_cmd:
            rows = await self.db.list_accounts()
            if rows:
                await self.db.set_command_bot(rows[0]["phone"])

    async def reload_runtime_state(self) -> None:
        self.state.direct_triggers = await self.db.scalar_table_values("direct_reply_messages", "message_text")
        self.state.blocked_phrases = await self.db.scalar_table_values("blocked_reply_messages", "message_text")
        self.state.auto_replies = await self.db.scalar_table_values("auto_reply_responses", "message_text")
        self.state.blocked_users = await self.db.blocked_users_map()

        kws = await self.db.load_keywords()
        self.cfg.KEYWORDS = kws or list(dict.fromkeys(self.cfg.DEFAULT_KEYWORDS))
        self.cfg.compile_keywords()

        excluded_rows = await self.db.list_excluded_groups()
        self.cfg.EXCLUDED_GROUPS = {int(r["group_id"]) for r in excluded_rows}

        fallback_val = await self.db.get_setting("fallback_group_id")
        command_val = await self.db.get_setting("command_group_id")
        if fallback_val:
            self.cfg.FALLBACK_GROUP_ID = int(fallback_val)
        if command_val:
            self.cfg.COMMAND_GROUP_ID = int(command_val)
        self.fallback.reset_cache()
        await self.refresh_command_runtime_flags()
        self.cfg.logger.info("Loaded runtime settings, stores, blocked users")

    async def refresh_command_runtime_flags(self) -> None:
        cmd_phone = await self.db.get_command_bot_phone()
        self.state.COMMAND_USER_ID = self.account_me_ids.get(cmd_phone) if cmd_phone else None
        for phone, bot in self.active_bots.items():
            bot.is_command_bot = (phone == cmd_phone)

    def _dup_lock(self, phone: str) -> asyncio.Lock:
        lock = self._duplicate_auth_locks.get(phone)
        if lock is None:
            lock = asyncio.Lock()
            self._duplicate_auth_locks[phone] = lock
        return lock

    async def handle_auth_key_duplicated(self, phone: str, where: str = "runtime", exc: Optional[Exception] = None) -> str:
        phone = TextUtils.normalize_phone(phone)
        async with self._dup_lock(phone):
            await self.db.set_account_error(phone, "AuthKeyDuplicatedError")
            await self.db.set_account_enabled(phone, False)
            if phone in self.active_bots:
                await self.unregister_running_account(phone, disconnect=True)

            msg = (
                f"⛔️ تم إيقاف الحساب `{phone}` تلقائيًا لأن جلسة Telethon أصبحت غير صالحة "
                f"بسبب استخدامها من عنواني IP مختلفين في نفس الوقت.\n"
                f"الموضع: `{where}`\n"
                "الحل: أوقف أي تشغيل آخر لنفس الجلسة، ثم احذف/أعد إنشاء الجلسة لهذا الحساب عبر /accadd ثم /acccode وربما /accpass."
            )
            self.cfg.logger.error(f"Duplicated authorization detected for {phone} at {where}: {exc or 'n/a'}")

            notifier = next((b for p, b in self.active_bots.items() if p != phone and b.is_command_bot), None)
            if notifier:
                await self.messenger.safe_send(notifier.client, self.cfg.COMMAND_GROUP_ID, msg, tag="AUTHKEY_DUP")
            return msg

    async def register_running_account(self, record: Dict[str, Any], client: TelegramClient, me_id: int) -> None:
        phone = record["phone"]
        if phone in self.active_bots:
            return
        bot = Bot(
            cfg=self.cfg,
            db=self.db,
            state=self.state,
            messenger=self.messenger,
            group_repo=self.group_repo,
            formatter=self.formatter,
            fallback=self.fallback,
            client=client,
            target_group_id=int(record["target_group_id"]),
            phone=phone,
            mode=str(record["mode"] or "both").lower(),
            manager=self,
            is_command_bot=bool(record["is_command_bot"]),
            logger=self.cfg.logger,
            backup=self.backup,
        )
        self.active_bots[phone] = bot
        self.state.bots.append(bot)
        self.account_me_ids[phone] = me_id
        task = asyncio.create_task(self._client_runner(phone, client))
        self.run_tasks[phone] = task
        await self.refresh_command_runtime_flags()

    async def _client_runner(self, phone: str, client: TelegramClient) -> None:
        try:
            await client.run_until_disconnected()
        except AuthKeyDuplicatedError as exc:
            await self.handle_auth_key_duplicated(phone, where="client_runner", exc=exc)
        except Exception as exc:
            self.cfg.logger.error(f"Client runner error for {phone}: {exc}", exc_info=True)
        finally:
            if self.active_bots.get(phone) and self.active_bots[phone].client is client:
                await self.unregister_running_account(phone, disconnect=False)

    async def unregister_running_account(self, phone: str, disconnect: bool = True) -> None:
        bot = self.active_bots.pop(phone, None)
        if not bot:
            return
        if bot in self.state.bots:
            self.state.bots.remove(bot)
        self.account_me_ids.pop(phone, None)
        self.state.joining_now.pop(phone, None)
        self.state.stop_joining_flags.pop(phone, None)
        self.fallback.reset_cache()
        if disconnect:
            try:
                if bot.client.is_connected():
                    await bot.client.disconnect()
            except Exception:
                pass
        await self.refresh_command_runtime_flags()

    async def start_account(self, phone: str) -> Tuple[bool, str]:
        phone = TextUtils.normalize_phone(phone)
        record = await self.db.get_account(phone)
        if not record:
            return False, f"الحساب غير مسجل: {phone}"
        if phone in self.active_bots:
            return True, f"✅ الحساب شغال بالفعل: {phone}"
        client = TelegramClient(record["session_name"], int(record["api_id"]), record["api_hash"])
        try:
            await client.connect()
            if not await client.is_user_authorized():
                await client.disconnect()
                await self.db.set_account_error(phone, "Session not authorized")
                return False, f"لا توجد جلسة صالحة للحساب {phone}. أعد إضافته عبر /accadd أو أكمل التوثيق."
            me = await client.get_me()
            await self.db.set_account_enabled(phone, True)
            await self.db.set_account_error(phone, "")
            record["enabled"] = 1
            await self.register_running_account(record, client, me.id)
            return True, f"✅ تم تشغيل الحساب {phone}."
        except AuthKeyDuplicatedError as exc:
            await self.db.set_account_error(phone, "AuthKeyDuplicatedError")
            await self.db.set_account_enabled(phone, False)
            return False, f"AuthKeyDuplicatedError للحساب {phone}. أوقف أي تشغيل آخر لنفس الجلسة ثم أعد التوثيق. التفاصيل: {exc}"
        except Exception as exc:
            try:
                await client.disconnect()
            except Exception:
                pass
            await self.db.set_account_error(phone, str(exc))
            return False, f"فشل تشغيل الحساب {phone}: {exc}"

    async def stop_account(self, phone: str, disable: bool = True) -> Tuple[bool, str]:
        phone = TextUtils.normalize_phone(phone)
        record = await self.db.get_account(phone)
        if not record:
            return False, f"الحساب غير مسجل: {phone}"
        if disable:
            await self.db.set_account_enabled(phone, False)
        if phone not in self.active_bots:
            return True, f"✅ الحساب متوقف أصلًا: {phone}"
        extra = ""
        if bool(record["is_command_bot"]):
            others = [p for p in self.active_bots.keys() if p != phone]
            if others:
                await self.promote_command_bot(others[0])
                extra = f" وتم تحويل command bot إلى {others[0]}"
            else:
                extra = " ولا يوجد حاليًا command bot نشط حتى تشغيل حساب آخر."
        await self.unregister_running_account(phone, disconnect=True)
        return True, f"✅ تم إيقاف الحساب {phone}.{extra}"

    async def restart_account(self, phone: str) -> Tuple[bool, str]:
        phone = TextUtils.normalize_phone(phone)
        record = await self.db.get_account(phone)
        if not record:
            return False, f"الحساب غير مسجل: {phone}"
        await self.stop_account(phone, disable=False)
        return await self.start_account(phone)

    async def update_account_mode(self, phone: str, mode: str) -> Tuple[bool, str]:
        phone = TextUtils.normalize_phone(phone)
        mode = mode.lower()
        if mode not in self.cfg.VALID_MODES:
            return False, f"المود غير صالح. المتاح: {', '.join(sorted(self.cfg.VALID_MODES))}"
        if not await self.db.get_account(phone):
            return False, f"الحساب غير مسجل: {phone}"
        await self.db.set_account_mode(phone, mode)
        if phone in self.active_bots:
            self.active_bots[phone].mode = mode
        return True, f"✅ تم تحديث مود الحساب {phone} إلى `{mode}`"

    async def update_account_target(self, phone: str, target_group_id: int) -> Tuple[bool, str]:
        phone = TextUtils.normalize_phone(phone)
        if not await self.db.get_account(phone):
            return False, f"الحساب غير مسجل: {phone}"
        await self.db.set_account_target_group(phone, target_group_id)
        if phone in self.active_bots:
            self.active_bots[phone].target_group_id = target_group_id
        return True, f"✅ تم تحديث target_group_id للحساب {phone} إلى `{target_group_id}`"

    async def promote_command_bot(self, phone: str) -> Tuple[bool, str]:
        phone = TextUtils.normalize_phone(phone)
        record = await self.db.get_account(phone)
        if not record:
            return False, f"الحساب غير مسجل: {phone}"
        await self.db.set_command_bot(phone)
        await self.refresh_command_runtime_flags()
        if phone in self.active_bots:
            return True, f"✅ تم تعيين {phone} كـ command bot نشط."
        return True, f"✅ تم تعيين {phone} كـ command bot، وسيُفعّل عند تشغيل الحساب."

    async def delete_account(self, phone: str, purge_session: bool = False) -> Tuple[bool, str]:
        phone = TextUtils.normalize_phone(phone)
        record = await self.db.get_account(phone)
        if not record:
            return False, f"الحساب غير مسجل: {phone}"
        pending = self.state.pending_auth.pop(phone, None)
        if pending:
            try:
                await pending.client.disconnect()
            except Exception:
                pass
        await self.stop_account(phone, disable=True)
        deleted = await self.db.delete_account(phone)
        if purge_session:
            self.purge_session_files(record["session_name"])
        await self.refresh_command_runtime_flags()
        if deleted:
            return True, f"✅ تم حذف الحساب {phone}" + (" مع ملفات الجلسة." if purge_session else ".")
        return False, f"تعذر حذف الحساب {phone}"

    async def complete_account_login(self, pending: PendingAuth) -> None:
        if pending.is_command_bot:
            await self.db.set_command_bot(pending.phone)
        await self.db.upsert_account(
            api_id=pending.api_id,
            api_hash=pending.api_hash,
            phone=pending.phone,
            target_group_id=pending.target_group_id,
            mode=pending.mode,
            enabled=True,
            is_command_bot=pending.is_command_bot,
            session_name=pending.session_name,
            last_error="",
        )
        me = await pending.client.get_me()
        record = await self.db.get_account(pending.phone)
        if record is None:
            raise RuntimeError("تعذر حفظ الحساب في قاعدة البيانات")
        await self.register_running_account(record, pending.client, me.id)

    async def restore_backup(self, in_path: str) -> Dict[str, Dict[str, int]]:
        summary = await self.backup.import_backup(in_path)
        await self.reload_runtime_state()
        rows = await self.db.list_accounts()
        for row in rows:
            phone = row["phone"]
            if int(row["enabled"]) and phone not in self.active_bots and self.session_files_exist(row["session_name"]):
                await self.start_account(phone)
        return summary

    async def start_enabled_accounts(self) -> None:
        rows = await self.db.list_accounts()
        for row in rows:
            if int(row["enabled"]):
                ok, msg = await self.start_account(row["phone"])
                self.cfg.logger.info(msg)
                if not ok:
                    self.cfg.logger.warning(msg)

    async def start(self) -> None:
        await self.db.init()
        await self.sync_env_accounts_to_db()
        await self.seed_defaults()
        await self.reload_runtime_state()
        await self.start_enabled_accounts()
        self.cfg.logger.info("Bot manager started")
        await asyncio.Event().wait()


# =============== Entry ===============


if __name__ == "__main__":
    try:
        asyncio.run(BotManager().start())
    except Exception:
        AppLogger.build().exception("❌ Fatal error in main()", exc_info=True)
        raise


# ===== Compatibility aliases for existing external references =====
DB.load_table = lambda self, name: self.scalar_table_values(name, "message_text") if name != "join_groups" else self.scalar_table_values(name, "group_link")
DB.insert_table = lambda self, name, text: self.insert_scalar_value(name, "message_text", text)
DB.delete_table = lambda self, name, text: self.delete_scalar_value(name, "message_text", text)


# Preserve alias names expected by the old code paths
async def _compat_join_groups_load(db: DB) -> List[str]:
    return await db.scalar_table_values("join_groups", "group_link")

GroupRepo.load_all = GroupRepo.all


# Telethon / runtime note:
# command handling, account start/stop, backup merge restore, keywords, excluded groups,
# fallback group, and command group are now DB-backed and runtime-refreshable.

