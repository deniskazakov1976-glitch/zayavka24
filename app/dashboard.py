import csv
import io
import logging
import re
import secrets
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Literal, Optional

from fastapi import APIRouter, Depends, HTTPException, Request, Response, status
from fastapi.responses import HTMLResponse, RedirectResponse
from pydantic import BaseModel, Field

from app.auth import (
    COOKIE_NAME,
    ROLE_CLIENT_ADMIN,
    ROLE_CLIENT_VIEWER,
    ROLE_PLATFORM_ADMIN,
    AuthService,
    CurrentUser,
    hash_password,
)
from app.company_store import CompanyStore
from app.config import Settings, update_env_file
from app.models import CompanyProfile
from app.price_parser import parse_price_list
from app.storage import Storage

logger = logging.getLogger(__name__)

TEMPLATES_DIR = Path(__file__).parent / "templates"
TEMPLATE_PATH = TEMPLATES_DIR / "dashboard.html"
LANDING_PATH = TEMPLATES_DIR / "landing.html"
LOGIN_PATH = TEMPLATES_DIR / "login.html"
ONBOARDING_PATH = TEMPLATES_DIR / "onboarding.html"

SESSION_TTL_HOURS = 72


class AvitoConfigRequest(BaseModel):
    avito_enabled: bool
    avito_client_id: str = ""
    avito_client_secret: Optional[str] = None
    avito_user_id: Optional[int] = None
    avito_default_company_slug: str = "demo_detailing"
    avito_manual_pause_hours: int = 24


class AvitoTestRequest(BaseModel):
    client_id: Optional[str] = None
    client_secret: Optional[str] = None


class CreateAvitoAccountRequest(BaseModel):
    company_slug: str
    user_id: int
    client_id: str
    client_secret: str
    enabled: bool = True


class AvitoWebhookRegisterRequest(BaseModel):
    webhook_url: str
    client_id: Optional[str] = None
    client_secret: Optional[str] = None


class LearnGapRequest(BaseModel):
    answer: str


class CreateCompanyRequest(BaseModel):
    slug: str = Field(..., min_length=1, pattern=r"^[a-zA-Z0-9_-]+$")
    company_name: str
    city: str
    niche: str = "автобизнес"
    manager_name: str = "Александр"
    manager_role: str = "старший мастер"
    address: str = ""
    work_hours: str = "Ежедневно с 10:00 до 20:00"
    discount_offer: str = "скидку 10% на первый визит"
    media_reply_promise: str = "в течение 15–30 минут в рабочее время"
    notify_chat_id: Optional[int] = None
    crm_type: Literal["none", "webhook", "amocrm", "bitrix24"] = "none"
    crm_webhook_url: str = ""
    crm_api_token: str = ""
    crm_subdomain: str = ""
    whatsapp_enabled: bool = False
    whatsapp_instance_id: str = ""
    whatsapp_api_token: str = ""
    whatsapp_webhook_secret: str = ""
    services: List[str] = Field(default_factory=list)
    advantages: List[str] = Field(default_factory=list)
    faq: List[str] = Field(default_factory=list)
    extra_rules: List[str] = Field(default_factory=list)


class UpdateCompanyRequest(BaseModel):
    company_name: Optional[str] = None
    city: Optional[str] = None
    niche: Optional[str] = None
    manager_name: Optional[str] = None
    manager_role: Optional[str] = None
    address: Optional[str] = None
    work_hours: Optional[str] = None
    discount_offer: Optional[str] = None
    media_reply_promise: Optional[str] = None
    notify_chat_id: Optional[int] = None
    crm_type: Optional[Literal["none", "webhook", "amocrm", "bitrix24"]] = None
    crm_webhook_url: Optional[str] = None
    crm_api_token: Optional[str] = None
    crm_subdomain: Optional[str] = None
    whatsapp_enabled: Optional[bool] = None
    whatsapp_instance_id: Optional[str] = None
    whatsapp_api_token: Optional[str] = None
    whatsapp_webhook_secret: Optional[str] = None
    services: Optional[List[str]] = None
    advantages: Optional[List[str]] = None
    faq: Optional[List[str]] = None
    extra_rules: Optional[List[str]] = None


class CreateTenantRequest(BaseModel):
    slug: str = Field(..., min_length=2, max_length=40, pattern=r"^[a-zA-Z0-9_-]+$")
    name: str
    brand_name: str = ""
    telegram_bot_token: str = ""
    owner_telegram_id: Optional[int] = None
    plan: str = "basic"
    admin_username: Optional[str] = None
    admin_password: Optional[str] = None


class UpdateTenantBotRequest(BaseModel):
    telegram_bot_token: str
    owner_telegram_id: Optional[int] = None


class CreateUserRequest(BaseModel):
    username: str = Field(..., min_length=3, max_length=40, pattern=r"^[a-zA-Z0-9_.-]+$")
    password: str = Field(..., min_length=8)
    role: str = ROLE_CLIENT_VIEWER
    tenant_slug: Optional[str] = None


class PlanRequest(BaseModel):
    slug: str = Field(..., min_length=2, max_length=40, pattern=r"^[a-zA-Z0-9_-]+$")
    name: str
    monthly_rub: float = 0.0
    included_messages: Optional[int] = None
    included_tokens: Optional[int] = None
    overage_rub_per_1k: float = 0.0
    is_active: int = 1


class UpdateTenantPlanRequest(BaseModel):
    plan: str


class ModelPriceRequest(BaseModel):
    model: str
    prompt_rub_per_1k: float = 0.0
    completion_rub_per_1k: float = 0.0


class OnboardingStepRequest(BaseModel):
    step: int
    data: Dict[str, Any] = Field(default_factory=dict)


class ParsePriceRequest(BaseModel):
    text: str


class TestDialogRequest(BaseModel):
    message: str


class SimulatorChatRequest(BaseModel):
    message: str = Field(..., min_length=1, max_length=1000)
    history: List[Dict[str, str]] = Field(default_factory=list)
    company_slug: str = "detailing_premium"


class LandingLeadRequest(BaseModel):
    phone: str = Field(..., min_length=5, max_length=50)
    name: str = Field(default="", max_length=100)
    service: str = Field(default="", max_length=200)


