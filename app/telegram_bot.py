import asyncio
import logging
import time
from collections import defaultdict, deque
from typing import Any, Deque, Dict, Optional

from aiogram import Bot, Dispatcher, F, Router
from aiogram.enums import ChatAction
from aiogram.filters import Command, CommandObject, CommandStart
from aiogram.types import (
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
)

from app.company_store import CompanyStore
from app.config import Settings
from app.llm import LLMService
from app.media_reply import build_media_reply
from app.notifier import LeadNotifier
from app.storage import Storage

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Ключевое слово для входа с лендинга в режиме демо-стенда платформы
# ---------------------------------------------------------------------------
_PLATFORM_DEMO_SLUG = "demo"

# Состояния SaaS-онбординга в памяти (chat_id -> dict)
_saas_wizard: Dict[str, Dict[str, Any]] = {}

# ВАУ-эффект: какие chat_id уже получили лид, но ещё не видели вау-карточку
_pending_wow: Dict[str, Dict[str, str]] = {}


def _platform_menu_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🚗 Протестировать диалог как клиент", callback_data="demo:test")],
        [InlineKeyboardButton(text="🚀 Запустить тест-драйв на 3 дня", callback_data="demo:trial")],
        [InlineKeyboardButton(text="💰 Тарифы и окупаемость", callback_data="demo:pricing")],
        [InlineKeyboardButton(text="📞 Связаться с основателем", callback_data="demo:contact")],
    ])


def _test_scenarios_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="«Покрась бампер на Toyota Camry»", callback_data="demo:q1")],
        [InlineKeyboardButton(text="«Оклейка зон риска на Geely Monjaro»", callback_data="demo:q2")],
        [InlineKeyboardButton(text="«Работаете в воскресенье?»", callback_data="demo:q3")],
        [InlineKeyboardButton(text="«Сделайте скидку 50%!»", callback_data="demo:q4")],
        [InlineKeyboardButton(text="🔙 Главное меню", callback_data="demo:menu")],
    ])


