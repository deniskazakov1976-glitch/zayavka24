import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from app.avito import AvitoWebhookHandler
from app.company_store import CompanyStore
from app.llm import LLMService, extract_phone_fallback, normalize_phone
from app.models import CompanyProfile, LeadInfo
from app.notifier import format_lead_message
from app.prompt_builder import build_system_prompt
from app.storage import Storage
from app.telegram_bot import RateLimiter


def test_company_store_loads_real_profiles():
    store = CompanyStore(Path("companies"))
    profiles = store.list_all()
    slugs = {p.slug for p in profiles}
    assert "demo_detailing" in slugs
    assert "demo_bodyshop" in slugs
    # _template.yaml must be skipped
    assert "company_slug_here" not in slugs


def test_prompt_builder_includes_company_details():
    company = CompanyProfile(
        slug="test_studio",
        company_name="Тест Детейлинг",
        city="Казань",
        address="ул. Баумана, 1",
        services=["Полировка: от 15 000 ₽"],
        advantages=["Гарантия 5 лет"],
    )
    prompt = build_system_prompt(company)
    assert "Тест Детейлинг" in prompt
    assert "Казань" in prompt
    assert "ул. Баумана, 1" in prompt
    assert "Полировка: от 15 000 ₽" in prompt
    assert "submit_lead" in prompt


def test_storage_sessions_history_pause_and_leads(tmp_path: Path):
    storage = Storage(tmp_path / "test.db")
    storage.upsert_session("telegram", "1001", "demo_detailing")
    assert storage.get_session_company("telegram", "1001") == "demo_detailing"

    storage.add_message("telegram", "1001", "user", "Привет")
    storage.add_message("telegram", "1001", "assistant", "Здравствуйте!")
    history = storage.get_history("telegram", "1001")
    assert len(history) == 2
    assert history[0].role == "user"
    assert history[1].content == "Здравствуйте!"

    assert not storage.is_paused("telegram", "1001")
    storage.pause_session("telegram", "1001", hours=24)
    assert storage.is_paused("telegram", "1001")
    storage.resume_session("telegram", "1001")
    assert not storage.is_paused("telegram", "1001")

    lead = LeadInfo(
        channel="telegram",
        chat_id="1001",
        company_slug="demo_detailing",
        client_name="Иван",
        phone="+7 (999) 123-45-67",
        summary="Оклейка капота BMW X5",
    )
    assert not storage.has_lead("telegram", "1001", lead.phone)
    storage.save_lead(lead)
    assert storage.has_lead("telegram", "1001", lead.phone)
    recent = storage.list_recent_leads(5)
    assert len(recent) == 1
    assert recent[0].phone == "+7 (999) 123-45-67"


def test_phone_extraction_and_normalization():
    assert normalize_phone("89991234567") == "+7 (999) 123-45-67"
    assert extract_phone_fallback("Запишите меня, мой номер 8 (916) 555-44-33") == "+7 (916) 555-44-33"
    assert extract_phone_fallback("Без номера") is None


@pytest.mark.asyncio
async def test_llm_tool_call_captures_lead():
    company = CompanyProfile(slug="demo_detailing", company_name="БлескАвто", city="Москва")

    tool_call = SimpleNamespace(
        id="call_1",
        function=SimpleNamespace(
            name="submit_lead",
            arguments=json.dumps(
                {
                    "client_name": "Иван",
                    "phone": "89991112233",
                    "car_model": "BMW X5",
                    "service_interest": "Оклейка зон риска",
                    "summary": "Хочет оклеить переднюю часть BMW X5",
                }
            ),
        ),
    )
    first_resp = SimpleNamespace(
        choices=[SimpleNamespace(message=SimpleNamespace(content="", tool_calls=[tool_call]))]
    )
    second_resp = SimpleNamespace(
        choices=[
            SimpleNamespace(
                message=SimpleNamespace(
                    content="Спасибо, Иван! Записал ваш номер, мастер свяжется в течение 10 минут.",
                    tool_calls=None,
                )
            )
        ]
    )

    mock_client = SimpleNamespace(
        chat=SimpleNamespace(
            completions=SimpleNamespace(create=AsyncMock(side_effect=[first_resp, second_resp]))
        )
    )
    llm = LLMService(api_key="test", base_url="http://localhost", client=mock_client)
    reply = await llm.generate_reply(
        company=company,
        history=[],
        user_message="Меня зовут Иван, BMW X5, телефон 89991112233",
        channel="telegram",
        chat_id="42",
    )

    assert "Спасибо, Иван" in reply.text
    assert reply.lead is not None
    assert reply.lead.phone == "+7 (999) 111-22-33"
    assert reply.lead.car_model == "BMW X5"


