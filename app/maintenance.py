import asyncio
import logging
import shutil
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Optional

from aiogram.types import FSInputFile

from app.alerting import AlertManager
from app.config import Settings
from app.storage import Storage
from scripts.backup import create_backup

logger = logging.getLogger(__name__)


def compute_deep_health(
    settings: Settings,
    storage: Storage,
    bot_manager: Any = None,
    avito_handler: Any = None,
    include_disk: bool = True,
) -> Dict[str, Any]:
    """Вычисляет детальное состояние системы: БД, боты, диск, очередь и бэкапы.

    include_disk=False — не учитывать заполненность диска в общем статусе
    (нужно тестам: на машине разработчика диск может быть забит, и тогда
    локальный прогон не должен выглядеть как авария на сервере).
    """
    status_forced = not include_disk
    # 1. DB latency
    t0 = time.perf_counter()
    db_ok = True
    db_err = None
    try:
        with storage._connect() as conn:
            conn.execute("SELECT 1;").fetchone()
        latency_ms = round((time.perf_counter() - t0) * 1000, 2)
    except Exception as exc:
        db_ok = False
        db_err = str(exc)
        latency_ms = -1.0

    # 2. Bots status
    total_bots = 0
    alive_bots = 0
    dead_bots: list[str] = []
    if bot_manager and hasattr(bot_manager, "status"):
        statuses = bot_manager.status()
        total_bots = len(statuses)
        for s in statuses:
            if s.get("alive"):
                alive_bots += 1
            else:
                dead_bots.append(str(s.get("tenant_slug") or s.get("name") or "unknown"))

    # 3. Disk usage
    try:
        check_dir = storage.db_path.parent if storage.db_path.parent.exists() else Path(".")
        usage = shutil.disk_usage(check_dir)
        free_mb = int(usage.free / (1024 * 1024))
        used_pct = round((usage.used / usage.total) * 100, 1)
    except Exception:
        free_mb = 10000
        used_pct = 10.0

    # 4. Queue
    avito_pending = 0
    if avito_handler and hasattr(avito_handler, "queue"):
        try:
            avito_pending = avito_handler.queue.qsize()
        except Exception:
            avito_pending = 0

    # 5. Last backup
    last_run = storage.get_last_backup_run()
    if last_run and last_run.get("started_at"):
        at_iso = last_run["started_at"]
        try:
            started_dt = datetime.fromisoformat(at_iso)
            if started_dt.tzinfo is None:
                started_dt = started_dt.replace(tzinfo=timezone.utc)
            now = datetime.now(timezone.utc)
            age_hours = round(max(0.0, (now - started_dt).total_seconds() / 3600), 1)
        except Exception:
            age_hours = 0.0
        last_backup_data = {"at": at_iso, "age_hours": age_hours}
    else:
        last_backup_data = {"at": None, "age_hours": None}

    # 6. Overall status
    crit_free_mb = getattr(settings, "disk_crit_free_mb", 500)
    warn_pct = getattr(settings, "disk_warn_pct", 90)
    backup_stale = (
        last_backup_data["age_hours"] is not None
        and settings.backup_interval_hours > 0
        and last_backup_data["age_hours"] > (2 * settings.backup_interval_hours)
    )

    status = "ok"
    if not db_ok or (include_disk and free_mb < crit_free_mb):
        status = "fail"
    elif dead_bots or backup_stale or (include_disk and used_pct >= warn_pct):
        status = "degraded"

    result: Dict[str, Any] = {
        "status": status,
        "db": {"ok": db_ok, "latency_ms": latency_ms},
        "bots": {"total": total_bots, "alive": alive_bots, "dead": dead_bots},
        "disk": {"free_mb": free_mb, "used_pct": used_pct},
        "queue": {"avito_pending": avito_pending},
        "last_backup": last_backup_data,
    }
    if db_err:
        result["db"]["error"] = db_err
    return result