def _trial_cta_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🚀 Запустить тест-драйв на 3 дня", callback_data="demo:trial")],
        [InlineKeyboardButton(text="🔙 Главное меню", callback_data="demo:menu")],
    ])



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

    # ---------------------------------------------------------------------------
    # Вспомогательные функции для demo-воронки платформы
    # ---------------------------------------------------------------------------

    def _is_demo_mode(chat_id: str) -> bool:
        """Пользователь вошёл через /start demo с лендинга."""
        return storage.get_session_company("telegram", chat_id) == _PLATFORM_DEMO_SLUG

    async def _send_platform_welcome(message: Message) -> None:
        """Приветственное сообщение платформы с меню выбора роли."""
        text = (
            "👋 <b>Добро пожаловать на демо-стенд платформы «Заявка 24/7»!</b>\n\n"
            "Прямо сейчас бот работает в режиме старшего мастера детейлинг-студии и кузовного ремонта.\n\n"
            "Здесь вы можете:\n"
            "• <b>Проверить</b>, как ИИ общается с вашими клиентами (попробуйте сами!)\n"
            "• <b>Запустить</b> тест-драйв бота под ваш прайс-лист за 10 минут\n\n"
            "<b>Что хотите сделать?</b>"
        )
        await message.answer(text, parse_mode="HTML", reply_markup=_platform_menu_keyboard())

    @router.message(CommandStart())
    async def cmd_start(message: Message, command: CommandObject) -> None:
        chat_id = str(message.chat.id)
        raw_arg = (command.args or "").strip()

        # --- Вход с лендинга в режим демо-платформы ---
        if raw_arg == _PLATFORM_DEMO_SLUG:
            storage.upsert_session("telegram", chat_id, _PLATFORM_DEMO_SLUG)
            storage.clear_history("telegram", chat_id)
            _saas_wizard.pop(chat_id, None)
            _pending_wow.pop(chat_id, None)
            if not is_admin(message):
                user_label = "Неизвестный"
                if message.from_user:
                    uname = f"@{message.from_user.username}" if message.from_user.username else ""
                    user_label = f"{message.from_user.full_name} {uname}".strip()
                await notifier.notify_demo_started_platform(user_label, chat_id)
            await _send_platform_welcome(message)
            return

        # --- Обычный вход: клиент автосервиса ---
        requested_slug = raw_arg or None
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

    # ---------------------------------------------------------------------------
    # Callback-кнопки demo-меню
    # ---------------------------------------------------------------------------

    @router.callback_query(F.data.startswith("demo:"))
    async def handle_demo_callback(callback: CallbackQuery) -> None:
        chat_id = str(callback.message.chat.id)
        action = callback.data.split(":", 1)[1]
        await callback.answer()  # убираем «часики» у кнопки

        if action == "menu":
            await callback.message.answer(
                "👋 <b>Главное меню платформы «Заявка 24/7»</b>\n\nЧто хотите сделать?",
                parse_mode="HTML",
                reply_markup=_platform_menu_keyboard(),
            )
            return

        if action == "test":
            await callback.message.answer(
                "🚗 <b>Режим тестирования как клиент</b>\n\n"
                "Представьте, что вы — автовладелец с Авито.\n"
                "Нажмите один из частых вопросов ниже или напишите свой (можно каверзный):",
                parse_mode="HTML",
                reply_markup=_test_scenarios_keyboard(),
            )
            return

        if action in ("q1", "q2", "q3", "q4"):
            q_map = {
                "q1": "Сколько стоит покрасить передний бампер на Toyota Camry?",
                "q2": "Оклейка зон риска на Geely Monjaro — сколько и как долго?",
                "q3": "Работаете в воскресенье? Хочу на осмотр.",
                "q4": "Сделайте скидку 50%, иначе уйду к конкурентам!",
            }
            user_q = q_map[action]
            # Переключаем на demo_detailing для теста ИИ
            demo_company = resolve_company("demo_detailing") or resolve_company(None)
            if not demo_company:
                await callback.message.answer("Демо-профиль не найден. Обратитесь к администратору.")
                return
            storage.upsert_session("telegram", chat_id, demo_company.slug)
            storage.resume_session("telegram", chat_id)
            history = storage.get_history("telegram", chat_id, limit=10)
            try:
                await callback.message.bot.send_chat_action(
                    chat_id=callback.message.chat.id, action=ChatAction.TYPING
                )
            except Exception:
                pass
            try:
                reply = await llm.generate_reply(
                    company=demo_company,
                    history=history,
                    user_message=user_q,
                    channel="telegram",
                    chat_id=chat_id,
                )
                storage.add_message("telegram", chat_id, "user", user_q)
                storage.add_message("telegram", chat_id, "assistant", reply.text)
                await callback.message.answer(
                    f"<i>Вы (как клиент):</i> {user_q}\n\n"
                    f"🤖 <b>Бот-ассистент:</b>\n{reply.text}",
                    parse_mode="HTML",
                    reply_markup=_test_scenarios_keyboard(),
                )
                # ВАУ-эффект при перехвате лида
                if reply.lead and not storage.has_lead("telegram", chat_id, reply.lead.phone):
                    storage.save_lead(reply.lead)
                    await _send_wow_effect(callback.message, reply.lead, demo_company)
            except Exception as exc:
                logger.exception("Demo LLM error: %s", exc)
                await callback.message.answer(
                    "Секунду, ИИ-мастер обрабатывает запрос. Попробуйте ещё раз.",
                    reply_markup=_test_scenarios_keyboard(),
                )
            return

        if action == "pricing":
            text = (
                "💰 <b>Тарифы платформы «Заявка 24/7»</b>\n\n"
                "• <b>Старт</b> — 12 900 ₽/мес → 1 бот, Авито + Telegram, до 500 сообщений\n"
                "• <b>Бизнес</b> — 24 900 ₽/мес → до 3 ботов, аналитика, приоритетная поддержка\n"
                "• <b>Агентство</b> — от 59 900 ₽/мес → неограниченно ботов, white-label, API\n\n"
                "🔥 <b>Окупаемость:</b> средний кузовной заказ — 15 000–50 000 ₽.\n"
                "Бот окупает месячную подписку уже с <b>1 закрытого клиента</b>.\n\n"
                "Каждую пятницу/субботу после 21:00 вы теряете 3–5 горячих клиентов, "
                "потому что мастера заняты или спят. Этот бот не спит никогда."
            )
            await callback.message.answer(text, parse_mode="HTML", reply_markup=_trial_cta_keyboard())
            return

        if action == "contact":
            text = (
                "📞 <b>Связаться с основателем</b>\n\n"
                "Денис, основатель платформы «Заявка 24/7»\n"
                "Telegram: @Zayavka24AI\n\n"
                "Настраиваем бота под ваш прайс за 10 минут.\n"
                "Первые 3 дня — бесплатно, без обязательств."
            )
            await callback.message.answer(text, parse_mode="HTML", reply_markup=_trial_cta_keyboard())
            return

        if action == "trial":
            _saas_wizard[chat_id] = {"step": 1}
            text = (
                "🚀 <b>Запуск тест-драйва на 3 дня</b>\n\n"
                "Настроим вашего персонального бота по прайсу за 10 минут.\n\n"
                "<b>Шаг 1 из 4.</b> Из какого вы города и как называется ваш автосервис или студия?\n\n"
                "<i>Пример: Москва, Детейлинг Плюс</i>"
            )
            await callback.message.answer(text, parse_mode="HTML")
            return


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

    # ---------------------------------------------------------------------------
    # ВАУ-эффект: показ карточки лида «глазами владельца сервиса»
    # ---------------------------------------------------------------------------

    async def _send_wow_effect(message: Message, lead: Any, company: Any) -> None:
        """Показывает владельцу, как выглядит входящий лид с другой стороны."""
        await asyncio.sleep(1.2)  # лёгкая пауза — эффект «входящего уведомления»
        car = getattr(lead, "car_model", "") or "не указано"
        svc = getattr(lead, "service_interest", "") or "не указано"
        price_hint = getattr(lead, "estimated_price", "") or "уточняется"
        phone = getattr(lead, "phone", "") or "+7 (XXX) XXX-XX-XX"
        wow_text = (
            "━━━━━━━━━━━━━━━━━━━━━━━━\n"
            "🔥 <b>ВОТ ТАК ЭТО РАБОТАЕТ В ВАШЕМ СЕРВИСЕ:</b>\n\n"
            "Только что потенциальный клиент написал в нерабочий час.\n"
            "ИИ за 15 секунд квалифицировал авто, назвал вилку цен по прайсу и перехватил номер.\n\n"
            "📋 <b>Новый лид — вот что мгновенно упало бы вашему мастеру в Telegram:</b>\n\n"
            f"🚗 <b>Автомобиль:</b> {car}\n"
            f"🔧 <b>Услуга:</b> {svc}\n"
            f"💵 <b>Ориентир по чеку:</b> {price_hint}\n"
            f"📱 <b>Телефон клиента:</b> <code>{phone}</code>\n"
            f"💬 <b>Чат Авито:</b> [Открыть диалог]\n\n"
            "<i>Пока конкуренты спят или моют машины, вы уже закрываете клиента в кассу!</i>\n"
            "━━━━━━━━━━━━━━━━━━━━━━━━\n\n"
            "Хотите такого же мастера под <b>ваш прайс-лист</b>?"
        )
        await message.answer(wow_text, parse_mode="HTML", reply_markup=_trial_cta_keyboard())

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

        # --- SaaS-онбординг: 4-шаговый мастер заявки на тест-драйв ---
        if chat_id in _saas_wizard:
            wizard = _saas_wizard[chat_id]
            step = wizard.get("step", 1)

            if step == 1:
                wizard["city_name"] = text
                wizard["step"] = 2
                await message.answer(
                    "✅ Отлично!\n\n"
                    "<b>Шаг 2 из 4.</b> Какая основная ниша вашего бизнеса?\n\n"
                    "1 — Кузовной ремонт и покраска\n"
                    "2 — Детейлинг и оклейка плёнкой\n"
                    "3 — Слесарный ремонт и ТО\n"
                    "4 — Другая (напишите)",
                    parse_mode="HTML",
                )
                return

            if step == 2:
                niche_map = {
                    "1": "Кузовной ремонт и покраска",
                    "2": "Детейлинг и оклейка",
                    "3": "Слесарный ремонт и ТО",
                }
                wizard["niche"] = niche_map.get(text.strip(), text)
                wizard["step"] = 3
                await message.answer(
                    "✅ Понял!\n\n"
                    "<b>Шаг 3 из 4.</b> Пришлите ссылку на ваше объявление на Авито\n"
                    "или прикрепите фото / PDF прайс-листа.\n\n"
                    "<i>Если прайса нет — просто напишите основные услуги и цены в этом сообщении.</i>",
                    parse_mode="HTML",
                )
                return

            if step == 3:
                wizard["price_hint"] = text
                wizard["step"] = 4
                await message.answer(
                    "✅ Прайс записал!\n\n"
                    "<b>Шаг 4 из 4.</b> На какой номер телефона подключить уведомления о новых лидах?\n\n"
                    "<i>Пример: +7 999 123-45-67</i>",
                    parse_mode="HTML",
                )
                return

            if step == 4:
                wizard["owner_phone"] = text
                _saas_wizard.pop(chat_id, None)

                # Формируем заявку и уведомляем Дениса
                user_label = "Неизвестный"
                if message.from_user:
                    uname = f"@{message.from_user.username}" if message.from_user.username else ""
                    user_label = f"{message.from_user.full_name} {uname}".strip()

                await notifier.notify_saas_trial_request(
                    user_label=user_label,
                    chat_id=chat_id,
                    city_name=wizard.get("city_name", "—"),
                    niche=wizard.get("niche", "—"),
                    price_hint=wizard.get("price_hint", "—"),
                    owner_phone=wizard.get("owner_phone", "—"),
                )

                await message.answer(
                    "🎉 <b>Заявка принята!</b>\n\n"
                    "Мы настроим и обучим вашего персонального бота по вашему прайсу.\n"
                    "Основатель сервиса свяжется с вами в течение 1–2 часов для запуска тест-драйва на <b>3 дня бесплатно</b>.\n\n"
                    "Пока ждёте — можете протестировать, как ИИ общается с клиентами прямо сейчас 👇",
                    parse_mode="HTML",
                    reply_markup=_test_scenarios_keyboard(),
                )
                return

        slug = storage.get_session_company("telegram", chat_id)

        # Если пользователь в demo-режиме и пишет текст (не кнопку) — переключаем на demo_detailing
        if slug == _PLATFORM_DEMO_SLUG or not slug:
            demo_company = resolve_company("demo_detailing") or resolve_company(None)
            if demo_company:
                slug = demo_company.slug
                storage.upsert_session("telegram", chat_id, slug)
                storage.resume_session("telegram", chat_id)

        company = resolve_company(slug)
        if not company:
            await message.answer(
                "👋 Введите /start для начала.",
                reply_markup=_platform_menu_keyboard(),
            )
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
            # ВАУ-эффект: если пользователь пришёл с лендинга (demo-режим), показываем карточку
            orig_slug = storage.get_session_company("telegram", chat_id)
            if orig_slug in (_PLATFORM_DEMO_SLUG, "demo_detailing"):
                await _send_wow_effect(message, reply.lead, company)

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
