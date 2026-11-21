import html
import logging
from typing import Optional

from aiogram import Bot

from app.models import CompanyProfile, KnowledgeGap, LeadInfo

logger = logging.getLogger(__name__)


def _esc(value: str) -> str:
    return html.escape(value, quote=True)


def format_lead_message(lead: LeadInfo, company: Optional[CompanyProfile] = None) -> str:
    source_label = "Авито" if lead.channel == "avito" else "Telegram (Демо)"
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
    """Sends captured leads and demo-visit alerts to the owner's Telegram chat or group."""

    def __init__(self, bot: Optional[Bot], default_chat_id: Optional[int]) -> None:
        self.bot = bot
        self.default_chat_id = default_chat_id

    async def notify(self, lead: LeadInfo, company: Optional[CompanyProfile] = None) -> bool:
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
        src_label = "Авито" if gap.channel == "avito" else "Telegram"
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
