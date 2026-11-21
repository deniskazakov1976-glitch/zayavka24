import asyncio
import json
import sqlite3
import zipfile
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
from starlette.testclient import TestClient

from app.avito import AvitoClient, AvitoWebhookHandler
from app.company_store import CompanyStore
from app.config import Settings
from app.guardrails import validate_reply
from app.llm import LLMService
from app.models import CompanyProfile, LeadInfo
from app.notifier import LeadNotifier
from app.server import create_app, create_webhook_app
from app.storage import Storage
from scripts.backup import create_backup


def test_guardrails_blocks_hallucinated_prices():
    company = CompanyProfile(
        slug="test_guard",
        company_name="Детейлинг Эксперт",
        city="Москва",
        services=[
            "Оклейка зон риска: от 45 000 до 75 000 ₽",
            "Полировка кузова: от 15 000 ₽",
        ],
        discount_offer="скидку 10% на первый визит",
    )

    # 1. Allowed price from services
    res_ok = validate_reply("Стоимость оклейки составит 45 000 ₽. Записать вас?", company, mode="enforce")
    assert res_ok.is_safe is True
    assert "45 000 ₽" in res_ok.text

    # 2. Hallucinated price not in company database
    res_bad = validate_reply("Полировка фар будет стоить 99 000 ₽.", company, mode="enforce")
    assert res_bad.is_safe is False
    assert len(res_bad.violations) > 0
    assert "99 000" not in res_bad.text
    assert "оставьте, пожалуйста, ваш номер телефона" in res_bad.text


def test_storage_event_deduplication(tmp_path: Path):
    storage = Storage(tmp_path / "dedup.db")
    assert storage.is_event_processed("avito", "msg_1") is False

    # First marking
    inserted = storage.mark_event_processed("avito", "msg_1", status="done")
    assert inserted is True
    assert storage.is_event_processed("avito", "msg_1") is True

    # Duplicate marking
    inserted_again = storage.mark_event_processed("avito", "msg_1", status="done")
    assert inserted_again is False


def test_storage_persistent_bot_echo_tracking(tmp_path: Path):
    storage = Storage(tmp_path / "echo.db")

    # Record bot sent message
    rec_id = storage.record_bot_sent_message(
        channel="avito",
        chat_id="chat_123",
        text="Здравствуйте! Какая марка автомобиля?",
        status="pending",
    )
    assert rec_id > 0

    # Test text hash matching before message_id is known (handles race condition)
    assert storage.is_bot_echo(
        channel="avito",
        chat_id="chat_123",
        message_id="sent_id_99",
        text="Здравствуйте! Какая марка автомобиля?",
        window_seconds=60,
    ) is True

    # After update with message_id
    storage.update_bot_sent_message_id(rec_id, "sent_id_99")
    assert storage.is_bot_echo(
        channel="avito",
        chat_id="chat_123",
        message_id="sent_id_99",
    ) is True

    # Different text not matching bot output
    assert storage.is_bot_echo(
        channel="avito",
        chat_id="chat_123",
        message_id="manual_msg_1",
        text="Я сам отвечу этому клиенту через час.",
    ) is False


def test_multi_tenant_avito_accounts_storage(tmp_path: Path):
    storage = Storage(tmp_path / "multi_avito.db")
    acc = storage.save_avito_account(
        company_slug="demo_detailing",
        user_id=111222,
        client_id="cid_1",
        client_secret="csec_1",
        webhook_secret="sec_abc123",
        enabled=True,
    )
    assert acc["user_id"] == 111222
    assert acc["webhook_secret"] == "sec_abc123"

    accounts = storage.list_avito_accounts()
    assert len(accounts) == 1
    assert accounts[0]["client_id"] == "cid_1"

    found = storage.get_avito_account_by_secret("sec_abc123")
    assert found is not None
    assert found["user_id"] == 111222

    assert storage.get_avito_account_by_secret("invalid_secret") is None


