import asyncio
import html
import logging
from typing import Optional

from aiogram import Bot

from app.crm import CRMDispatcher
from app.models import CompanyProfile, KnowledgeGap, LeadInfo

logger = logging.getLogger(__name__)


def _esc(value: str) -> str:
    return html.escape(value, quote=True)


def format_lead_message(lead: LeadInfo, company: Optional[CompanyProfile] = None) -> str:
    if lead.channel == "avito":
        source_label = "Авито"
    elif lead.channel == "whatsapp":
        source_label = "WhatsApp"
    else:
        source_label = "Telegram"
    company_title = _esc(company.company_name if company else lead.company_slug)

    lines = [
        f"🔥 <b>Новая заявка ({source_label})!</b>",
        f"<b>Компания:</b> {company_title}",
    ]
    if lead.client_name:
        lines.append(f"<b>Клиент:</b> {_esc(lead.client_name)}")
    lines.append(f"<b>Телефон:</b> <code>{_esc(lead.phone)}</code>")
    if lead.car_model:
        lines.append(f"<b>Автомобиль:</b> {_esc(lead.car_model)}")
    if lead.service_interest:
        lines.append(f"<b>Услуга:</b> {_esc(lead.service_interest)}")
    if lead.estimated_price:
        lines.append(f"<b>Ориентир по цене:</b> {_esc(lead.estimated_price)}")
    if lead.preferred_time:
        lines.append(f"<b>Удобное время:</b> {_esc(lead.preferred_time)}")
    if lead.summary:
        lines.append(f"<b>Детали:</b> {_esc(lead.summary)}")
    if lead.avito_chat_url:
        lines.append(f'<b>Чат Авито:</b> <a href="{_esc(lead.avito_chat_url)}">Открыть диалог</a>')
    return "\n".join(lines)


