import asyncio
import logging
import time
from typing import Any, Dict, List, Optional, Set

import httpx

from app.company_store import CompanyStore
from app.llm import LLMService
from app.media_reply import build_media_reply
from app.notifier import LeadNotifier
from app.storage import Storage

logger = logging.getLogger(__name__)


class AvitoClient:
    """Async client for Avito Messenger API (OAuth2 client_credentials)."""

    BASE_URL = "https://api.avito.ru"

    def __init__(
        self,
        client_id: str,
        client_secret: str,
        user_id: int,
        http_client: Optional[httpx.AsyncClient] = None,
    ) -> None:
        self.client_id = client_id
        self.client_secret = client_secret
        self.user_id = user_id
        self._http = http_client or httpx.AsyncClient(timeout=15.0)
        self._access_token: Optional[str] = None
        self._token_expires_at: float = 0.0

    async def close(self) -> None:
        await self._http.aclose()

    async def get_access_token(self) -> str:
        now = time.time()
        if self._access_token and now < self._token_expires_at - 60:
            return self._access_token

        resp = await self._http.post(
            f"{self.BASE_URL}/token",
            data={
                "grant_type": "client_credentials",
                "client_id": self.client_id,
                "client_secret": self.client_secret,
            },
            headers={"Content-Type": "application/x-www-form-urlencoded"},
        )
        resp.raise_for_status()
        data = resp.json()
        self._access_token = data["access_token"]
        self._token_expires_at = now + float(data.get("expires_in", 3600))
        return self._access_token

    async def send_message(self, chat_id: str, text: str) -> Dict[str, Any]:
        token = await self.get_access_token()
        url = f"{self.BASE_URL}/messenger/v1/accounts/{self.user_id}/chats/{chat_id}/messages"
        resp = await self._http.post(
            url,
            json={"message": {"text": text}, "type": "text"},
            headers={"Authorization": f"Bearer {token}"},
        )
        resp.raise_for_status()
        return resp.json()

    async def register_webhook(self, webhook_url: str) -> Dict[str, Any]:
        token = await self.get_access_token()
        url = f"{self.BASE_URL}/messenger/v3/webhook"
        resp = await self._http.post(
            url,
            json={"url": webhook_url},
            headers={"Authorization": f"Bearer {token}"},
        )
        resp.raise_for_status()
        return resp.json()


