import asyncio
import logging
from datetime import datetime, timezone
from typing import Any, Dict, Optional

logger = logging.getLogger(__name__)


class AlertManager:
    """Менеджер алертов в Telegram с защитой от флуда (cooldown по типу события)."""

    def __init__(
        self,
        bot_manager: Any = None,
        default_chat_id: Optional[int] = None,
        enabled: bool = True,
        cooldown_minutes: int = 30,
    ) -> None:
        self.bot_manager = bot_manager
        self.default_chat_id = default_chat_id
        self.enabled = enabled
        self.cooldown_minutes = cooldown_minutes
        self._last_sent: Dict[str, datetime] = {}
        self._lock = asyncio.Lock()

    def is_cooling_down(self, event_type: str) -> bool:
        last = self._last_sent.get(event_type)
        if not last:
            return False
        now = datetime.now(timezone.utc)
        elapsed_sec = (now - last).total_seconds()
        return elapsed_sec < (self.cooldown_minutes * 60)

    async def send_alert(self, event_type: str, text: str, force: bool = False) -> bool:
        """Отправляет алерт владельцу платформы, если не активен cooldown."""
        if not self.enabled:
            logger.info("Alerts disabled, skipping alert [%s]: %s", event_type, text)
            return False

        async with self._lock:
            if not force and self.is_cooling_down(event_type):
                logger.warning("Алерт [%s] подавлен (cooldown %d мин): %s", event_type, self.cooldown_minutes, text)
                return False

            bot = self.bot_manager.primary_bot() if self.bot_manager else None
            chat_id = self.default_chat_id

            now_str = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
            formatted = (
                f"🚨 *[BotAvitoTelegram Alert]*\n"
                f"*Тип:* `{event_type}`\n"
                f"*Время:* `{now_str}`\n\n"
                f"{text}"
            )

            if not bot or not chat_id:
                logger.warning("Невозможно отправить алерт в Telegram (нет primary_bot или default_chat_id): [%s] %s", event_type, text)
                self._last_sent[event_type] = datetime.now(timezone.utc)
                return False

            try:
                await bot.send_message(chat_id=chat_id, text=formatted, parse_mode="Markdown")
                self._last_sent[event_type] = datetime.now(timezone.utc)
                logger.info("Алерт [%s] успешно отправлен в chat_id=%s", event_type, chat_id)
                return True
            except Exception as exc:
                logger.error("Ошибка при отправке алерта [%s] в Telegram: %s", event_type, exc)
                return False
