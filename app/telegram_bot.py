import logging
import time
from collections import defaultdict, deque
from typing import Any, Deque, Dict, Optional

from aiogram import Bot, Dispatcher, F, Router
from aiogram.enums import ChatAction
from aiogram.filters import Command, CommandObject, CommandStart
from aiogram.types import Message

from app.company_store import CompanyStore
from app.config import Settings
from app.llm import LLMService
from app.media_reply import build_media_reply
from app.notifier import LeadNotifier
from app.storage import Storage

logger = logging.getLogger(__name__)


class RateLimiter:
    """Simple per-chat rate limiter to protect LLM API balance from spam."""

    def __init__(self, min_interval_sec: float = 1.5, max_per_hour: int = 30) -> None:
        self.min_interval_sec = min_interval_sec
        self.max_per_hour = max_per_hour
        self._timestamps: Dict[str, Deque[float]] = defaultdict(deque)

    def allow(self, chat_id: str, now: Optional[float] = None) -> bool:
        ts = now if now is not None else time.monotonic()
        bucket = self._timestamps[chat_id]
        if bucket and (ts - bucket[-1]) < self.min_interval_sec:
            return False
        cutoff = ts - 3600.0
        while bucket and bucket[0] < cutoff:
            bucket.popleft()
        if len(bucket) >= self.max_per_hour:
            return False
        bucket.append(ts)
        return True


