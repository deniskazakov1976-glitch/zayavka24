"""Аутентификация и роли кабинета клиента.

Пароли хранятся как PBKDF2-HMAC-SHA256 (соль на пользователя).
Сессия — случайный токен в HttpOnly-cookie, запись в таблице user_sessions.

Роли:
    platform_admin — владелец платформы: видит всех клиентов, управляет тенантами;
    client_admin   — администратор клиента: видит только свои компании, может менять;
    client_viewer  — сотрудник клиента: только чтение.
"""

import hashlib
import hmac
import logging
import secrets
from dataclasses import dataclass
from typing import Optional

from fastapi import HTTPException, Request, status

from app.config import Settings
from app.storage import Storage

logger = logging.getLogger(__name__)

PBKDF2_ITERATIONS = 260_000
COOKIE_NAME = "zayavka_session"

ROLE_PLATFORM_ADMIN = "platform_admin"
ROLE_CLIENT_ADMIN = "client_admin"
ROLE_CLIENT_VIEWER = "client_viewer"
VALID_ROLES = (ROLE_PLATFORM_ADMIN, ROLE_CLIENT_ADMIN, ROLE_CLIENT_VIEWER)


def hash_password(password: str) -> str:
    salt = secrets.token_hex(16)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt.encode("utf-8"), PBKDF2_ITERATIONS)
    return f"pbkdf2_sha256${PBKDF2_ITERATIONS}${salt}${digest.hex()}"


def verify_password(password: str, stored: str) -> bool:
    if not stored or not password:
        return False
    try:
        algo, iterations, salt, digest = stored.split("$", 3)
        if algo != "pbkdf2_sha256":
            return False
        expected = hashlib.pbkdf2_hmac(
            "sha256", password.encode("utf-8"), salt.encode("utf-8"), int(iterations)
        ).hex()
        return hmac.compare_digest(expected, digest)
    except Exception:
        return False


def new_session_token() -> str:
    return secrets.token_urlsafe(32)


@dataclass
class CurrentUser:
    id: int
    username: str
    role: str
    tenant_slug: Optional[str]

    @property
    def is_platform_admin(self) -> bool:
        return self.role == ROLE_PLATFORM_ADMIN

    @property
    def is_viewer(self) -> bool:
        return self.role == ROLE_CLIENT_VIEWER

    @property
    def can_write(self) -> bool:
        return self.role in (ROLE_PLATFORM_ADMIN, ROLE_CLIENT_ADMIN)

    @property
    def scope_tenant(self) -> Optional[str]:
        """None для платформенного администратора — значит «все клиенты»."""
        return None if self.is_platform_admin else (self.tenant_slug or "__none__")


