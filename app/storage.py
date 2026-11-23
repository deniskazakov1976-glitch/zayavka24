import hashlib
import json
import sqlite3
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence

from app.models import ConversationMessage, KnowledgeGap, LeadInfo


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _text_hash(text: Optional[str]) -> str:
    cleaned = "".join((text or "").split()).lower()
    return hashlib.sha256(cleaned.encode("utf-8")).hexdigest()[:16]


def _scope(company_slugs: Optional[List[str]], column: str = "company_slug") -> tuple[str, tuple]:
    """Возвращает SQL-фрагмент и параметры для изоляции данных по компаниям клиента.

    None  — без ограничений (платформенный администратор);
    []    — клиент без компаний, ничего не видит.
    """
    if company_slugs is None:
        return "", ()
    if not company_slugs:
        return " AND 1=0", ()
    placeholders = ",".join("?" for _ in company_slugs)
    return f" AND {column} IN ({placeholders})", tuple(company_slugs)


class Storage:
    """Lightweight SQLite persistence with WAL mode, migrations, and multi-tenant tables."""

    def __init__(self, db_path: Path | str) -> None:
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._price_cache: Dict[str, tuple[float, float, float]] = {}
        self._init_db()

    def _connect(self) -> sqlite3.Connection:
        # WT Hardening: 15s timeout + WAL mode + 15000ms busy_timeout
        conn = sqlite3.connect(self.db_path, timeout=15.0)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL;")
        conn.execute("PRAGMA busy_timeout=15000;")
        conn.execute("PRAGMA foreign_keys=ON;")
        conn.execute("PRAGMA synchronous=NORMAL;")
        return conn

    def _init_db(self) -> None:
        with self._connect() as conn:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS schema_migrations (
                    version INTEGER PRIMARY KEY,
                    name TEXT NOT NULL,
                    applied_at TEXT NOT NULL
                );
                """
            )
            applied = {row[0] for row in conn.execute("SELECT version FROM schema_migrations").fetchall()}

            migrations = [
                (1, "core_tables", self._migration_v1),
                (2, "hardening_tables", self._migration_v2),
                (3, "multitenant_and_users", self._migration_v3),
                (4, "backups_and_usage", self._migration_v4),
                (5, "onboarding", self._migration_v5),
                (6, "smart_followup", self._migration_v6),
            ]
            for version, name, func in migrations:
                if version not in applied:
                    func(conn)
                    conn.execute(
                        "INSERT INTO schema_migrations (version, name, applied_at) VALUES (?, ?, ?)",
                        (version, name, _utcnow().isoformat()),
                    )

    def _migration_v1(self, conn: sqlite3.Connection) -> None:
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS sessions (
                channel TEXT NOT NULL,
                chat_id TEXT NOT NULL,
                company_slug TEXT NOT NULL,
                paused_until TEXT,
                updated_at TEXT NOT NULL,
                PRIMARY KEY (channel, chat_id)
            );

            CREATE TABLE IF NOT EXISTS messages (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                channel TEXT NOT NULL,
                chat_id TEXT NOT NULL,
                role TEXT NOT NULL,
                content TEXT NOT NULL,
                created_at TEXT NOT NULL
            );

            CREATE INDEX IF NOT EXISTS idx_messages_chat
            ON messages (channel, chat_id, id);

            CREATE TABLE IF NOT EXISTS leads (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                channel TEXT NOT NULL,
                chat_id TEXT NOT NULL,
                company_slug TEXT NOT NULL,
                client_name TEXT,
                phone TEXT NOT NULL,
                car_model TEXT,
                service_interest TEXT,
                estimated_price TEXT,
                preferred_time TEXT,
                summary TEXT,
                avito_chat_url TEXT,
                created_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS knowledge_gaps (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                channel TEXT NOT NULL,
                chat_id TEXT NOT NULL,
                company_slug TEXT NOT NULL,
                question TEXT NOT NULL,
                customer_message TEXT,
                suggested_answer TEXT,
                status TEXT NOT NULL DEFAULT 'pending',
                created_at TEXT NOT NULL
            );
            """
        )

    def _migration_v2(self, conn: sqlite3.Connection) -> None:
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS processed_events (
                channel TEXT NOT NULL,
                message_id TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'done',
                created_at TEXT NOT NULL,
                PRIMARY KEY (channel, message_id)
            );

            CREATE TABLE IF NOT EXISTS bot_sent_messages (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                channel TEXT NOT NULL,
                chat_id TEXT NOT NULL,
                message_id TEXT,
                text_hash TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'sent',
                created_at TEXT NOT NULL
            );

            CREATE INDEX IF NOT EXISTS idx_bot_sent_chat
            ON bot_sent_messages (channel, chat_id, created_at);

            CREATE INDEX IF NOT EXISTS idx_bot_sent_msg_id
            ON bot_sent_messages (message_id);

            CREATE TABLE IF NOT EXISTS avito_accounts (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                company_slug TEXT NOT NULL,
                user_id INTEGER NOT NULL,
                client_id TEXT NOT NULL,
                client_secret TEXT NOT NULL,
                webhook_secret TEXT NOT NULL UNIQUE,
                enabled INTEGER NOT NULL DEFAULT 1,
                last_event_at TEXT,
                created_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS guard_events (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                channel TEXT NOT NULL,
                chat_id TEXT NOT NULL,
                company_slug TEXT NOT NULL,
                original_reply TEXT NOT NULL,
                safe_reply TEXT NOT NULL,
                blocked_terms TEXT NOT NULL,
                created_at TEXT NOT NULL
            );
            """
        )

    def _migration_v3(self, conn: sqlite3.Connection) -> None:
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS tenants (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                slug TEXT NOT NULL UNIQUE,
                name TEXT NOT NULL,
                brand_name TEXT NOT NULL DEFAULT '',
                telegram_bot_token TEXT NOT NULL DEFAULT '',
                bot_username TEXT NOT NULL DEFAULT '',
                owner_telegram_id INTEGER,
                plan TEXT NOT NULL DEFAULT 'basic',
                status TEXT NOT NULL DEFAULT 'active',
                created_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS users (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                username TEXT NOT NULL UNIQUE,
                password_hash TEXT NOT NULL,
                role TEXT NOT NULL DEFAULT 'client_admin',
                tenant_slug TEXT,
                is_active INTEGER NOT NULL DEFAULT 1,
                created_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS user_sessions (
                token TEXT PRIMARY KEY,
                user_id INTEGER NOT NULL,
                created_at TEXT NOT NULL,
                expires_at TEXT NOT NULL,
                FOREIGN KEY(user_id) REFERENCES users(id) ON DELETE CASCADE
            );

            CREATE INDEX IF NOT EXISTS idx_user_sessions_user
            ON user_sessions (user_id);

            CREATE INDEX IF NOT EXISTS idx_users_tenant
            ON users (tenant_slug);
            """
        )

    def _migration_v4(self, conn: sqlite3.Connection) -> None:
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS backup_runs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                started_at TEXT NOT NULL,
                finished_at TEXT,
                status TEXT NOT NULL DEFAULT 'running',   -- running | ok | failed | uploaded
                size_bytes INTEGER,
                path TEXT,
                remote TEXT,                              -- local | telegram | none
                error TEXT
            );

            CREATE TABLE IF NOT EXISTS llm_usage (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                tenant_slug TEXT NOT NULL,
                company_slug TEXT NOT NULL,
                channel TEXT NOT NULL,          -- telegram | avito
                chat_id TEXT,
                model TEXT NOT NULL,
                prompt_tokens INTEGER NOT NULL DEFAULT 0,
                completion_tokens INTEGER NOT NULL DEFAULT 0,
                total_tokens INTEGER NOT NULL DEFAULT 0,
                cost_rub REAL NOT NULL DEFAULT 0,
                created_at TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_usage_tenant_time ON llm_usage (tenant_slug, created_at);

            CREATE TABLE IF NOT EXISTS model_prices (
                model TEXT PRIMARY KEY,
                prompt_rub_per_1k REAL NOT NULL DEFAULT 0,
                completion_rub_per_1k REAL NOT NULL DEFAULT 0,
                updated_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS plans (
                slug TEXT PRIMARY KEY,
                name TEXT NOT NULL,
                monthly_rub REAL NOT NULL DEFAULT 0,
                included_messages INTEGER,       -- NULL = без лимита
                included_tokens INTEGER,
                overage_rub_per_1k REAL NOT NULL DEFAULT 0,
                is_active INTEGER NOT NULL DEFAULT 1
            );

            INSERT OR IGNORE INTO plans (slug, name, monthly_rub, included_messages, included_tokens, overage_rub_per_1k, is_active)
            VALUES
                ('basic', 'Базовый', 0, 1000, NULL, 0, 1),
                ('pro', 'ПРО', 2990, 5000, NULL, 0, 1),
                ('internal', 'Внутренний (безлимит)', 0, NULL, NULL, 0, 1);
            """
        )

    def _migration_v5(self, conn: sqlite3.Connection) -> None:
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS onboarding (
                tenant_slug TEXT PRIMARY KEY,
                step INTEGER NOT NULL DEFAULT 1,
                draft_json TEXT NOT NULL DEFAULT '{}',
                completed_at TEXT,
                updated_at TEXT NOT NULL
            );
            """
        )

    def _migration_v6(self, conn: sqlite3.Connection) -> None:
        cols = [row[1] for row in conn.execute("PRAGMA table_info(sessions)").fetchall()]
        if "followup_sent_at" not in cols:
            conn.execute("ALTER TABLE sessions ADD COLUMN followup_sent_at TEXT;")

    # === Tenants (мультибот) ===
    def save_tenant(
        self,
        slug: str,
        name: str,
        telegram_bot_token: str = "",
        brand_name: str = "",
        owner_telegram_id: Optional[int] = None,
        plan: str = "basic",
        status: str = "active",
        bot_username: str = "",
    ) -> Dict[str, Any]:
        now = _utcnow().isoformat()
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO tenants (slug, name, brand_name, telegram_bot_token, bot_username,
                                     owner_telegram_id, plan, status, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(slug) DO UPDATE SET
                    name = excluded.name,
                    brand_name = excluded.brand_name,
                    telegram_bot_token = excluded.telegram_bot_token,
                    bot_username = excluded.bot_username,
                    owner_telegram_id = excluded.owner_telegram_id,
                    plan = excluded.plan,
                    status = excluded.status
                """,
                (slug, name, brand_name, telegram_bot_token, bot_username,
                 owner_telegram_id, plan, status, now),
            )
            row = conn.execute("SELECT * FROM tenants WHERE slug = ?", (slug,)).fetchone()
        return dict(row) if row else {}

    def get_tenant(self, slug: str) -> Optional[Dict[str, Any]]:
        with self._connect() as conn:
            row = conn.execute("SELECT * FROM tenants WHERE slug = ?", (slug,)).fetchone()
        return dict(row) if row else None

    def list_tenants(self, include_tokens: bool = False) -> List[Dict[str, Any]]:
        with self._connect() as conn:
            rows = conn.execute("SELECT * FROM tenants ORDER BY id ASC").fetchall()
        result = []
        for r in rows:
            item = dict(r)
            if not include_tokens:
                item["telegram_bot_token"] = "***" if item.get("telegram_bot_token") else ""
            result.append(item)
        return result

    def list_active_tenants_with_bots(self) -> List[Dict[str, Any]]:
        with self._connect() as conn:
            rows = conn.execute(
                """
                SELECT * FROM tenants
                WHERE status = 'active' AND telegram_bot_token != ''
                ORDER BY id ASC
                """
            ).fetchall()
        return [dict(r) for r in rows]

    def update_tenant_bot_username(self, slug: str, username: str) -> None:
        with self._connect() as conn:
            conn.execute("UPDATE tenants SET bot_username = ? WHERE slug = ?", (username, slug))

    def delete_tenant(self, slug: str) -> bool:
        if slug == "default":
            return False
        with self._connect() as conn:
            cur = conn.execute("DELETE FROM tenants WHERE slug = ?", (slug,))
            conn.execute("DELETE FROM users WHERE tenant_slug = ?", (slug,))
            return cur.rowcount > 0

    # === Users & sessions (кабинет клиента) ===
    def save_user(
        self,
        username: str,
        password_hash: str,
        role: str = "client_admin",
        tenant_slug: Optional[str] = None,
        is_active: bool = True,
    ) -> Dict[str, Any]:
        now = _utcnow().isoformat()
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO users (username, password_hash, role, tenant_slug, is_active, created_at)
                VALUES (?, ?, ?, ?, ?, ?)
                ON CONFLICT(username) DO UPDATE SET
                    password_hash = excluded.password_hash,
                    role = excluded.role,
                    tenant_slug = excluded.tenant_slug,
                    is_active = excluded.is_active
                """,
                (username, password_hash, role, tenant_slug, 1 if is_active else 0, now),
            )
            row = conn.execute("SELECT * FROM users WHERE username = ?", (username,)).fetchone()
        return dict(row) if row else {}

    def get_user_by_username(self, username: str) -> Optional[Dict[str, Any]]:
        with self._connect() as conn:
            row = conn.execute("SELECT * FROM users WHERE username = ?", (username,)).fetchone()
        return dict(row) if row else None

    def get_user_by_id(self, user_id: int) -> Optional[Dict[str, Any]]:
        with self._connect() as conn:
            row = conn.execute("SELECT * FROM users WHERE id = ?", (user_id,)).fetchone()
        return dict(row) if row else None

    def list_users(self, tenant_slug: Optional[str] = None) -> List[Dict[str, Any]]:
        with self._connect() as conn:
            if tenant_slug is None:
                rows = conn.execute("SELECT * FROM users ORDER BY id ASC").fetchall()
            else:
                rows = conn.execute(
                    "SELECT * FROM users WHERE tenant_slug = ? ORDER BY id ASC", (tenant_slug,)
                ).fetchall()
        return [dict(r) for r in rows]

    def delete_user(self, username: str) -> bool:
        with self._connect() as conn:
            row = conn.execute("SELECT role FROM users WHERE username = ?", (username,)).fetchone()
            if row and row["role"] == "platform_admin":
                return False
            cur = conn.execute("DELETE FROM users WHERE username = ?", (username,))
            return cur.rowcount > 0

    def create_user_session(self, token: str, user_id: int, ttl_hours: int = 72) -> None:
        now = _utcnow()
        with self._connect() as conn:
            conn.execute(
                "INSERT INTO user_sessions (token, user_id, created_at, expires_at) VALUES (?, ?, ?, ?)",
                (token, user_id, now.isoformat(), (now + timedelta(hours=ttl_hours)).isoformat()),
            )

    def get_user_session(self, token: str) -> Optional[Dict[str, Any]]:
        with self._connect() as conn:
            row = conn.execute(
                """
                SELECT s.token, s.expires_at, u.id AS user_id, u.username, u.role,
                       u.tenant_slug, u.is_active
                FROM user_sessions s
                JOIN users u ON u.id = s.user_id
                WHERE s.token = ?
                LIMIT 1
                """,
                (token,),
            ).fetchone()
        if not row:
            return None
        try:
            expires = datetime.fromisoformat(row["expires_at"])
            if expires.tzinfo is None:
                expires = expires.replace(tzinfo=timezone.utc)
            if _utcnow() >= expires:
                self.delete_user_session(token)
                return None
        except Exception:
            return None
        if not row["is_active"]:
            return None
        return dict(row)

    def delete_user_session(self, token: str) -> None:
        with self._connect() as conn:
            conn.execute("DELETE FROM user_sessions WHERE token = ?", (token,))

    def purge_expired_sessions(self) -> int:
        with self._connect() as conn:
            cur = conn.execute(
                "DELETE FROM user_sessions WHERE expires_at <= ?", (_utcnow().isoformat(),)
            )
            return cur.rowcount

    def upsert_session(self, channel: str, chat_id: str, company_slug: str) -> None:
        now = _utcnow().isoformat()
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO sessions (channel, chat_id, company_slug, updated_at)
                VALUES (?, ?, ?, ?)
                ON CONFLICT(channel, chat_id) DO UPDATE SET
                    company_slug = excluded.company_slug,
                    updated_at = excluded.updated_at
                """,
                (channel, chat_id, company_slug, now),
            )

    def get_session_company(self, channel: str, chat_id: str) -> Optional[str]:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT company_slug FROM sessions WHERE channel = ? AND chat_id = ?",
                (channel, chat_id),
            ).fetchone()
            return row["company_slug"] if row else None

    def pause_session(self, channel: str, chat_id: str, hours: int = 24, company_slug: str = "") -> None:
        until = (_utcnow() + timedelta(hours=hours)).isoformat()
        now = _utcnow().isoformat()
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO sessions (channel, chat_id, company_slug, paused_until, updated_at)
                VALUES (?, ?, ?, ?, ?)
                ON CONFLICT(channel, chat_id) DO UPDATE SET
                    paused_until = excluded.paused_until,
                    updated_at = excluded.updated_at
                """,
                (channel, chat_id, company_slug, until, now),
            )

    def resume_session(self, channel: str, chat_id: str) -> None:
        now = _utcnow().isoformat()
        with self._connect() as conn:
            conn.execute(
                "UPDATE sessions SET paused_until = NULL, updated_at = ? WHERE channel = ? AND chat_id = ?",
                (now, channel, chat_id),
            )

    def is_paused(self, channel: str, chat_id: str) -> bool:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT paused_until FROM sessions WHERE channel = ? AND chat_id = ?",
                (channel, chat_id),
            ).fetchone()
            if not row or not row["paused_until"]:
                return False
            paused_until = datetime.fromisoformat(row["paused_until"])
            if paused_until.tzinfo is None:
                paused_until = paused_until.replace(tzinfo=timezone.utc)
            return _utcnow() < paused_until

    def get_sessions_needing_followup(
        self,
        older_than_hours: float = 3.0,
        max_age_hours: float = 48.0,
    ) -> List[Dict[str, Any]]:
        """Returns active sessions without a captured lead that need a gentle follow-up."""
        now = _utcnow()
        min_time = (now - timedelta(hours=max_age_hours)).isoformat()
        max_time = (now - timedelta(hours=older_than_hours)).isoformat()

        with self._connect() as conn:
            rows = conn.execute(
                """
                SELECT s.channel, s.chat_id, s.company_slug, s.updated_at
                FROM sessions s
                WHERE s.followup_sent_at IS NULL
                  AND (s.paused_until IS NULL OR s.paused_until <= ?)
                  AND s.updated_at >= ?
                  AND s.updated_at <= ?
                  AND NOT EXISTS (
                      SELECT 1 FROM leads l
                      WHERE l.channel = s.channel AND l.chat_id = s.chat_id
                  )
                ORDER BY s.updated_at ASC
                LIMIT 50;
                """,
                (now.isoformat(), min_time, max_time),
            ).fetchall()
            return [dict(r) for r in rows]

    def mark_followup_sent(self, channel: str, chat_id: str) -> None:
        now = _utcnow().isoformat()
        with self._connect() as conn:
            conn.execute(
                "UPDATE sessions SET followup_sent_at = ? WHERE channel = ? AND chat_id = ?",
                (now, channel, chat_id),
            )

    def add_message(self, channel: str, chat_id: str, role: str, content: str) -> None:
        now = _utcnow().isoformat()
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO messages (channel, chat_id, role, content, created_at)
                VALUES (?, ?, ?, ?, ?)
                """,
                (channel, chat_id, role, content, now),
            )

    def get_history(self, channel: str, chat_id: str, limit: int = 14) -> List[ConversationMessage]:
        with self._connect() as conn:
            rows = conn.execute(
                """
                SELECT role, content, created_at FROM (
                    SELECT id, role, content, created_at
                    FROM messages
                    WHERE channel = ? AND chat_id = ?
                    ORDER BY id DESC
                    LIMIT ?
                ) ORDER BY id ASC
                """,
                (channel, chat_id, limit),
            ).fetchall()
        return [
            ConversationMessage(
                role=row["role"],
                content=row["content"],
                created_at=datetime.fromisoformat(row["created_at"]),
            )
            for row in rows
        ]

    def clear_history(self, channel: str, chat_id: str) -> None:
        with self._connect() as conn:
            conn.execute(
                "DELETE FROM messages WHERE channel = ? AND chat_id = ?",
                (channel, chat_id),
            )

    def has_lead(self, channel: str, chat_id: str, phone: str) -> bool:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT 1 FROM leads WHERE channel = ? AND chat_id = ? AND phone = ? LIMIT 1",
                (channel, chat_id, phone),
            ).fetchone()
            return row is not None

    def has_lead_for_chat(self, channel: str, chat_id: str) -> bool:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT 1 FROM leads WHERE channel = ? AND chat_id = ? LIMIT 1",
                (channel, chat_id),
            ).fetchone()
            return row is not None

    def save_lead(self, lead: LeadInfo) -> int:
        now = _utcnow().isoformat()
        with self._connect() as conn:
            cursor = conn.execute(
                """
                INSERT INTO leads (
                    channel, chat_id, company_slug, client_name, phone,
                    car_model, service_interest, estimated_price,
                    preferred_time, summary, avito_chat_url, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    lead.channel,
                    lead.chat_id,
                    lead.company_slug,
                    lead.client_name,
                    lead.phone,
                    lead.car_model,
                    lead.service_interest,
                    lead.estimated_price,
                    lead.preferred_time,
                    lead.summary,
                    lead.avito_chat_url,
                    now,
                ),
            )
            return int(cursor.lastrowid)

    def list_recent_leads(
        self, limit: int = 10, company_slugs: Optional[List[str]] = None
    ) -> List[LeadInfo]:
        clause, params = _scope(company_slugs)
        with self._connect() as conn:
            rows = conn.execute(
                f"""
                SELECT channel, chat_id, company_slug, client_name, phone,
                       car_model, service_interest, estimated_price,
                       preferred_time, summary, avito_chat_url, created_at
                FROM leads
                WHERE 1=1{clause}
                ORDER BY id DESC
                LIMIT ?
                """,
                (*params, limit),
            ).fetchall()
        return [
            LeadInfo(
                channel=row["channel"],
                chat_id=row["chat_id"],
                company_slug=row["company_slug"],
                client_name=row["client_name"] or "",
                phone=row["phone"],
                car_model=row["car_model"] or "",
                service_interest=row["service_interest"] or "",
                estimated_price=row["estimated_price"] or "",
                preferred_time=row["preferred_time"] or "",
                summary=row["summary"] or "",
                avito_chat_url=row["avito_chat_url"] or "",
                created_at=datetime.fromisoformat(row["created_at"]),
            )
            for row in rows
        ]

    def list_sessions(
        self, limit: int = 50, company_slugs: Optional[List[str]] = None
    ) -> List[dict]:
        clause, params = _scope(company_slugs, "s.company_slug")
        with self._connect() as conn:
            rows = conn.execute(
                f"""
                SELECT s.channel, s.chat_id, s.company_slug, s.paused_until, s.updated_at,
                       COUNT(m.id) AS message_count,
                       (SELECT content FROM messages WHERE channel = s.channel AND chat_id = s.chat_id ORDER BY id DESC LIMIT 1) AS last_message,
                       (SELECT role FROM messages WHERE channel = s.channel AND chat_id = s.chat_id ORDER BY id DESC LIMIT 1) AS last_role
                FROM sessions s
                LEFT JOIN messages m ON s.channel = m.channel AND s.chat_id = m.chat_id
                WHERE 1=1{clause}
                GROUP BY s.channel, s.chat_id
                ORDER BY s.updated_at DESC
                LIMIT ?
                """,
                (*params, limit),
            ).fetchall()

        now = _utcnow()
        result = []
        for r in rows:
            paused = False
            if r["paused_until"]:
                try:
                    p_until = datetime.fromisoformat(r["paused_until"])
                    if p_until.tzinfo is None:
                        p_until = p_until.replace(tzinfo=timezone.utc)
                    paused = now < p_until
                except Exception:
                    paused = False
            result.append(
                {
                    "channel": r["channel"],
                    "chat_id": r["chat_id"],
                    "company_slug": r["company_slug"],
                    "is_paused": paused,
                    "paused_until": r["paused_until"],
                    "updated_at": r["updated_at"],
                    "message_count": r["message_count"] or 0,
                    "last_message": r["last_message"] or "",
                    "last_role": r["last_role"] or "",
                }
            )
        return result

    def get_stats(self, company_slugs: Optional[List[str]] = None) -> dict:
        clause, params = _scope(company_slugs)
        with self._connect() as conn:
            total_sessions = conn.execute(
                f"SELECT COUNT(*) AS c FROM sessions WHERE 1=1{clause}", params
            ).fetchone()["c"]
            total_messages = conn.execute(
                f"""
                SELECT COUNT(*) AS c FROM messages
                WHERE (channel, chat_id) IN (
                    SELECT channel, chat_id FROM sessions WHERE 1=1{clause}
                )
                """,
                params,
            ).fetchone()["c"]
            total_leads = conn.execute(
                f"SELECT COUNT(*) AS c FROM leads WHERE 1=1{clause}", params
            ).fetchone()["c"]
            pending_gaps = conn.execute(
                f"SELECT COUNT(*) AS c FROM knowledge_gaps WHERE status = 'pending'{clause}",
                params,
            ).fetchone()["c"]
            now_str = _utcnow().isoformat()
            paused_sessions = conn.execute(
                f"""
                SELECT COUNT(*) AS c FROM sessions
                WHERE paused_until IS NOT NULL AND paused_until > ?{clause}
                """,
                (now_str, *params),
            ).fetchone()["c"]

        return {
            "total_sessions": total_sessions,
            "total_messages": total_messages,
            "total_leads": total_leads,
            "paused_sessions": paused_sessions,
            "pending_gaps": pending_gaps,
        }

    def save_knowledge_gap(self, gap: KnowledgeGap) -> int:
        now = _utcnow().isoformat()
        with self._connect() as conn:
            # Deduplicate: if an identical pending gap already exists for this company, return existing ID
            existing = conn.execute(
                """
                SELECT id FROM knowledge_gaps
                WHERE company_slug = ? AND status = 'pending' AND question = ?
                ORDER BY id DESC LIMIT 1
                """,
                (gap.company_slug, gap.question),
            ).fetchone()
            if existing:
                return int(existing["id"])

            cursor = conn.execute(
                """
                INSERT INTO knowledge_gaps (
                    channel, chat_id, company_slug, question,
                    customer_message, suggested_answer, status, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    gap.channel,
                    gap.chat_id,
                    gap.company_slug,
                    gap.question,
                    gap.customer_message,
                    gap.suggested_answer,
                    gap.status,
                    now,
                ),
            )
            return int(cursor.lastrowid)

    def list_knowledge_gaps(
        self, limit: int = 50, status: Optional[str] = None,
        company_slugs: Optional[List[str]] = None,
        company_slug: Optional[str] = None,
    ) -> List[KnowledgeGap]:
        if company_slug and not company_slugs:
            company_slugs = [company_slug]
        clause, params = _scope(company_slugs)
        status_clause = " AND status = ?" if status else ""
        status_params: tuple = (status,) if status else ()
        with self._connect() as conn:
            rows = conn.execute(
                f"""
                SELECT id, channel, chat_id, company_slug, question,
                       customer_message, suggested_answer, status, created_at
                FROM knowledge_gaps
                WHERE 1=1{status_clause}{clause}
                ORDER BY id DESC
                LIMIT ?
                """,
                (*status_params, *params, limit),
            ).fetchall()

        return [
            KnowledgeGap(
                id=row["id"],
                channel=row["channel"],
                chat_id=row["chat_id"],
                company_slug=row["company_slug"],
                question=row["question"],
                customer_message=row["customer_message"] or "",
                suggested_answer=row["suggested_answer"] or "",
                status=row["status"],
                created_at=datetime.fromisoformat(row["created_at"]),
            )
            for row in rows
        ]

    def resolve_knowledge_gap(self, gap_id: int, status: str = "learned", answer: str = "") -> None:
        with self._connect() as conn:
            conn.execute(
                "UPDATE knowledge_gaps SET status = ?, suggested_answer = ? WHERE id = ?",
                (status, answer, gap_id),
            )

    # === Deduplication (K3) ===
    def is_event_processed(self, channel: str, message_id: str) -> bool:
        if not message_id:
            return False
        with self._connect() as conn:
            row = conn.execute(
                "SELECT 1 FROM processed_events WHERE channel = ? AND message_id = ? LIMIT 1",
                (channel, message_id),
            ).fetchone()
            return row is not None

    def mark_event_processed(self, channel: str, message_id: str, status: str = "done") -> bool:
        if not message_id:
            return False
        now = _utcnow().isoformat()
        with self._connect() as conn:
            cur = conn.execute(
                """
                INSERT OR IGNORE INTO processed_events (channel, message_id, status, created_at)
                VALUES (?, ?, ?, ?)
                """,
                (channel, message_id, status, now),
            )
            return cur.rowcount > 0

    # === Persistent Bot Echo Tracking (K4) ===
    def record_bot_sent_message(
        self,
        channel: str,
        chat_id: str,
        message_id: Optional[str] = None,
        text: Optional[str] = None,
        status: str = "pending",
    ) -> int:
        now = _utcnow().isoformat()
        thash = _text_hash(text)
        with self._connect() as conn:
            cur = conn.execute(
                """
                INSERT INTO bot_sent_messages (channel, chat_id, message_id, text_hash, status, created_at)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (channel, chat_id, message_id, thash, status, now),
            )
            return cur.lastrowid

    def update_bot_sent_message_id(self, record_id: int, message_id: str) -> None:
        with self._connect() as conn:
            conn.execute(
                "UPDATE bot_sent_messages SET message_id = ?, status = 'sent' WHERE id = ?",
                (message_id, record_id),
            )

    def is_bot_echo(
        self,
        channel: str,
        chat_id: str,
        message_id: str,
        text: Optional[str] = None,
        window_seconds: int = 60,
    ) -> bool:
        with self._connect() as conn:
            if message_id:
                row = conn.execute(
                    """
                    SELECT id FROM bot_sent_messages
                    WHERE channel = ? AND message_id = ?
                    LIMIT 1
                    """,
                    (channel, message_id),
                ).fetchone()
                if row:
                    return True

            if text:
                thash = _text_hash(text)
                cutoff = (_utcnow() - timedelta(seconds=window_seconds)).isoformat()
                row = conn.execute(
                    """
                    SELECT id FROM bot_sent_messages
                    WHERE channel = ? AND chat_id = ? AND text_hash = ? AND created_at >= ?
                    ORDER BY id DESC LIMIT 1
                    """,
                    (channel, chat_id, thash, cutoff),
                ).fetchone()
                if row:
                    if message_id:
                        conn.execute(
                            "UPDATE bot_sent_messages SET message_id = ?, status = 'sent' WHERE id = ?",
                            (message_id, row["id"]),
                        )
                    return True

        return False

    # === Multi-tenant Avito Accounts (K6) ===
    def save_avito_account(
        self,
        company_slug: str,
        user_id: int,
        client_id: str,
        client_secret: str,
        webhook_secret: Optional[str] = None,
        enabled: bool = True,
    ) -> Dict[str, Any]:
        import secrets

        sec = webhook_secret or secrets.token_hex(16)
        now = _utcnow().isoformat()
        with self._connect() as conn:
            cur = conn.execute(
                """
                INSERT INTO avito_accounts (company_slug, user_id, client_id, client_secret, webhook_secret, enabled, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(webhook_secret) DO UPDATE SET
                    company_slug = excluded.company_slug,
                    user_id = excluded.user_id,
                    client_id = excluded.client_id,
                    client_secret = excluded.client_secret,
                    enabled = excluded.enabled
                """,
                (company_slug, user_id, client_id, client_secret, sec, 1 if enabled else 0, now),
            )
            acc_id = cur.lastrowid
            return {
                "id": acc_id,
                "company_slug": company_slug,
                "user_id": user_id,
                "client_id": client_id,
                "webhook_secret": sec,
                "enabled": enabled,
            }

    def list_avito_accounts(self) -> List[Dict[str, Any]]:
        with self._connect() as conn:
            rows = conn.execute(
                """
                SELECT id, company_slug, user_id, client_id, webhook_secret, enabled, last_event_at, created_at
                FROM avito_accounts
                ORDER BY id ASC
                """
            ).fetchall()
        return [
            {
                "id": r["id"],
                "company_slug": r["company_slug"],
                "user_id": r["user_id"],
                "client_id": r["client_id"],
                "webhook_secret": r["webhook_secret"],
                "enabled": bool(r["enabled"]),
                "last_event_at": r["last_event_at"],
                "created_at": r["created_at"],
            }
            for r in rows
        ]

    def get_avito_account_by_secret(self, webhook_secret: str) -> Optional[Dict[str, Any]]:
        with self._connect() as conn:
            row = conn.execute(
                """
                SELECT id, company_slug, user_id, client_id, client_secret, webhook_secret, enabled, last_event_at
                FROM avito_accounts
                WHERE webhook_secret = ? AND enabled = 1
                LIMIT 1
                """,
                (webhook_secret,),
            ).fetchone()
        if not row:
            return None
        return dict(row)

    def get_avito_account_by_user_id(self, user_id: int) -> Optional[Dict[str, Any]]:
        with self._connect() as conn:
            row = conn.execute(
                """
                SELECT id, company_slug, user_id, client_id, client_secret, webhook_secret, enabled, last_event_at
                FROM avito_accounts
                WHERE user_id = ?
                LIMIT 1
                """,
                (user_id,),
            ).fetchone()
        if not row:
            return None
        return dict(row)

    def delete_avito_account(self, account_id: int) -> bool:
        with self._connect() as conn:
            cur = conn.execute("DELETE FROM avito_accounts WHERE id = ?", (account_id,))
            return cur.rowcount > 0

    def update_avito_account_activity(self, user_id: int) -> None:
        now = _utcnow().isoformat()
        with self._connect() as conn:
            conn.execute("UPDATE avito_accounts SET last_event_at = ? WHERE user_id = ?", (now, user_id))

    # === Guardrails Events (P2) ===
    def record_guard_event(
        self,
        channel: str,
        chat_id: str,
        company_slug: str,
        original_reply: str,
        safe_reply: str,
        blocked_terms: str,
    ) -> None:
        now = _utcnow().isoformat()
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO guard_events (channel, chat_id, company_slug, original_reply, safe_reply, blocked_terms, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (channel, chat_id, company_slug, original_reply, safe_reply, blocked_terms, now),
            )

    def list_guard_events(
        self, limit: int = 50, company_slugs: Optional[List[str]] = None
    ) -> List[Dict[str, Any]]:
        clause, params = _scope(company_slugs)
        with self._connect() as conn:
            rows = conn.execute(
                f"""
                SELECT id, channel, chat_id, company_slug, original_reply, safe_reply, blocked_terms, created_at
                FROM guard_events
                WHERE 1=1{clause}
                ORDER BY id DESC LIMIT ?
                """,
                (*params, limit),
            ).fetchall()
        return [dict(r) for r in rows]

    def cleanup_old_records(self) -> Dict[str, int]:
        cutoff_events = (_utcnow() - timedelta(days=7)).isoformat()
        cutoff_echo = (_utcnow() - timedelta(days=2)).isoformat()
        with self._connect() as conn:
            c1 = conn.execute("DELETE FROM processed_events WHERE created_at < ?", (cutoff_events,)).rowcount
            c2 = conn.execute("DELETE FROM bot_sent_messages WHERE created_at < ?", (cutoff_echo,)).rowcount
        return {"deleted_events": c1, "deleted_echo_records": c2}

    # === 152-FZ Privacy & Data Retention (P11 / TZ 4) ===
    def delete_subject_data(self, target: str, company_slugs: Optional[Sequence[str]] = None) -> int:
        """Удаляет все персональные данные субъекта (по chat_id или по номеру телефона).
        Если указан company_slugs, удаление ограничивается компаниями данного тенанта.
        """
        target_clean = target.strip()
        total_deleted = 0
        with self._connect() as conn:
            if company_slugs:
                slug_list = list(company_slugs)
                placeholders = ",".join("?" for _ in slug_list)
                # 1. Удаление лидов по телефону или chat_id в рамках компаний тенанта
                c1 = conn.execute(
                    f"DELETE FROM leads WHERE (chat_id = ? OR phone LIKE ?) AND company_slug IN ({placeholders})",
                    (target_clean, f"%{target_clean}%", *slug_list),
                ).rowcount
                # 2. Удаление сообщений по chat_id для сессий или лидов этих компаний
                c2 = conn.execute(
                    f"""
                    DELETE FROM messages 
                    WHERE chat_id = ? 
                      AND (
                          chat_id IN (SELECT chat_id FROM sessions WHERE company_slug IN ({placeholders}))
                          OR chat_id IN (SELECT chat_id FROM leads WHERE company_slug IN ({placeholders}))
                      )
                    """,
                    (target_clean, *slug_list, *slug_list),
                ).rowcount
                # 3. Удаление сессий
                c3 = conn.execute(
                    f"DELETE FROM sessions WHERE chat_id = ? AND company_slug IN ({placeholders})",
                    (target_clean, *slug_list),
                ).rowcount
                # 4. Удаление пробелов знаний
                c4 = conn.execute(
                    f"DELETE FROM knowledge_gaps WHERE chat_id = ? AND company_slug IN ({placeholders})",
                    (target_clean, *slug_list),
                ).rowcount
            else:
                # 1. Удаление лидов по телефону или chat_id
                c1 = conn.execute(
                    "DELETE FROM leads WHERE chat_id = ? OR phone LIKE ?",
                    (target_clean, f"%{target_clean}%"),
                ).rowcount
                # 2. Удаление сообщений по chat_id
                c2 = conn.execute("DELETE FROM messages WHERE chat_id = ?", (target_clean,)).rowcount
                # 3. Удаление сессий
                c3 = conn.execute("DELETE FROM sessions WHERE chat_id = ?", (target_clean,)).rowcount
                # 4. Удаление пробелов знаний по chat_id
                c4 = conn.execute("DELETE FROM knowledge_gaps WHERE chat_id = ?", (target_clean,)).rowcount
            total_deleted = c1 + c2 + c3 + c4
        return total_deleted

    def purge_expired(self, message_days: int = 90, lead_days: int = 365) -> Dict[str, int]:
        """Очищает устаревшие сообщения и лиды по истечении срока хранения."""
        msg_cutoff = (_utcnow() - timedelta(days=message_days)).isoformat()
        lead_cutoff = (_utcnow() - timedelta(days=lead_days)).isoformat()
        with self._connect() as conn:
            m_del = conn.execute("DELETE FROM messages WHERE created_at <= ?", (msg_cutoff,)).rowcount
            l_del = conn.execute("DELETE FROM leads WHERE created_at <= ?", (lead_cutoff,)).rowcount
        return {"purged_messages": m_del, "purged_leads": l_del}

    # === Backups (Блок A) ===
    def record_backup_run(self, started_at: str, status: str = "running", remote: str = "local") -> int:
        with self._connect() as conn:
            cur = conn.execute(
                """
                INSERT INTO backup_runs (started_at, status, remote)
                VALUES (?, ?, ?)
                """,
                (started_at, status, remote),
            )
            return int(cur.lastrowid)

    def finish_backup_run(
        self,
        run_id: int,
        status: str,
        size_bytes: Optional[int] = None,
        path: Optional[str] = None,
        remote: Optional[str] = None,
        error: Optional[str] = None,
    ) -> None:
        finished_at = _utcnow().isoformat()
        with self._connect() as conn:
            conn.execute(
                """
                UPDATE backup_runs
                SET finished_at = ?,
                    status = ?,
                    size_bytes = COALESCE(?, size_bytes),
                    path = COALESCE(?, path),
                    remote = COALESCE(?, remote),
                    error = ?
                WHERE id = ?
                """,
                (finished_at, status, size_bytes, path, remote, error, run_id),
            )

    def get_last_backup_run(self) -> Optional[Dict[str, Any]]:
        with self._connect() as conn:
            row = conn.execute(
                """
                SELECT id, started_at, finished_at, status, size_bytes, path, remote, error
                FROM backup_runs
                WHERE status IN ('ok', 'uploaded')
                ORDER BY id DESC LIMIT 1
                """
            ).fetchone()
            if not row:
                row = conn.execute(
                    """
                    SELECT id, started_at, finished_at, status, size_bytes, path, remote, error
                    FROM backup_runs
                    ORDER BY id DESC LIMIT 1
                    """
                ).fetchone()
            return dict(row) if row else None

    def list_backup_runs(self, limit: int = 10) -> List[Dict[str, Any]]:
        with self._connect() as conn:
            rows = conn.execute(
                """
                SELECT id, started_at, finished_at, status, size_bytes, path, remote, error
                FROM backup_runs
                ORDER BY id DESC LIMIT ?
                """,
                (limit,),
            ).fetchall()
            return [dict(r) for r in rows]

    # === Token Accounting & Tariffs (Блок B) ===
    def get_model_price(self, model: str) -> tuple[float, float]:
        now = time.time()
        cached = self._price_cache.get(model)
        if cached and cached[2] > now:
            return (cached[0], cached[1])

        with self._connect() as conn:
            row = conn.execute(
                "SELECT prompt_rub_per_1k, completion_rub_per_1k FROM model_prices WHERE model = ?",
                (model,),
            ).fetchone()
            if row:
                p, c = float(row[0]), float(row[1])
            else:
                p, c = 0.0, 0.0

        self._price_cache[model] = (p, c, now + 300)
        return (p, c)

    def ensure_model_price(
        self,
        model: str,
        prompt_rub_per_1k: float,
        completion_rub_per_1k: float,
    ) -> bool:
        """Добавляет цену для модели, если её ещё нет. Существующую не перезаписывает.

        Возвращает True, если строка была создана.
        """
        if not model:
            return False
        with self._connect() as conn:
            row = conn.execute(
                "SELECT 1 FROM model_prices WHERE model = ? LIMIT 1", (model,)
            ).fetchone()
            if row:
                return False
            conn.execute(
                """
                INSERT INTO model_prices (model, prompt_rub_per_1k, completion_rub_per_1k, updated_at)
                VALUES (?, ?, ?, ?)
                """,
                (model, float(prompt_rub_per_1k), float(completion_rub_per_1k), _utcnow().isoformat()),
            )
        self._price_cache.pop(model, None)
        return True

    def save_model_price(self, model: str, prompt_rub_per_1k: float, completion_rub_per_1k: float) -> None:
        now_iso = _utcnow().isoformat()
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO model_prices (model, prompt_rub_per_1k, completion_rub_per_1k, updated_at)
                VALUES (?, ?, ?, ?)
                ON CONFLICT(model) DO UPDATE SET
                    prompt_rub_per_1k = excluded.prompt_rub_per_1k,
                    completion_rub_per_1k = excluded.completion_rub_per_1k,
                    updated_at = excluded.updated_at
                """,
                (model, prompt_rub_per_1k, completion_rub_per_1k, now_iso),
            )
        self._price_cache[model] = (float(prompt_rub_per_1k), float(completion_rub_per_1k), time.time() + 300)

    def list_model_prices(self) -> List[Dict[str, Any]]:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT model, prompt_rub_per_1k, completion_rub_per_1k, updated_at FROM model_prices ORDER BY model"
            ).fetchall()
            return [dict(r) for r in rows]

    def list_plans(self) -> List[Dict[str, Any]]:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT slug, name, monthly_rub, included_messages, included_tokens, overage_rub_per_1k, is_active FROM plans ORDER BY monthly_rub ASC"
            ).fetchall()
            return [dict(r) for r in rows]

    def get_plan(self, slug: str) -> Optional[Dict[str, Any]]:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT slug, name, monthly_rub, included_messages, included_tokens, overage_rub_per_1k, is_active FROM plans WHERE slug = ?",
                (slug,),
            ).fetchone()
            return dict(row) if row else None

    def save_plan(
        self,
        slug: str,
        name: str,
        monthly_rub: float,
        included_messages: Optional[int],
        included_tokens: Optional[int],
        overage_rub_per_1k: float = 0.0,
        is_active: int = 1,
    ) -> None:
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO plans (slug, name, monthly_rub, included_messages, included_tokens, overage_rub_per_1k, is_active)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(slug) DO UPDATE SET
                    name = excluded.name,
                    monthly_rub = excluded.monthly_rub,
                    included_messages = excluded.included_messages,
                    included_tokens = excluded.included_tokens,
                    overage_rub_per_1k = excluded.overage_rub_per_1k,
                    is_active = excluded.is_active
                """,
                (slug, name, monthly_rub, included_messages, included_tokens, overage_rub_per_1k, is_active),
            )

    def set_tenant_plan(self, tenant_slug: str, plan_slug: str) -> None:
        with self._connect() as conn:
            conn.execute("UPDATE tenants SET plan = ? WHERE slug = ?", (plan_slug, tenant_slug))

    def record_llm_usage(
        self,
        tenant_slug: str,
        company_slug: str,
        channel: str,
        chat_id: Optional[str],
        model: str,
        prompt_tokens: int,
        completion_tokens: int,
        total_tokens: int,
        cost_rub: float,
    ) -> int:
        now_iso = _utcnow().isoformat()
        with self._connect() as conn:
            cur = conn.execute(
                """
                INSERT INTO llm_usage (
                    tenant_slug, company_slug, channel, chat_id,
                    model, prompt_tokens, completion_tokens, total_tokens,
                    cost_rub, created_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    tenant_slug, company_slug, channel, str(chat_id or ""),
                    model, prompt_tokens, completion_tokens, total_tokens,
                    cost_rub, now_iso,
                ),
            )
            return int(cur.lastrowid)

    def get_tenant_month_usage(self, tenant_slug: str, month_start: str) -> Dict[str, Any]:
        with self._connect() as conn:
            row = conn.execute(
                """
                SELECT
                    COUNT(*) as messages,
                    COALESCE(SUM(total_tokens), 0) as tokens,
                    ROUND(COALESCE(SUM(cost_rub), 0.0), 4) as cost_rub
                FROM llm_usage
                WHERE tenant_slug = ? AND created_at >= ?
                """,
                (tenant_slug, month_start),
            ).fetchone()
            return {
                "messages": int(row[0]) if row else 0,
                "tokens": int(row[1]) if row else 0,
                "cost_rub": float(row[2]) if row else 0.0,
            }

    def get_usage_summary(self, tenant_slug: Optional[str] = None, days: int = 30) -> Dict[str, Any]:
        cutoff = (_utcnow() - timedelta(days=days)).isoformat()
        where_clause = "WHERE created_at >= ?"
        params: list[Any] = [cutoff]
        if tenant_slug:
            where_clause += " AND tenant_slug = ?"
            params.append(tenant_slug)

        with self._connect() as conn:
            totals_row = conn.execute(
                f"""
                SELECT
                    COUNT(*),
                    COALESCE(SUM(prompt_tokens), 0),
                    COALESCE(SUM(completion_tokens), 0),
                    COALESCE(SUM(total_tokens), 0),
                    ROUND(COALESCE(SUM(cost_rub), 0.0), 2)
                FROM llm_usage
                {where_clause}
                """,
                params,
            ).fetchone()

            by_model_rows = conn.execute(
                f"""
                SELECT
                    model,
                    COUNT(*) as messages,
                    COALESCE(SUM(prompt_tokens), 0) as prompt_tokens,
                    COALESCE(SUM(completion_tokens), 0) as completion_tokens,
                    COALESCE(SUM(total_tokens), 0) as total_tokens,
                    ROUND(COALESCE(SUM(cost_rub), 0.0), 2) as cost_rub
                FROM llm_usage
                {where_clause}
                GROUP BY model
                ORDER BY cost_rub DESC
                """,
                params,
            ).fetchall()

            by_day_rows = conn.execute(
                f"""
                SELECT
                    substr(created_at, 1, 10) as day,
                    COUNT(*) as messages,
                    COALESCE(SUM(total_tokens), 0) as total_tokens,
                    ROUND(COALESCE(SUM(cost_rub), 0.0), 2) as cost_rub
                FROM llm_usage
                {where_clause}
                GROUP BY day
                ORDER BY day ASC
                """,
                params,
            ).fetchall()

            return {
                "messages": int(totals_row[0]) if totals_row else 0,
                "prompt_tokens": int(totals_row[1]) if totals_row else 0,
                "completion_tokens": int(totals_row[2]) if totals_row else 0,
                "total_tokens": int(totals_row[3]) if totals_row else 0,
                "cost_rub": float(totals_row[4]) if totals_row else 0.0,
                "by_model": [dict(r) for r in by_model_rows],
                "by_day": [dict(r) for r in by_day_rows],
            }

    def get_all_tenants_usage_summary(self, days: int = 30) -> List[Dict[str, Any]]:
        cutoff = (_utcnow() - timedelta(days=days)).isoformat()
        with self._connect() as conn:
            tenants = conn.execute("SELECT slug, name, plan, status FROM tenants ORDER BY id ASC").fetchall()
            plans_dict = {
                r["slug"]: dict(r)
                for r in conn.execute("SELECT slug, name, included_messages, included_tokens FROM plans").fetchall()
            }

            result = []
            for t in tenants:
                tslug = t["slug"]
                p_slug = t["plan"] or "basic"
                p_info = plans_dict.get(p_slug, {})
                inc_msgs = p_info.get("included_messages")
                inc_tokens = p_info.get("included_tokens")

                row = conn.execute(
                    """
                    SELECT
                        COUNT(*),
                        COALESCE(SUM(total_tokens), 0),
                        ROUND(COALESCE(SUM(cost_rub), 0.0), 2)
                    FROM llm_usage
                    WHERE tenant_slug = ? AND created_at >= ?
                    """,
                    (tslug, cutoff),
                ).fetchone()

                msgs = int(row[0]) if row else 0
                tokens = int(row[1]) if row else 0
                cost = float(row[2]) if row else 0.0

                limit_pct = None
                if inc_msgs and inc_msgs > 0:
                    limit_pct = round(100.0 * msgs / inc_msgs, 1)

                result.append(
                    {
                        "tenant_slug": tslug,
                        "name": t["name"],
                        "plan": p_slug,
                        "plan_name": p_info.get("name", p_slug),
                        "messages": msgs,
                        "tokens": tokens,
                        "cost_rub": cost,
                        "included_messages": inc_msgs,
                        "included_tokens": inc_tokens,
                        "limit_pct": limit_pct,
                    }
                )
            return result

    # === Onboarding Wizard (Блок C) ===
    def get_onboarding(self, tenant_slug: str) -> Optional[Dict[str, Any]]:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT tenant_slug, step, draft_json, completed_at, updated_at FROM onboarding WHERE tenant_slug = ?",
                (tenant_slug,),
            ).fetchone()
            if not row:
                return None
            res = dict(row)
            try:
                res["draft"] = json.loads(res["draft_json"])
            except Exception:
                res["draft"] = {}
            return res

    def save_onboarding(
        self,
        tenant_slug: str,
        step: int,
        draft_data: Dict[str, Any],
        completed: bool = False,
    ) -> Dict[str, Any]:
        now_iso = _utcnow().isoformat()
        draft_json = json.dumps(draft_data, ensure_ascii=False)
        completed_at = now_iso if completed else None

        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO onboarding (tenant_slug, step, draft_json, completed_at, updated_at)
                VALUES (?, ?, ?, ?, ?)
                ON CONFLICT(tenant_slug) DO UPDATE SET
                    step = excluded.step,
                    draft_json = excluded.draft_json,
                    completed_at = COALESCE(excluded.completed_at, onboarding.completed_at),
                    updated_at = excluded.updated_at
                """,
                (tenant_slug, step, draft_json, completed_at, now_iso),
            )
        return {
            "tenant_slug": tenant_slug,
            "step": step,
            "draft": draft_data,
            "completed_at": completed_at,
            "updated_at": now_iso,
        }

    def is_onboarding_completed(self, tenant_slug: str) -> bool:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT completed_at FROM onboarding WHERE tenant_slug = ?",
                (tenant_slug,),
            ).fetchone()
            return bool(row and row["completed_at"])
