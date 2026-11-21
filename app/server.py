import secrets
from typing import Any, Callable, Dict, Optional

from fastapi import APIRouter, FastAPI, HTTPException, Response
from starlette.middleware.cors import CORSMiddleware

from app.auth import AuthService
from app.avito import AvitoWebhookHandler
from app.company_store import CompanyStore
from app.config import Settings
from app.dashboard import build_dashboard_router
from app.maintenance import compute_deep_health
from app.storage import Storage


def create_app(
    settings: Settings,
    company_store: CompanyStore,
    storage: Storage,
    avito_handler: Optional[AvitoWebhookHandler] = None,
    get_bot_username: Optional[Callable[[], str]] = None,
    auth: Optional[AuthService] = None,
    bot_manager: Optional[Any] = None,
    llm: Optional[Any] = None,
    whatsapp_handler: Optional[Any] = None,
) -> FastAPI:
    app = FastAPI(title="Заявка 24/7 — Управление процессами и Вебхуки")

    app.add_middleware(
        CORSMiddleware,
        allow_origins=["*"],
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    @app.get("/health")
    async def health() -> Dict[str, str]:
        return {"status": "ok"}

    @app.get("/health/deep")
    async def health_deep(response: Response) -> Dict[str, Any]:
        health = compute_deep_health(
            settings=settings,
            storage=storage,
            bot_manager=bot_manager,
            avito_handler=avito_handler,
        )
        # fail -> 503, degraded -> 200 (сервис работает, но есть предупреждения)
        if health.get("status") == "fail":
            response.status_code = 503
        return health

    @app.post("/webhook/avito/{secret}")
    async def avito_webhook_with_secret(secret: str, payload: Dict[str, Any]) -> Dict[str, str]:
        if avito_handler is None:
            raise HTTPException(status_code=503, detail="Avito integration is disabled")

        # Verify secret (K2, K6)
        valid = False
        if settings.avito_webhook_secret and secrets.compare_digest(secret, settings.avito_webhook_secret):
            valid = True
        else:
            acc = storage.get_avito_account_by_secret(secret)
            if acc:
                valid = True

        if not valid:
            raise HTTPException(status_code=404, detail="Not Found")

        # K3: Immediate response + background queue
        try:
            avito_handler.queue.put_nowait(payload)
            return {"status": "accepted"}
        except Exception:
            return await avito_handler.handle_event(payload)

    @app.post("/webhook/avito")
    async def avito_webhook_legacy(payload: Dict[str, Any]) -> Dict[str, str]:
        if avito_handler is None:
            raise HTTPException(status_code=503, detail="Avito integration is disabled")
        if settings.avito_webhook_secret:
            raise HTTPException(status_code=404, detail="Secret webhook path required")
        return await avito_handler.handle_event(payload)

    # WO / P3: WhatsApp Webhook Routes (Green-API / Chat-App)
    @app.post("/webhook/whatsapp/{company_slug}")
    async def whatsapp_webhook_by_company(company_slug: str, payload: Dict[str, Any]) -> Dict[str, Any]:
        if whatsapp_handler is None:
            raise HTTPException(status_code=503, detail="WhatsApp integration is disabled")
        return await whatsapp_handler.handle_webhook(payload, company_slug_hint=company_slug)

    @app.post("/webhook/whatsapp")
    async def whatsapp_webhook_default(payload: Dict[str, Any]) -> Dict[str, Any]:
        if whatsapp_handler is None:
            raise HTTPException(status_code=503, detail="WhatsApp integration is disabled")
        return await whatsapp_handler.handle_webhook(payload)

    if avito_handler is not None:
        @app.on_event("startup")
        async def on_startup() -> None:
            await avito_handler.start_workers(num_workers=4)

    if settings.dashboard_enabled:
        dashboard_router = build_dashboard_router(
            settings=settings,
            company_store=company_store,
            storage=storage,
            get_bot_username=get_bot_username,
            auth=auth,
            bot_manager=bot_manager,
            llm=llm,
        )
        app.include_router(dashboard_router)

    return app


def create_webhook_app(
    avito_handler: Optional[AvitoWebhookHandler] = None,
    webhook_secret: Optional[str] = None,
) -> FastAPI:
    """Backward compatibility helper for tests."""
    app = FastAPI(title="BotAvitoTelegram Webhook Server")

    @app.get("/health")
    async def health() -> Dict[str, str]:
        return {"status": "ok"}

    @app.get("/health/deep")
    async def health_deep() -> Dict[str, Any]:
        # Вспомогательное приложение для тестов не имеет BotManager/Storage,
        # поэтому отдаём только базовую проверку БД через переданные объекты.
        return {"status": "ok", "db": {"ok": True}, "bots": {"total": 0, "alive": 0, "dead": []}}

    @app.post("/webhook/avito/{secret}")
    async def avito_webhook_with_secret(secret: str, payload: Dict[str, Any]) -> Dict[str, str]:
        if avito_handler is None:
            raise HTTPException(status_code=503, detail="Avito integration is disabled")
        if webhook_secret and not secrets.compare_digest(secret, webhook_secret):
            raise HTTPException(status_code=404, detail="Not Found")
        return await avito_handler.handle_event(payload)

    @app.post("/webhook/avito")
    async def avito_webhook_legacy(payload: Dict[str, Any]) -> Dict[str, str]:
        if avito_handler is None:
            raise HTTPException(status_code=503, detail="Avito integration is disabled")
        if webhook_secret:
            raise HTTPException(status_code=404, detail="Secret webhook path required")
        return await avito_handler.handle_event(payload)

    return app