def test_webhook_secret_routing_and_rejection():
    avito_handler = AsyncMock()
    avito_handler.handle_event = AsyncMock(return_value={"status": "replied"})

    app = create_webhook_app(avito_handler=avito_handler, webhook_secret="valid_token_123")
    client = TestClient(app)

    # 1. Valid secret -> 200 OK
    resp_ok = client.post("/webhook/avito/valid_token_123", json={"payload": {"type": "test"}})
    assert resp_ok.status_code == 200

    # 2. Invalid secret -> 404 Not Found
    resp_bad = client.post("/webhook/avito/wrong_token", json={"payload": {"type": "test"}})
    assert resp_bad.status_code == 404

    # 3. Unauthenticated legacy path when secret is configured -> 404
    resp_legacy = client.post("/webhook/avito", json={"payload": {"type": "test"}})
    assert resp_legacy.status_code == 404


@pytest.mark.asyncio
async def test_llm_failure_emergency_lead_capture(tmp_path: Path):
    storage = Storage(tmp_path / "llm_fail.db")
    company = CompanyProfile(slug="demo_detailing", company_name="БлескАвто", city="Москва")

    # Mock client that raises connection failure
    mock_client = AsyncMock()
    mock_client.chat.completions.create.side_effect = ConnectionError("Polza.ai gateway timeout")

    llm = LLMService(
        api_key="mock",
        base_url="http://mock",
        client=mock_client,
        storage=storage,
        timeout=1.0,
    )

    # User message contains a phone number
    reply = await llm.generate_reply(
        company=company,
        history=[],
        user_message="Хочу записаться на завтра, мой телефон 8 (916) 777-88-99",
        channel="telegram",
        chat_id="chat_fail_1",
    )

    # The lead MUST be captured even when LLM completely crashed!
    assert reply.lead is not None
    assert reply.lead.phone == "+7 (916) 777-88-99"
    assert "Записал ваш номер" in reply.text


def test_hot_sqlite_backup(tmp_path: Path):
    db_file = tmp_path / "test_bot.db"
    storage = Storage(db_file)
    storage.upsert_session("telegram", "chat_bk", "demo_detailing")

    companies_dir = tmp_path / "companies"
    companies_dir.mkdir()
    (companies_dir / "test_comp.yaml").write_text("slug: test_comp\ncompany_name: Тест\n", encoding="utf-8")

    backups_dir = tmp_path / "backups"
    zip_path = create_backup(
        db_path=db_file,
        companies_dir=companies_dir,
        backups_dir=backups_dir,
        keep_count=2,
    )

    assert zip_path.exists()
    assert zip_path.suffix == ".zip"

    with zipfile.ZipFile(zip_path, "r") as z:
        names = z.namelist()
        assert "bot.db" in names
        assert "companies/test_comp.yaml" in names


def test_ai_disclosure_and_honest_bot_rules():
    from app.prompt_builder import build_system_prompt

    comp = CompanyProfile(
        slug="auto_honesty",
        company_name="Честный Сервис",
        city="Екатеринбург",
        manager_name="Сергей",
        manager_role="старший мастер",
        disclose_ai=True,
        assistant_label="виртуальный ассистент",
    )
    prompt = build_system_prompt(comp)
    assert "виртуальный ассистент" in prompt
    assert "Честный Сервис" in prompt
    assert "Сергей" in prompt
    assert "Если клиент прямо спрашивает, робот ли ты или человек" in prompt

    # With disclose_ai=False
    comp_secret = comp.model_copy(update={"disclose_ai": False})
    prompt_secret = build_system_prompt(comp_secret)
    assert "Ты — Сергей, опытный и вежливый старший мастер" in prompt_secret


