"""Мультибот: отдельный Telegram-бот на каждого клиента (тенанта).

Каждый активный тенант со своим токеном получает собственный Bot и Dispatcher,
опрос Telegram идёт параллельно в одном процессе. Падение одного бота не
останавливает остальные.
"""

import asyncio
import logging
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

from aiogram import Bot

from app.company_store import CompanyStore
from app.config import Settings
from app.llm import LLMService
from app.notifier import LeadNotifier
from app.storage import Storage
from app.telegram_bot import create_dispatcher

logger = logging.getLogger(__name__)


@dataclass
class TenantRuntime:
    slug: str
    name: str
    bot: Bot
    username: str = ""
    task: Optional[asyncio.Task] = None
    admin_ids: List[int] = field(default_factory=list)

    @property
    def alive(self) -> bool:
        return bool(self.task) and not self.task.done()


class BotManager:
    def __init__(
        self,
        settings: Settings,
        company_store: CompanyStore,
        storage: Storage,
        llm: LLMService,
        alert_manager: Optional[Any] = None,
        quota_service: Optional[Any] = None,
    ) -> None:
        self.settings = settings
        self.company_store = company_store
        self.storage = storage
        self.llm = llm
        self.alert_manager = alert_manager
        self.quota_service = quota_service
        self._runtimes: Dict[str, TenantRuntime] = {}
        self._start_lock = asyncio.Lock()

    # === Запуск ===
    async def start(self) -> List[asyncio.Task]:
        async with self._start_lock:
            tenants = self.storage.list_active_tenants_with_bots()
            if not tenants and self.settings.telegram_bot_token:
                tenants = [
                    {
                        "slug": "default",
                        "name": "Платформа",
                        "telegram_bot_token": self.settings.telegram_bot_token,
                        "owner_telegram_id": self.settings.default_notify_chat_id,
                    }
                ]

            started: List[asyncio.Task] = []
            for tenant in tenants:
                task = await self._start_tenant(tenant)
                if task:
                    started.append(task)

            if not started:
                logger.error("Ни один Telegram-бот не запущен: нет валидных токенов у тенантов")
            else:
                logger.info("Запущено ботов: %d (%s)", len(started), ", ".join(self._runtimes))
            return started

    async def _start_tenant(self, tenant: dict) -> Optional[asyncio.Task]:
        slug = tenant["slug"]
        token = (tenant.get("telegram_bot_token") or "").strip()
        if not token:
            return None
            token = self.settings.telegram_bot_token
        if not token:
            return None

        bot = Bot(token=token)
        try:
            me = await bot.get_me()
        except Exception as exc:
            logger.error("Бот тенанта '%s' не отвечает: %s", slug, exc)
            await bot.session.close()
            if self.alert_manager:
                try:
                    await self.alert_manager.send_alert(
                        "bot_start_failed",
                        f"Не удалось запустить бота клиента *{slug}*:\n`{exc}`",
                    )
                except Exception:
                    pass
            return None

        username = me.username or ""
        self.storage.update_tenant_bot_username(slug, username)

        owner_id = tenant.get("owner_telegram_id")
        admin_ids: List[int] = []
        if owner_id:
            admin_ids.append(int(owner_id))
        if slug == "default":
            admin_ids.extend(int(i) for i in self.settings.admin_ids)

        notifier = LeadNotifier(bot=bot, default_chat_id=owner_id or self.settings.default_notify_chat_id)
        dp = create_dispatcher(
            self.settings,
            self.company_store,
            self.storage,
            self.llm,
            notifier,
            tenant_slug=slug,
            admin_ids=admin_ids,
            quota_service=self.quota_service,
            alert_manager=self.alert_manager,
        )
        task = asyncio.create_task(self._poll_forever(dp, bot, slug), name=f"tg:{slug}")
        self._runtimes[slug] = TenantRuntime(
            slug=slug, name=tenant.get("name", slug), bot=bot, username=username,
            task=task, admin_ids=admin_ids,
        )
        logger.info("Бот @%s запущен для клиента '%s'", username or "?", slug)
        return task

    async def _poll_forever(self, dp, bot: Bot, slug: str) -> None:
        """Опрос Telegram с изоляцией ошибок: падение одного бота не роняет остальные."""
        while True:
            try:
                await dp.start_polling(bot)
                return
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                logger.exception("Бот клиента '%s' упал, перезапуск через 15 с: %s", slug, exc)
                if self.alert_manager:
                    try:
                        await self.alert_manager.send_alert(
                            "bot_crashed",
                            f"Бот клиента *{slug}* упал:\n`{exc}`\nАвтоматический перезапуск через 15 с.",
                        )
                    except Exception:
                        pass
                await asyncio.sleep(15)

    # === Информация ===
    def username_for(self, tenant_slug: Optional[str]) -> str:
        """Имя бота клиента. Пустая строка — если у клиента нет своего бота.

        Для tenant_slug=None возвращается первый доступный бот (системные нужды).
        """
        if tenant_slug:
            runtime = self._runtimes.get(tenant_slug)
            if runtime and runtime.username:
                return runtime.username
            tenant = self.storage.get_tenant(tenant_slug)
            if tenant and tenant.get("bot_username"):
                return tenant["bot_username"]
            return ""
        for runtime in self._runtimes.values():
            if runtime.username:
                return runtime.username
        return "Zayavka24AI_bot"

    def status(self) -> List[dict]:
        result = []
        for runtime in self._runtimes.values():
            result.append(
                {
                    "tenant_slug": runtime.slug,
                    "name": runtime.name,
                    "username": runtime.username,
                    "alive": runtime.alive,
                    "admin_ids": runtime.admin_ids,
                }
            )
        return result

    def is_running(self, tenant_slug: str) -> bool:
        runtime = self._runtimes.get(tenant_slug)
        return bool(runtime and runtime.alive)

    def primary_bot(self) -> Optional[Bot]:
        """Бот для системных уведомлений (заявки, алерты)."""
        runtime = self._runtimes.get("default")
        if runtime is None and self._runtimes:
            runtime = next(iter(self._runtimes.values()))
        return runtime.bot if runtime else None

    # === Перезапуск после изменения токенов ===
    async def restart(self, tenant_slug: Optional[str] = None) -> None:
        """Перезапускает конкретного бота или все, если slug не указан."""
        async with self._start_lock:
            if tenant_slug is None:
                await self._stop_all()
                tenants = self.storage.list_active_tenants_with_bots()
                for tenant in tenants:
                    await self._start_tenant(tenant)
                return

            old = self._runtimes.pop(tenant_slug, None)
            if old:
                await self._stop_runtime(old)

            tenant = self.storage.get_tenant(tenant_slug)
            if tenant and tenant.get("status") == "active":
                await self._start_tenant(tenant)

    async def stop_tenant(self, tenant_slug: str) -> None:
        """Останавливает и выгружает рантайм конкретного тенанта."""
        async with self._lock:
            old = self._runtimes.pop(tenant_slug, None)
            if old:
                await self._stop_runtime(old)

    async def _stop_runtime(self, runtime: TenantRuntime) -> None:
        if runtime.task and not runtime.task.done():
            runtime.task.cancel()
            try:
                await runtime.task
            except (asyncio.CancelledError, Exception):
                pass
        try:
            await runtime.bot.session.close()
        except Exception:
            pass

    async def _stop_all(self) -> None:
        for runtime in list(self._runtimes.values()):
            await self._stop_runtime(runtime)
        self._runtimes.clear()

    async def stop(self) -> None:
        await self._stop_all()
