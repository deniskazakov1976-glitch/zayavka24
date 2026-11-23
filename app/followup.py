import asyncio
import logging
from typing import Any, Optional

from app.company_store import CompanyStore
from app.media_reply import get_work_status
from app.models import CompanyProfile
from app.storage import Storage

logger = logging.getLogger(__name__)


class FollowupService:
    """Smart Follow-up Service: sends 1 gentle reminder after 3 hours during working hours if no lead was captured."""

    def __init__(
        self,
        storage: Storage,
        company_store: CompanyStore,
        bot_manager: Optional[Any] = None,
        avito_client: Optional[Any] = None,
        older_than_hours: float = 3.0,
        interval_seconds: int = 900,
    ) -> None:
        self.storage = storage
        self.company_store = company_store
        self.bot_manager = bot_manager
        self.avito_client = avito_client
        self.older_than_hours = older_than_hours
        self.interval_seconds = interval_seconds
        self._task: Optional[asyncio.Task] = None
        self._running = False

    def build_followup_text(self, company: CompanyProfile) -> str:
        manager = company.manager_name or "наш старший мастер"
        offer = company.discount_offer or "спецпредложение на первый визит"
        return (
            f"Здравствуйте! Хотели уточнить, актуален ли ещё ваш вопрос? "
            f"Мастер {manager} готов проконсультировать вас и закрепить за вами {offer}. "
            f"Напишите удобный номер телефона — наберём в удобное для вас время!"
        )

    async def run_once(self) -> int:
        """Checks pending sessions and dispatches follow-ups. Returns number of sent follow-ups."""
        sessions = self.storage.get_sessions_needing_followup(older_than_hours=self.older_than_hours)
        sent_count = 0
        for s in sessions:
            channel = s["channel"]
            chat_id = s["chat_id"]
            slug = s["company_slug"]
            company = self.company_store.get(slug)
            if not company:
                continue

            # Respect working hours: do not disturb clients at night
            try:
                status = get_work_status(company)
                if not status.is_open:
                    continue
            except Exception:
                pass

            text = self.build_followup_text(company)
            sent = False
            try:
                if channel == "telegram" and self.bot_manager:
                    bot = None
                    tenant_slug = company.tenant_slug or "default"
                    tenant_data = getattr(self.bot_manager, "tenants", {}).get(tenant_slug)
                    if tenant_data:
                        bot = tenant_data.get("bot")
                    if bot:
                        await bot.send_message(chat_id=int(chat_id), text=text)
                        sent = True
                elif channel == "avito" and self.avito_client:
                    await self.avito_client.send_message(chat_id=chat_id, text=text)
                    sent = True
                elif channel == "whatsapp":
                    if company.whatsapp_enabled and company.whatsapp_instance_id and company.whatsapp_api_token:
                        from app.whatsapp import WhatsAppClient
                        wa = WhatsAppClient(company.whatsapp_instance_id, company.whatsapp_api_token)
                        await wa.send_message(chat_id=chat_id, text=text)
                        sent = True
            except Exception as exc:
                logger.warning("Failed to send follow-up to %s:%s - %s", channel, chat_id, exc)

            if sent:
                self.storage.mark_followup_sent(channel, chat_id)
                self.storage.add_message(channel, chat_id, "assistant", text)
                sent_count += 1
            else:
                self.storage.mark_followup_sent(channel, chat_id)

        return sent_count

    async def _loop(self) -> None:
        while self._running:
            try:
                await self.run_once()
            except Exception as exc:
                logger.exception("Error in FollowupService loop: %s", exc)
            await asyncio.sleep(self.interval_seconds)

    def start(self) -> None:
        if not self._running:
            self._running = True
            self._task = asyncio.create_task(self._loop(), name="smart_followup_service")
            logger.info("FollowupService started (interval %ds)", self.interval_seconds)

    def stop(self) -> None:
        self._running = False
        if self._task and not self._task.done():
            self._task.cancel()
            logger.info("FollowupService stopped")