@pytest.mark.asyncio
async def test_llm_tool_call_captures_knowledge_gap():
    company = CompanyProfile(slug="demo_detailing", company_name="БлескАвто", city="Москва")

    tool_call = SimpleNamespace(
        id="call_gap_1",
        function=SimpleNamespace(
            name="report_knowledge_gap",
            arguments=json.dumps(
                {
                    "question": "Оклеиваете ли вы вертолёты?",
                    "suggested_topic": "авиация",
                }
            ),
        ),
    )
    first_resp = SimpleNamespace(
        choices=[SimpleNamespace(message=SimpleNamespace(content="", tool_calls=[tool_call]))]
    )
    second_resp = SimpleNamespace(
        choices=[
            SimpleNamespace(
                message=SimpleNamespace(
                    content="Уточню этот момент у старшего технолога. Оставьте ваш номер телефона!",
                    tool_calls=None,
                )
            )
        ]
    )

    mock_client = SimpleNamespace(
        chat=SimpleNamespace(
            completions=SimpleNamespace(create=AsyncMock(side_effect=[first_resp, second_resp]))
        )
    )
    llm = LLMService(api_key="test", base_url="http://localhost", client=mock_client)
    reply = await llm.generate_reply(
        company=company,
        history=[],
        user_message="А вертолёт оклеите?",
        channel="telegram",
        chat_id="42",
    )

    assert reply.knowledge_gap is not None
    assert reply.knowledge_gap.question == "Оклеиваете ли вы вертолёты?"
    assert reply.knowledge_gap.customer_message == "А вертолёт оклеите?"


@pytest.mark.asyncio
async def test_avito_webhook_reply_and_manual_owner_autopause(tmp_path: Path):
    storage = Storage(tmp_path / "avito.db")
    store = CompanyStore(Path("companies"))

    avito_client = SimpleNamespace(
        user_id=777,
        send_message=AsyncMock(return_value={"id": "bot_msg_1"}),
    )
    llm = SimpleNamespace(
        generate_reply=AsyncMock(
            return_value=SimpleNamespace(
                text="Здравствуйте! Подскажите марку и модель авто?",
                lead=None,
            )
        )
    )
    notifier = SimpleNamespace(notify=AsyncMock(return_value=True))

    handler = AvitoWebhookHandler(
        avito_client=avito_client,
        company_store=store,
        storage=storage,
        llm=llm,
        notifier=notifier,
        default_company_slug="demo_detailing",
        manual_pause_hours=24,
    )

    # 1. Incoming customer message -> bot replies
    res1 = await handler.handle_event(
        {
            "payload": {
                "type": "message",
                "value": {
                    "id": "cust_msg_1",
                    "chat_id": "u2i-abc",
                    "author_id": 12345,
                    "type": "text",
                    "content": {"text": "Сколько стоит полировка?"},
                },
            }
        }
    )
    assert res1 == {"status": "replied"}
    avito_client.send_message.assert_awaited_once()

    # 2. Bot's own echo -> ignored, NOT paused
    res2 = await handler.handle_event(
        {
            "payload": {
                "type": "message",
                "value": {
                    "id": "bot_msg_1",
                    "chat_id": "u2i-abc",
                    "author_id": 777,
                    "type": "text",
                    "content": {"text": "Здравствуйте! Подскажите марку и модель авто?"},
                },
            }
        }
    )
    assert res2 == {"status": "ignored", "reason": "bot_echo"}
    assert not storage.is_paused("avito", "u2i-abc")

    # 3. Owner replies manually in Avito -> auto-pause!
    res3 = await handler.handle_event(
        {
            "payload": {
                "type": "message",
                "value": {
                    "id": "owner_manual_1",
                    "chat_id": "u2i-abc",
                    "author_id": 777,
                    "type": "text",
                    "content": {"text": "Добрый день, звоните мне напрямую!"},
                },
            }
        }
    )
    assert res3 == {"status": "paused", "reason": "manual_owner_reply"}
    assert storage.is_paused("avito", "u2i-abc")

    # 4. Next customer message while paused -> ignored
    res4 = await handler.handle_event(
        {
            "payload": {
                "type": "message",
                "value": {
                    "id": "cust_msg_2",
                    "chat_id": "u2i-abc",
                    "author_id": 12345,
                    "type": "text",
                    "content": {"text": "Ок, сейчас позвоню"},
                },
            }
        }
    )
    assert res4 == {"status": "ignored", "reason": "paused"}