class AuthService:
    def __init__(self, settings: Settings, storage: Storage) -> None:
        self.settings = settings
        self.storage = storage
        self._failed: dict[str, list[float]] = {}

    # === Инициализация ===
    def bootstrap(self) -> None:
        """Создаёт тенанта по умолчанию и администратора платформы из .env."""
        if not self.storage.get_tenant("default"):
            self.storage.save_tenant(
                slug="default",
                name="Платформа (по умолчанию)",
                telegram_bot_token=self.settings.telegram_bot_token,
                brand_name="Заявка 24/7",
                owner_telegram_id=self.settings.default_notify_chat_id,
                plan="internal",
            )
            logger.info("Создан tenant 'default' из .env")

        admin_user = self.settings.dashboard_user or "admin"
        if not self.storage.get_user_by_username(admin_user):
            password = self.settings.dashboard_password or secrets.token_urlsafe(12)
            self.storage.save_user(
                username=admin_user,
                password_hash=hash_password(password),
                role=ROLE_PLATFORM_ADMIN,
                tenant_slug=None,
            )
            if not self.settings.dashboard_password:
                logger.warning(
                    "DASHBOARD_PASSWORD не задан. Сгенерирован временный пароль для '%s': %s",
                    admin_user,
                    password,
                )
            else:
                logger.info("Создан администратор платформы '%s'", admin_user)

    # === Проверка учётных данных ===
    def _rate_limited(self, client_ip: str) -> bool:
        import time

        now = time.time()
        attempts = [t for t in self._failed.get(client_ip, []) if now - t < 600]
        self._failed[client_ip] = attempts
        return len(attempts) >= 8

    def _note_failure(self, client_ip: str) -> None:
        import time

        self._failed.setdefault(client_ip, []).append(time.time())

    def client_ip(self, request: Request) -> str:
        forwarded = request.headers.get("x-forwarded-for")
        if forwarded:
            return forwarded.split(",")[0].strip()
        return request.client.host if request.client else "unknown"

    def login(self, username: str, password: str, client_ip: str = "") -> Optional[str]:
        """Проверяет логин/пароль и возвращает токен сессии."""
        if client_ip and self._rate_limited(client_ip):
            raise HTTPException(
                status_code=status.HTTP_429_TOO_MANY_REQUESTS,
                detail="Слишком много попыток входа. Попробуйте через 10 минут.",
            )

        user = self.storage.get_user_by_username((username or "").strip())
        if not user or not user.get("is_active"):
            if client_ip:
                self._note_failure(client_ip)
            return None

        if not verify_password(password, user["password_hash"]):
            if client_ip:
                self._note_failure(client_ip)
            return None

        token = new_session_token()
        self.storage.create_user_session(token, int(user["id"]))
        logger.info("Вход в кабинет: %s (%s)", user["username"], user["role"])
        return token

    def logout(self, token: Optional[str]) -> None:
        if token:
            self.storage.delete_user_session(token)

    def user_from_token(self, token: Optional[str]) -> Optional[CurrentUser]:
        if not token:
            return None
        row = self.storage.get_user_session(token)
        if not row:
            return None
        return CurrentUser(
            id=int(row["user_id"]),
            username=row["username"],
            role=row["role"],
            tenant_slug=row["tenant_slug"],
        )

    # === Зависимости FastAPI ===
    def current_user(self, request: Request) -> CurrentUser:
        """Cookie-сессия, либо HTTP Basic (обратная совместимость для API и админа из .env)."""
        token = request.cookies.get(COOKIE_NAME)
        user = self.user_from_token(token)
        if user:
            return user

        auth_header = request.headers.get("authorization", "")
        if auth_header.lower().startswith("basic "):
            import base64

            try:
                raw = base64.b64decode(auth_header[6:]).decode("utf-8")
                username, _, password = raw.partition(":")
            except Exception:
                username = password = ""

            expected_user = self.settings.dashboard_user or "admin"
            expected_pass = self.settings.dashboard_password or ""
            if (
                expected_pass
                and hmac.compare_digest(username, expected_user)
                and hmac.compare_digest(password, expected_pass)
            ):
                stored = self.storage.get_user_by_username(expected_user)
                if stored:
                    return CurrentUser(
                        id=int(stored["id"]),
                        username=stored["username"],
                        role=stored["role"],
                        tenant_slug=stored["tenant_slug"],
                    )
                return CurrentUser(id=0, username=expected_user, role=ROLE_PLATFORM_ADMIN, tenant_slug=None)

        # Локальная разработка: без пароля пускаем только с самого сервера
        if not (self.settings.dashboard_password or "").strip():
            ip = self.client_ip(request)
            if ip in ("127.0.0.1", "::1", "localhost", "testclient"):
                return CurrentUser(
                    id=0,
                    username=self.settings.dashboard_user or "admin",
                    role=ROLE_PLATFORM_ADMIN,
                    tenant_slug=None,
                )

        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Требуется вход в кабинет",
            headers={"WWW-Authenticate": "Basic"},
        )

    def require_writer(self, request: Request) -> CurrentUser:
        user = self.current_user(request)
        if not user.can_write:
            raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Недостаточно прав")
        return user

    def require_platform_admin(self, request: Request) -> CurrentUser:
        user = self.current_user(request)
        if not user.is_platform_admin:
            raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Только для владельца платформы")
        return user
