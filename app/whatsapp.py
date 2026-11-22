import json
import logging
import secrets
from typing import Any, Dict, Optional
from datetime import datetime, timezone

import httpx

from app.company_store import CompanyStore
from app.guardrails import validate_reply
from app.llm import LLMService
from app.media_reply import build_media_reply
from app.models import CompanyProfile, ConversationMessage, LeadInfo
from app.notifier import LeadNotifier
from app.storage import Storage

logger = logging.getLogger(__name__)


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


class WhatsAppClient:
    """Sends messages and media via Green-API HTTP gateway."""

    def __init__(self, instance_id: str, api_token: str, client: Optional[httpx.AsyncClient] = None) -> None:
        self.instance_id = instance_id.strip()
        self.api_token = api_token.strip()
        self.base_url = f"https://api.green-api.com/waInstance{self.instance_id}"
        self._client = client

    async def _get_client(self) -> httpx.AsyncClient:
        if self._client and not self._client.is_closed:
            return self._client
        return httpx.AsyncClient(timeout=10.0)

    async def send_message(self, chat_id: str, text: str) -> Dict[str, Any]:
        """Sends a text message to a WhatsApp chat (e.g. 79991234567@c.us)."""
        url = f"{self.base_url}/sendMessage/{self.api_token}"
        payload = {"chatId": chat_id, "message": text}
        client = await self._get_client()
        try:
            resp = await client.post(url, json=payload)
            if 200 <= resp.status_code < 300:
                return resp.json()
            logger.warning("Green-API sendMessage returned %d: %s", resp.status_code, resp.text[:200])
            return {"error": resp.text[:200], "status_code": resp.status_code}
        finally:
            if client != self._client:
                await client.aclose()