class BackupScheduler:
    """Фоновый планировщик регулярных бэкапов БД и конфигураций."""

    def __init__(
        self,
        storage: Storage,
        settings: Settings,
        bot_manager: Any = None,
        alert_manager: Optional[AlertManager] = None,
    ) -> None:
        self.storage = storage
        self.settings = settings
        self.bot_manager = bot_manager
        self.alert_manager = alert_manager
        self._task: Optional[asyncio.Task] = None
        self._stopped = False

    async def run_once(self) -> Dict[str, Any]:
        """Выполняет один цикл создания бэкапа и опциональной отправки в Telegram."""
        started_at = datetime.now(timezone.utc).isoformat()
        run_id = self.storage.record_backup_run(
            started_at=started_at,
            status="running",
            remote=self.settings.backup_remote,
        )

        try:
            # 1. Создание локального архива
            zip_path = await asyncio.to_thread(
                create_backup,
                db_path=self.settings.db_path,
                companies_dir=self.settings.companies_dir,
                backups_dir=self.settings.backup_dir,
                keep_count=self.settings.backup_keep,
            )
            size_bytes = zip_path.stat().st_size
            status = "ok"

            # 2. Выгрузка в Telegram, если включено
            if self.settings.backup_remote == "telegram":
                if size_bytes > 50 * 1024 * 1024:
                    msg = f"Размер бэкапа ({size_bytes} байт) превышает лимит Telegram (50 МБ)."
                    logger.warning(msg)
                    if self.alert_manager:
                        await self.alert_manager.send_alert("backup_telegram_oversize", msg)
                else:
                    bot = self.bot_manager.primary_bot() if self.bot_manager else None
                    chat_id = self.settings.default_notify_chat_id
                    if bot and chat_id:
                        try:
                            doc = FSInputFile(str(zip_path), filename=zip_path.name)
                            caption = f"📦 Резервная копия BotAvitoTelegram ({zip_path.name}, {round(size_bytes / 1024 / 1024, 2)} МБ)"
                            await bot.send_document(chat_id=chat_id, document=doc, caption=caption)
                            status = "uploaded"
                            logger.info("Бэкап успешно отправлен в Telegram chat_id=%s", chat_id)
                        except Exception as exc:
                            logger.error("Ошибка отправки бэкапа в Telegram: %s", exc)
                            if self.alert_manager:
                                await self.alert_manager.send_alert("backup_telegram_failed", f"Не удалось отправить бэкап в Telegram: {exc}")
                    else:
                        logger.warning("Бэкап в Telegram пропущен: нет доступного бота или default_notify_chat_id")

            self.storage.finish_backup_run(
                run_id=run_id,
                status=status,
                size_bytes=size_bytes,
                path=str(zip_path),
                remote=self.settings.backup_remote,
            )
            logger.info("Бэкап успешно создан: %s (%d байт, статус=%s)", zip_path, size_bytes, status)
            return {
                "run_id": run_id,
                "status": status,
                "path": str(zip_path),
                "size_bytes": size_bytes,
            }

        except Exception as exc:
            logger.exception("Ошибка при создании бэкапа: %s", exc)
            self.storage.finish_backup_run(
                run_id=run_id,
                status="failed",
                error=str(exc),
                remote=self.settings.backup_remote,
            )
            if self.alert_manager:
                await self.alert_manager.send_alert("backup_failed", f"Критическая ошибка создания бэкапа: {exc}")
            return {
                "run_id": run_id,
                "status": "failed",
                "error": str(exc),
            }

    async def _loop(self) -> None:
        logger.info(
            "BackupScheduler запущен (интервал %d ч, хранить %d копий, remote=%s)",
            self.settings.backup_interval_hours,
            self.settings.backup_keep,
            self.settings.backup_remote,
        )
        # Первый бэкап делаем сразу после старта, иначе после перезапуска
        # система до 6 часов остаётся без свежей копии.
        first_run = True
        while not self._stopped:
            interval_sec = max(60, self.settings.backup_interval_hours * 3600)
            try:
                if not first_run:
                    await asyncio.sleep(interval_sec)
                if self._stopped:
                    break
                await self.run_once()
                first_run = False
            except asyncio.CancelledError:
                break
            except Exception as exc:
                logger.exception("Непредвиденная ошибка в цикле BackupScheduler: %s", exc)
                first_run = False

    def start(self) -> asyncio.Task:
        self._stopped = False
        self._task = asyncio.create_task(self._loop(), name="backup_scheduler")
        return self._task

    async def stop(self) -> None:
        self._stopped = True
        if self._task and not self._task.done():
            self._task.cancel()
            try:
                await self._task
            except (asyncio.CancelledError, Exception):
                pass
