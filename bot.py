import os
import re
import sys
import asyncio
import logging
import unicodedata
from typing import Any, Dict, List, Optional, Set, Tuple

import aiomysql
from dotenv import load_dotenv
from logging.handlers import RotatingFileHandler
from urllib.parse import urlparse

from rapidfuzz import fuzz
import telethon
from telethon import events, types, Button
from telethon.errors import (
    FloodWaitError, UserIsBlockedError, MessageTooLongError, UserAlreadyParticipantError
)
from telethon.tl.functions.channels import JoinChannelRequest, GetParticipantRequest
from telethon.tl.types import (
    ChannelParticipant, ChannelParticipantSelf, ChannelParticipantAdmin, ChannelParticipantCreator,
    InputPeerUser, User
)
from telethon import TelegramClient

# ==== Backup/Restore & utils ====
import json
import gzip
import datetime
from pathlib import Path
# ==================================

# إضافات جديدة
import hashlib


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
    def __init__(self) -> None:
        load_dotenv()
        self.logger = AppLogger.build()

        self.EXCLUDED_GROUPS: Set[int] = {
            -1002272546210, -1002405645012, -1002353780992, -1002311800895
        }
        self.FALLBACK_GROUP_ID: int = int(os.getenv("FALLBACK_GROUP_ID", "-1002353780992"))
        self.COMMAND_GROUP_ID: int = int(os.getenv("COMMAND_GROUP_ID", "-1002311800895"))

        self.COMMANDS = {
            "help", "unblock",
            "add", "del", "list", "find",
            "blkadd", "blkdel", "blklist", "blkfind",
            "autoadd", "autodel", "autolist", "autofind",
            "groupadd", "groupdel", "groupupdate", "grouplist", "joingroups",
            "stopjoin", "groupcount", "usergroups", "usergroups_notin",
            "dbbackup", "dbrestore",
            # blocked users & auto-replies log
            "blkuser_add", "blkuser_del", "blkuser_list", "blkuser_find",
            "autoreplies_count", "autoreplies_list", "autoreplies_clear",
            # unified counters
            "stats",
        }

        self.KEYWORDS = [
            "ابي مساعده", "يسوي", "يحل", "خصوصي", "شاطر", "تحل", "تسوي", "يعرف", "تعرف", "واجب", "بروجكت",
            "فاهم", "سكليف", "بحث", "مشروع", "يساعد", "اسايمنت",
            "ابي مساعده", "ابغى مساعده", "ابغا مساعده", "محتاج مساعده", "حد يساعدني", "احد يساعدني",
            "ابي مساعده", "ابغى مساعده", "ابغا مساعده", "محتاج مساعده", "حد يساعدني", "احد يساعدني",
            "ابي حد يحضر عني", "ابغا حد يحضر عني", "يحضر عني", "يحظر", "يحضر",
            "عندي اختبار",
            "احد عنده خصوصي", "احد يعرف مختص",
            "اسايمنت", "بروجكت", "مشروع", "س ك ل ي ف", "case study", "كيس ستدي",
            "بوربوينت", "بووربوينت", "عذر طبي", "اجازة مرضية",
        ]
        self.KW_RE = re.compile("|".join(map(re.escape, self.KEYWORDS)), re.IGNORECASE)

        self.LINK_RE = re.compile(r"(https://t\.me/(?:c/)?(?:\d+|[A-Za-z0-9_]+)/?\d*)(?:\?comment=\d+)?")

        self.command_bot_index = int(os.getenv("COMMAND_BOT_INDEX", "2"))  # default 2nd account owns commands


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
            host=url.hostname, port=url.port or 3306,
            user=url.username, password=url.password,
            db=url.path.lstrip("/"),
            autocommit=True,
            charset="utf8mb4",
            cursorclass=aiomysql.DictCursor,
            pool_recycle=300
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

                # ====== ترقية السكيمة المطلوبة: dedupe_key + src_chat_id + src_msg_id + فهرس فريد ======
                # قد لا يدعم بعض الإصدارات IF NOT EXISTS، لذلك نحوط بـ try/except
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
                # محاولة إنشاء الفهرس الفريد
                try:
                    await cur.execute("CREATE UNIQUE INDEX uq_auto_user_dedupe ON auto_reply_log (user_id, dedupe_key)")
                except Exception:
                    pass

                await cur.execute("SET sql_notes=1")
        self.logger.info("Database initialized / upgraded")

    async def load_table(self, name: str) -> List[str]:
        assert self.pool
        async with self.pool.acquire() as conn:
            await conn.ping(reconnect=True)
            async with conn.cursor() as cur:
                await cur.execute(f"SELECT message_text FROM {name}")
                rows = await cur.fetchall()
                return [r["message_text"] for r in rows]

    async def insert_table(self, name: str, text: str) -> None:
        assert self.pool
        async with self.pool.acquire() as conn:
            await conn.ping(reconnect=True)
            async with conn.cursor() as cur:
                await cur.execute(f"INSERT INTO {name}(message_text) VALUES(%s)", (text,))

    async def delete_table(self, name: str, text: str) -> None:
        assert self.pool
        async with self.pool.acquire() as conn:
            await conn.ping(reconnect=True)
            async with conn.cursor() as cur:
                await cur.execute(f"DELETE FROM {name} WHERE message_text=%s", (text,))

    # ===== Blocked users & auto replies log =====

    async def blocked_users_map(self) -> Dict[int, Tuple[str, str]]:
        assert self.pool
        async with self.pool.acquire() as conn:
            await conn.ping(reconnect=True)
            async with conn.cursor() as cur:
                await cur.execute("""
                    SELECT user_id, COALESCE(username,'' ) AS username, COALESCE(display_name,'') AS display_name
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
                    VALUES (%s, %s, %s, NOW()) AS new
                    ON DUPLICATE KEY UPDATE
                        username = new.username,
                        display_name = new.display_name
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

    async def count_auto_replies(self, user_id: int) -> int:
        assert self.pool
        async with self.pool.acquire() as conn:
            await conn.ping(reconnect=True)
            async with conn.cursor() as cur:
                await cur.execute("SELECT COUNT(*) AS c FROM auto_reply_log WHERE user_id=%s", (user_id,))
                row = await cur.fetchone()
                return int(row["c"] if row else 0)

    # === جديد: عدّاد مميز ضمن نافذة زمنية ===
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

    # === محدثة: تسجل بنية الرد مع dedupe_key وتتفادى التكرار ===
    async def log_auto_reply_pending(self,
                                    user_id: int,
                                    username: Optional[str],
                                    display_name: Optional[str],
                                    dedupe_key: str,
                                    src_chat_id: Optional[int],
                                    src_msg_id: Optional[int]) -> int:
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
        sets = []
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

    # ===== unified counters =====
    async def count_rows(self, table: str) -> int:
        assert self.pool
        async with self.pool.acquire() as conn:
            await conn.ping(reconnect=True)
            async with conn.cursor() as cur:
                await cur.execute(f"SELECT COUNT(*) AS c FROM {table}")
                row = await cur.fetchone()
                return int(row["c"] if row else 0)

    async def get_stats(self) -> Dict[str, int]:
        direct = await self.count_rows("direct_reply_messages")
        blocked_text = await self.count_rows("blocked_reply_messages")
        blocked_users = await self.count_rows("blocked_users")
        groups = await self.count_rows("join_groups")
        return {
            "direct": direct,
            "blocked_text": blocked_text,
            "blocked_users": blocked_users,
            "groups": groups,
        }


# =============== Backup / Restore Manager ===============

class DbBackupManager:
    TABLES = {
        "direct_reply_messages": "message_text",
        "blocked_reply_messages": "message_text",
        "auto_reply_responses": "message_text",
        "join_groups": "group_link",
        "blocked_users": None,  # حفظ كامل
    }

    def __init__(self, db: DB, logger: logging.Logger):
        self.db = db
        self.logger = logger

    async def export_json_gz(self, out_path: str):
        assert self.db.pool
        payload = {
            "meta": {"version": 2, "created_at": datetime.datetime.utcnow().isoformat() + "Z"},
            "tables": {}
        }
        async with self.db.pool.acquire() as conn:
            async with self.db.pool.acquire() as conn2:  # نفس الاتصال غير ضروري، لكنه لا يضر
                async with conn.cursor() as cur:
                    for table, col in self.TABLES.items():
                        if col:
                            await cur.execute(f"SELECT {col} FROM {table}")
                            rows = await cur.fetchall()
                            payload["tables"][table] = [r[col] for r in rows]
                        else:
                            await cur.execute(f"SELECT * FROM {table}")
                            payload["tables"][table] = await cur.fetchall()
        Path(out_path).parent.mkdir(parents=True, exist_ok=True)
        with gzip.open(out_path, "wt", encoding="utf-8") as f:
            json.dump(payload, f, ensure_ascii=False)
        self.logger.info(f"Backup exported → {out_path}")

    async def import_json_gz(self, in_path: str):
        assert self.db.pool
        with gzip.open(in_path, "rt", encoding="utf-8") as f:
            data = json.load(f)

        tables = data.get("tables", {})
        async with self.db.pool.acquire() as conn:
            await conn.begin()
            try:
                async with conn.cursor() as cur:
                    for table, col in self.TABLES.items():
                        rows = tables.get(table, [])
                        if not rows:
                            continue
                        if col:
                            await cur.execute(f"SELECT {col} FROM {table}")
                            existing = {r[col] for r in await cur.fetchall()}
                            to_insert = [r for r in rows if r not in existing]
                            if not to_insert:
                                continue
                            BATCH = 1000
                            for i in range(0, len(to_insert), BATCH):
                                batch = [(val,) for val in to_insert[i:i+BATCH]]
                                await cur.executemany(
                                    f"INSERT INTO {table}({col}) VALUES(%s)",
                                    batch
                                )
                        else:
                            if table == "blocked_users":
                                for r in rows:
                                    await cur.execute("""
                                        INSERT INTO blocked_users (user_id, username, display_name, created_at)
                                        VALUES (%s, %s, %s, %s) AS new
                                        ON DUPLICATE KEY UPDATE
                                            username = new.username,
                                            display_name = new.display_name
                                    """, (
                                        r.get("user_id"),
                                        r.get("username", "") or "",
                                        r.get("display_name", "") or "",
                                        r.get("created_at") or datetime.datetime.utcnow().isoformat()
                                    ))
                await conn.commit()
                self.logger.info(f"Restore (merge) finished from {in_path}")
            except Exception as e:
                await conn.rollback()
                self.logger.error(f"Restore failed, rolled back. {e}", exc_info=True)
                raise


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
        return [text[i:i+chunk] for i in range(0, len(text), chunk)]

    # === جديد: مولد بصمة عدم التكرار للمفتاح ===
    @staticmethod
    def make_dedupe_key(key: Tuple[Any, ...]) -> str:
        raw = "|".join(map(str, key))
        return hashlib.sha1(raw.encode("utf-8")).hexdigest()


class Messenger:
    def __init__(self, logger: logging.Logger) -> None:
        self.logger = logger

    async def safe_send(
        self, client: TelegramClient, dst: int | types.User | types.InputPeerUser, text: str,
        tag: str = "SEND", buttons=None
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
                self.logger.warning(f"{tag} ⏳{e.seconds}s")
                await asyncio.sleep(delay)
                delay *= 2
            except MessageTooLongError:
                for part in TextUtils.split_long(text, 4000):
                    await client.send_message(dst, part, parse_mode="Markdown")
                return None
            except UserIsBlockedError:
                self.logger.warning(f"{tag} 🚫 blocked by {dst}")
                return None
            except Exception as ex:
                self.logger.error(f"{tag} error: {ex}", exc_info=True)
                return None
        return None

    async def safe_send_file(
        self, client: TelegramClient, dst: int | types.User | types.InputPeerUser, file_path: str,
        caption: str = "", tag: str = "FILE"
    ) -> Optional[types.Message]:
        delay = 1
        for _ in range(3):
            try:
                return await client.send_file(dst, file_path, caption=caption, force_document=True)
            except FloodWaitError as e:
                self.logger.warning(f"{tag} ⏳{e.seconds}s")
                await asyncio.sleep(delay)
                delay *= 2
            except UserIsBlockedError:
                self.logger.warning(f"{tag} 🚫 blocked by {dst}")
                return None
            except Exception as ex:
                self.logger.error(f"{tag} error: {ex}", exc_info=True)
                return None
        return None


# =============== Group Repo ===============

class GroupRepo:
    def __init__(self, db: DB) -> None:
        self.db = db

    async def add(self, link: str) -> None:
        assert self.db.pool
        async with self.db.pool.acquire() as conn:
            async with conn.cursor() as cur:
                await cur.execute(
                    "INSERT IGNORE INTO join_groups (group_link) VALUES (%s)", (link,)
                )

    async def delete(self, link: str) -> None:
        assert self.db.pool
        async with self.db.pool.acquire() as conn:
            async with conn.cursor() as cur:
                await cur.execute("DELETE FROM join_groups WHERE group_link = %s", (link,))

    async def update(self, old_link: str, new_link: str) -> None:
        assert self.db.pool
        async with self.db.pool.acquire() as conn:
            async with conn.cursor() as cur:
                await cur.execute(
                    "UPDATE join_groups SET group_link = %s WHERE group_link = %s", (new_link, old_link)
                )

    async def all(self) -> List[str]:
        assert self.db.pool
        async with self.db.pool.acquire() as conn:
            async with conn.cursor() as cur:
                await cur.execute("SELECT group_link FROM join_groups")
                rows = await cur.fetchall()
                return [r["group_link"] for r in rows]


# =============== Message Formatting ===============

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
            elif chat.id == ev.peer_id:
                group_line = "📍 **المحادثة:** محادثة خاصة"
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

        self.pending_ops: Dict[Tuple[int, int], Tuple[str, str]] = {}
        self.COMMAND_USER_ID: Optional[int] = None

        self.stop_joining_flags: Dict[str, bool] = {}
        self.joining_now: Dict[str, asyncio.Task] = {}

        self._fallback_entity_cache: Dict[int, Any] = {}
        self._fallback_member_cache: Dict[int, bool] = {}
        self.REPLY_LOCKS: Dict[Tuple[Any, ...], asyncio.Lock] = {}

        self.blocked_users: Dict[int, Tuple[str, str]] = {}  # user_id -> (username, display_name)

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
                (ChannelParticipant, ChannelParticipantSelf, ChannelParticipantAdmin, ChannelParticipantCreator)
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
        is_command_bot: bool = False,
        logger: Optional[logging.Logger] = None,
        backup: Optional["DbBackupManager"] = None,
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

        client.add_event_handler(self.on_message, events.NewMessage(incoming=True))
        client.add_event_handler(self.on_message, events.NewMessage(outgoing=True))

        if self.is_command_bot:
            pat = r'(?i)^/(?:' + "|".join(self.cfg.COMMANDS) + r')\b'
            client.add_event_handler(self.on_command, events.NewMessage(incoming=True, pattern=pat, chats=[self.cfg.COMMAND_GROUP_ID]))
            client.add_event_handler(self.on_command, events.NewMessage(outgoing=True, pattern=pat, chats=[self.cfg.COMMAND_GROUP_ID]))

    # ===== حل كيان المستخدم بين الحسابات =====
    async def _resolve_target_peer(self, target_client: TelegramClient, sender: types.User) -> Optional[Any]:
        try:
            if target_client is self.client:
                return sender
            if isinstance(sender, User) and getattr(sender, "access_hash", None):
                return InputPeerUser(sender.id, sender.access_hash)
            username = getattr(sender, "username", None)
            if username:
                ent = await target_client.get_entity(username)
                return ent
            return await target_client.get_entity(sender.id)
        except Exception:
            return None

    async def join_sleep(self, phone: str, seconds: int) -> None:
        for _ in range(seconds):
            if self.state.stop_joining_flags.get(phone):
                break
            await asyncio.sleep(1)

    async def join_groups_with_account(self, start_index: int = 0) -> None:
        links = await self.group_repo.all()
        joined = 0
        total = len(links)
        await self.messenger.safe_send(self.client, self.cfg.COMMAND_GROUP_ID, f"🚀 بدء انضمام الحساب {self.phone} من {start_index+1} إلى {total}...", tag="JOIN_GROUPS")
        for idx, link in enumerate(links[start_index:], start=start_index):
            if self.state.stop_joining_flags.get(self.phone):
                await self.messenger.safe_send(self.client, self.cfg.COMMAND_GROUP_ID, f"⏹ إيقاف يدوي عند {idx+1}/{total}.", tag="JOIN_GROUPS")
                return
            try:
                entity = await self.client.get_entity(link)
                await self.client(JoinChannelRequest(entity))
                joined += 1
                await self.messenger.safe_send(self.client, self.cfg.COMMAND_GROUP_ID, f"✅ [{idx+1}/{total}] انضم: {link}", tag="JOIN_GROUPS")
                await self.join_sleep(self.phone, 250)
            except UserAlreadyParticipantError:
                await self.messenger.safe_send(self.client, self.cfg.COMMAND_GROUP_ID, f"ℹ️ [{idx+1}/{total}] عضو مسبقاً: {link}", tag="JOIN_GROUPS")
            except FloodWaitError as e:
                await self.messenger.safe_send(self.client, self.cfg.COMMAND_GROUP_ID, f"⏸ توقف {e.seconds}s عند [{idx+1}/{total}].", tag="JOIN_GROUPS")
                await self.join_sleep(self.phone, e.seconds + 2)
                if self.state.stop_joining_flags.get(self.phone):
                    await self.messenger.safe_send(self.client, self.cfg.COMMAND_GROUP_ID, "⏹ تم الإيقاف أثناء الانتظار.", tag="JOIN_GROUPS")
                    return
                await self.messenger.safe_send(self.client, self.cfg.COMMAND_GROUP_ID, "✅ استئناف.", tag="JOIN_GROUPS")
            except Exception as ex:
                await self.messenger.safe_send(self.client, self.cfg.COMMAND_GROUP_ID, f"❌ [{idx+1}/{total}] فشل: {link}\n{ex}", tag="JOIN_GROUPS")
        await self.messenger.safe_send(self.client, self.cfg.COMMAND_GROUP_ID, f"🏁 {self.phone}: انضم {joined}/{total}.", tag="JOIN_GROUPS")

    async def user_groups_status(self) -> Tuple[List[str], List[str]]:
        links = await self.group_repo.all()
        in_groups, not_in = [], []
        me = await self.client.get_me()
        for link in links:
            try:
                entity = await self.client.get_entity(link)
                result = await self.client(GetParticipantRequest(entity, me.id))
                participant = result.participant
                if isinstance(participant, (ChannelParticipant, ChannelParticipantSelf, ChannelParticipantAdmin, ChannelParticipantCreator)):
                    in_groups.append(link)
                else:
                    not_in.append(link)
            except Exception:
                not_in.append(link)
        return in_groups, not_in

    # ---------- unified dispatch ----------

    async def unified_dispatch(self, ev: events.NewMessage.Event) -> None:
        key = self.formatter.message_key(ev)
        text = ev.message.message or ""

        # Forward once إلى مجموعات forward/both — هذا لا يؤثر على الحظر
        if self.cfg.KW_RE.search(text) and key not in self.state.FORWARD_DONE:
            self.state.FORWARD_DONE.add(key)
            fwd_txt = await self.formatter.build_forward_text(ev)
            await asyncio.gather(*[
                self.messenger.safe_send(b.client, b.target_group_id, fwd_txt, tag=f"FWD⌁{b.phone}")
                for b in self.state.bots if b.mode in ("forward", "both")
            ], return_exceptions=True)

        # find src bot
        src_bot = next((b for b in self.state.bots if b.client is ev.client), None)
        if not src_bot:
            return

        # حظر مسبق/عداد محلي
        sender = await ev.get_sender()
        sender_id = getattr(sender, "id", None)
        if sender_id and sender_id in self.state.blocked_users:
            return  # محظور = تجاهل

        # قرار الرد (كما هو)
        wants_reply = (
            self.cfg.KW_RE.search(text)
            and any(TextUtils.fuzzy_match(text, trg) for trg in self.state.direct_triggers)
            and TextUtils.normalize_text(text) not in {TextUtils.normalize_text(p) for p in self.state.blocked_phrases}
        )
        if not wants_reply:
            return

        # ======== الإصلاح: قفل مبكر على مفتاح الرسالة ========
        lock = self.state.get_reply_lock(key)
        async with lock:
            # منع التكرار داخل العملية
            if key in self.state.REPLY_DONE:
                return

            dedupe_key = TextUtils.make_dedupe_key(key)

            # سجّل نية الرد لمرة واحدة فقط (مع dedupe_key لتفادي التكرار عبر السيرفرات)
            pending_log_id = 0
            try:
                uname = getattr(sender, "username", "") or ""
                disp_name = f"{(getattr(sender, 'first_name', '') or '').strip()} {(getattr(sender, 'last_name','') or '').strip()}".strip()
                src_chat_id = ev.chat_id
                src_msg_id = ev.message.id
                pending_log_id = await self.db.log_auto_reply_pending(
                    sender_id or 0, uname, disp_name, dedupe_key, src_chat_id, src_msg_id
                )
            except Exception as ex:
                self.logger.warning(f"log_auto_reply_pending failed: {ex}")

            # الحظر العادل: DISTINCT خلال آخر 24 ساعة
            THRESHOLD = 4
            if sender_id:
                try:
                    prev = await self.db.count_auto_replies_distinct(sender_id, hours=24)
                    if prev >= THRESHOLD:
                        uname = getattr(sender, "username", "") or ""
                        disp_name = f"{(getattr(sender, 'first_name', '') or '').strip()} {(getattr(sender, 'last_name','') or '').strip()}".strip()
                        await self.db.add_blocked_user(sender_id, uname, disp_name)
                        self.state.blocked_users[sender_id] = (uname, disp_name)
                        self.state.REPLY_DONE.add(key)  # حتى لا تتكرر محليًا
                        return
                except Exception as ex:
                    self.logger.warning(f"auto-replies threshold check failed: {ex}")

            # من هذه النقطة مسموح برد واحد فقط لهذه الرسالة
            self.state.REPLY_DONE.add(key)

            # pre-forward للفولباك (كما لديك)
            await self.fallback.forward_any(ev)

            # الرد من أي حساب reply/both
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

                sent_orig = await self.messenger.safe_send(b.client, tgt, text, tag=f"ORIG_REPLY⌁{b.phone}")
                if not sent_orig:
                    continue
                sent_auto = await self.messenger.safe_send(b.client, tgt, self.state.next_auto_reply(), tag=f"AUTO_REPLY⌁{b.phone}")
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

    # ---------- handlers ----------

    async def on_message(self, ev: events.NewMessage.Event) -> None:
        chat_id, sender_id = ev.chat_id, ev.message.sender_id
        text = ev.message.message or ""

        # أوامر تفاعلية معلّقة
        key = (chat_id, sender_id)
        if key in self.state.pending_ops:
            if chat_id == self.cfg.COMMAND_GROUP_ID and not self.is_command_bot:
                return
            op, table = self.state.pending_ops.pop(key)
            lines = [l.strip() for l in text.splitlines() if l.strip()]

            if op == "stopjoin":
                choice = lines[0].strip().lower()
                if choice == "all":
                    for phone in list(self.state.joining_now):
                        self.state.stop_joining_flags[phone] = True
                    await self.messenger.safe_send(self.client, chat_id, "⏹ تم إيقاف **كل** عمليات الانضمام الجارية.", tag="CMD")
                else:
                    if choice in self.state.joining_now:
                        self.state.stop_joining_flags[choice] = True
                        await self.messenger.safe_send(self.client, chat_id, f"⏹ أوقفنا {choice}.", tag="CMD")
                    else:
                        await self.messenger.safe_send(self.client, chat_id, f"⚠️ لا توجد عملية نشطة للحساب: {choice}", tag="CMD")
                return

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
                    return
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
                    return
                if op == "groupupdate":
                    if len(lines) == 2:
                        old_link, new_link = lines
                        await self.group_repo.update(old_link, new_link)
                        await self.messenger.safe_send(self.client, chat_id, f"تم التحديث:\n{old_link} → {new_link}", tag="CMD")
                    else:
                        await self.messenger.safe_send(self.client, chat_id, "⚠️ أرسل **سطرين**: الرابط القديم ثم الجديد.", tag="CMD")
                    return

            if op == "joingroups":
                parts = lines[0].split()
                phone = parts[0]
                start_index = int(parts[1]) - 1 if len(parts) > 1 and parts[1].isdigit() else 0
                target_bot = next((b for b in self.state.bots if b.phone == phone), None)
                if not target_bot:
                    await self.messenger.safe_send(self.client, chat_id, f"⚠️ لا يوجد حساب برقم: {phone}", tag="CMD")
                    return
                t = asyncio.create_task(target_bot.join_groups_with_account(start_index))
                self.state.joining_now[phone] = t
                self.state.stop_joining_flags[phone] = False
                await self.messenger.safe_send(self.client, chat_id, f"⏳ بدأنا انضمام {phone} من رقم {start_index+1}.", tag="JOIN_GROUPS")
                return

            if op == "usergroups":
                phone = lines[0]
                target_bot = next((b for b in self.state.bots if b.phone == phone), None)
                if not target_bot:
                    await self.messenger.safe_send(self.client, chat_id, f"⚠️ الحساب غير موجود: {phone}", tag="CMD")
                    return
                in_g, not_in = await target_bot.user_groups_status()
                msg = (
                    f"🔢 **{phone} عضو في {len(in_g)} من {len(in_g)+len(not_in)} جروب.**\n"
                    f"❌ خارجها: {len(not_in)}\n"
                    f"اكتب /usergroups_notin {phone} لعرض الروابط غير المنتسب لها."
                )
                await self.messenger.safe_send(self.client, chat_id, msg, tag="CMD")
                return

            stores_map = {
                "direct_reply_messages": self.state.direct_triggers,
                "blocked_reply_messages": self.state.blocked_phrases,
                "auto_reply_responses": self.state.auto_replies
            }
            if op in ("add", "blkadd", "autoadd"):
                store = stores_map[table]
                res = []
                for line in lines:
                    if line not in store:
                        await self.db.insert_table(table, line)
                        store.append(line)
                        res.append(f"✓ أضفنا: {line}")
                    else:
                        res.append(f"⚠️ موجود: {line}")
                await self.messenger.safe_send(self.client, chat_id, "نتيجة الإضافة:\n" + "\n".join(res), tag="CMD")
                return
            if op in ("del", "blkdel", "autodel"):
                store = stores_map[table]
                res = []
                for line in lines:
                    if line in store:
                        await self.db.delete_table(table, line)
                        store.remove(line)
                        res.append(f"✓ حذفنا: {line}")
                    else:
                        res.append(f"⚠️ غير موجود: {line}")
                await self.messenger.safe_send(self.client, chat_id, "نتيجة الحذف:\n" + "\n".join(res), tag="CMD")
                return
            if op in ("find", "blkfind", "autofind"):
                store = stores_map[table]
                patterns = [l.strip() for l in lines if l.strip()]
                thresh = 100 if op.startswith("blk") else 80
                if not patterns:
                    await self.messenger.safe_send(self.client, chat_id, "— لم يتم إدخال أي نمط بحث —", tag="CMD")
                    return
                msg_lines = []
                for pat in patterns:
                    matches = [m for m in store if fuzz.ratio(TextUtils.normalize_text(pat), TextUtils.normalize_text(m)) >= thresh]
                    if matches:
                        msg_lines.append(f"🔎 **نتائج `{pat}`:**")
                        for m in matches:
                            msg_lines.append(f"```\n{m}\n```")
                    else:
                        msg_lines.append(f"🔎 **نتائج `{pat}`:**\n— لا توجد مطابقات —")
                await self.messenger.safe_send(self.client, chat_id, "\n".join(msg_lines), tag="CMD")
                return

        # ignore filters
        if text.startswith("✉") or ev.is_private or ev.out or chat_id in self.cfg.EXCLUDED_GROUPS:
            return
        if any([re.search(r"@\w{5,}", text), re.search(r"https?://\S+", text),
                len(text.split()) > 17, re.search(r"\d", text)]):
            return
        sender = ev.message.sender
        if getattr(sender, "bot", False):
            return
        try:
            part = await ev.client.get_participant(chat_id, sender_id)
            if isinstance(part, (ChannelParticipantAdmin, ChannelParticipantCreator)):
                return
        except:
            pass

        if self.mode == "self":
            if self.cfg.KW_RE.search(text):
                fwd = await self.formatter.build_forward_text(ev)
                await self.messenger.safe_send(self.client, self.target_group_id, fwd, tag="SELFFWD")
            await self.unified_dispatch(ev)
        else:
            await self.unified_dispatch(ev)

    # ---------- commands ----------

    async def on_command(self, ev: events.NewMessage.Event) -> None:
        chat_id, sender_id = ev.chat_id, ev.message.sender_id
        raw = ev.message.message.strip()
        parts = raw.split(maxsplit=1)
        cmd = parts[0].lstrip("/").lower()
        arg = parts[1] if len(parts) > 1 else ""

        if self.state.COMMAND_USER_ID and sender_id != self.state.COMMAND_USER_ID:
            await self.messenger.safe_send(self.client, chat_id, "⚠️ غير مصرح.", tag="CMD")
            return
        if cmd not in self.cfg.COMMANDS:
            await self.messenger.safe_send(self.client, chat_id, "⚠️ أمر غير معروف. اكتب /help.", tag="CMD")
            return

        # ===== DB BACKUP / RESTORE =====
        if cmd == "dbbackup":
            ts = datetime.datetime.utcnow().strftime("%Y%m%d_%H%M%S")
            out_path = f"backups/db_{ts}.json.gz"
            try:
                await self.messenger.safe_send(self.client, chat_id, "⏳ نُنشئ نسخة احتياطية الآن...", tag="CMD")
                await self.backup.export_json_gz(out_path)
                caption = f"✅ تم الإنشاء.\nالملف: `{out_path}`"
                await self.messenger.safe_send_file(self.client, chat_id, out_path, caption=caption, tag="DBFILE")
            except Exception as e:
                await self.messenger.safe_send(self.client, chat_id, f"❌ فشل النسخ الاحتياطي:\n`{e}`", tag="CMD")
            return

        if cmd == "dbrestore":
            tmp_path = None
            try:
                if ev.is_reply:
                    rep = await ev.get_reply_message()
                    if rep and rep.document:
                        await self.messenger.safe_send(self.client, chat_id, "⏳ ننزّل ملف النسخة...", tag="CMD")
                        Path("backups").mkdir(parents=True, exist_ok=True)
                        tmp_path = await self.client.download_media(rep, file="backups/")
                        if not tmp_path or not tmp_path.endswith(".gz"):
                            await self.messenger.safe_send(self.client, chat_id, "❌ الملف المرفق ليس gzip.", tag="CMD")
                            return
                        in_path = tmp_path
                    else:
                        await self.messenger.safe_send(self.client, chat_id, "ℹ️ أرسل ملف النسخة كـ **رد** أو سنستخدم أحدث نسخة محليًا.", tag="CMD")
                        in_path = None
                else:
                    in_path = None

                if not in_path:
                    Path("backups").mkdir(parents=True, exist_ok=True)
                    files = sorted(Path("backups").glob("db_*.json.gz"), reverse=True)
                    if not files:
                        await self.messenger.safe_send(self.client, chat_id, "❌ لا توجد نسخ محلية في مجلد backups/", tag="CMD")
                        return
                    in_path = str(files[0])

                await self.messenger.safe_send(self.client, chat_id, f"⏳ نسترجع من `{in_path}`...", tag="CMD")
                await self.backup.import_json_gz(in_path)

                self.state.direct_triggers = await self.db.load_table("direct_reply_messages")
                self.state.blocked_phrases = await self.db.load_table("blocked_reply_messages")
                self.state.auto_replies = await self.db.load_table("auto_reply_responses")
                self.state.blocked_users = await self.db.blocked_users_map()

                await self.messenger.safe_send(self.client, chat_id, "✅ تم الاسترجاع وتحديث الذاكرة.", tag="CMD")
            except Exception as e:
                await self.messenger.safe_send(self.client, chat_id, f"❌ فشل الاسترجاع:\n`{e}`", tag="CMD")
            return

        # ===== unified stats =====
        if cmd == "stats":
            try:
                s = await self.db.get_stats()
                msg = (
                    "📊 **ملخص التخزين الحالي:**\n\n"
                    f"• 🟢 الجُمل للرد المباشر: **{s['direct']}**\n"
                    f"• ⛔️ الجُمل المحظورة للنص: **{s['blocked_text']}**\n"
                    f"• 🚫 المستخدمون المحظورون: **{s['blocked_users']}**\n"
                    f"• 🔗 روابط الجروبات: **{s['groups']}**\n"
                )
                kb = [
                    [Button.text("/list"), Button.text("/list raw")],
                    [Button.text("/blklist"), Button.text("/blklist raw")],
                    [Button.text("/autolist"), Button.text("/autolist raw")],
                    [Button.text("/blkuser_list"), Button.text("/blkuser_list raw")],
                    [Button.text("/grouplist"), Button.text("/grouplist raw")],
                ]
                await self.messenger.safe_send(self.client, chat_id, msg, tag="CMD", buttons=kb)
            except Exception as e:
                await self.messenger.safe_send(self.client, chat_id, f"❌ تعذّر جلب الإحصاءات: `{e}`", tag="CMD")
            return

        # ===== Blocked Users Management =====
        if cmd == "blkuser_add":
            if ev.is_reply:
                rep = await ev.get_reply_message()
                snd = await rep.get_sender()
                uid = getattr(snd, "id", None)
                uname = getattr(snd, "username", "") or ""
                dname = f"{(getattr(snd, 'first_name','') or '').strip()} {(getattr(snd, 'last_name','') or '').strip()}".strip()
                if not uid:
                    await self.messenger.safe_send(self.client, chat_id, "❌ لا أستطيع استخراج user_id من الرد.", tag="CMD"); return
                await self.db.add_blocked_user(uid, uname, dname)
                self.state.blocked_users[uid] = (uname, dname)
                await self.messenger.safe_send(self.client, chat_id, f"✅ أُضيف للمحظورين: {uid} @{uname or '—'} | {dname or '—'}", tag="CMD"); return
            else:
                if not arg:
                    await self.messenger.safe_send(self.client, chat_id, "استخدم:\n/blkuser_add <user_id> [username] [display name...]\nأو بالرد على رسالة له.", tag="CMD"); return
                parts = arg.split()
                try:
                    uid = int(parts[0]); uname = parts[1] if len(parts) > 1 else ""; dname = " ".join(parts[2:]) if len(parts) > 2 else ""
                except:
                    await self.messenger.safe_send(self.client, chat_id, "صيغة غير صحيحة.", tag="CMD"); return
                await self.db.add_blocked_user(uid, uname, dname)
                self.state.blocked_users[uid] = (uname, dname)
                await self.messenger.safe_send(self.client, chat_id, f"✅ أُضيف للمحظورين: {uid} @{uname or '—'} | {dname or '—'}", tag="CMD"); return

        if cmd == "blkuser_del":
            if not arg and not ev.is_reply:
                await self.messenger.safe_send(self.client, chat_id, "استخدم:\n/blkuser_del <user_id>\nأو بالرد على رسالة.", tag="CMD"); return
            if ev.is_reply:
                snd = await (await ev.get_reply_message()).get_sender()
                try: uid = int(getattr(snd, "id", 0))
                except: uid = 0
            else:
                try: uid = int(arg.strip())
                except: uid = 0
            if not uid:
                await self.messenger.safe_send(self.client, chat_id, "❌ لم أتمكن من تحديد user_id.", tag="CMD"); return
            c = await self.db.del_blocked_user(uid)
            self.state.blocked_users.pop(uid, None)
            await self.messenger.safe_send(self.client, chat_id, f"✅ أُزيل من المحظورين (حُذِف {c}).", tag="CMD"); return

        if cmd == "blkuser_list":
            raw_flag = (arg.strip().lower() if arg else "") in {"raw", "بدون", "no"}
            rows = await self.db.list_blocked_users()
            if not rows:
                await self.messenger.safe_send(self.client, chat_id, "— لا يوجد محظورون —", tag="CMD"); return
            if raw_flag:
                msg = "\n".join([f"{r['user_id']} @{r['username'] or '—'} | {r['display_name'] or '—'} | {r['created_at']}" for r in rows[:200]])
            else:
                msg_lines = []
                for i, r in enumerate(rows[:200]):
                    msg_lines.append(
                        f"🔹 **#{i+1}**\n"
                        f"👤 **User ID:** `{r['user_id']}`"
                        f"{' | @' + r['username'] if r['username'] else ''}\n"
                        f"📝 **Name:** `{r['display_name'] or '—'}`\n"
                        f"🕒 **Blocked At:** `{r['created_at']}`\n"
                         "==================================="
                    )
                msg = "\n".join(msg_lines)
            kb = [[Button.text("/blkuser_list"), Button.text("/blkuser_list raw")]]
            await self.messenger.safe_send(self.client, chat_id, msg, tag="CMD", buttons=kb); return

        if cmd == "blkuser_find":
            if not arg:
                await self.messenger.safe_send(self.client, chat_id, "اكتب: /blkuser_find <pattern>", tag="CMD"); return
            rows = await self.db.find_blocked_users(arg)
            if not rows:
                await self.messenger.safe_send(self.client, chat_id, "— لا توجد مطابقة —", tag="CMD"); return
            msg = "\n".join([f"- {r['user_id']} @{r['username'] or '—'} | {r['display_name'] or '—'} | {r['created_at']}" for r in rows[:200]])
            await self.messenger.safe_send(self.client, chat_id, msg, tag="CMD"); return

        # ===== Auto replies log commands =====
        if cmd == "autoreplies_count":
            if not arg:
                await self.messenger.safe_send(self.client, chat_id, "استخدام: /autoreplies_count <user_id>", tag="CMD"); return
            try: uid = int(arg.strip())
            except: await self.messenger.safe_send(self.client, chat_id, "صيغة غير صحيحة.", tag="CMD"); return
            c = await self.db.count_auto_replies(uid)
            await self.messenger.safe_send(self.client, chat_id, f"🔢 عدد محاولات/ردود المستخدم {uid}: {c}", tag="CMD"); return

        if cmd == "autoreplies_list":
            limit = 50
            if arg.strip().isdigit():
                limit = int(arg.strip())
            if ev.is_reply:
                snd = await (await ev.get_reply_message()).get_sender()
                uid = getattr(snd, "id", None)
                if not uid:
                    await self.messenger.safe_send(self.client, chat_id, "❌ لا أستطيع استخراج user_id من الرد.", tag="CMD"); return
                rows = await self.db.list_auto_replies_for_user(uid, limit=limit)
                title = f"📒 آخر {len(rows)} سجل للمستخدم {uid}:"
            else:
                rows = await self.db.list_auto_replies(limit=limit)
                title = f"📒 آخر {len(rows)} سجل عام:"
            if not rows:
                await self.messenger.safe_send(self.client, chat_id, "— لا يوجد سجلات —", tag="CMD"); return
            lines = [
                (
                    f"🔹 **#{r['id']}**\n"
                    f"👤 **User:** `{r['user_id']}`"
                    f"{' | @' + r['username'] if r['username'] else ''}\n"
                    f"📝 **Name:** `{r['display_name'] or '—'}`\n"
                    f"🤖 **Bot:** `{r['bot_phone'] or '—'}`\n"
                    f"✉️ **Msg ID:** `{r['message_id'] or '—'}`\n"
                    f"🕒 **Time:** `{r['created_at']}`\n"
                    "==================================="
                )
                for r in rows
            ]
            await self.messenger.safe_send(self.client, chat_id, title + "\n" + "\n".join(lines), tag="CMD"); return

        if cmd == "autoreplies_clear":
            if not arg and not ev.is_reply:
                await self.messenger.safe_send(self.client, chat_id, "استخدام:\n/autoreplies_clear all\n/autoreplies_clear <user_id>\nأو بالرد على رسالة.", tag="CMD"); return
            if arg.strip().lower() == "all":
                async with self.db.pool.acquire() as conn:
                    await conn.ping(reconnect=True)
                    async with conn.cursor() as cur:
                        await cur.execute("SELECT COUNT(*) AS c FROM auto_reply_log")
                        c = (await cur.fetchone() or {}).get("c", 0)
                        await cur.execute("TRUNCATE TABLE auto_reply_log")
                await self.messenger.safe_send(self.client, chat_id, f"🧹 مسحنا {c} سجل من السجل العام.", tag="CMD"); return
            if ev.is_reply and not arg:
                snd = await (await ev.get_reply_message()).get_sender()
                uid = getattr(snd, "id", None)
                if not uid:
                    await self.messenger.safe_send(self.client, chat_id, "❌ لا أستطيع استخراج user_id.", tag="CMD"); return
            else:
                try: uid = int(arg.strip())
                except: await self.messenger.safe_send(self.client, chat_id, "استخدم: /autoreplies_clear <user_id> أو all", tag="CMD"); return
            async with self.db.pool.acquire() as conn:
                await conn.ping(reconnect=True)
                async with conn.cursor() as cur:
                    await cur.execute("DELETE FROM auto_reply_log WHERE user_id=%s", (uid,))
                    c = cur.rowcount
            await self.messenger.safe_send(self.client, chat_id, f"🧹 مسحنا سجلات المستخدم {uid}: {c}", tag="CMD"); return

        # ===== mappings for text stores =====
        stores = {
            "add": ("direct_reply_messages", self.state.direct_triggers),
            "del": ("direct_reply_messages", self.state.direct_triggers),
            "find": ("direct_reply_messages", self.state.direct_triggers),
            "blkadd": ("blocked_reply_messages", self.state.blocked_phrases),
            "blkdel": ("blocked_reply_messages", self.state.blocked_phrases),
            "blkfind": ("blocked_reply_messages", self.state.blocked_phrases),
            "autoadd": ("auto_reply_responses", self.state.auto_replies),
            "autodel": ("auto_reply_responses", self.state.auto_replies),
            "autofind": ("auto_reply_responses", self.state.auto_replies),
        }

        # إدخال تفاعلي
        if cmd in stores and not arg:
            await self.messenger.safe_send(self.client, chat_id, f"✍️ أرسل الآن العناصر الخاصة بـ **{cmd}**:\n- كل سطر عنصر مستقل.", tag="CMD")
            self.state.pending_ops[(chat_id, sender_id)] = (cmd, stores[cmd][0])
            return

        # تنفيذ مع وسيط
        if cmd in stores and arg:
            table, store_list = stores[cmd]
            lines = [l.strip() for l in arg.splitlines() if l.strip()]
            results = []
            if cmd in ("add", "blkadd", "autoadd"):
                for line in lines:
                    if line not in store_list:
                        await self.db.insert_table(table, line)
                        store_list.append(line)
                        results.append(f"✓ أضفنا: {line}")
                    else:
                        results.append(f"⚠️ موجود: {line}")
                await self.messenger.safe_send(self.client, chat_id, "نتيجة الإضافة:\n" + "\n".join(results), tag="CMD")
            elif cmd in ("del", "blkdel", "autodel"):
                for line in lines:
                    if line in store_list:
                        await self.db.delete_table(table, line)
                        store_list.remove(line)
                        results.append(f"✓ حذفنا: {line}")
                    else:
                        results.append(f"⚠️ غير موجود: {line}")
                await self.messenger.safe_send(self.client, chat_id, "نتيجة الحذف:\n" + "\n".join(results), tag="CMD")
            elif cmd in ("find", "blkfind", "autofind"):
                thresh = 100 if cmd.startswith("blk") else 80
                patterns = [l.strip() for l in arg.splitlines() if l.strip()]
                if not patterns:
                    await self.messenger.safe_send(self.client, chat_id, "— لم يتم إدخال أي نمط بحث —", tag="CMD")
                    return
                msg_lines = []
                for pat in patterns:
                    matches = [m for m in store_list if fuzz.ratio(TextUtils.normalize_text(pat), TextUtils.normalize_text(m)) >= thresh]
                    if matches:
                        msg_lines.append(f"🔎 **نتائج `{pat}`:**")
                        for m in matches:
                            msg_lines.append(f"```\n{m}\n```")
                    else:
                        msg_lines.append(f"🔎 **نتائج `{pat}`:**\n— لا توجد مطابقات —")
                await self.messenger.safe_send(self.client, chat_id, "\n".join(msg_lines), tag="CMD")
            return

        # ===== list-style outputs with numbering toggle =====
        if cmd in ("list", "blklist", "autolist"):
            store = {
            "list": self.state.direct_triggers,
            "blklist": self.state.blocked_phrases,
            "autolist": self.state.auto_replies,
            }[cmd]
            raw_flag = (arg.strip().lower() if arg else "") in {"raw", "بدون", "no", "بدون ترقيم"}
            if not store:
                await self.messenger.safe_send(self.client, chat_id, "— لا يوجد —", tag="CMD")
                return
            if raw_flag:
                msg = "\n".join([f"`{s}`" for s in store])
            else:
                msg = "\n".join([f"{i+1}. `{s}`" for i, s in enumerate(store)])
            kb = [[Button.text(f"/{cmd}"), Button.text(f"/{cmd} raw")]]
            await self.messenger.safe_send(self.client, chat_id, msg, tag="CMD", buttons=kb)
            return

        # group management shortcuts
        if cmd in ("groupadd", "groupdel", "groupupdate") and not arg:
            await self.messenger.safe_send(self.client, chat_id, f"✍️ أرسل الآن لكل أمر **{cmd}**:\n- كل سطر رابط.\n- في التحديث: سطر1=القديم، سطر2=الجديد.", tag="CMD")
            self.state.pending_ops[(chat_id, sender_id)] = (cmd, "join_groups")
            return
        if cmd == "groupadd" and arg:
            lines = [l.strip() for l in arg.splitlines() if l.strip()]
            existing = await self.group_repo.all()
            results = []
            for link in lines:
                if link not in existing:
                    await self.group_repo.add(link)
                    results.append(f"✓ أضفنا: {link}")
                else:
                    results.append(f"⚠️ موجود: {link}")
            await self.messenger.safe_send(self.client, chat_id, "نتيجة الإضافة:\n" + "\n".join(results), tag="CMD")
            return
        if cmd == "groupdel" and arg:
            lines = [l.strip() for l in arg.splitlines() if l.strip()]
            existing = await self.group_repo.all()
            results = []
            for link in lines:
                if link in existing:
                    await self.group_repo.delete(link)
                    results.append(f"✓ حذفنا: {link}")
                else:
                    results.append(f"⚠️ غير موجود: {link}")
            await self.messenger.safe_send(self.client, chat_id, "نتيجة الحذف:\n" + "\n".join(results), tag="CMD")
            return
        if cmd == "groupupdate" and arg:
            lines = [l.strip() for l in arg.splitlines() if l.strip()]
            if len(lines) == 2:
                old_link, new_link = lines
                await self.group_repo.update(old_link, new_link)
                await self.messenger.safe_send(self.client, chat_id, f"تم التحديث:\n{old_link} → {new_link}", tag="CMD")
            else:
                await self.messenger.safe_send(self.client, chat_id, "⚠️ أرسل **سطرين**: القديم ثم الجديد.", tag="CMD")
            return
        if cmd == "grouplist":
            links = await self.group_repo.all()
            raw_flag = (arg.strip().lower() if arg else "") in {"raw", "بدون", "no", "بدون ترقيم"}
            if links:
                msg = "\n".join(links) if raw_flag else "\n".join([f"{i+1}. {lnk}" for i, lnk in enumerate(links)])
            else:
                msg = "لا يوجد روابط جروبات مخزنة."
            kb = [[Button.text("/grouplist"), Button.text("/grouplist raw")]]
            await self.messenger.safe_send(self.client, chat_id, msg, tag="CMD", buttons=kb)
            return
        if cmd == "groupcount":
            links = await self.group_repo.all()
            await self.messenger.safe_send(self.client, chat_id, f"📊 العدد: {len(links)}", tag="CMD")
            return

        # usergroups/notin
        if cmd == "usergroups":
            if not arg:
                accs = [f"- {b.phone}" for b in self.state.bots]
                msg = "**الحسابات المتوفرة:**\n" + "\n".join(accs) + "\n\n✍️ أرسل رقم الحساب."
                await self.messenger.safe_send(self.client, chat_id, msg, tag="CMD")
                self.state.pending_ops[(chat_id, sender_id)] = ("usergroups", "join_groups")
                return
            phone = arg.strip()
            target_bot = next((b for b in self.state.bots if b.phone == phone), None)
            if not target_bot:
                await self.messenger.safe_send(self.client, chat_id, f"⚠️ الحساب غير موجود: {phone}", tag="CMD")
                return
            in_g, not_in = await target_bot.user_groups_status()
            msg = (
                f"🔢 **{phone} عضو في {len(in_g)} من {len(in_g)+len(not_in)}.**\n"
                f"❌ خارجها: {len(not_in)}\n"
                f"اكتب /usergroups_notin {phone} لعرضها."
            )
            await self.messenger.safe_send(self.client, chat_id, msg, tag="CMD")
            return
        if cmd == "usergroups_notin":
            phone = arg.strip()
            target_bot = next((b for b in self.state.bots if b.phone == phone), None)
            if not target_bot:
                await self.messenger.safe_send(self.client, chat_id, f"⚠️ الحساب غير موجود: {phone}", tag="CMD")
                return
            _, not_in = await target_bot.user_groups_status()
            if not_in:
                await self.messenger.safe_send(self.client, chat_id, "❗️الجروبات **غير المنتسب لها**:\n" + "\n".join(not_in), tag="CMD")
            else:
                await self.messenger.safe_send(self.client, chat_id, "✅ عضو في كل الجروبات المخزنة!", tag="CMD")
            return

        # joingroups
        if cmd == "joingroups":
            parts = arg.strip().split()
            if not parts:
                accs = [f"- {b.phone}" for b in self.state.bots]
                msg = "**الحسابات المتوفرة:**\n" + "\n".join(accs) + "\n\n✍️ أرسل: <رقم الجوال> [بداية رقمية]"
                await self.messenger.safe_send(self.client, chat_id, msg, tag="CMD")
                self.state.pending_ops[(chat_id, sender_id)] = ("joingroups", "join_groups")
                return
            phone = parts[0]
            start_index = int(parts[1]) - 1 if len(parts) > 1 and parts[1].isdigit() else 0
            target_bot = next((b for b in self.state.bots if b.phone == phone), None)
            if not target_bot:
                await self.messenger.safe_send(self.client, chat_id, f"⚠️ لا يوجد حساب: {phone}", tag="CMD")
                return
            t = asyncio.create_task(target_bot.join_groups_with_account(start_index))
            self.state.joining_now[phone] = t
            self.state.stop_joining_flags[phone] = False
            await self.messenger.safe_send(self.client, chat_id, f"⏳ يبدأ {phone} من {start_index+1}...", tag="CMD")
            return

        # stopjoin
        if cmd == "stopjoin":
            if not self.state.joining_now:
                await self.messenger.safe_send(self.client, chat_id, "لا توجد عمليات انضمام حالية.", tag="CMD")
                return
            accs = "\n".join(f"- {p}" for p in self.state.joining_now)
            msg = f"**عمليات نشطة:**\n{accs}\n\n✍️ أرسل رقم الحساب لإيقافه أو all لإيقاف الكل."
            await self.messenger.safe_send(self.client, chat_id, msg, tag="CMD")
            self.state.pending_ops[(chat_id, sender_id)] = ("stopjoin", "join_groups")
            return

        # help / unblock
        if cmd == "help":
            help_text = (
                "✨ **أوامر البوت — قائمة شاملة ومُزخرفة** ✨\n\n"
                "==============================\n"
                "📊 **ملخص التخزين:**\n"
                "• /stats — يعرض عدّادات التخزين.\n"
                "==============================\n"
                "🟢 **محفّزات الرد (Triggers):**\n"
                "• /add — إضافة محفّز جديد.\n"
                "• /del — حذف محفّز.\n"
                "• /list [raw] — عرض جميع المحفّزات.\n"
                "• /find — بحث عن محفّز.\n"
                "==============================\n"
                "⛔️ **عبارات تمنع الرد:**\n"
                "• /blkadd — إضافة عبارة للحظر.\n"
                "• /blkdel — حذف عبارة من الحظر.\n"
                "• /blklist [raw] — عرض العبارات المحظورة.\n"
                "• /blkfind — بحث في العبارات المحظورة.\n"
                "==============================\n"
                "🔁 **قوالب الرد التلقائي:**\n"
                "• /autoadd — إضافة رد تلقائي.\n"
                "• /autodel — حذف رد تلقائي.\n"
                "• /autolist [raw] — عرض الردود التلقائية.\n"
                "• /autofind — بحث في الردود التلقائية.\n"
                "==============================\n"
                "🔗 **إدارة روابط الجروبات:**\n"
                "• /groupadd — إضافة روابط جروبات.\n"
                "• /groupdel — حذف روابط جروبات.\n"
                "• /groupupdate — تحديث رابط جروب.\n"
                "• /grouplist [raw] — عرض جميع الروابط.\n"
                "• /groupcount — عدد الجروبات.\n"
                "==============================\n"
                "👥 **مراقبة انتساب الحسابات:**\n"
                "• /usergroups — تحقق من انتساب حساب لجروبات.\n"
                "• /usergroups_notin — عرض الجروبات غير المنتسب لها.\n"
                "• /joingroups — انضمام تلقائي للجروبات.\n"
                "• /stopjoin — إيقاف عمليات الانضمام.\n"
                "==============================\n"
                "🗄 **نسخ احتياطي/استرجاع:**\n"
                "• /dbbackup — إنشاء نسخة احتياطية.\n"
                "• /dbrestore — استرجاع نسخة احتياطية.\n"
                "==============================\n"
                "🚫 **إدارة قائمة المحظورين (Users):**\n"
                "• /blkuser_add — إضافة مستخدم للقائمة السوداء.\n"
                "• /blkuser_del — حذف مستخدم من القائمة السوداء.\n"
                "• /blkuser_list [raw] — عرض قائمة المحظورين.\n"
                "• /blkuser_find — بحث في قائمة المحظورين.\n"
                "==============================\n"
                "📒 **سجل الردود التلقائية (Intent أولًا):**\n"
                "• /autoreplies_count <user_id> — عدد الردود لمستخدم.\n"
                "• /autoreplies_list [limit] — عرض سجل الردود (أو بالرد على رسالة).\n"
                "• /autoreplies_clear all | <user_id> — مسح السجل.\n"
                "==============================\n"
                "🛡 **أوامر مساعدة وإلغاء الحظر:**\n"
                "• /help — عرض هذه القائمة.\n"
                "• /unblock — محاولة إلغاء الحظر من @SpamBot.\n"
                "==============================\n"
                "🔔 **ملاحظة:**\n"
                "• الحد قبل الحظر التلقائي: **4** محاولات خلال 24 ساعة.\n"
            )
            keyboard = [
                [Button.text("/stats")],
                [Button.text("/list"), Button.text("/list raw")],
                [Button.text("/blklist"), Button.text("/blklist raw")],
                [Button.text("/autolist"), Button.text("/autolist raw")],
                [Button.text("/grouplist"), Button.text("/grouplist raw")],
                [Button.text("/blkuser_list"), Button.text("/blkuser_list raw")],
                [Button.text("/dbbackup"), Button.text("/dbrestore")],
                [Button.text("/help"), Button.text("/unblock")]
            ]
            await self.messenger.safe_send(self.client, chat_id, help_text, tag="CMD", buttons=keyboard)
            return

        if cmd == "unblock":
            for b in self.state.bots:
                asyncio.create_task(self._start_spambot(b.client))
                asyncio.create_task(self._start_spambot(b.client))
            await self.messenger.safe_send(self.client, chat_id, "✓ أرسلنا /start إلى @SpamBot مرتين لكل حساب.", tag="CMD")
            return

    async def _start_spambot(self, cli: TelegramClient) -> None:
        try:
            async with cli.conversation("@SpamBot") as conv:
                await conv.send_message("/start")
                await conv.get_response(timeout=10)
        except:
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

    async def start(self) -> None:
        await self.db.init()
        self.state.direct_triggers = await self.db.load_table("direct_reply_messages")
        self.state.blocked_phrases = await self.db.load_table("blocked_reply_messages")
        self.state.auto_replies = await self.db.load_table("auto_reply_responses")
        self.state.blocked_users = await self.db.blocked_users_map()
        self.cfg.logger.info("Loaded triggers, blocks, auto-replies, blocked-users")

        clients: List[TelegramClient] = []

        for i in range(1, 100):
            aid = os.getenv(f"TELEGRAM_API_ID_{i}")
            ah = os.getenv(f"TELEGRAM_API_HASH_{i}")
            ph = os.getenv(f"TELEGRAM_PHONE_{i}")
            tg = os.getenv(f"TELEGRAM_TARGET_GROUP_ID_{i}")
            md = os.getenv(f"TELEGRAM_MODE_{i}", "both")
            if not all((aid, ah, ph, tg)):
                break

            session_name = f"session_{ph.replace('+','').replace(' ','').replace('-','')}"
            client = TelegramClient(session_name, int(aid), ah)
            clients.append(client)

            self.cfg.logger.info(f"Init account idx={i} phone={ph} mode={md} target_group={tg}")

            bot = Bot(
                cfg=self.cfg,
                db=self.db,
                state=self.state,
                messenger=self.messenger,
                group_repo=self.group_repo,
                formatter=self.formatter,
                fallback=self.fallback,
                client=client,
                target_group_id=int(tg),
                phone=ph,
                mode=md,
                is_command_bot=(i == self.cfg.command_bot_index),
                logger=self.cfg.logger,
                backup=self.backup,
            )
            self.state.bots.append(bot)

        command_user_id = None
        for idx, cli in enumerate(clients, start=1):
            try:
                await cli.start(os.getenv(f"TELEGRAM_PHONE_{idx}"))
                me = await cli.get_me()
                if idx == self.cfg.command_bot_index:
                    command_user_id = me.id
                self.cfg.logger.info(f"Account {idx} ({me.id}) started")
            except telethon.errors.AuthKeyDuplicatedError:
                self.cfg.logger.error(f"❌ AuthKeyDuplicatedError for account {idx}. Delete session file then restart.")
                continue

        self.state.COMMAND_USER_ID = command_user_id

        if clients:
            await asyncio.gather(*(c.run_until_disconnected() for c in clients))


# =============== Entry ===============

if __name__ == "__main__":
    try:
        asyncio.run(BotManager().start())
    except Exception:
        AppLogger.build().exception("❌ Fatal error in main()", exc_info=True)
        