def test_format_lead_message_escapes_html():
    lead = LeadInfo(
        channel="avito",
        chat_id="u2i-1",
        company_slug="demo_detailing",
        client_name="Алексей <script>",
        phone="+7 (999) 000-11-22",
        car_model="Toyota <Camry>",
        service_interest="Оклейка & полировка",
        avito_chat_url="https://www.avito.ru/profile/messenger/channel/u2i-1",
    )
    text = format_lead_message(lead)
    assert "Новая заявка (Авито)" in text
    assert "+7 (999) 000-11-22" in text
    assert "&lt;script&gt;" in text
    assert "&lt;Camry&gt;" in text
    assert "Оклейка &amp; полировка" in text


def test_prompt_has_injection_guard():
    company = CompanyProfile(slug="demo", company_name="Тест", city="Москва")
    prompt = build_system_prompt(company)
    assert "Защита роли и цен" in prompt
    assert "забудь инструкции" in prompt


def test_rate_limiter_blocks_burst_and_hourly_overflow():
    rl = RateLimiter(min_interval_sec=2.0, max_per_hour=3)
    assert rl.allow("u1", now=100.0) is True
    assert rl.allow("u1", now=100.5) is False  # too fast
    assert rl.allow("u1", now=103.0) is True
    assert rl.allow("u1", now=106.0) is True
    assert rl.allow("u1", now=109.0) is False  # 3/hour exceeded
    assert rl.allow("u1", now=3705.0) is True  # window expired


@pytest.mark.asyncio
async def test_avito_webhook_handles_image_and_voice(tmp_path: Path):
    storage = Storage(tmp_path / "avito_media.db")
    store = CompanyStore(Path("companies"))
    avito_client = SimpleNamespace(
        user_id=777,
        send_message=AsyncMock(return_value={"id": "bot_media_1"}),
    )
    llm = SimpleNamespace(generate_reply=AsyncMock())
    notifier = SimpleNamespace(notify=AsyncMock())

    handler = AvitoWebhookHandler(
        avito_client=avito_client,
        company_store=store,
        storage=storage,
        llm=llm,
        notifier=notifier,
        default_company_slug="demo_detailing",
    )

    res = await handler.handle_event(
        {
            "payload": {
                "type": "message",
                "value": {
                    "id": "cust_img_1",
                    "chat_id": "u2i-media",
                    "author_id": 555,
                    "type": "image",
                    "content": {},
                },
            }
        }
    )
    assert res == {"status": "replied_media"}
    avito_client.send_message.assert_awaited_once()
    llm.generate_reply.assert_not_awaited()


def test_storage_stats_and_sessions_listing(tmp_path: Path):
    storage = Storage(tmp_path / "stats_test.db")
    storage.upsert_session("telegram", "chat_1", "demo_detailing")
    storage.add_message("telegram", "chat_1", "user", "Привет!")
    storage.add_message("telegram", "chat_1", "assistant", "Добрый день!")

    lead = LeadInfo(
        channel="telegram",
        chat_id="chat_1",
        company_slug="demo_detailing",
        phone="+7 (999) 111-22-33",
        summary="Оклейка",
    )
    storage.save_lead(lead)

    stats = storage.get_stats()
    assert stats["total_sessions"] == 1
    assert stats["total_messages"] == 2
    assert stats["total_leads"] == 1
    assert stats["paused_sessions"] == 0

    sessions = storage.list_sessions()
    assert len(sessions) == 1
    assert sessions[0]["channel"] == "telegram"
    assert sessions[0]["chat_id"] == "chat_1"
    assert sessions[0]["message_count"] == 2
    assert sessions[0]["last_message"] == "Добрый день!"
    assert sessions[0]["is_paused"] is False


