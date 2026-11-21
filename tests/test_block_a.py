import asyncio
import sqlite3
import zipfile
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest
from starlette.testclient import TestClient

from app.alerting import AlertManager
from app.config import Settings
from app.maintenance import BackupScheduler, compute_deep_health
from app.models import LeadInfo
from app.server import create_app
from app.storage import Storage
from scripts.backup import create_backup


def test_backup_creates_readable_sqlite(tmp_path: Path):
    db_file = tmp_path / "test.db"
    storage = Storage(db_file)
    storage.save_lead(
        LeadInfo(
            channel="telegram",
            chat_id="12345",
            company_slug="demo_detailing",
            phone="+79991112233",
            client_name="Алексей",
            car_model="BMW X5",
        )
    )

    companies_dir = tmp_path / "companies"
    companies_dir.mkdir(parents=True)
    (companies_dir / "demo.yaml").write_text("slug: demo_detailing", encoding="utf-8")

    backups_dir = tmp_path / "backups"
    zip_path = create_backup(
        db_path=db_file,
        companies_dir=companies_dir,
        backups_dir=backups_dir,
        keep_count=5,
    )

    assert zip_path.exists()
    assert zip_path.suffix == ".zip"

    # Распаковываем и проверяем целостность данных в отдельной папке
    extract_dir = tmp_path / "extracted"
    with zipfile.ZipFile(zip_path, "r") as zf:
        zf.extractall(extract_dir)

    extracted_db = extract_dir / "bot.db"
    assert extracted_db.exists()

    conn = sqlite3.connect(extracted_db)
    conn.row_factory = sqlite3.Row
    row = conn.execute("SELECT * FROM leads WHERE chat_id = '12345'").fetchone()
    assert row is not None
    assert row["client_name"] == "Алексей"
    assert row["phone"] == "+79991112233"
    conn.close()


def test_backup_rotation_keeps_n(tmp_path: Path):
    db_file = tmp_path / "test.db"
    storage = Storage(db_file)
    companies_dir = tmp_path / "companies"
    companies_dir.mkdir(parents=True)
    backups_dir = tmp_path / "backups"

    # Создаём 4 бэкапа
    for i in range(4):
        create_backup(
            db_path=db_file,
            companies_dir=companies_dir,
            backups_dir=backups_dir,
            keep_count=2,
        )

    all_zips = list(backups_dir.glob("backup_*.zip"))
    assert len(all_zips) == 2


@pytest.mark.asyncio
async def test_alert_cooldown():
    fake_bot = MagicMock()
    fake_bot.send_message = AsyncMock(return_value=True)

    fake_bm = MagicMock()
    fake_bm.primary_bot.return_value = fake_bot

    alert_mgr = AlertManager(
        bot_manager=fake_bm,
        default_chat_id=123456,
        enabled=True,
        cooldown_minutes=30,
    )

    # 1. Первый алерт должен уйти
    sent1 = await alert_mgr.send_alert("bot_crash", "Бот клиента упал")
    assert sent1 is True
    assert fake_bot.send_message.call_count == 1

    # 2. Второй алерт с тем же типом события должен быть подавлен cooldown
    sent2 = await alert_mgr.send_alert("bot_crash", "Бот клиента упал снова")
    assert sent2 is False
    assert fake_bot.send_message.call_count == 1

    # 3. Алерт с другим типом события должен пройти
    sent3 = await alert_mgr.send_alert("backup_failed", "Бэкап упал")
    assert sent3 is True
    assert fake_bot.send_message.call_count == 2

    # 4. При force=True должен пройти даже во время cooldown
    sent4 = await alert_mgr.send_alert("bot_crash", "Срочный алерт", force=True)
    assert sent4 is True
    assert fake_bot.send_message.call_count == 3


def test_health_deep_reports_bots(tmp_path: Path):
    db_file = tmp_path / "test.db"
    storage = Storage(db_file)
    settings = Settings(db_path=db_file, backup_dir=tmp_path / "backups")

    # 1. Сценарий: 2 живых бота -> статус ok
    fake_bm_healthy = MagicMock()
    fake_bm_healthy.status.return_value = [
        {"tenant_slug": "default", "alive": True},
        {"tenant_slug": "client1", "alive": True},
    ]

    health_ok = compute_deep_health(settings, storage, bot_manager=fake_bm_healthy, include_disk=False)
    assert health_ok["status"] == "ok"
    assert health_ok["bots"]["total"] == 2
    assert health_ok["bots"]["alive"] == 2
    assert health_ok["bots"]["dead"] == []
    assert health_ok["db"]["ok"] is True

    # 2. Сценарий: 1 мёртвый бот -> статус degraded
    fake_bm_degraded = MagicMock()
    fake_bm_degraded.status.return_value = [
        {"tenant_slug": "default", "alive": True},
        {"tenant_slug": "client1", "alive": False},
    ]

    health_deg = compute_deep_health(settings, storage, bot_manager=fake_bm_degraded, include_disk=False)
    assert health_deg["status"] == "degraded"
    assert health_deg["bots"]["total"] == 2
    assert health_deg["bots"]["alive"] == 1
    assert "client1" in health_deg["bots"]["dead"]


@pytest.mark.asyncio
async def test_backup_scheduler_run_once(tmp_path: Path):
    db_file = tmp_path / "test.db"
    storage = Storage(db_file)
    backups_dir = tmp_path / "backups"
    companies_dir = tmp_path / "companies"
    companies_dir.mkdir(parents=True)

    settings = Settings(
        db_path=db_file,
        companies_dir=companies_dir,
        backup_dir=backups_dir,
        backup_remote="local",
    )

    scheduler = BackupScheduler(storage=storage, settings=settings)
    res = await scheduler.run_once()

    assert res["status"] == "ok"
    assert Path(res["path"]).exists()

    last_run = storage.get_last_backup_run()
    assert last_run is not None
    assert last_run["status"] == "ok"
    assert last_run["size_bytes"] > 0


def test_health_deep_endpoint(tmp_path: Path):
    db_file = tmp_path / "test.db"
    storage = Storage(db_file)
    companies_dir = tmp_path / "companies"
    companies_dir.mkdir(parents=True)
    from app.company_store import CompanyStore

    company_store = CompanyStore(companies_dir)
    settings = Settings(db_path=db_file, companies_dir=companies_dir)

    app = create_app(
        settings=settings,
        company_store=company_store,
        storage=storage,
    )

    client = TestClient(app)
    resp = client.get("/health/deep")
    assert resp.status_code == 200
    data = resp.json()
    assert "status" in data
    assert "db" in data
    assert "bots" in data
    assert "disk" in data
    assert "queue" in data
    assert "last_backup" in data
    assert data["db"]["ok"] is True
