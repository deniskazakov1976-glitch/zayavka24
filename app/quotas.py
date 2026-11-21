from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Optional

from app.alerting import AlertManager
from app.storage import Storage


@dataclass
class QuotaStatus:
    allowed: bool
    reason: str
    used_messages: int
    limit_messages: Optional[int]
    used_tokens: int
    limit_tokens: Optional[int]
    used_rub: float
    soft_limit: bool


class QuotaService:
    """Управление лимитами тарифов тенантов (сообщения, токены, рубли)."""

    def __init__(self, storage: Storage, alert_manager: Optional[AlertManager] = None) -> None:
        self.storage = storage
        self.alert_manager = alert_manager

    def check(self, tenant_slug: str) -> QuotaStatus:
        # Platform admin and internal plan are unlimited
        if not tenant_slug or tenant_slug in ("platform_admin", "admin"):
            return QuotaStatus(
                allowed=True,
                reason="Платформенный администратор",
                used_messages=0,
                limit_messages=None,
                used_tokens=0,
                limit_tokens=None,
                used_rub=0.0,
                soft_limit=False,
            )

        tenant = self.storage.get_tenant(tenant_slug)
        plan_slug = (tenant.get("plan") if tenant else "basic") or "basic"
        if plan_slug == "internal":
            return QuotaStatus(
                allowed=True,
                reason="Безлимитный тариф (internal)",
                used_messages=0,
                limit_messages=None,
                used_tokens=0,
                limit_tokens=None,
                used_rub=0.0,
                soft_limit=False,
            )

        plan = self.storage.get_plan(plan_slug)
        if not plan:
            # Неизвестный тариф — не даём безлимит по ошибке, берём базовый
            plan = self.storage.get_plan("basic")
        limit_messages = plan.get("included_messages") if plan else None
        limit_tokens = plan.get("included_tokens") if plan else None

        now = datetime.now(timezone.utc)
        month_start = f"{now.year:04d}-{now.month:02d}-01T00:00:00"
        usage = self.storage.get_tenant_month_usage(tenant_slug, month_start)
        used_messages = usage["messages"]
        used_tokens = usage["tokens"]
        used_rub = usage["cost_rub"]

        allowed = True
        reason = "В рамках тарифа"
        soft_limit = False

        if limit_messages is not None and limit_messages > 0:
            if used_messages >= limit_messages:
                allowed = False
                reason = f"Превышен лимит сообщений по тарифу ({used_messages}/{limit_messages})"
            elif used_messages >= int(limit_messages * 0.8):
                soft_limit = True

        if allowed and limit_tokens is not None and limit_tokens > 0:
            if used_tokens >= limit_tokens:
                allowed = False
                reason = f"Превышен лимит токенов по тарифу ({used_tokens}/{limit_tokens})"
            elif used_tokens >= int(limit_tokens * 0.8):
                soft_limit = True

        return QuotaStatus(
            allowed=allowed,
            reason=reason,
            used_messages=used_messages,
            limit_messages=limit_messages,
            used_tokens=used_tokens,
            limit_tokens=limit_tokens,
            used_rub=used_rub,
            soft_limit=soft_limit,
        )