def test_schedule_advanced_work_status():
    from datetime import datetime, timezone, timedelta
    from app.media_reply import get_work_status

    comp = CompanyProfile(
        slug="auto_sched",
        company_name="График Сервис",
        city="Москва",
        work_hours="Пн–Сб с 10:00 до 20:00",
        lunch_break="14:00-15:00",
        holidays=["2026-01-01"],
        timezone="Europe/Moscow",
    )

    tz = timezone(timedelta(hours=3))

    # 1. Воскресенье (2026-11-22) -> выходной
    sunday_dt = datetime(2026, 11, 22, 12, 0, tzinfo=tz)
    st_sun = get_work_status(comp, now=sunday_dt)
    assert not st_sun.is_open
    assert st_sun.reason == "day_off"
    assert "понедельник" in st_sun.next_open_str

    # 2. Обед во вторник (2026-11-24, 14:30)
    lunch_dt = datetime(2026, 11, 24, 14, 30, tzinfo=tz)
    st_lunch = get_work_status(comp, now=lunch_dt)
    assert not st_lunch.is_open
    assert st_lunch.reason == "lunch"
    assert "15:00" in st_lunch.next_open_str

    # 3. Праздник (2026-01-01, четверг)
    hol_dt = datetime(2026, 1, 1, 12, 0, tzinfo=tz)
    st_hol = get_work_status(comp, now=hol_dt)
    assert not st_hol.is_open
    assert st_hol.reason == "holiday"

    # 4. Рабочее время во вторник (2026-11-24, 12:00)
    work_dt = datetime(2026, 11, 24, 12, 0, tzinfo=tz)
    st_work = get_work_status(comp, now=work_dt)
    assert st_work.is_open
    assert st_work.reason == "open"


def test_storage_152_fz_forget_and_purge(tmp_path: Path):
    db_file = tmp_path / "privacy.db"
    storage = Storage(db_file)

    storage.upsert_session("telegram", "chat_user_1", "demo_detailing")
    storage.add_message("telegram", "chat_user_1", "user", "Мой номер 89991234567")
    storage.add_message("telegram", "chat_user_1", "assistant", "Записал номер!")

    lead = LeadInfo(
        channel="telegram",
        chat_id="chat_user_1",
        company_slug="demo_detailing",
        phone="+7 (999) 123-45-67",
        summary="Полировка",
    )
    storage.save_lead(lead)
    assert storage.has_lead("telegram", "chat_user_1", "+7 (999) 123-45-67")

    # Command /forget deletes everything for that subject
    deleted = storage.delete_subject_data("chat_user_1")
    assert deleted >= 3
    assert not storage.has_lead("telegram", "chat_user_1", "+7 (999) 123-45-67")
    assert len(storage.get_history("telegram", "chat_user_1")) == 0

    # Test purge
    storage.add_message("telegram", "old_chat", "user", "Старое сообщение")
    purge_res = storage.purge_expired(message_days=0, lead_days=0)
    assert purge_res["purged_messages"] >= 1


@pytest.mark.asyncio
async def test_llm_vision_multimodal_support():
    mock_client = AsyncMock()
    mock_choice = MagicMock()
    mock_choice.message.content = "На фото видна глубокая царапина на лаке правого крыла."
    mock_choice.message.tool_calls = None
    mock_client.chat.completions.create.return_value = MagicMock(choices=[mock_choice])

    service = LLMService(
        api_key="mock",
        base_url="mock",
        client=mock_client,
    )

    comp = CompanyProfile(
        slug="auto_vis",
        company_name="Вижн Сервис",
        city="Москва",
    )

    reply = await service.generate_reply(
        company=comp,
        history=[],
        user_message="Оцените фото",
        channel="telegram",
        chat_id="tg_123",
        image_url="https://example.com/car.jpg",
    )

    assert "глубокая царапина" in reply.text
    call_args = mock_client.chat.completions.create.call_args[1]
    last_msg = call_args["messages"][-1]
    assert isinstance(last_msg["content"], list)
    assert last_msg["content"][1]["type"] == "image_url"
    assert last_msg["content"][1]["image_url"]["url"] == "https://example.com/car.jpg"