class AvitoWebhookHandler:
    """Processes incoming Avito Messenger webhooks with queue, deduplication, and persistent echo tracking."""

    def __init__(
        self,
        avito_client: AvitoClient,
        company_store: CompanyStore,
        storage: Storage,
        llm: LLMService,
        notifier: LeadNotifier,
        default_company_slug: str,
        manual_pause_hours: int = 24,
        max_history_messages: int = 14,
        quota_service: Optional[Any] = None,
        alert_manager: Optional[Any] = None,
    ) -> None:
        self.avito_client = avito_client
        self.company_store = company_store
        self.storage = storage
        self.llm = llm
        self.notifier = notifier
        self.default_company_slug = default_company_slug
        self.manual_pause_hours = manual_pause_hours
        self.max_history_messages = max_history_messages
        self.quota_service = quota_service
        self.alert_manager = alert_manager
        self._bot_sent_message_ids: Set[str] = set()
        self._chat_locks: Dict[str, asyncio.Lock] = {}
        self._client_cache: Dict[str, AvitoClient] = {}
        self.queue: asyncio.Queue = asyncio.Queue()
        self._worker_tasks: List[asyncio.Task] = []

    def _get_client_for_account(self, company_slug: str, avito_user_id: Optional[int] = None) -> AvitoClient:
        acc = None
        if avito_user_id:
            try:
                acc = self.storage.get_avito_account_by_user_id(int(avito_user_id))
            except (ValueError, TypeError):
                pass
        if not acc and company_slug:
            accounts = self.storage.list_avito_accounts()
            for a in accounts:
                if a.get("company_slug") == company_slug and a.get("enabled"):
                    acc = self.storage.get_avito_account_by_user_id(a["user_id"])
                    break
        if acc and acc.get("client_id") and acc.get("client_secret"):
            c_key = f"{acc['client_id']}:{acc['user_id']}"
            if c_key not in self._client_cache:
                self._client_cache[c_key] = AvitoClient(
                    client_id=acc["client_id"],
                    client_secret=acc["client_secret"],
                    user_id=int(acc["user_id"]),
                )
            return self._client_cache[c_key]
        return self.avito_client

    def _get_chat_lock(self, chat_id: str) -> asyncio.Lock:
        if chat_id not in self._chat_locks:
            self._chat_locks[chat_id] = asyncio.Lock()
        return self._chat_locks[chat_id]

    async def start_workers(self, num_workers: int = 4) -> None:
        for i in range(num_workers):
            t = asyncio.create_task(self._worker_loop(), name=f"avito_worker_{i}")
            self._worker_tasks.append(t)

    async def _worker_loop(self) -> None:
        while True:
            try:
                payload = await self.queue.get()
                await self.handle_event(payload)
                self.queue.task_done()
            except asyncio.CancelledError:
                break
            except Exception as exc:
                logger.error("Error in Avito background queue worker: %s", exc)
                self.queue.task_done()

    async def handle_event(self, payload: Dict[str, Any]) -> Dict[str, str]:
        event_payload = payload.get("payload", {})
        if event_payload.get("type") != "message":
            return {"status": "ignored", "reason": "not_a_message"}

        value = event_payload.get("value", {})
        chat_id = str(value.get("chat_id", ""))
        message_id = str(value.get("id", ""))
        author_id = value.get("author_id")
        msg_type = value.get("type", "")
        content = value.get("content", {}) or {}
        text = (content.get("text") or "").strip()

        if not chat_id:
            return {"status": "ignored", "reason": "no_chat_id"}

        # K3: Deduplication check
        if message_id and self.storage.is_event_processed("avito", message_id):
            logger.info("Avito duplicate event %s skipped", message_id)
            return {"status": "ignored", "reason": "duplicate"}

        is_media = msg_type in {"image", "voice", "file"}
        if msg_type != "text" and not is_media:
            return {"status": "ignored", "reason": "unsupported_type"}
        if msg_type == "text" and not text:
            return {"status": "ignored", "reason": "empty_text"}

        lock = self._get_chat_lock(chat_id)
        async with lock:
            # Mark processing
            if message_id:
                self.storage.mark_event_processed("avito", message_id, status="processing")

            company_slug = (
                payload.get("_company_slug")
                or self.storage.get_session_company("avito", chat_id)
                or self.default_company_slug
            )
            company = self.company_store.get_or_default(company_slug, self.default_company_slug)
            if not company:
                logger.error("No company profile available for Avito chat %s", chat_id)
                return {"status": "error", "reason": "no_company_profile"}

            user_id_raw = value.get("user_id") or payload.get("_avito_user_id")
            business_user_id: Optional[int] = None
            if user_id_raw is not None:
                try:
                    business_user_id = int(user_id_raw)
                except (ValueError, TypeError):
                    business_user_id = None
            active_client = self._get_client_for_account(company.slug, avito_user_id=business_user_id)
            if not active_client:
                logger.warning("No active AvitoClient configured for company %s", company.slug)
                return {"status": "error", "reason": "no_avito_client"}

            # K4: Persistent echo check using active tenant client's user_id
            bot_user_id = getattr(active_client, "user_id", None)
            if bot_user_id is not None and author_id == bot_user_id:
                if message_id and message_id in self._bot_sent_message_ids:
                    self._bot_sent_message_ids.discard(message_id)
                    return {"status": "ignored", "reason": "bot_echo"}

                if self.storage.is_bot_echo("avito", chat_id, message_id, text=text, window_seconds=60):
                    return {"status": "ignored", "reason": "bot_echo"}

                self.storage.pause_session(
                    channel="avito",
                    chat_id=chat_id,
                    hours=self.manual_pause_hours,
                    company_slug=company.slug,
                )
                logger.info(
                    "Auto-paused Avito chat %s (company=%s) for %dh after manual owner reply",
                    chat_id,
                    company.slug,
                    self.manual_pause_hours,
                )
                return {"status": "paused", "reason": "manual_owner_reply"}

            if self.storage.is_paused("avito", chat_id):
                logger.info("Avito chat %s is paused; skipping AI reply", chat_id)
                return {"status": "ignored", "reason": "paused"}

            self.storage.upsert_session("avito", chat_id, company.slug)

            # Quota check (Block B: TZ B3) — проверяем ДО вызова генерации для текста и медиа
            if self.quota_service:
                t_slug = getattr(company, "tenant_slug", None) or "default"
                q_status = self.quota_service.check(t_slug)
                if not q_status.allowed:
                    logger.warning("Quota exceeded for tenant '%s' in Avito chat %s: %s", t_slug, chat_id, q_status.reason)
                    fallback_reply = "Передал ваш запрос менеджеру, он свяжется с вами."
                    self.storage.add_message("avito", chat_id, "user", text or f"[{msg_type}]")
                    self.storage.add_message("avito", chat_id, "assistant", fallback_reply)
                    self.storage.record_guard_event(
                        channel="avito",
                        chat_id=chat_id,
                        company_slug=company.slug,
                        original_reply="<quota_blocked>",
                        safe_reply=fallback_reply,
                        blocked_terms="quota",
                    )
                    rec_id = self.storage.record_bot_sent_message("avito", chat_id, text=fallback_reply, status="pending")
                    sent = await active_client.send_message(chat_id, fallback_reply)
                    sent_id = str(sent.get("id", ""))
                    if sent_id:
                        self._bot_sent_message_ids.add(sent_id)
                        self.storage.update_bot_sent_message_id(rec_id, sent_id)
                    if message_id:
                        self.storage.mark_event_processed("avito", message_id, status="done")
                    return {"status": "quota_blocked"}

                if q_status.soft_limit and self.alert_manager:
                    await self.alert_manager.send_alert(
                        f"quota_soft_{t_slug}",
                        f"⚠️ Внимание: клиент *{t_slug}* израсходовал более 80% лимита тарифа "
                        f"({q_status.used_messages}/{q_status.limit_messages} сообщений).",
                    )

            if is_media:
                has_phone = self.storage.has_lead_for_chat("avito", chat_id)
                img_url = None
                if msg_type == "image":
                    img_data = content.get("image", {})
                    if isinstance(img_data, dict):
                        sizes = img_data.get("sizes", {})
                        if isinstance(sizes, dict):
                            sorted_keys = sorted(
                                sizes.keys(),
                                key=lambda s: int(s.split("x")[0]) if "x" in s and s.split("x")[0].isdigit() else 0,
                                reverse=True,
                            )
                            if sorted_keys:
                                img_url = sizes[sorted_keys[0]]

                if img_url:
                    try:
                        history = self.storage.get_history("avito", chat_id, limit=self.max_history_messages)
                        avito_chat_url = f"https://www.avito.ru/profile/messenger/channel/{chat_id}"
                        reply = await self.llm.generate_reply(
                            company=company,
                            history=history,
                            user_message="Оцените повреждение или объём работ по прикреплённой фотографии автомобиля.",
                            channel="avito",
                            chat_id=chat_id,
                            avito_chat_url=avito_chat_url,
                            image_url=img_url,
                        )
                        reply_text = reply.text
                        self.storage.add_message("avito", chat_id, "user", "[фото]")
                        self.storage.add_message("avito", chat_id, "assistant", reply_text)
                        rec_id = self.storage.record_bot_sent_message("avito", chat_id, text=reply_text, status="pending")
                        sent = await active_client.send_message(chat_id, reply_text)
                        sent_id = str(sent.get("id", ""))
                        if sent_id:
                            self._bot_sent_message_ids.add(sent_id)
                            self.storage.update_bot_sent_message_id(rec_id, sent_id)
                        if reply.lead and not self.storage.has_lead("avito", chat_id, reply.lead.phone):
                            self.storage.save_lead(reply.lead)
                            await self.notifier.notify(reply.lead, company)
                        if message_id:
                            self.storage.mark_event_processed("avito", message_id, status="done")
                        return {"status": "replied_vision"}
                    except Exception as exc:
                        logger.warning("Vision analysis in Avito failed (%s), falling back to template reply", exc)

                media_reply = build_media_reply(company, media_type=msg_type, has_phone=has_phone)
                self.storage.add_message("avito", chat_id, "user", f"[{msg_type}]")
                self.storage.add_message("avito", chat_id, "assistant", media_reply)

                # Pre-record pending echo to defeat race conditions (K4)
                rec_id = self.storage.record_bot_sent_message("avito", chat_id, text=media_reply, status="pending")
                sent = await active_client.send_message(chat_id, media_reply)
                sent_id = str(sent.get("id", ""))
                if sent_id:
                    self._bot_sent_message_ids.add(sent_id)
                    self.storage.update_bot_sent_message_id(rec_id, sent_id)
                if message_id:
                    self.storage.mark_event_processed("avito", message_id, status="done")
                return {"status": "replied_media"}

            history = self.storage.get_history("avito", chat_id, limit=self.max_history_messages)
            avito_chat_url = f"https://www.avito.ru/profile/messenger/channel/{chat_id}"

            reply = await self.llm.generate_reply(
                company=company,
                history=history,
                user_message=text,
                channel="avito",
                chat_id=chat_id,
                avito_chat_url=avito_chat_url,
            )

            self.storage.add_message("avito", chat_id, "user", text)
            self.storage.add_message("avito", chat_id, "assistant", reply.text)

            # Pre-record pending echo (K4)
            rec_id = self.storage.record_bot_sent_message("avito", chat_id, text=reply.text, status="pending")
            sent = await active_client.send_message(chat_id, reply.text)
            sent_id = str(sent.get("id", ""))
            if sent_id:
                self._bot_sent_message_ids.add(sent_id)
                self.storage.update_bot_sent_message_id(rec_id, sent_id)
                if len(self._bot_sent_message_ids) > 1000:
                    self._bot_sent_message_ids.clear()

            if reply.lead and not self.storage.has_lead("avito", chat_id, reply.lead.phone):
                self.storage.save_lead(reply.lead)
                await self.notifier.notify(reply.lead, company)

            gap = getattr(reply, "knowledge_gap", None)
            if gap:
                self.storage.save_knowledge_gap(gap)
                await self.notifier.notify_knowledge_gap(gap, company)

            if message_id:
                self.storage.mark_event_processed("avito", message_id, status="done")

            return {"status": "replied"}