def build_telegram_router(
    settings: Settings,
    company_store: CompanyStore,
    storage: Storage,
    llm: LLMService,
    notifier: LeadNotifier,
    tenant_slug: Optional[str] = None,
    admin_ids: Optional[list] = None,
    quota_service: Optional[Any] = None,
    alert_manager: Optional[Any] = None,
) -> Router:
    router = Router()
    rate_limiter = RateLimiter()
    effective_admins = set(admin_ids if admin_ids is not None else settings.admin_ids)

    def is_admin(message: Message) -> bool:
        return bool(message.from_user and message.from_user.id in effective_admins)

    def resolve_company(requested_slug: Optional[str]):
        """Компания только из числа компаний этого тенанта."""
        return company_store.get_or_default_for_tenant(
            tenant_slug, requested_slug, settings.default_company_slug
        )

    @router.message(CommandStart())
    async def cmd_start(message: Message, command: CommandObject) -> None:
        chat_id = str(message.chat.id)
        requested_slug = (command.args or "").strip() or None
        company = resolve_company(requested_slug)
        if not company:
            await message.answer(
                "Профили компаний ещё не настроены. Добавьте YAML-файл в папку companies/."
            )
            return

        storage.upsert_session("telegram", chat_id, company.slug)
        storage.clear_history("telegram", chat_id)
        storage.resume_session("telegram", chat_id)

        if company.greeting.strip():
            greeting = company.greeting.strip()
        elif company.disclose_ai:
            greeting = (
                f"Здравствуйте! На связи студия «{company.company_name}» ({company.city}). "
                f"Помогу сориентироваться по стоимости работ и передам расчёт старшему мастеру {company.manager_name}. "
                f"Подскажите, какой у вас автомобиль и что планируете сделать?"
            )
        else:
            greeting = (
                f"Здравствуйте! Я {company.manager_name}, {company.manager_role} "
                f"компании «{company.company_name}» ({company.city}). "
                f"Подскажите, какая услуга вас интересует?"
            )
        storage.add_message("telegram", chat_id, "assistant", greeting)
        await message.answer(greeting)

        if not is_admin(message):
            user_label = "Неизвестный"
            if message.from_user:
                uname = f"@{message.from_user.username}" if message.from_user.username else ""
                user_label = f"{message.from_user.full_name} {uname}".strip()
            await notifier.notify_demo_started(company, user_label, chat_id)

    @router.message(Command("reset"))
    async def cmd_reset(message: Message) -> None:
        chat_id = str(message.chat.id)
        storage.clear_history("telegram", chat_id)
        storage.resume_session("telegram", chat_id)
        slug = storage.get_session_company("telegram", chat_id)
        company = resolve_company(slug)
        if company and company.greeting:
            storage.add_message("telegram", chat_id, "assistant", company.greeting.strip())
            await message.answer(f"Диалог сброшен.\n\n{company.greeting.strip()}")
        else:
            await message.answer("Диалог сброшен. Напишите ваш вопрос!")

    @router.message(Command("admin", "companies"))
    async def cmd_companies(message: Message, bot: Bot) -> None:
        if not is_admin(message):
            return
        me = await bot.get_me()
        bot_username = me.username or "your_bot"
        profiles = company_store.list_for_tenant(tenant_slug)
        if not profiles:
            await message.answer("В папке companies/ пока нет активных профилей.")
            return

        lines = ["<b>Активные демо-профили компаний:</b>\n"]
        for p in profiles:
            link = f"https://t.me/{bot_username}?start={p.slug}"
            lines.append(
                f"• <b>{p.company_name}</b> ({p.city})\n"
                f"  Slug: <code>{p.slug}</code>\n"
                f"  Ссылка для клиента: {link}\n"
            )
        lines.append(
            "Команды админа: /reload (обновить YAML), /leads (последние заявки), "
            "/pause &lt;chat_id&gt;, /resume &lt;chat_id&gt;"
        )
        await message.answer("\n".join(lines), parse_mode="HTML", disable_web_page_preview=True)

    @router.message(Command("reload"))
    async def cmd_reload(message: Message) -> None:
        if not is_admin(message):
            return
        count = company_store.reload()
        await message.answer(f"Профили перезагружены. Загружено компаний: {count}.")

    @router.message(Command("leads"))
    async def cmd_leads(message: Message) -> None:
        if not is_admin(message):
            return
        leads = storage.list_recent_leads(limit=10)
        if not leads:
            await message.answer("Заявок пока нет.")
            return
        lines = ["<b>Последние 10 заявок:</b>\n"]
        for lead in leads:
            src = "Авито" if lead.channel == "avito" else "TG"
            lines.append(
                f"• [{src}] <b>{lead.company_slug}</b> — <code>{lead.phone}</code> "
                f"({lead.client_name or 'без имени'})\n"
                f"  {lead.summary or lead.service_interest or '-'}"
            )
        await message.answer("\n".join(lines), parse_mode="HTML")

    @router.message(Command("pause"))
    async def cmd_pause(message: Message, command: CommandObject) -> None:
        if not is_admin(message):
            return
        target = (command.args or "").strip()
        if not target:
            await message.answer("Использование: /pause <chat_id>")
            return
        channel = "avito" if not target.lstrip("-").isdigit() else "telegram"
        storage.pause_session(channel, target, hours=settings.avito_manual_pause_hours)
        await message.answer(
            f"Бот поставлен на паузу для {channel}:{target} на {settings.avito_manual_pause_hours} ч."
        )

    @router.message(Command("resume"))
    async def cmd_resume(message: Message, command: CommandObject) -> None:
        if not is_admin(message):
            return
        target = (command.args or "").strip()
        if not target:
            await message.answer("Использование: /resume <chat_id>")
            return
        channel = "avito" if not target.lstrip("-").isdigit() else "telegram"
        storage.resume_session(channel, target)
        await message.answer(f"Бот снова активен для {channel}:{target}.")

    @router.message(Command("forget"))
    async def cmd_forget(message: Message, command: CommandObject) -> None:
        chat_id = str(message.chat.id)
        target = (command.args or "").strip()
        if target and is_admin(message):
            del_cnt = storage.delete_subject_data(target)
            await message.answer(f"152-ФЗ: Данные субъекта «{target}» удалены ({del_cnt} записей).")
        else:
            storage.clear_history("telegram", chat_id)
            del_cnt = storage.delete_subject_data(chat_id)
            await message.answer(
                "🔒 Ваша история диалога и персональные данные полностью удалены в соответствии со 152-ФЗ."
            )

    @router.message(F.photo | F.voice | F.video | F.document)
    async def handle_media(message: Message, bot: Bot) -> None:
        chat_id = str(message.chat.id)
        if storage.is_paused("telegram", chat_id):
            return
        if not is_admin(message) and not rate_limiter.allow(chat_id):
            return

        slug = storage.get_session_company("telegram", chat_id)
        company = resolve_company(slug)
        if not company:
            return

        storage.upsert_session("telegram", chat_id, company.slug)

        if message.photo:
            mtype = "photo"
            if settings.vision_enabled:
                # Квота проверяется и для фото: vision-запросы самые дорогие
                tenant_key = tenant_slug or getattr(company, "tenant_slug", None) or "default"
                if quota_service:
                    q_status = quota_service.check(tenant_key)
                    if not q_status.allowed:
                        logger.warning(
                            "Quota exceeded (vision) for tenant '%s': %s", tenant_key, q_status.reason
                        )
                        fallback_reply = "Передал ваш запрос менеджеру, он свяжется с вами."
                        storage.add_message("telegram", chat_id, "user", "[фото]")
                        storage.add_message("telegram", chat_id, "assistant", fallback_reply)
                        storage.record_guard_event(
                            channel="telegram",
                            chat_id=chat_id,
                            company_slug=company.slug,
                            original_reply="<quota_blocked_vision>",
                            safe_reply=fallback_reply,
                            blocked_terms="quota",
                        )
                        await message.answer(fallback_reply)
                        return
                try:
                    photo_size = message.photo[-1]
                    file_info = await bot.get_file(photo_size.file_id)
                    if file_info.file_path:
                        photo_url = f"https://api.telegram.org/file/bot{bot.token}/{file_info.file_path}"
                        history = storage.get_history("telegram", chat_id, limit=settings.max_history_messages)
                        caption = (message.caption or "").strip()
                        user_prompt = caption or "Оцените повреждение или объём работ по прикреплённой фотографии автомобиля."
                        reply = await llm.generate_reply(
                            company=company,
                            history=history,
                            user_message=user_prompt,
                            channel="telegram",
                            chat_id=chat_id,
                            image_url=photo_url,
                        )
                        storage.add_message("telegram", chat_id, "user", f"[фото] {caption}".strip())
                        storage.add_message("telegram", chat_id, "assistant", reply.text)
                        if reply.lead and not storage.has_lead("telegram", chat_id, reply.lead.phone):
                            storage.save_lead(reply.lead)
                            await notifier.notify(reply.lead, company)
                        gap = getattr(reply, "knowledge_gap", None)
                        if gap:
                            storage.save_knowledge_gap(gap)
                            await notifier.notify_knowledge_gap(gap, company)
                        await message.answer(reply.text)
                        return
                except Exception as exc:
                    logger.warning("Vision analysis in Telegram failed (%s), falling back to template", exc)
        elif message.voice or message.audio:
            mtype = "voice"
        elif message.video or message.video_note:
            mtype = "video"
        elif message.document:
            mtype = "document"
        else:
            mtype = "media"

        has_phone = storage.has_lead_for_chat("telegram", chat_id)
        media_reply = build_media_reply(company, media_type=mtype, has_phone=has_phone)
        storage.add_message("telegram", chat_id, "user", f"[{mtype}]")
        storage.add_message("telegram", chat_id, "assistant", media_reply)
        await message.answer(media_reply)

    @router.message(F.text)
    async def handle_text(message: Message, bot: Bot) -> None:
        chat_id = str(message.chat.id)
        text = (message.text or "").strip()
        if not text:
            return

        if storage.is_paused("telegram", chat_id):
            return

        if not is_admin(message) and not rate_limiter.allow(chat_id):
            await message.answer("Секунду, обрабатываю предыдущее сообщение — напишите чуть позже.")
            return

        slug = storage.get_session_company("telegram", chat_id)
        company = resolve_company(slug)
        if not company:
            await message.answer("Профиль компании не найден.")
            return

        storage.upsert_session("telegram", chat_id, company.slug)
        history = storage.get_history("telegram", chat_id, limit=settings.max_history_messages)
        client_name_hint: Optional[str] = None
        if message.from_user:
            client_name_hint = message.from_user.full_name

        try:
            await bot.send_chat_action(chat_id=message.chat.id, action=ChatAction.TYPING)
        except Exception:
            pass

        # Quota check (Block B: TZ B3)
        tenant_key = tenant_slug or getattr(company, "tenant_slug", None) or "default"
        if quota_service:
            q_status = quota_service.check(tenant_key)
            if not q_status.allowed:
                logger.warning("Quota exceeded for tenant '%s': %s", tenant_key, q_status.reason)
                storage.add_message("telegram", chat_id, "user", text)
                fallback_reply = "Передал ваш запрос менеджеру, он свяжется с вами."
                storage.add_message("telegram", chat_id, "assistant", fallback_reply)
                storage.record_guard_event(
                    channel="telegram",
                    chat_id=chat_id,
                    company_slug=company.slug,
                    original_reply="<quota_blocked>",
                    safe_reply=fallback_reply,
                    blocked_terms="quota",
                )
                await message.answer(fallback_reply)
                return

            if q_status.soft_limit and alert_manager:
                await alert_manager.send_alert(
                    f"quota_soft_{tenant_key}",
                    f"⚠️ Внимание: клиент *{tenant_key}* израсходовал более 80% лимита тарифа "
                    f"({q_status.used_messages}/{q_status.limit_messages} сообщений).",
                )

        try:
            reply = await llm.generate_reply(
                company=company,
                history=history,
                user_message=text,
                channel="telegram",
                chat_id=chat_id,
                client_name_hint=client_name_hint or "",
            )
        except Exception as exc:
            logger.exception("LLM error for telegram chat %s: %s", chat_id, exc)
            await message.answer(
                "Секунду, уточняю информацию по базе. Напишите, пожалуйста, ещё раз через минуту."
            )
            return

        storage.add_message("telegram", chat_id, "user", text)
        storage.add_message("telegram", chat_id, "assistant", reply.text)
        await message.answer(reply.text)

        if reply.lead and not storage.has_lead("telegram", chat_id, reply.lead.phone):
            storage.save_lead(reply.lead)
            await notifier.notify(reply.lead, company)

        gap = getattr(reply, "knowledge_gap", None)
        if gap:
            storage.save_knowledge_gap(gap)
            await notifier.notify_knowledge_gap(gap, company)

    return router


def create_dispatcher(
    settings: Settings,
    company_store: CompanyStore,
    storage: Storage,
    llm: LLMService,
    notifier: LeadNotifier,
    tenant_slug: Optional[str] = None,
    admin_ids: Optional[list] = None,
    quota_service: Optional[Any] = None,
    alert_manager: Optional[Any] = None,
) -> Dispatcher:
    dp = Dispatcher()
    dp.include_router(
        build_telegram_router(
            settings, company_store, storage, llm, notifier,
            tenant_slug=tenant_slug, admin_ids=admin_ids,
            quota_service=quota_service, alert_manager=alert_manager,
        )
    )
    return dp
