import asyncio
import logging
from typing import Any, Awaitable, Callable, Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)


class MessageDebouncer:
    """Buffers consecutive messages from the same chat within a short window (e.g. 2.0s)
    and executes a single callback with combined text to prevent multiple concurrent LLM calls.
    """

    def __init__(self, delay_seconds: float = 2.0) -> None:
        self.delay_seconds = delay_seconds
        self._buffers: Dict[Tuple[str, str], List[str]] = {}
        self._timers: Dict[Tuple[str, str], asyncio.TimerHandle] = {}
        self._locks: Dict[Tuple[str, str], asyncio.Lock] = {}
        self._global_lock = asyncio.Lock()

    async def get_chat_lock(self, channel: str, chat_id: str) -> asyncio.Lock:
        key = (channel, str(chat_id))
        async with self._global_lock:
            if key not in self._locks:
                self._locks[key] = asyncio.Lock()
            return self._locks[key]

    async def buffer_message(
        self,
        channel: str,
        chat_id: str,
        text: str,
        on_flush: Callable[[str], Awaitable[Any]],
    ) -> None:
        """Adds text to the chat buffer and resets the debounce timer."""
        key = (channel, str(chat_id))
        lock = await self.get_chat_lock(channel, chat_id)

        async with lock:
            if key not in self._buffers:
                self._buffers[key] = []
            if text:
                self._buffers[key].append(text)

            # Cancel existing timer if still pending
            if key in self._timers:
                self._timers[key].cancel()

            loop = asyncio.get_running_loop()

            def _trigger() -> None:
                asyncio.create_task(self._flush(channel, chat_id, on_flush))

            self._timers[key] = loop.call_later(self.delay_seconds, _trigger)

    async def _flush(
        self,
        channel: str,
        chat_id: str,
        on_flush: Callable[[str], Awaitable[Any]],
    ) -> None:
        key = (channel, str(chat_id))
        lock = await self.get_chat_lock(channel, chat_id)

        combined_text = ""
        async with lock:
            self._timers.pop(key, None)
            msgs = self._buffers.pop(key, [])
            combined_text = "\n".join(msgs).strip()

        if combined_text:
            try:
                await on_flush(combined_text)
            except Exception as exc:
                logger.exception("Error executing debounced flush for %s:%s - %s", channel, chat_id, exc)

        # Cleanup lock if idle
        async with self._global_lock:
            if key not in self._buffers and key not in self._timers:
                self._locks.pop(key, None)