class WhatsAppWebhookHandler:
    """Processes incoming Green-API webhooks, routes dialogues, manages auto-pause and lead capture."""

    def __init__(
        self,
        company_store: CompanyStore,
        storage: Storage,
        llm: LLMService,
        notifier: LeadNotifier,
        manual_pause_hours: int = 24,
        client: Optional[httpx.AsyncClient] = None,
        quota_service: Optional[Any] = None,
    ) -> None:
        self.company_store = company_store
        self.storage = storage
        self.llm = llm
        self.notifier = notifier
        self.manual_pause_hours = manual_pause_hours
        self._http_client = client
        self.quota_service = quota_service

    def _resolve_company(self, slug_or_instance: str) -> Optional[CompanyProfile]:
        # 1. Direct lookup by slug
        comp = self.company_store.get(slug_or_instance)
        if comp and comp.whatsapp_enabled:
            return comp
        # 2. Lookup by instance_id
        for c in self.company_store.list_all():
            if c.whatsapp_enabled and c.whatsapp_instance_id == slug_or_instance:
                return c
        return None

    async def handle_webhook(
        self,
        payload: Dict[str, Any],
        company_slug_hint: str = "",
        secret: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Dispatches an incoming Green-API webhook event."""
        type_webhook = payload.get("typeWebhook")
        if not type_webhook:
            return {"status": "ignored", "reason": "no_typeWebhook"}

        # Resolve company
        instance_data = payload.get("instanceData") or {}
        instance_id = str(instance_data.get("idInstance") or company_slug_hint)
        company = self._resolve_company(company_slug_hint) or self._resolve_company(instance_id)

        if not company:
            logger.warning("WhatsApp webhook: no active company found for %s / %s", company_slug_hint, instance_id)
            return {"status": "error", "reason": "no_company_found"}

        expected_secret = getattr(company, "whatsapp_webhook_secret", None)
        if expected_secret:
            if not secret or not secrets.compare_digest(secret, expected_secret):
                return {"status": "error", "reason": "invalid_webhook_secret"}

        # 1. Dedup event
        event_id = str(payload.get("idMessage") or payload.get("timestamp") or "")
        if event_id and self.storage.is_event_processed("whatsapp", event_id):
            return {"status": "ignored", "reason": "duplicate_event"}

        sender_data = payload.get("senderData") or {}
        chat_id = sender_data.get("chatId") or ""
        sender_phone = sender_data.get("sender") or ""
        sender_name = sender_data.get("senderName") or ""

        if not chat_id:
            return {"status": "ignored", "reason": "no_chat_id"}

        # 2. Human owner reply detection -> Auto-pause
        # If typeWebhook is outgoingMessageReceived, the salon owner wrote from their phone app
        if type_webhook in ("outgoingMessageReceived", "outgoingAPIMessageReceived"):
            msg_data = payload.get("messageData") or {}
            txt = (msg_data.get("extendedTextMessageData") or {}).get("text") or (msg_data.get("textMessageData") or {}).get("textMessage") or ""
            # Don't pause on bot's own echo
            if not self.storage.is_bot_echo("whatsapp", chat_id, message_id=event_id, text=txt):
                logger.info("Human owner replied in WhatsApp chat %s -> activating %dh auto-pause", chat_id, self.manual_pause_hours)
                self.storage.pause_session("whatsapp", chat_id, hours=self.manual_pause_hours, company_slug=company.slug)
                if event_id:
                    self.storage.mark_event_processed("whatsapp", event_id, "paused_by_owner")
                return {"status": "paused_by_human"}
            return {"status": "ignored_echo"}

        if type_webhook != "incomingMessageReceived":
            return {"status": "ignored", "reason": f"unhandled_{type_webhook}"}

        # 3. Check chat pause
        if self.storage.is_paused("whatsapp", chat_id):
            logger.info("WhatsApp chat %s is paused; bot skipping response", chat_id)
            if event_id:
                self.storage.mark_event_processed("whatsapp", event_id, "skipped_paused")
            return {"status": "paused"}

        msg_data = payload.get("messageData") or {}
        type_message = msg_data.get("typeMessage") or "textMessage"

        wa_client = WhatsAppClient(
            instance_id=company.whatsapp_instance_id,
            api_token=company.whatsapp_api_token,
            client=self._http_client,
        )

        # 4. Handle Media Messages (Photos, Voice)
        if type_message in ("imageMessage", "audioMessage", "voiceMessage", "documentMessage"):
            has_phone = bool(sender_phone and len(sender_phone) >= 10)
            media_kind = "photo" if type_message == "imageMessage" else ("voice" if "audio" in type_message or "voice" in type_message else "file")
            reply_text = build_media_reply(
                company=company,
                media_type=media_kind,
                has_phone=has_phone,
            )
            # Record user media stub in history
            self.storage.add_message("whatsapp", chat_id, "user", f"[{media_kind.upper()}]")
            # Send reply
            await wa_client.send_message(chat_id, reply_text)
            self.storage.record_bot_sent_message("whatsapp", chat_id, text=reply_text)
            self.storage.add_message("whatsapp", chat_id, "assistant", reply_text)

            if event_id:
                self.storage.mark_event_processed("whatsapp", event_id, "replied_media")
            return {"status": "replied_media", "reply": reply_text}

        # 5. Handle Text Messages
        incoming_text = (
            (msg_data.get("extendedTextMessageData") or {}).get("text")
            or (msg_data.get("textMessageData") or {}).get("textMessage")
            or ""
        ).strip()

        if not incoming_text:
            return {"status": "ignored", "reason": "empty_text"}

        # Add incoming message
        self.storage.add_message("whatsapp", chat_id, "user", incoming_text)
        history_msgs = self.storage.get_history("whatsapp", chat_id, limit=12)

        # Quota check
        if self.quota_service:
            t_slug = getattr(company, "tenant_slug", None) or "default"
            q_status = self.quota_service.check(t_slug)
            if not q_status.allowed:
                logger.warning("Quota exceeded for tenant '%s' in WhatsApp chat %s", t_slug, chat_id)
                fallback = "Передал ваш запрос менеджеру, он свяжется с вами."
                await wa_client.send_message(chat_id, fallback)
                self.storage.add_message("whatsapp", chat_id, "assistant", fallback)
                if event_id:
                    self.storage.mark_event_processed("whatsapp", event_id, "quota_blocked")
                return {"status": "quota_blocked"}

        # Generate LLM response with proper channel attribution
        llm_reply = await self.llm.generate_reply(
            company=company,
            history=history_msgs,
            user_message=incoming_text,
            channel="whatsapp",
            chat_id=chat_id,
            tenant_slug=getattr(company, "tenant_slug", None),
        )

        final_text = llm_reply.text

        # Send reply
        await wa_client.send_message(chat_id, final_text)
        self.storage.record_bot_sent_message("whatsapp", chat_id, text=final_text)
        self.storage.add_message("whatsapp", chat_id, "assistant", final_text)

        # 6. Lead Capture with deduplication check
        if llm_reply.lead:
            lead = llm_reply.lead
            lead.channel = "whatsapp"
            lead.chat_id = chat_id
            lead.company_slug = company.slug
            if not lead.client_name and sender_name:
                lead.client_name = sender_name
            # If phone wasn't explicitly in message, fallback to WhatsApp sender number
            if not lead.phone and sender_phone:
                clean_p = sender_phone.replace("@c.us", "")
                if clean_p.startswith("7") or clean_p.startswith("8"):
                    lead.phone = f"+{clean_p}"
            # Record in storage & notify Telegram + CRM only if new
            if lead.phone and not self.storage.has_lead("whatsapp", chat_id, lead.phone):
                self.storage.save_lead(lead)
                await self.notifier.notify(lead, company)

        if event_id:
            self.storage.mark_event_processed("whatsapp", event_id, "replied")

        return {"status": "replied", "reply": final_text, "has_lead": bool(llm_reply.lead)}