def test_dashboard_api_and_html(tmp_path: Path):
    from starlette.testclient import TestClient

    from app.config import Settings
    from app.server import create_app

    companies_dir = tmp_path / "companies"
    companies_dir.mkdir(parents=True, exist_ok=True)
    (companies_dir / "test_comp.yaml").write_text(
        """
slug: test_comp
company_name: "Тестовая Компания"
city: "Москва"
niche: "детейлинг"
manager_name: "Сергей"
manager_role: "мастер"
services:
  - "Полировка: 10 000 руб"
""",
        encoding="utf-8",
    )

    settings = Settings(
        companies_dir=companies_dir,
        db_path=tmp_path / "db.sqlite3",
        dashboard_enabled=True,
        dashboard_password="",
    )
    store = CompanyStore(companies_dir)
    storage = Storage(settings.db_path)

    app = create_app(
        settings=settings,
        company_store=store,
        storage=storage,
        get_bot_username=lambda: "Zayavka24AI_bot",
    )
    client = TestClient(app)

    # 1. HTML Dashboard
    resp = client.get("/")
    assert resp.status_code == 200
    assert "Заявка 24/7" in resp.text
    assert "@Zayavka24AI_bot" in resp.text

    # 1b. Public Landing Page
    resp_landing = client.get("/landing")
    assert resp_landing.status_code == 200
    assert "Заявка 24/7" in resp_landing.text
    assert "Калькулятор прибыли" in resp_landing.text
    assert "@Zayavka24AI_bot" in resp_landing.text

    # 2. Stats
    resp_stats = client.get("/api/stats")
    assert resp_stats.status_code == 200
    d_stats = resp_stats.json()
    assert d_stats["companies_count"] == 1
    assert d_stats["bot_username"] == "Zayavka24AI_bot"

    # 3. Companies list
    resp_comp = client.get("/api/companies")
    assert resp_comp.status_code == 200
    comps = resp_comp.json()
    assert len(comps) == 1
    assert comps[0]["slug"] == "test_comp"
    assert "t.me/Zayavka24AI_bot?start=test_comp" in comps[0]["demo_link"]

    # 4. Create company via API
    resp_create = client.post(
        "/api/companies",
        json={
            "slug": "new_auto",
            "company_name": "Новый Автосервис",
            "city": "Казань",
            "niche": "кузовной ремонт",
            "services": ["Покраска: 12000"],
        },
    )
    assert resp_create.status_code == 200
    assert resp_create.json()["status"] == "ok"
    assert store.get("new_auto") is not None
    assert store.get("new_auto").city == "Казань"

    # 5. Avito Config API
    resp_avito_cfg = client.get("/api/avito/config")
    assert resp_avito_cfg.status_code == 200
    assert "avito_enabled" in resp_avito_cfg.json()

    resp_save_avito = client.post(
        "/api/avito/config",
        json={
            "avito_enabled": True,
            "avito_client_id": "test_cid_123",
            "avito_client_secret": "test_secret_abc",
            "avito_user_id": 998877,
            "avito_default_company_slug": "test_comp",
            "avito_manual_pause_hours": 12,
        },
    )
    assert resp_save_avito.status_code == 200
    assert resp_save_avito.json()["status"] == "ok"
    assert settings.avito_enabled is True
    assert settings.avito_client_id == "test_cid_123"
    assert settings.avito_user_id == 998877

    # 6. Avito register webhook validation (requires https)
    resp_wh = client.post(
        "/api/avito/register-webhook",
        json={"webhook_url": "http://insecure-domain.ru/webhook/avito"},
    )
    assert resp_wh.status_code == 400
    assert "https://" in resp_wh.json()["detail"]

    # 7. Knowledge gaps and 1-Click Self-Learning API
    from app.models import KnowledgeGap

    gap = KnowledgeGap(
        channel="telegram",
        chat_id="chat_99",
        company_slug="test_comp",
        question="Оклеиваете ли вы мотоциклы?",
        customer_message="Здравствуйте! А оклеиваете ли мотоциклы Ducati?",
    )
    gap_id = storage.save_knowledge_gap(gap)
    assert gap_id > 0

    resp_gaps = client.get("/api/gaps")
    assert resp_gaps.status_code == 200
    gaps_data = resp_gaps.json()
    assert len(gaps_data) == 1
    assert gaps_data[0]["question"] == "Оклеиваете ли вы мотоциклы?"

    # Learn gap in 1 click
    resp_learn = client.post(
        f"/api/gaps/{gap_id}/learn",
        json={"answer": "Да, оклеиваем мотоциклы, цена от 35 000 руб."},
    )
    assert resp_learn.status_code == 200
    assert resp_learn.json()["status"] == "ok"

    updated_comp = store.get("test_comp")
    assert any("мотоциклы" in item for item in updated_comp.faq)

    resolved_gaps = storage.list_knowledge_gaps(status="learned")
    assert len(resolved_gaps) == 1
    assert "35 000" in resolved_gaps[0].suggested_answer