TRANSLIT_MAP = {
    'а': 'a', 'б': 'b', 'в': 'v', 'г': 'g', 'д': 'd', 'е': 'e', 'ё': 'yo',
    'ж': 'zh', 'з': 'z', 'и': 'i', 'й': 'y', 'к': 'k', 'л': 'l', 'м': 'm',
    'н': 'n', 'о': 'o', 'п': 'p', 'р': 'r', 'с': 's', 'т': 't', 'у': 'u',
    'ф': 'f', 'х': 'kh', 'ц': 'ts', 'ч': 'ch', 'ш': 'sh', 'щ': 'shch',
    'ъ': '', 'ы': 'y', 'ь': '', 'э': 'e', 'ю': 'yu', 'я': 'ya'
}


def slugify_russian(text: str) -> str:
    import re
    res = []
    for ch in text.lower():
        if ch in TRANSLIT_MAP:
            res.append(TRANSLIT_MAP[ch])
        elif ch.isalnum():
            res.append(ch)
        elif ch in (' ', '-', '_'):
            res.append('-')
    cleaned = re.sub(r'-+', '-', ''.join(res)).strip('-')
    return cleaned or "company"


def build_dashboard_router(
    settings: Settings,
    company_store: CompanyStore,
    storage: Storage,
    get_bot_username: Optional[Any] = None,
    auth: Optional[AuthService] = None,
    bot_manager: Optional[Any] = None,
    llm: Optional[Any] = None,
    notifier: Optional[Any] = None,
) -> APIRouter:
    router = APIRouter()
    auth_service = auth or AuthService(settings, storage)

    current_user = auth_service.current_user
    require_writer = auth_service.require_writer
    require_platform_admin = auth_service.require_platform_admin

    def scope_slugs(user: CurrentUser) -> Optional[List[str]]:
        return company_store.slugs_for_tenant(user.scope_tenant)

    def _resolve_bot_username(tenant_slug: Optional[str] = None) -> str:
        if bot_manager is not None:
            try:
                name = bot_manager.username_for(tenant_slug)
                if name:
                    return name
                if tenant_slug and tenant_slug != "default":
                    return ""  # у клиента нет своего бота — чужой не подставляем
            except Exception:
                pass
        if tenant_slug:
            tenant = storage.get_tenant(tenant_slug)
            if tenant and tenant.get("bot_username"):
                return tenant["bot_username"]
            if tenant_slug != "default":
                return ""
        if callable(get_bot_username):
            try:
                uname = get_bot_username()
                if uname:
                    return uname
            except Exception:
                pass
        return "Zayavka24AI_bot"

    def demo_link_for(profile: CompanyProfile) -> str:
        """Ссылка на демо-бота клиента.

        Пустая строка, если у клиента нет собственного бота: платформенный бот
        не должен показывать чужую компанию (у него своя область видимости).
        """
        tenant_slug = profile.tenant_slug or "default"
        uname = _resolve_bot_username(tenant_slug)
        if tenant_slug != "default":
            own = storage.get_tenant(tenant_slug) or {}
            if not (own.get("telegram_bot_token") and uname):
                return ""
        return f"https://t.me/{uname}?start={profile.slug}"

    def ensure_owner(user: CurrentUser, slug: str) -> None:
        if not company_store.owns_company(user.scope_tenant, slug):
            raise HTTPException(status_code=404, detail="Компания не найдена")

    # === Вход / выход ===
    @router.get("/login", response_class=HTMLResponse)
    async def login_page(request: Request) -> Any:
        if auth_service.user_from_token(request.cookies.get(COOKIE_NAME)):
            return RedirectResponse("/", status_code=302)
        return get_login_html()

    @router.post("/login")
    async def login_submit(request: Request) -> Any:
        form = await request.form()
        username = str(form.get("username", "")).strip()
        password = str(form.get("password", ""))
        ip = auth_service.client_ip(request)
        try:
            token = auth_service.login(username, password, ip)
        except HTTPException as exc:
            return RedirectResponse(f"/login?error={exc.status_code}", status_code=303)
        if not token:
            return RedirectResponse("/login?error=1", status_code=303)

        response = RedirectResponse("/", status_code=303)
        response.set_cookie(
            COOKIE_NAME,
            token,
            max_age=SESSION_TTL_HOURS * 3600,
            httponly=True,
            samesite="lax",
            secure=request.url.scheme == "https" or request.headers.get("x-forwarded-proto") == "https",
            path="/",
        )
        return response

    @router.get("/logout")
    async def logout(request: Request) -> Any:
        auth_service.logout(request.cookies.get(COOKIE_NAME))
        response = RedirectResponse("/login", status_code=303)
        response.delete_cookie(COOKIE_NAME, path="/")
        return response

    # === Кто я ===
    @router.get("/api/me")
    async def api_me(user: CurrentUser = Depends(current_user)) -> Dict[str, Any]:
        tenant = storage.get_tenant(user.tenant_slug) if user.tenant_slug else None
        return {
            "username": user.username,
            "role": user.role,
            "tenant_slug": user.tenant_slug,
            "tenant_name": tenant["name"] if tenant else ("Платформа" if user.is_platform_admin else ""),
            "is_platform_admin": user.is_platform_admin,
            "can_write": user.can_write,
        }

    # === Статистика и данные (с изоляцией) ===
    @router.get("/api/stats")
    async def api_stats(user: CurrentUser = Depends(current_user)) -> Dict[str, Any]:
        slugs = scope_slugs(user)
        stats = storage.get_stats(company_slugs=slugs)
        companies = company_store.list_for_tenant(user.scope_tenant)
        bot_username = _resolve_bot_username(user.scope_tenant)
        if not user.is_platform_admin and not bot_username:
            bot_username = ""
        return {
            **stats,
            "companies_count": len(companies),
            "bot_username": bot_username,
            "has_own_bot": bool(bot_username) or user.is_platform_admin,
            "llm_model": settings.llm_model,
            "avito_enabled": settings.avito_enabled,
        }

    @router.get("/api/companies")
    async def api_companies(user: CurrentUser = Depends(current_user)) -> List[Dict[str, Any]]:
        result = []
        for p in company_store.list_for_tenant(user.scope_tenant):
            item = p.model_dump()
            item["demo_link"] = demo_link_for(p)
            item["crm_has_token"] = bool(p.crm_api_token)
            if item.get("crm_api_token"):
                item["crm_api_token"] = "••••••••"
            item["whatsapp_has_token"] = bool(p.whatsapp_api_token)
            if item.get("whatsapp_api_token"):
                item["whatsapp_api_token"] = "••••••••"
            wh_secret = item.get("whatsapp_webhook_secret") or ""
            item["whatsapp_webhook_url"] = (
                f"/webhook/whatsapp?secret={wh_secret}" if wh_secret else "/webhook/whatsapp"
            )
            result.append(item)
        return result

    @router.post("/api/companies")
    async def api_create_company(
        payload: CreateCompanyRequest,
        user: CurrentUser = Depends(require_writer),
    ) -> Dict[str, Any]:
        # Клиент создаёт компанию в своём тенанте; владелец платформы — в тенанте по умолчанию
        tenant_slug = (user.tenant_slug or "default")

        existing = company_store.get(payload.slug)
        if existing and not company_store.owns_company(user.scope_tenant, payload.slug):
            raise HTTPException(status_code=409, detail="Такой slug уже занят другим клиентом")

        profile = CompanyProfile(**payload.model_dump(), tenant_slug=tenant_slug or "default")
        company_store.save(profile)
        company_store.reload()
        return {
            "status": "ok",
            "slug": profile.slug,
            "demo_link": demo_link_for(profile),
            "message": f"Компания «{profile.company_name}» успешно сохранена!",
        }

    @router.put("/api/companies/{slug}")
    async def api_update_company(
        slug: str,
        payload: UpdateCompanyRequest,
        user: CurrentUser = Depends(require_writer),
    ) -> Dict[str, Any]:
        ensure_owner(user, slug)
        existing = company_store.get(slug)
        if not existing:
            raise HTTPException(status_code=404, detail="Компания не найдена")

        update_dict = payload.model_dump(exclude_unset=True)
        # Preserve sensitive tokens if placeholder or not updated
        if "crm_api_token" in update_dict:
            if not update_dict["crm_api_token"] or update_dict["crm_api_token"] == "••••••••":
                update_dict.pop("crm_api_token")
        if "whatsapp_api_token" in update_dict:
            if not update_dict["whatsapp_api_token"] or update_dict["whatsapp_api_token"] == "••••••••":
                update_dict.pop("whatsapp_api_token")

        curr_data = existing.model_dump()
        curr_data.update(update_dict)
        updated_profile = CompanyProfile(**curr_data)
        company_store.save(updated_profile)
        company_store.reload()
        return {
            "status": "ok",
            "slug": updated_profile.slug,
            "demo_link": demo_link_for(updated_profile),
            "message": f"Профиль компании «{updated_profile.company_name}» обновлён!",
        }

    @router.delete("/api/companies/{slug}")
    async def api_delete_company(
        slug: str,
        user: CurrentUser = Depends(require_writer),
    ) -> Dict[str, Any]:
        ensure_owner(user, slug)
        existing = company_store.get(slug)
        if not existing:
            raise HTTPException(status_code=404, detail="Компания не найдена")

        deleted = company_store.delete(slug)
        return {
            "status": "ok",
            "deleted": deleted,
            "message": f"Компания «{slug}» удалена",
        }

    @router.get("/api/leads")
    async def api_leads(user: CurrentUser = Depends(current_user)) -> List[Dict[str, Any]]:
        leads = storage.list_recent_leads(limit=50, company_slugs=scope_slugs(user))
        return [le.model_dump() for le in leads]

    @router.get("/api/leads/export.csv")
    async def api_export_leads_csv(user: CurrentUser = Depends(current_user)) -> Response:
        leads = storage.list_recent_leads(limit=1000, company_slugs=scope_slugs(user))
        output = io.StringIO()
        # UTF-8 BOM so Excel opens Cyrillic properly
        output.write("\ufeff")
        writer = csv.writer(output, delimiter=";")
        writer.writerow([
            "ID",
            "Дата создания",
            "Канал",
            "Компания",
            "Имя клиента",
            "Телефон",
            "Автомобиль",
            "Интересующая услуга",
            "Ориентир цены",
            "Удобное время",
            "Комментарий / резюме",
            "Ссылка на чат Авито",
        ])
        for idx, le in enumerate(leads, 1):
            created_str = le.created_at.strftime("%Y-%m-%d %H:%M:%S") if le.created_at else ""
            writer.writerow([
                idx,
                created_str,
                le.channel,
                le.company_slug,
                le.client_name or "",
                le.phone or "",
                le.car_model or "",
                le.service_interest or "",
                le.estimated_price or "",
                le.preferred_time or "",
                le.summary or "",
                le.avito_chat_url or "",
            ])

        csv_content = output.getvalue().encode("utf-8")
        filename = f"leads_export_{datetime.now().strftime('%Y%m%d_%H%M%S')}.csv"
        return Response(
            content=csv_content,
            media_type="text/csv; charset=utf-8",
            headers={"Content-Disposition": f'attachment; filename="{filename}"'},
        )

    @router.get("/api/sessions")
    async def api_sessions(user: CurrentUser = Depends(current_user)) -> List[Dict[str, Any]]:
        return storage.list_sessions(limit=50, company_slugs=scope_slugs(user))

    @router.get("/api/sessions/{channel}/{chat_id}/history")
    async def api_session_history(
        channel: str, chat_id: str, user: CurrentUser = Depends(current_user)
    ) -> List[Dict[str, Any]]:
        owner_slug = storage.get_session_company(channel, chat_id)
        if owner_slug:
            ensure_owner(user, owner_slug)
        elif not user.is_platform_admin:
            raise HTTPException(status_code=404, detail="Диалог не найден")
        history = storage.get_history(channel, chat_id, limit=50)
        return [m.model_dump() for m in history]

    @router.get("/api/gaps")
    async def api_get_gaps(
        status_filter: Optional[str] = None,
        user: CurrentUser = Depends(current_user),
    ) -> List[Dict[str, Any]]:
        gaps = storage.list_knowledge_gaps(
            limit=50, status=status_filter, company_slugs=scope_slugs(user)
        )
        return [g.model_dump() for g in gaps]

    @router.post("/api/gaps/{gap_id}/learn")
    async def api_learn_gap(
        gap_id: int,
        payload: LearnGapRequest,
        user: CurrentUser = Depends(require_writer),
    ) -> Dict[str, Any]:
        gaps = storage.list_knowledge_gaps(limit=100, company_slugs=scope_slugs(user))
        target_gap = next((g for g in gaps if g.id == gap_id), None)
        if not target_gap:
            raise HTTPException(status_code=404, detail="Вопрос не найден")

        ans = payload.answer.strip()
        if not ans:
            raise HTTPException(status_code=400, detail="Ответ не может быть пустым")

        ensure_owner(user, target_gap.company_slug)
        company = company_store.get(target_gap.company_slug)
        if not company:
            raise HTTPException(status_code=404, detail="Компания не найдена")

        faq_entry = f"{target_gap.question} — {ans}"
        if faq_entry not in company.faq:
            company.faq.append(faq_entry)
            company_store.save(company)
            company_store.reload()

        storage.resolve_knowledge_gap(gap_id, status="learned", answer=ans)
        return {
            "status": "ok",
            "message": f"Бот успешно обучен! Ответ добавлен в базу знаний «{company.company_name}».",
        }

    @router.post("/api/gaps/{gap_id}/ignore")
    async def api_ignore_gap(gap_id: int, user: CurrentUser = Depends(require_writer)) -> Dict[str, Any]:
        gaps = storage.list_knowledge_gaps(limit=100, company_slugs=scope_slugs(user))
        if not any(g.id == gap_id for g in gaps):
            raise HTTPException(status_code=404, detail="Вопрос не найден")
        storage.resolve_knowledge_gap(gap_id, status="ignored")
        return {"status": "ok"}

    @router.post("/api/sessions/{channel}/{chat_id}/pause")
    @router.post("/api/sessions/{channel}/{chat_id}/toggle-pause")
    async def api_toggle_pause(
        channel: str, chat_id: str, user: CurrentUser = Depends(require_writer)
    ) -> Dict[str, Any]:
        owner_slug = storage.get_session_company(channel, chat_id)
        if owner_slug:
            ensure_owner(user, owner_slug)
        elif not user.is_platform_admin:
            raise HTTPException(status_code=404, detail="Диалог не найден")

        is_paused = storage.is_paused(channel, chat_id)
        if is_paused:
            storage.resume_session(channel, chat_id)
            new_state = False
        else:
            storage.pause_session(channel, chat_id, hours=settings.avito_manual_pause_hours)
            new_state = True
        return {"status": "ok", "is_paused": new_state}

    # === Авито ===
    @router.get("/api/avito/config")
    async def api_avito_config(user: CurrentUser = Depends(current_user)) -> Dict[str, Any]:
        wh_path = (
            f"/webhook/avito/{settings.avito_webhook_secret}"
            if settings.avito_webhook_secret
            else "/webhook/avito"
        )
        return {
            "avito_enabled": settings.avito_enabled,
            "avito_client_id": settings.avito_client_id if user.is_platform_admin else "",
            "avito_has_secret": bool(settings.avito_client_secret) if user.is_platform_admin else False,
            "avito_user_id": settings.avito_user_id if user.is_platform_admin else None,
            "avito_default_company_slug": settings.avito_default_company_slug,
            "avito_manual_pause_hours": settings.avito_manual_pause_hours,
            "webhook_path": wh_path,
            "public_base_url": settings.public_base_url,
        }

    @router.get("/api/avito/accounts")
    async def api_list_avito_accounts(user: CurrentUser = Depends(current_user)) -> List[Dict[str, Any]]:
        slugs = scope_slugs(user)
        accounts = storage.list_avito_accounts()
        if slugs is not None:
            accounts = [a for a in accounts if a["company_slug"] in slugs]
        for a in accounts:
            base = settings.public_base_url.rstrip("/") if settings.public_base_url else ""
            a["webhook_url"] = f"{base}/webhook/avito/{a['webhook_secret']}"
        return accounts

    @router.post("/api/avito/accounts")
    async def api_create_avito_account(
        payload: CreateAvitoAccountRequest,
        user: CurrentUser = Depends(require_writer),
    ) -> Dict[str, Any]:
        ensure_owner(user, payload.company_slug)
        acc = storage.save_avito_account(
            company_slug=payload.company_slug,
            user_id=payload.user_id,
            client_id=payload.client_id,
            client_secret=payload.client_secret,
            enabled=payload.enabled,
        )
        return {"status": "ok", "account": acc}

    @router.delete("/api/avito/accounts/{account_id}")
    async def api_delete_avito_account(
        account_id: int, user: CurrentUser = Depends(require_writer)
    ) -> Dict[str, Any]:
        slugs = scope_slugs(user)
        allowed = {a["id"] for a in storage.list_avito_accounts() if slugs is None or a["company_slug"] in slugs}
        if account_id not in allowed:
            raise HTTPException(status_code=404, detail="Аккаунт не найден")
        deleted = storage.delete_avito_account(account_id)
        return {"status": "ok", "deleted": deleted}

    @router.get("/api/guard-events")
    async def api_guard_events(
        limit: int = 50, user: CurrentUser = Depends(current_user)
    ) -> List[Dict[str, Any]]:
        return storage.list_guard_events(limit=limit, company_slugs=scope_slugs(user))

    @router.post("/api/avito/config")
    async def api_avito_save_config(
        payload: AvitoConfigRequest,
        user: CurrentUser = Depends(require_platform_admin),
    ) -> Dict[str, Any]:
        settings.avito_enabled = payload.avito_enabled
        settings.avito_client_id = payload.avito_client_id
        if payload.avito_client_secret:
            settings.avito_client_secret = payload.avito_client_secret
        settings.avito_user_id = payload.avito_user_id
        settings.avito_default_company_slug = payload.avito_default_company_slug
        settings.avito_manual_pause_hours = payload.avito_manual_pause_hours

        env_updates = {
            "AVITO_ENABLED": "true" if payload.avito_enabled else "false",
            "AVITO_CLIENT_ID": payload.avito_client_id,
            "AVITO_USER_ID": str(payload.avito_user_id or ""),
            "AVITO_DEFAULT_COMPANY_SLUG": payload.avito_default_company_slug,
            "AVITO_MANUAL_PAUSE_HOURS": str(payload.avito_manual_pause_hours),
        }
        if payload.avito_client_secret:
            env_updates["AVITO_CLIENT_SECRET"] = payload.avito_client_secret

        try:
            update_env_file(env_updates)
        except Exception as exc:
            logger.error("Failed to update .env: %s", exc)

        return {"status": "ok", "message": "Настройки Авито сохранены!"}

    @router.post("/api/avito/test-connection")
    async def api_avito_test_connection(
        payload: AvitoTestRequest,
        user: CurrentUser = Depends(require_platform_admin),
    ) -> Dict[str, Any]:
        client_id = payload.client_id or settings.avito_client_id
        client_secret = payload.client_secret or settings.avito_client_secret

        if not client_id or not client_secret:
            raise HTTPException(
                status_code=400,
                detail="Укажите Client ID и Client Secret для проверки связи с Авито",
            )

        import httpx

        try:
            async with httpx.AsyncClient(timeout=10.0) as http:
                resp = await http.post(
                    "https://api.avito.ru/token",
                    data={
                        "grant_type": "client_credentials",
                        "client_id": client_id,
                        "client_secret": client_secret,
                    },
                    headers={"Content-Type": "application/x-www-form-urlencoded"},
                )
                if resp.status_code == 200:
                    data = resp.json()
                    expires_in = data.get("expires_in", 3600)
                    return {
                        "status": "ok",
                        "message": f"Связь с Авито установлена! Токен успешно получен (срок действия: {expires_in} сек).",
                    }
                return {
                    "status": "error",
                    "message": f"Авито вернул ошибку ({resp.status_code}): {resp.text}",
                }
        except Exception as exc:
            return {"status": "error", "message": f"Не удалось подключиться к api.avito.ru: {exc}"}

    @router.post("/api/avito/register-webhook")
    async def api_avito_register_webhook(
        payload: AvitoWebhookRegisterRequest,
        user: CurrentUser = Depends(require_platform_admin),
    ) -> Dict[str, Any]:
        client_id = payload.client_id or settings.avito_client_id
        client_secret = payload.client_secret or settings.avito_client_secret

        if not client_id or not client_secret:
            raise HTTPException(
                status_code=400,
                detail="Не указаны Client ID / Client Secret для регистрации вебхука",
            )
        if not payload.webhook_url.startswith("https://"):
            raise HTTPException(
                status_code=400,
                detail="Авито требует публичный HTTPS URL для вебхука (начиная с https://)",
            )

        import httpx

        try:
            async with httpx.AsyncClient(timeout=10.0) as http:
                token_resp = await http.post(
                    "https://api.avito.ru/token",
                    data={
                        "grant_type": "client_credentials",
                        "client_id": client_id,
                        "client_secret": client_secret,
                    },
                    headers={"Content-Type": "application/x-www-form-urlencoded"},
                )
                if token_resp.status_code != 200:
                    return {
                        "status": "error",
                        "message": f"Ошибка авторизации Авито: {token_resp.text}",
                    }
                token = token_resp.json()["access_token"]

                wh_resp = await http.post(
                    "https://api.avito.ru/messenger/v3/webhook",
                    json={"url": payload.webhook_url},
                    headers={"Authorization": f"Bearer {token}"},
                )
                if wh_resp.status_code in (200, 201):
                    return {
                        "status": "ok",
                        "message": f"Вебхук успешно зарегистрирован в Авито: {payload.webhook_url}",
                    }
                return {
                    "status": "error",
                    "message": f"Ошибка регистрации вебхука ({wh_resp.status_code}): {wh_resp.text}",
                }
        except Exception as exc:
            return {"status": "error", "message": f"Ошибка вызова Авито API: {exc}"}

    # === Клиенты (только владелец платформы) ===
    @router.get("/api/tenants")
    async def api_tenants(user: CurrentUser = Depends(require_platform_admin)) -> List[Dict[str, Any]]:
        tenants = storage.list_tenants()
        now = datetime.now(timezone.utc)
        month_start = f"{now.year:04d}-{now.month:02d}-01T00:00:00"
        plans_dict = {p["slug"]: p.get("name", p["slug"]) for p in storage.list_plans()}
        for t in tenants:
            t["bot_running"] = bool(bot_manager and bot_manager.is_running(t["slug"]))
            t["companies"] = [p.slug for p in company_store.list_for_tenant(t["slug"])]
            t["users"] = [u["username"] for u in storage.list_users(t["slug"])]
            t["plan_name"] = plans_dict.get(t.get("plan", "basic"), t.get("plan", "basic"))
            m_usage = storage.get_tenant_month_usage(t["slug"], month_start)
            t["month_messages"] = m_usage["messages"]
            t["month_cost_rub"] = m_usage["cost_rub"]
        return tenants

    @router.post("/api/tenants")
    async def api_create_tenant(
        payload: CreateTenantRequest,
        user: CurrentUser = Depends(require_platform_admin),
    ) -> Dict[str, Any]:
        if payload.plan and not storage.get_plan(payload.plan):
            raise HTTPException(status_code=400, detail=f"Тариф '{payload.plan}' не найден")

        tenant = storage.save_tenant(
            slug=payload.slug,
            name=payload.name,
            brand_name=payload.brand_name,
            telegram_bot_token=payload.telegram_bot_token,
            owner_telegram_id=payload.owner_telegram_id,
            plan=payload.plan,
        )
        created_user = None
        if payload.admin_username and payload.admin_password:
            if len(payload.admin_password) < 8:
                raise HTTPException(status_code=400, detail="Пароль минимум 8 символов")
            storage.save_user(
                username=payload.admin_username,
                password_hash=hash_password(payload.admin_password),
                role=ROLE_CLIENT_ADMIN,
                tenant_slug=payload.slug,
            )
            created_user = payload.admin_username

        if bot_manager and payload.telegram_bot_token:
            await bot_manager.restart(payload.slug)

        return {
            "status": "ok",
            "tenant": {k: v for k, v in tenant.items() if k != "telegram_bot_token"},
            "admin_username": created_user,
            "message": f"Клиент «{payload.name}» создан",
        }

    @router.post("/api/tenants/{slug}/bot")
    async def api_set_tenant_bot(
        slug: str,
        payload: UpdateTenantBotRequest,
        user: CurrentUser = Depends(require_platform_admin),
    ) -> Dict[str, Any]:
        tenant = storage.get_tenant(slug)
        if not tenant:
            raise HTTPException(status_code=404, detail="Клиент не найден")
        owner_id = (
            payload.owner_telegram_id
            if payload.owner_telegram_id is not None
            else tenant.get("owner_telegram_id")
        )
        storage.save_tenant(
            slug=slug,
            name=tenant["name"],
            brand_name=tenant.get("brand_name") or "",
            telegram_bot_token=payload.telegram_bot_token,
            owner_telegram_id=owner_id,
            plan=tenant.get("plan") or "basic",
            status=tenant.get("status") or "active",
        )
        if bot_manager:
            await bot_manager.restart(slug)
            return {
                "status": "ok",
                "bot_running": bot_manager.is_running(slug),
                "bot_username": _resolve_bot_username(slug),
                "message": "Токен сохранён, бот перезапущен",
            }
        return {"status": "ok", "message": "Токен сохранён"}

    @router.delete("/api/tenants/{slug}")
    async def api_delete_tenant(
        slug: str, user: CurrentUser = Depends(require_platform_admin)
    ) -> Dict[str, Any]:
        if slug == "default":
            raise HTTPException(status_code=400, detail="Клиента по умолчанию удалить нельзя")
        deleted = storage.delete_tenant(slug)
        if bot_manager:
            if hasattr(bot_manager, "stop_tenant"):
                await bot_manager.stop_tenant(slug)
            else:
                await bot_manager.restart(slug)
        return {"status": "ok", "deleted": deleted}

    # === Пользователи кабинета ===
    @router.get("/api/users")
    async def api_list_users(user: CurrentUser = Depends(current_user)) -> List[Dict[str, Any]]:
        users = storage.list_users(None if user.is_platform_admin else user.tenant_slug)
        return [
            {
                "id": u["id"],
                "username": u["username"],
                "role": u["role"],
                "tenant_slug": u["tenant_slug"],
                "is_active": bool(u["is_active"]),
                "created_at": u["created_at"],
            }
            for u in users
        ]

    @router.post("/api/users")
    async def api_create_user(
        payload: CreateUserRequest,
        user: CurrentUser = Depends(require_writer),
    ) -> Dict[str, Any]:
        if payload.role not in (ROLE_CLIENT_ADMIN, ROLE_CLIENT_VIEWER):
            raise HTTPException(status_code=400, detail="Недопустимая роль")

        if user.is_platform_admin:
            tenant_slug = payload.tenant_slug
        else:
            tenant_slug = user.tenant_slug
            if payload.role == ROLE_CLIENT_ADMIN:
                raise HTTPException(status_code=403, detail="Клиент не может создавать администраторов")

        if tenant_slug and not storage.get_tenant(tenant_slug):
            raise HTTPException(status_code=400, detail="Клиент не найден")

        storage.save_user(
            username=payload.username,
            password_hash=hash_password(payload.password),
            role=payload.role,
            tenant_slug=tenant_slug,
        )
        return {"status": "ok", "message": f"Пользователь «{payload.username}» сохранён"}

    @router.delete("/api/users/{username}")
    async def api_delete_user(
        username: str, user: CurrentUser = Depends(require_writer)
    ) -> Dict[str, Any]:
        target = storage.get_user_by_username(username)
        if not target:
            raise HTTPException(status_code=404, detail="Пользователь не найден")
        if not user.is_platform_admin and target["tenant_slug"] != user.tenant_slug:
            raise HTTPException(status_code=404, detail="Пользователь не найден")
        if target["username"] == user.username:
            raise HTTPException(status_code=400, detail="Нельзя удалить самого себя")
        deleted = storage.delete_user(username)
        return {"status": "ok", "deleted": deleted}

    # === Token Accounting & Tariffs (Блок B: TZ B4) ===
    @router.get("/api/usage")
    async def api_get_usage(
        days: int = 30,
        tenant_slug: Optional[str] = None,
        user: CurrentUser = Depends(current_user),
    ) -> Dict[str, Any]:
        target_slug = user.scope_tenant
        if user.is_platform_admin and tenant_slug:
            target_slug = tenant_slug
        return storage.get_usage_summary(tenant_slug=target_slug, days=days)

    @router.get("/api/usage/tenants")
    async def api_get_tenants_usage(
        days: int = 30,
        user: CurrentUser = Depends(require_platform_admin),
    ) -> List[Dict[str, Any]]:
        return storage.get_all_tenants_usage_summary(days=days)

    @router.get("/api/plans")
    async def api_list_plans(user: CurrentUser = Depends(current_user)) -> List[Dict[str, Any]]:
        return storage.list_plans()

    @router.post("/api/plans")
    async def api_save_plan(
        req: PlanRequest,
        user: CurrentUser = Depends(require_platform_admin),
    ) -> Dict[str, Any]:
        storage.save_plan(
            slug=req.slug,
            name=req.name,
            monthly_rub=req.monthly_rub,
            included_messages=req.included_messages,
            included_tokens=req.included_tokens,
            overage_rub_per_1k=req.overage_rub_per_1k,
            is_active=req.is_active,
        )
        return {"status": "ok", "slug": req.slug}

    @router.post("/api/tenants/{slug}/plan")
    async def api_set_tenant_plan(
        slug: str,
        req: UpdateTenantPlanRequest,
        user: CurrentUser = Depends(require_platform_admin),
    ) -> Dict[str, Any]:
        tenant = storage.get_tenant(slug)
        if not tenant:
            raise HTTPException(status_code=404, detail="Клиент не найден")
        plan = storage.get_plan(req.plan)
        if not plan:
            raise HTTPException(status_code=400, detail="Тариф не найден")
        storage.set_tenant_plan(slug, req.plan)
        return {"status": "ok", "tenant_slug": slug, "plan": req.plan}

    @router.get("/api/model-prices")
    async def api_list_model_prices(
        user: CurrentUser = Depends(require_platform_admin),
    ) -> List[Dict[str, Any]]:
        return storage.list_model_prices()

    @router.post("/api/model-prices")
    async def api_save_model_price(
        req: ModelPriceRequest,
        user: CurrentUser = Depends(require_platform_admin),
    ) -> Dict[str, Any]:
        storage.save_model_price(
            model=req.model,
            prompt_rub_per_1k=req.prompt_rub_per_1k,
            completion_rub_per_1k=req.completion_rub_per_1k,
        )
        return {"status": "ok", "model": req.model}

    # === Onboarding Wizard (Блок C: TZ C4) ===
    @router.get("/api/onboarding")
    async def api_get_onboarding(user: CurrentUser = Depends(require_writer)) -> Dict[str, Any]:
        target_tenant = user.scope_tenant or "default"
        data = storage.get_onboarding(target_tenant)
        if not data:
            data = storage.save_onboarding(target_tenant, step=1, draft_data={})
        return {
            "status": "ok",
            "tenant_slug": target_tenant,
            "step": data["step"],
            "draft": data["draft"],
            "completed": bool(data.get("completed_at")),
        }

    @router.post("/api/onboarding/step")
    async def api_save_onboarding_step(
        req: OnboardingStepRequest, user: CurrentUser = Depends(require_writer)
    ) -> Dict[str, Any]:
        target_tenant = user.scope_tenant or "default"
        existing = storage.get_onboarding(target_tenant)
        draft = existing["draft"] if existing else {}
        draft.update(req.data)
        saved = storage.save_onboarding(target_tenant, step=req.step, draft_data=draft)
        return {"status": "ok", "step": saved["step"], "draft": saved["draft"]}

    @router.post("/api/onboarding/parse-price")
    async def api_parse_price(
        req: ParsePriceRequest, user: CurrentUser = Depends(require_writer)
    ) -> Dict[str, Any]:
        res = parse_price_list(req.text)
        return {"status": "ok", "services": res["services"], "extra_lines": res["extra_lines"]}

    @router.post("/api/onboarding/test-dialog")
    async def api_test_dialog(
        req: TestDialogRequest, user: CurrentUser = Depends(require_writer)
    ) -> Dict[str, Any]:
        target_tenant = user.scope_tenant or "default"
        existing = storage.get_onboarding(target_tenant)
        draft = existing["draft"] if existing else {}

        active_llm = llm
        if active_llm is None and bot_manager is not None:
            active_llm = getattr(bot_manager, "llm", None)

        if active_llm is None:
            return {"status": "ok", "reply": "Здравствуйте! Готов проконсультировать вас по услугам и ценам."}

        temp_company = CompanyProfile(
            slug="onboarding_preview",
            company_name=draft.get("company_name") or "Автобизнес",
            city=draft.get("city") or "Москва",
            niche=draft.get("niche") or "автобизнес",
            address=draft.get("address") or "",
            work_hours=draft.get("work_hours") or "Ежедневно с 10:00 до 20:00",
            services=draft.get("services") or [],
            advantages=draft.get("advantages") or [],
            manager_name=draft.get("manager_name") or "Александр",
            manager_role=draft.get("manager_role") or "старший мастер",
            discount_offer=draft.get("discount_offer") or "скидку 10% на первый визит",
            media_reply_promise=draft.get("media_reply_promise") or "в течение 15–30 минут",
            tenant_slug=target_tenant,
        )

        try:
            reply = await active_llm.generate_reply(
                company=temp_company,
                history=[],
                user_message=req.message,
                channel="telegram",
                chat_id="test_onboarding",
            )
            return {"status": "ok", "reply": reply.text}
        except Exception as exc:
            return {"status": "error", "reply": f"Ошибка генерации ответа: {exc}"}

    @router.post("/api/onboarding/complete")
    async def api_complete_onboarding(user: CurrentUser = Depends(require_writer)) -> Dict[str, Any]:
        target_tenant = user.scope_tenant or "default"
        existing = storage.get_onboarding(target_tenant)
        if not existing or not existing.get("draft"):
            raise HTTPException(status_code=400, detail="Черновик онбординга пуст")

        draft = existing["draft"]
        c_name = (draft.get("company_name") or "").strip()
        c_city = (draft.get("city") or "").strip()
        if not c_name or not c_city:
            raise HTTPException(status_code=400, detail="Название компании и город обязательны")

        base_slug = slugify_russian(c_name)
        candidate_slug = base_slug
        suffix = 2
        while company_store.get(candidate_slug) is not None:
            candidate_slug = f"{base_slug}-{suffix}"
            suffix += 1

        profile = CompanyProfile(
            slug=candidate_slug,
            company_name=c_name,
            city=c_city,
            niche=draft.get("niche") or "автобизнес",
            address=draft.get("address") or "",
            work_hours=draft.get("work_hours") or "Ежедневно с 10:00 до 20:00",
            services=draft.get("services") or [],
            advantages=draft.get("advantages") or [],
            manager_name=draft.get("manager_name") or "Александр",
            manager_role=draft.get("manager_role") or "старший мастер",
            discount_offer=draft.get("discount_offer") or "скидку 10% на первый визит",
            media_reply_promise=draft.get("media_reply_promise") or "в течение 15–30 минут",
            tenant_slug=target_tenant,
        )
        company_store.save(profile)
        storage.save_onboarding(target_tenant, step=5, draft_data=draft, completed=True)

        return {
            "status": "ok",
            "slug": candidate_slug,
            "demo_link": demo_link_for(profile),
        }

    # === Страницы ===
    @router.get("/", response_class=HTMLResponse)
    async def dashboard_html(request: Request) -> Any:
        user = auth_service.get_user_from_cookie(request)
        if not user:
            return RedirectResponse(url="/login", status_code=status.HTTP_302_FOUND)

        tenant_slug = user.tenant_slug
        if tenant_slug and not user.is_platform_admin:
            companies = company_store.list_for_tenant(tenant_slug)
            if not companies and not storage.is_onboarding_completed(tenant_slug):
                return RedirectResponse(url="/onboarding", status_code=status.HTTP_302_FOUND)
        return get_dashboard_html(_resolve_bot_username(tenant_slug))

    @router.get("/onboarding", response_class=HTMLResponse)
    async def onboarding_page(request: Request) -> Any:
        user = auth_service.get_user_from_cookie(request)
        if not user:
            return RedirectResponse(url="/login", status_code=status.HTTP_302_FOUND)
        tenant_slug = user.tenant_slug
        return get_onboarding_html(_resolve_bot_username(tenant_slug))

    # === Живой симулятор диалога на лендинге с реальным ИИ (AnyModel) ===
    _sim_rate_limits: Dict[str, List[float]] = {}

    @router.post("/api/simulator/chat")
    async def api_simulator_chat(req: SimulatorChatRequest, request: Request) -> Dict[str, Any]:
        # 1. Защита от спама и перерасхода токенов: лимит 30 сообщений в минуту на 1 IP
        real_ip = request.headers.get("x-real-ip")
        if real_ip:
            client_ip = real_ip.strip()
        else:
            forwarded = request.headers.get("x-forwarded-for", "")
            parts = [p.strip() for p in forwarded.split(",") if p.strip()]
            client_ip = parts[-1] if parts else (request.client.host if request.client else "unknown")

        now = time.time()
        if len(_sim_rate_limits) > 500:
            stale = [k for k, v in _sim_rate_limits.items() if not v or (now - v[-1] >= 60)]
            for k in stale:
                _sim_rate_limits.pop(k, None)
        timestamps = _sim_rate_limits.setdefault(client_ip, [])
        _sim_rate_limits[client_ip] = [t for t in timestamps if now - t < 60]
        if len(_sim_rate_limits[client_ip]) >= 30:
            raise HTTPException(
                status_code=429,
                detail="Слишком много сообщений в симулятор. Пожалуйста, подождите 1 минуту перед следующим вопросом."
            )
        _sim_rate_limits[client_ip].append(now)

        # 2. Проверка наличия номера телефона в сообщении пользователя
        phone_match = re.search(r"(?:\+?7|8)[\s\-\(\)]*\d{3}[\s\-\(\)]*\d{3}[\s\-]*\d{2}[\s\-]*\d{2}", req.message)
        extracted_phone = phone_match.group(0).strip() if phone_match else None

        # 3. Выбор профиля компании: строго разрешенные публичные демо-профили во избежание утечки прайсов клиентов
        ALLOWED_SIMULATOR_SLUGS = {"detailing_premium", "demo_detailing", "demo_bodyshop"}
        target_slug = req.company_slug if req.company_slug in ALLOWED_SIMULATOR_SLUGS else "detailing_premium"
        target_company = (
            company_store.get(target_slug)
            or company_store.get("demo_detailing")
            or company_store.get("demo_bodyshop")
        )

        active_llm = llm
        if active_llm is None and bot_manager is not None:
            active_llm = getattr(bot_manager, "llm", None)

        if not active_llm or not target_company:
            if extracted_phone:
                return {
                    "status": "ok",
                    "reply": f"Спасибо! Зафиксировал ваш номер {extracted_phone}. Старший мастер перезвонит вам в течение 10 минут с детальным расчётом.",
                    "lead_captured": True,
                    "phone": extracted_phone,
                }
            return {
                "status": "ok",
                "reply": "Здравствуйте! С удовольствием подскажем по стоимости и срокам. Уточните, пожалуйста, марку и модель вашего автомобиля и какие именно работы планируете сделать?",
                "lead_captured": False,
                "phone": None,
            }

        # 4. Формирование истории диалога с ограничением длины каждого сообщения
        from app.models import ConversationMessage
        history_msgs: List[ConversationMessage] = []
        for item in req.history[-10:]:
            role = item.get("role")
            content = str(item.get("content", ""))[:1000]
            if role in ("user", "assistant") and content:
                history_msgs.append(ConversationMessage(role=role, content=content))

        try:
            reply_obj = await active_llm.generate_reply(
                company=target_company,
                history=history_msgs,
                user_message=req.message,
                channel="telegram",
                chat_id="simulator_demo",
                tenant_slug="simulator",
            )
            reply_text = reply_obj.text
            lead = reply_obj.lead
            phone = (lead.phone if lead else None) or extracted_phone
            lead_captured = bool(phone)

            return {
                "status": "ok",
                "reply": reply_text,
                "lead_captured": lead_captured,
                "phone": phone,
            }
        except Exception as exc:
            logger.error("Simulator AI error: %s", exc)
            if extracted_phone:
                return {
                    "status": "ok",
                    "reply": f"Спасибо! Зафиксировал ваш номер {extracted_phone}. Старший мастер перезвонит вам в течение 10 минут с расчётом.",
                    "lead_captured": True,
                    "phone": extracted_phone,
                }
            return {
                "status": "ok",
                "reply": "Здравствуйте! Подскажите, какой у вас автомобиль и что планируете сделать? Могу сразу назвать ориентировочную вилку цен и записать на бесплатный осмотр.",
                "lead_captured": False,
                "phone": None,
            }

    @router.post("/api/landing/lead")
    async def api_landing_lead(req: LandingLeadRequest, request: Request) -> Dict[str, Any]:
        real_ip = request.headers.get("x-real-ip")
        if real_ip:
            client_ip = real_ip.strip()
        else:
            forwarded = request.headers.get("x-forwarded-for", "")
            parts = [p.strip() for p in forwarded.split(",") if p.strip()]
            client_ip = parts[-1] if parts else (request.client.host if request.client else "unknown")
        # Rate limit: max 5 form submissions per minute per IP
        now = time.time()
        timestamps = _sim_rate_limits.setdefault(f"lead_{client_ip}", [])
        _sim_rate_limits[f"lead_{client_ip}"] = [t for t in timestamps if now - t < 60]
        if len(_sim_rate_limits[f"lead_{client_ip}"]) >= 5:
            raise HTTPException(status_code=429, detail="Слишком много заявок с вашего адреса. Мы уже получили ваш номер!")
        _sim_rate_limits[f"lead_{client_ip}"].append(now)

        from app.models import LeadInfo
        lead = LeadInfo(
            channel="web",
            chat_id=f"web_{int(now)}",
            company_slug="default",
            client_name=req.name.strip() or "Заявка с сайта",
            phone=req.phone.strip(),
            car_model="",
            service_interest=req.service.strip() or "Тест-драйв 3 дня (с сайта)",
            estimated_price="",
            preferred_time="",
            summary=f"Лид из веб-формы лендинга (IP: {client_ip})",
        )
        try:
            storage.save_lead(lead)
            if notifier:
                try:
                    await notifier.notify_saas_trial_request(
                        user_label=lead.client_name,
                        chat_id=lead.chat_id,
                        city_name="Веб-сайт (форма)",
                        niche=lead.service_interest,
                        price_hint="Заявка с сайта",
                        owner_phone=lead.phone,
                    )
                except Exception as ne:
                    logger.warning("Could not send telegram notification for landing lead: %s", ne)
        except Exception as exc:
            logger.warning("Could not persist landing lead: %s", exc)

        return {
            "status": "ok",
            "message": "Заявка успешно принята! Основатель свяжется с вами в течение 10 минут."
        }

    @router.get("/landing", response_class=HTMLResponse)
    async def landing_html() -> str:
        return get_landing_html(_resolve_bot_username(None))

    return router


