import hashlib
import hmac
import json
import logging
from datetime import datetime, timezone
from typing import Any, Dict, Optional
import httpx

from app.models import CompanyProfile, LeadInfo

logger = logging.getLogger(__name__)


def _utcnow_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


class CRMDispatcher:
    """Dispatches captured leads to external CRMs (amoCRM, Bitrix24, Outbound Webhooks)."""

    def __init__(self, timeout: float = 8.0, client: Optional[httpx.AsyncClient] = None) -> None:
        self.timeout = timeout
        self._client = client

    async def _get_client(self) -> httpx.AsyncClient:
        if self._client and not self._client.is_closed:
            return self._client
        return httpx.AsyncClient(timeout=self.timeout)

    async def dispatch(self, lead: LeadInfo, company: Optional[CompanyProfile] = None) -> Dict[str, Any]:
        """Dispatches a lead according to the company's CRM configuration."""
        if not company or not company.crm_type or company.crm_type == "none":
            return {"status": "skipped", "reason": "crm_disabled"}

        crm_type = company.crm_type.lower().strip()
        try:
            if crm_type == "webhook":
                return await self.send_webhook(lead, company)
            elif crm_type == "amocrm":
                return await self.send_amocrm(lead, company)
            elif crm_type == "bitrix24":
                return await self.send_bitrix24(lead, company)
            else:
                logger.warning("Unknown CRM type '%s' for company %s", crm_type, company.slug)
                return {"status": "skipped", "reason": f"unknown_crm_type_{crm_type}"}
        except Exception as exc:
            logger.error("Failed to dispatch lead to CRM (%s) for %s: %s", crm_type, company.slug, exc)
            return {"status": "error", "error": str(exc), "crm_type": crm_type}

    async def send_webhook(self, lead: LeadInfo, company: CompanyProfile) -> Dict[str, Any]:
        """Sends lead payload to an arbitrary HTTP/HTTPS webhook (Make, n8n, Zapier, custom CRM)."""
        url = (company.crm_webhook_url or "").strip()
        if not url:
            return {"status": "error", "reason": "empty_webhook_url"}

        payload = {
            "event": "lead_created",
            "timestamp": _utcnow_iso(),
            "lead": {
                "phone": lead.phone,
                "client_name": lead.client_name,
                "car_model": lead.car_model,
                "service_interest": lead.service_interest,
                "estimated_price": lead.estimated_price,
                "preferred_time": lead.preferred_time,
                "summary": lead.summary,
                "channel": lead.channel,
                "chat_id": lead.chat_id,
                "company_slug": lead.company_slug,
                "avito_chat_url": lead.avito_chat_url,
                "created_at": lead.created_at.isoformat() if lead.created_at else _utcnow_iso(),
            },
            "company": {
                "name": company.company_name,
                "city": company.city,
                "slug": company.slug,
                "niche": company.niche,
            },
        }

        body_bytes = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        headers = {
            "Content-Type": "application/json; charset=utf-8",
            "User-Agent": "Zayavka24-7-CRM-Dispatcher/1.0",
        }

        if company.crm_api_token:
            token = company.crm_api_token.strip()
            headers["X-CRM-Token"] = token
            headers["Authorization"] = f"Bearer {token}"
            # Compute HMAC signature for webhook authenticity verification
            sig = hmac.new(token.encode("utf-8"), body_bytes, hashlib.sha256).hexdigest()
            headers["X-Webhook-Signature"] = sig

        client = await self._get_client()
        try:
            resp = await client.post(url, content=body_bytes, headers=headers)
            is_ok = 200 <= resp.status_code < 300
            if is_ok:
                logger.info("Successfully dispatched lead to Webhook for %s (status %d)", company.slug, resp.status_code)
                return {"status": "ok", "provider": "webhook", "http_code": resp.status_code}
            else:
                logger.warning("Webhook endpoint returned non-2xx code %d for %s: %s", resp.status_code, company.slug, resp.text[:200])
                return {"status": "error", "provider": "webhook", "http_code": resp.status_code, "response": resp.text[:200]}
        finally:
            if client != self._client:
                await client.aclose()

    async def send_amocrm(self, lead: LeadInfo, company: CompanyProfile) -> Dict[str, Any]:
        """Creates lead and contact in amoCRM via API v4 /api/v4/leads/complex."""
        subdomain = (company.crm_subdomain or "").strip().replace(".amocrm.ru", "")
        token = (company.crm_api_token or "").strip()
        if not subdomain or not token:
            return {"status": "error", "reason": "missing_amocrm_subdomain_or_token"}

        api_url = f"https://{subdomain}.amocrm.ru/api/v4/leads/complex"

        lead_title = f"Заявка 24/7: {lead.service_interest or lead.car_model or 'Клиент с Авито/ТГ'}"
        notes_text = (
            f"Автомобиль: {lead.car_model or 'Не указан'}\n"
            f"Интерес: {lead.service_interest or 'Не указан'}\n"
            f"Ориентир цены: {lead.estimated_price or 'Не рассчитан'}\n"
            f"Удобное время: {lead.preferred_time or 'Не указано'}\n"
            f"Канал: {lead.channel}\n"
            f"Детали: {lead.summary or ''}\n"
        )
        if lead.avito_chat_url:
            notes_text += f"Чат Авито: {lead.avito_chat_url}\n"

        # Complex lead creation payload (amoCRM API v4 format)
        payload = [
            {
                "name": lead_title,
                "_embedded": {
                    "contacts": [
                        {
                            "name": lead.client_name or f"Клиент {lead.car_model or lead.phone[-4:]}",
                            "custom_fields_values": [
                                {
                                    "field_code": "PHONE",
                                    "values": [
                                        {"value": lead.phone, "enum_code": "WORK"}
                                    ]
                                }
                            ]
                        }
                    ],
                    "tags": [
                        {"name": "Заявка 24/7"},
                        {"name": lead.channel.capitalize()},
                        {"name": company.niche},
                    ]
                }
            }
        ]

        headers = {
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
            "User-Agent": "Zayavka24-7-AmoCRM/1.0",
        }

        client = await self._get_client()
        try:
            resp = await client.post(api_url, json=payload, headers=headers)
            is_ok = 200 <= resp.status_code < 300
            if is_ok:
                logger.info("Successfully pushed lead to amoCRM for %s (%s)", company.slug, subdomain)
                return {"status": "ok", "provider": "amocrm", "http_code": resp.status_code}
            else:
                logger.warning("amoCRM returned code %d for %s: %s", resp.status_code, company.slug, resp.text[:200])
                return {"status": "error", "provider": "amocrm", "http_code": resp.status_code, "response": resp.text[:200]}
        finally:
            if client != self._client:
                await client.aclose()

    async def send_bitrix24(self, lead: LeadInfo, company: CompanyProfile) -> Dict[str, Any]:
        """Creates lead in Bitrix24 via Inbound Webhook /crm.lead.add.json."""
        webhook_url = (company.crm_webhook_url or "").strip().rstrip("/")
        if not webhook_url:
            return {"status": "error", "reason": "missing_bitrix24_webhook_url"}

        endpoint = f"{webhook_url}/crm.lead.add.json" if not webhook_url.endswith(".json") else webhook_url

        lead_title = f"Заявка 24/7: {lead.service_interest or lead.car_model or 'Новый клиент'}"
        comments = (
            f"<b>Автомобиль:</b> {lead.car_model or 'Не указан'}<br>"
            f"<b>Услуга:</b> {lead.service_interest or 'Не указана'}<br>"
            f"<b>Ориентир цены:</b> {lead.estimated_price or 'Не рассчитан'}<br>"
            f"<b>Канал:</b> {lead.channel}<br>"
            f"<b>Детали диалога:</b> {lead.summary or ''}<br>"
        )
        if lead.avito_chat_url:
            comments += f'<b>Чат Авито:</b> <a href="{lead.avito_chat_url}">Открыть диалог</a><br>'

        payload = {
            "fields": {
                "TITLE": lead_title,
                "NAME": lead.client_name or "Клиент",
                "PHONE": [{"VALUE": lead.phone, "VALUE_TYPE": "WORK"}],
                "COMMENTS": comments,
                "SOURCE_ID": "WEB",
                "STATUS_ID": "NEW",
            },
            "params": {"REGISTER_SONET_EVENT": "Y"}
        }

        client = await self._get_client()
        try:
            resp = await client.post(endpoint, json=payload)
            is_ok = 200 <= resp.status_code < 300
            if is_ok:
                logger.info("Successfully pushed lead to Bitrix24 for %s", company.slug)
                return {"status": "ok", "provider": "bitrix24", "http_code": resp.status_code}
            else:
                logger.warning("Bitrix24 returned code %d for %s: %s", resp.status_code, company.slug, resp.text[:200])
                return {"status": "error", "provider": "bitrix24", "http_code": resp.status_code, "response": resp.text[:200]}
        finally:
            if client != self._client:
                await client.aclose()