class LeadNotifier:
    """Sends captured leads and demo-visit alerts to the owner's Telegram chat or group, and syncs to CRM."""

    def __init__(
        self,
        bot: Optional[Bot],
        default_chat_id: Optional[int],
        crm_dispatcher: Optional[CRMDispatcher] = None,
    ) -> None:
        self.bot = bot
        self.default_chat_id = default_chat_id
        self.crm_dispatcher = crm_dispatcher or CRMDispatcher()
        self._background_tasks: set[asyncio.Task] = set()

    async def notify(self, lead: LeadInfo, company: Optional[CompanyProfile] = None) -> bool:
        # 1. Asynchronously dispatch lead to external CRM if configured (amoCRM, Bitrix24, Webhook)
        if self.crm_dispatcher and company and company.crm_type and company.crm_type != "none":
            try:
                task = asyncio.create_task(self.crm_dispatcher.dispatch(lead, company))
                self._background_tasks.add(task)
                task.add_done_callback(self._background_tasks.discard)
            except Exception as crm_err:
                logger.warning("Failed to schedule CRM dispatch: %s", crm_err)

        target_chat_id = (
            company.notify_chat_id
            if company and company.notify_chat_id is not None
            else self.default_chat_id
        )
        if not self.bot or target_chat_id is None:
            logger.info("Lead captured (no Telegram notify target configured): %s", lead.phone)
            return False

        text = format_lead_message(lead, company)
        try:
            await self.bot.send_message(
                chat_id=target_chat_id,
                text=text,
                parse_mode="HTML",
                disable_web_page_preview=True,
            )
            return True
        except Exception as exc:
            logger.error("Failed to send lead notification to %s: %s", target_chat_id, exc)
            return False

    async def notify_demo_started(
        self,
        company: CompanyProfile,
        user_label: str,
        chat_id: str,
    ) -> bool:
        if not self.bot or self.default_chat_id is None:
            return False
        text = (
            f"👀 <b>Кто-то открыл демо-бота!</b>\n"
            f"<b>Профиль:</b> {_esc(company.company_name)} (<code>{_esc(company.slug)}</code>)\n"
            f"<b>Пользователь:</b> {_esc(user_label)} (id: <code>{_esc(chat_id)}</code>)"
        )
        try:
            await self.bot.send_message(
                chat_id=self.default_chat_id,
                text=text,
                parse_mode="HTML",
                disable_web_page_preview=True,
            )
            return True
        except Exception as exc:
            logger.error("Failed to send demo-start notification: %s", exc)
            return False

    async def notify_knowledge_gap(
        self,
        gap: KnowledgeGap,
        company: Optional[CompanyProfile] = None,
    ) -> bool:
        if not self.bot or self.default_chat_id is None:
            return False
        comp_name = company.company_name if company else gap.company_slug
        if gap.channel == "avito":
            src_label = "Авито"
        elif gap.channel == "whatsapp":
            src_label = "WhatsApp"
        else:
            src_label = "Telegram"
        text = (
            f"🧠 <b>Новый вопрос от клиента (нет в базе знаний)!</b>\n"
            f"<b>Компания:</b> {_esc(comp_name)}\n"
            f"<b>Канал:</b> {src_label}\n"
            f"<b>Вопрос клиента:</b> <i>«{_esc(gap.question)}»</i>\n\n"
            f"💡 <i>Откройте дашборд во вкладке «Самообучение», чтобы дать ответ и обучить бота в 1 клик!</i>"
        )
        try:
            await self.bot.send_message(
                chat_id=self.default_chat_id,
                text=text,
                parse_mode="HTML",
                disable_web_page_preview=True,
            )
            return True
        except Exception as exc:
            logger.error("Failed to send knowledge gap alert: %s", exc)
            return False

    async def notify_demo_started_platform(
        self,
        user_label: str,
        chat_id: str,
    ) -> bool:
        """Уведомление о переходе владельца с лендинга на демо-стенд платформы."""
        if not self.bot or self.default_chat_id is None:
            return False
        text = (
            f"🚀 <b>Новый потенциальный клиент на демо-стенде платформы!</b>\n"
            f"<b>Пользователь:</b> {_esc(user_label)} (id: <code>{_esc(chat_id)}</code>)\n"
            f"<i>Перешёл с лендинга по кнопке «Запустить демо в Telegram»</i>"
        )
        try:
            await self.bot.send_message(
                chat_id=self.default_chat_id,
                text=text,
                parse_mode="HTML",
                disable_web_page_preview=True,
            )
            return True
        except Exception as exc:
            logger.error("Failed to send platform demo notification: %s", exc)
            return False

    async def notify_saas_trial_request(
        self,
        user_label: str,
        chat_id: str,
        city_name: str,
        niche: str,
        price_hint: str,
        owner_phone: str,
    ) -> bool:
        """Уведомление о новой заявке на 3-дневный тест-драйв SaaS-платформы."""
        if not self.bot or self.default_chat_id is None:
            logger.info("SaaS trial request (no notify target): %s %s %s", user_label, city_name, owner_phone)
            return False
        text = (
            f"🎯 <b>НОВАЯ ЗАЯВКА НА ТЕСТ-ДРАЙВ!</b>\n\n"
            f"<b>Кто:</b> {_esc(user_label)} (TG id: <code>{_esc(chat_id)}</code>)\n"
            f"<b>Город / Сервис:</b> {_esc(city_name)}\n"
            f"<b>Ниша:</b> {_esc(niche)}\n"
            f"<b>Прайс / Авито:</b> {_esc(price_hint[:300])}\n"
            f"<b>Телефон владельца:</b> <code>{_esc(owner_phone)}</code>\n\n"
            f"<i>Настрой бота под их прайс и пришли ссылку для тест-драйва!</i>"
        )
        try:
            await self.bot.send_message(
                chat_id=self.default_chat_id,
                text=text,
                parse_mode="HTML",
                disable_web_page_preview=True,
            )
            return True
        except Exception as exc:
            logger.error("Failed to send SaaS trial notification: %s", exc)
            return False