def get_onboarding_html(bot_username: str) -> str:
    if ONBOARDING_PATH.exists():
        raw = ONBOARDING_PATH.read_text(encoding="utf-8")
        return raw.replace("{{BOT_USERNAME}}", bot_username)
    return "<h1>Мастер онбординга</h1>"


def get_dashboard_html(bot_username: str) -> str:
    if TEMPLATE_PATH.exists():
        raw = TEMPLATE_PATH.read_text(encoding="utf-8")
        return raw.replace("{{BOT_USERNAME}}", bot_username)
    return f"<h1>Заявка 24/7</h1><p>Бот: @{bot_username}</p>"


def get_landing_html(bot_username: str) -> str:
    if LANDING_PATH.exists():
        raw = LANDING_PATH.read_text(encoding="utf-8")
        return raw.replace("{{BOT_USERNAME}}", bot_username)
    return f"<h1>Заявка 24/7 — Лендинг</h1><p>Бот: @{bot_username}</p>"


def get_login_html() -> str:
    if LOGIN_PATH.exists():
        return LOGIN_PATH.read_text(encoding="utf-8")
    return (
        "<html><body><h1>Вход</h1>"
        '<form method="post" action="/login">'
        '<input name="username"><input name="password" type="password">'
        '<button type="submit">Войти</button></form></body></html>'
    )