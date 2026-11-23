import asyncio
import logging

import uvicorn

from app.alerting import AlertManager
from app.auth import AuthService
from app.avito import AvitoClient, AvitoWebhookHandler
from app.bot_manager import BotManager
from app.company_store import CompanyStore
from app.config import get_settings
from app.llm import LLMService
from app.maintenance import BackupScheduler, compute_deep_health
from app.notifier import LeadNotifier
from app.quotas import QuotaService
from app.server import create_app
from app.storage import Storage

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger(__name__)


async def run() -> None:
    settings = get_settings()
    if not settings.llm_api_key:
        raise RuntimeError("LLM_API_KEY is not set. Get a key on ProxyAPI.ru or VseGPT.ru.")

    company_store = CompanyStore(settings.companies_dir)
    storage = Storage(settings.db_path)

    # Цена для модели из LLM_MODEL: без неё стоимость всегда считалась бы нулевой
    if storage.ensure_model_price(
        settings.llm_model,
        settings.llm_price_prompt_rub_per_1k,
        settings.llm_price_completion_rub_per_1k,
    ):
        logger.warning(
            "Для модели '%s' не было цены — добавлена заглушка %.2f/%.2f ₽ за 1k токенов. "
            "Укажите реальные цены в разделе «Расходы» или через POST /api/model-prices.",
            settings.llm_model,
            settings.llm_price_prompt_rub_per_1k,
            settings.llm_price_completion_rub_per_1k,
        )

    llm = LLMService(
        api_key=settings.llm_api_key,
        base_url=settings.llm_base_url,
        model=settings.llm_model,
        fallback_models=settings.llm_fallback_models,
        temperature=settings.llm_temperature,
        timeout=settings.llm_timeout_sec,
        guardrails_mode=settings.guardrails_mode,
        storage=storage,
    )

    # Кабинет клиента: пользователи, роли, сессии
    auth = AuthService(settings, storage)
    auth.bootstrap()

    # Алерты и мониторинг (Блок A)
    alert_manager = AlertManager(
        bot_manager=None,
        default_chat_id=settings.default_notify_chat_id,
        enabled=settings.alerts_enabled,
        cooldown_minutes=settings.alert_cooldown_minutes,
    )

    # Учёт токенов и лимиты тарифов (Блок B)
    quota_service = QuotaService(storage, alert_manager=alert_manager)

    # Мультибот: свой Telegram-бот на каждого клиента
    bot_manager = BotManager(
        settings, company_store, storage, llm,
        alert_manager=alert_manager,
        quota_service=quota_service,
    )
    alert_manager.bot_manager = bot_manager
    tasks: list[asyncio.Task] = list(await bot_manager.start())

    # Планировщик бэкапов (Блок A)
    backup_scheduler = BackupScheduler(
        storage=storage,
        settings=settings,
        bot_manager=bot_manager,
        alert_manager=alert_manager,
    )
    tasks.append(backup_scheduler.start())

    # Legacy Avito migration into DB accounts (K6)
    if settings.avito_enabled and settings.avito_client_id:
        existing_accounts = storage.list_avito_accounts()
        if not existing_accounts:
            storage.save_avito_account(
                company_slug=settings.avito_default_company_slug,
                user_id=settings.avito_user_id or 0,
                client_id=settings.avito_client_id,
                client_secret=settings.avito_client_secret,
                webhook_secret=settings.avito_webhook_secret or None,
                enabled=True,
            )
            logger.info("Auto-migrated legacy Avito credentials from .env to database avito_accounts.")

    notifier = LeadNotifier(
        bot=bot_manager.primary_bot(),
        default_chat_id=settings.default_notify_chat_id,
    )

    avito_client: AvitoClient | None = None
    avito_handler: AvitoWebhookHandler | None = None
    if settings.avito_enabled:
        if not (settings.avito_client_id and settings.avito_client_secret and settings.avito_user_id):
            raise RuntimeError(
                "AVITO_ENABLED=true, but AVITO_CLIENT_ID / AVITO_CLIENT_SECRET / AVITO_USER_ID are missing."
            )
        avito_client = AvitoClient(
            client_id=settings.avito_client_id,
            client_secret=settings.avito_client_secret,
            user_id=settings.avito_user_id,
        )
        avito_handler = AvitoWebhookHandler(
            avito_client=avito_client,
            company_store=company_store,
            storage=storage,
            llm=llm,
            notifier=notifier,
            default_company_slug=settings.avito_default_company_slug,
            manual_pause_hours=settings.avito_manual_pause_hours,
            max_history_messages=settings.max_history_messages,
            quota_service=quota_service,
            alert_manager=alert_manager,
        )

    from app.whatsapp import WhatsAppWebhookHandler
    whatsapp_handler = WhatsAppWebhookHandler(
        company_store=company_store,
        storage=storage,
        llm=llm,
        notifier=notifier,
        manual_pause_hours=settings.avito_manual_pause_hours,
        quota_service=quota_service,
    )

    if settings.dashboard_enabled or settings.avito_enabled:
        server_app = create_app(
            settings=settings,
            company_store=company_store,
            storage=storage,
            avito_handler=avito_handler,
            get_bot_username=lambda: bot_manager.username_for(None),
            auth=auth,
            bot_manager=bot_manager,
            llm=llm,
            whatsapp_handler=whatsapp_handler,
            notifier=notifier,
        )
        config = uvicorn.Config(
            server_app,
            host=settings.webhook_host,
            port=settings.webhook_port,
            log_level="warning",
        )
        server = uvicorn.Server(config)
        tasks.append(asyncio.create_task(server.serve(), name="web_server"))
        if settings.dashboard_enabled:
            logger.info("Кабинет клиента доступен на http://%s:%d/", settings.webhook_host, settings.webhook_port)
        if settings.avito_enabled:
            logger.info(
                "Avito webhook listening on http://%s:%d/webhook/avito",
                settings.webhook_host,
                settings.webhook_port,
            )

    # Фоновый самоконтроль здоровья системы (Блок A: каждые 5 минут)
    async def _health_monitor_loop() -> None:
        while True:
            try:
                await asyncio.sleep(300)
                h = compute_deep_health(settings, storage, bot_manager, avito_handler)
                st = h.get("status")
                if st in ("fail", "degraded"):
                    await alert_manager.send_alert(
                        f"system_health_{st}",
                        f"Самопроверка системы выявила статус: *{st}*\n"
                        f"БД: `{h['db']}`\n"
                        f"Боты: `{h['bots']}`\n"
                        f"Диск: `{h['disk']}`\n"
                        f"Бэкап: `{h['last_backup']}`",
                    )
            except asyncio.CancelledError:
                break
            except Exception as exc:
                logger.exception("Ошибка в фоновом мониторе здоровья: %s", exc)

    tasks.append(asyncio.create_task(_health_monitor_loop(), name="health_monitor"))

    logger.info("Запущено. Профилей компаний: %d", len(company_store.list_all()))
    try:
        results = await asyncio.gather(*tasks, return_exceptions=True)
        for result in results:
            if isinstance(result, Exception):
                logger.error("Фоновая задача завершилась с ошибкой: %s", result)
    finally:
        if avito_client:
            await avito_client.close()
        await backup_scheduler.stop()
        await bot_manager.stop()


if __name__ == "__main__":
    asyncio.run(run())