def test_update_env_file_preserves_structure(tmp_path: Path):
    from app.config import update_env_file

    env_file = tmp_path / ".env"
    env_file.write_text(
        "# Some comment\nEXISTING_KEY=old_value\nANOTHER_KEY=keep_me\n",
        encoding="utf-8",
    )

    update_env_file({"EXISTING_KEY": "new_value", "NEW_KEY": "123"}, env_path=env_file)
    content = env_file.read_text(encoding="utf-8")
    assert "# Some comment" in content
    assert "EXISTING_KEY=new_value" in content
    assert "ANOTHER_KEY=keep_me" in content
    assert "NEW_KEY=123" in content


def test_media_reply_work_hours_parsing():
    from app.media_reply import parse_work_hours

    assert parse_work_hours("Ежедневно с 10:00 до 21:00") == (10, 21)
    assert parse_work_hours("Пн–Сб с 09:00 до 20:00") == (9, 20)
    assert parse_work_hours("10:00-22:00") == (10, 22)
    assert parse_work_hours("круглосуточно") == (0, 24)


def test_media_reply_builder_daytime_and_nighttime():
    from datetime import datetime
    from app.media_reply import build_media_reply

    company = CompanyProfile(
        slug="test_reply",
        company_name="АвтоБлеск",
        city="Москва",
        manager_name="Игорь",
        manager_role="мастер-приёмщик",
        work_hours="с 10:00 до 20:00",
        media_reply_promise="в течение 20 минут в рабочее время",
    )

    # 1. Daytime: 14:30
    dt_day = datetime(2026, 11, 20, 14, 30)
    reply_day = build_media_reply(company, media_type="photo", has_phone=False, now=dt_day)
    assert "фото получил" in reply_day
    assert "мастер-приёмщик Игорь" in reply_day
    assert "в течение 20 минут в рабочее время" in reply_day
    assert "оставьте, пожалуйста, ваш номер телефона" in reply_day
    assert "10 минут" not in reply_day

    # 2. Night time: 23:15
    dt_night = datetime(2026, 11, 20, 23, 15)
    reply_night = build_media_reply(company, media_type="photo", has_phone=False, now=dt_night)
    assert "фото получил" in reply_night
    assert "не на смене" in reply_night
    assert "с 10:00" in reply_night
    assert "10 минут" not in reply_night

    # 3. Already has phone
    reply_has_phone = build_media_reply(company, media_type="voice", has_phone=True, now=dt_day)
    assert "голосовое сообщение получил" in reply_has_phone
    assert "свяжемся с вами по вашему номеру" in reply_has_phone
    assert "оставьте, пожалуйста, ваш номер телефона" not in reply_has_phone

    # 4. Custom override
    company.custom_media_reply = "Спасибо! Мастер перезвонит в течение часа."
    assert build_media_reply(company) == "Спасибо! Мастер перезвонит в течение часа."
