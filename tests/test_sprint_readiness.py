import asyncio
from datetime import datetime, timedelta, timezone
import io
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch
import pytest
from fastapi.testclient import TestClient

from app.auth import AuthService, COOKIE_NAME, hash_password
from app.avito import AvitoClient, AvitoWebhookHandler
from app.company_store import CompanyStore
from app.config import Settings
from app.followup import FollowupService
from app.llm import LLMService
from app.models import CompanyProfile, ConversationMessage, KnowledgeGap, LeadInfo, LLMReply
from app.notifier import LeadNotifier
from app.server import create_app
from app.storage import Storage
from app.stt import SpeechToTextService
from app.whatsapp import WhatsAppWebhookHandler


@pytest.fixture
def storage(tmp_path: Path) -> Storage:
    return Storage(str(tmp_path / "sprint_test.db"))


@pytest.fixture
def test_company() -> CompanyProfile:
    return CompanyProfile(
        slug="comp_test_ready",
        company_name="АвтоСтудия Тест",
        city="Москва",
        tenant_slug="client_alpha",
        services=["Покраска крыла от 10 000 руб"],
        notify_chat_id=12345678,
        crm_type="webhook",
        crm_webhook_url="https://crm.example.com/webhook",
        crm_api_token="secret_crm_key",
        whatsapp_enabled=True,
        whatsapp_instance_id="998877",
        whatsapp_api_token="wa_secret_token",
        whatsapp_webhook_secret="wa_wh_sec",
    )


# 1. Test P0.1: Avito Multi-Tenancy Routing & Echo Check
@pytest.mark.asyncio
async def test_avito_multitenant_routing_and_echo(storage: Storage, test_company: CompanyProfile):
    company_store = MagicMock()
    company_store.get_or_default.return_value = test_company
    llm = AsyncMock()
    notifier = AsyncMock()

    # Save multi-tenant avito account
    storage.save_avito_account(
        company_slug="comp_test_ready",
        user_id=777888,
        client_id="client_777",
        client_secret="sec_777",
        enabled=True,
    )

    handler = AvitoWebhookHandler(
        avito_client=None,
        company_store=company_store,
        storage=storage,
        llm=llm,
        notifier=notifier,
        default_company_slug="comp_test_ready",
    )

    # 1. Message from the tenant shop manager (author_id == 777888) -> Should trigger auto-pause for comp_test_ready
    manager_reply_payload = {
        "_company_slug": "comp_test_ready",
        "payload": {
            "type": "message",
            "value": {
                "id": "avito_mgr_msg_1",
                "chat_id": "u2i-chat-alpha",
                "author_id": 777888,
                "type": "text",
                "content": {"text": "Здравствуйте, мастер свяжется с вами через 10 минут"},
            },
        },
    }

    res = await handler.handle_event(manager_reply_payload)
    assert res["status"] == "paused"
    assert storage.is_paused("avito", "u2i-chat-alpha")
    assert storage.get_session_company("avito", "u2i-chat-alpha") == "comp_test_ready"


# 2. Test P0.2: LeadNotifier Multi-Tenant Routing
@pytest.mark.asyncio
async def test_lead_notifier_tenant_routing(storage: Storage, test_company: CompanyProfile):
    tenant_bot = AsyncMock()
    tenant_bot.send_message = AsyncMock()

    bot_manager = MagicMock()
    bot_manager.get_bot.return_value = tenant_bot
    bot_manager.tenants = {
        "client_alpha": {"owner_telegram_id": 99990000, "bot": tenant_bot}
    }

    notifier = LeadNotifier(
        bot=None,
        default_chat_id="11112222",
        bot_manager=bot_manager,
        storage=storage,
    )

    lead = LeadInfo(
        channel="telegram",
        chat_id="user_chat_1",
        company_slug="comp_test_ready",
        phone="+79991234567",
        client_name="Иван",
        service_interest="Покраска",
    )

    # When company has notify_chat_id, it should route to company.notify_chat_id using tenant_bot
    await notifier.notify(lead, test_company)
    assert tenant_bot.send_message.called
    call_args = tenant_bot.send_message.call_args[1]
    assert call_args["chat_id"] == 12345678


# 3. Test P0.3: LLM Failover Token Billing with Actual Used Model
@pytest.mark.asyncio
async def test_llm_failover_billing_records_actual_model(storage: Storage):
    llm = LLMService(
        api_key="mock_key",
        base_url="https://mock.api/v1",
        model="primary_expensive_model",
        fallback_models=["fallback_cheaper_model"],
        storage=storage,
    )

    # Set model prices in DB
    storage.save_model_price("primary_expensive_model", 1.0, 2.0)
    storage.save_model_price("fallback_cheaper_model", 0.1, 0.2)

    # Mock _call_llm_with_retry to fail on primary and succeed on fallback
    async def mock_call(model, **kwargs):
        if model == "primary_expensive_model":
            raise Exception("503 Service Unavailable")
        # Return success with fallback_cheaper_model
        choice = SimpleNamespace(message=SimpleNamespace(content="Здравствуйте! Сориентирую по цене.", tool_calls=None))
        usage = SimpleNamespace(prompt_tokens=100, completion_tokens=50, total_tokens=150)
        return SimpleNamespace(choices=[choice], usage=usage)

    llm._call_llm_with_retry = AsyncMock(side_effect=mock_call)

    comp = CompanyProfile(slug="test_comp", company_name="Тест", city="Москва", tenant_slug="t_alpha")
    reply = await llm.generate_reply(company=comp, history=[], user_message="Сколько стоит?", tenant_slug="t_alpha")
    assert "Сориентирую" in reply.text

    # Verify that storage logged the fallback model in llm_usage table
    with storage._connect() as conn:
        rows = conn.execute("SELECT model, cost_rub FROM llm_usage WHERE tenant_slug = ?", ("t_alpha",)).fetchall()
        assert len(rows) >= 1
        assert rows[0]["model"] == "fallback_cheaper_model"
        # Cost calculation: 100 * 0.1 / 1000 + 50 * 0.2 / 1000 = 0.01 + 0.01 = 0.02
        assert pytest.approx(rows[0]["cost_rub"], 0.001) == 0.02


# 4. Test P1.1: Company PUT, DELETE and Token Masking in API
def test_company_api_crud_and_masking(tmp_path: Path, storage: Storage, test_company: CompanyProfile):
    companies_dir = tmp_path / "companies"
    companies_dir.mkdir(parents=True, exist_ok=True)
    comp_file = companies_dir / "comp_test_ready.yaml"
    import yaml
    comp_file.write_text(yaml.dump(test_company.model_dump()), encoding="utf-8")
    store = CompanyStore(companies_dir)

    settings = Settings()
    auth = AuthService(settings=settings, storage=storage)
    storage.save_user("admin_user", hash_password("secretpass123"), "platform_admin", "default")
    token = auth.login("admin_user", "secretpass123")

    app = create_app(settings=settings, company_store=store, storage=storage, auth=auth)
    client = TestClient(app)
    client.cookies.set(COOKIE_NAME, token)

    # 1. GET /api/companies -> check token masking
    resp = client.get("/api/companies")
    assert resp.status_code == 200
    data = resp.json()
    assert len(data) >= 1
    c_item = next(c for c in data if c["slug"] == "comp_test_ready")
    assert c_item["crm_has_token"] is True
    assert c_item["crm_api_token"] == "••••••••"
    assert c_item["whatsapp_has_token"] is True
    assert c_item["whatsapp_api_token"] == "••••••••"
    assert "secret=wa_wh_sec" in c_item["whatsapp_webhook_url"]

    # 2. PUT /api/companies/comp_test_ready -> update manager & address while preserving tokens
    update_payload = {
        "manager_name": "Михаил",
        "address": "Новый бокс 10",
        "crm_api_token": "••••••••",  # Should preserve existing secret_crm_key
    }
    resp_put = client.put("/api/companies/comp_test_ready", json=update_payload)
    assert resp_put.status_code == 200
    updated_comp = store.get("comp_test_ready")
    assert updated_comp.manager_name == "Михаил"
    assert updated_comp.address == "Новый бокс 10"
    assert updated_comp.crm_api_token == "secret_crm_key"

    # 3. DELETE /api/companies/comp_test_ready
    resp_del = client.delete("/api/companies/comp_test_ready")
    assert resp_del.status_code == 200
    assert store.get("comp_test_ready") is None


# 5. Test P1.2 & P1.3: CSV Export & Security Headers
def test_csv_export_and_security_headers(tmp_path: Path, storage: Storage, test_company: CompanyProfile):
    companies_dir = tmp_path / "companies"
    companies_dir.mkdir(parents=True, exist_ok=True)
    store = CompanyStore(companies_dir)
    store.save(test_company)

    # Insert a test lead
    lead = LeadInfo(
        channel="telegram",
        chat_id="chat_777",
        company_slug="comp_test_ready",
        client_name="Сергей",
        phone="+79998887766",
        car_model="BMW X5",
        service_interest="Полировка",
    )
    storage.save_lead(lead)

    settings = Settings()
    auth = AuthService(settings=settings, storage=storage)
    storage.save_user("admin_user", hash_password("secretpass123"), "platform_admin", "default")
    token = auth.login("admin_user", "secretpass123")

    app = create_app(settings=settings, company_store=store, storage=storage, auth=auth)
    client = TestClient(app)
    client.cookies.set(COOKIE_NAME, token)

    # Check Security Headers on GET /health
    health_resp = client.get("/health")
    assert health_resp.headers["X-Content-Type-Options"] == "nosniff"
    assert health_resp.headers["X-Frame-Options"] == "DENY"
    assert health_resp.headers["Referrer-Policy"] == "strict-origin-when-cross-origin"

    # Check CSV export
    csv_resp = client.get("/api/leads/export.csv")
    assert csv_resp.status_code == 200
    assert csv_resp.headers["content-type"].startswith("text/csv")
    assert "attachment; filename=" in csv_resp.headers["content-disposition"]
    # Check UTF-8 BOM
    content = csv_resp.content
    assert content.startswith(b"\xef\xbb\xbf")
    decoded = content.decode("utf-8")
    assert "BMW X5" in decoded
    assert "+79998887766" in decoded
    assert "Сергей" in decoded


# 6. Test P2.1: Smart Follow-Up Service
@pytest.mark.asyncio
async def test_smart_followup_service(tmp_path: Path, storage: Storage, test_company: CompanyProfile):
    companies_dir = tmp_path / "companies"
    companies_dir.mkdir(parents=True, exist_ok=True)
    store = CompanyStore(companies_dir)
    test_company.work_hours = "круглосуточно 24/7"  # Ensure open
    store.save(test_company)

    # Insert session updated 4 hours ago without lead
    now = datetime.now(timezone.utc)
    old_time = (now - timedelta(hours=4)).isoformat()
    with storage._connect() as conn:
        conn.execute(
            "INSERT INTO sessions (channel, chat_id, company_slug, updated_at) VALUES (?, ?, ?, ?)",
            ("telegram", "999888", "comp_test_ready", old_time),
        )

    # Mock BotManager and Bot
    mock_bot = AsyncMock()
    mock_bot.send_message = AsyncMock()
    mock_bm = MagicMock()
    mock_bm.tenants = {
        "client_alpha": {"bot": mock_bot}
    }

    followup = FollowupService(
        storage=storage,
        company_store=store,
        bot_manager=mock_bm,
        older_than_hours=3.0,
    )

    sent = await followup.run_once()
    assert sent == 1
    assert mock_bot.send_message.called
    msg_text = mock_bot.send_message.call_args[1]["text"]
    assert "актуален ли ещё ваш вопрос" in msg_text

    # Second run should send 0 because followup_sent_at was recorded
    sent_again = await followup.run_once()
    assert sent_again == 0


# 7. Test P2.2: WhatsApp Secret Enforcement & Multimodal Vision
@pytest.mark.asyncio
async def test_whatsapp_secret_and_vision_caption(tmp_path: Path, storage: Storage, test_company: CompanyProfile):
    companies_dir = tmp_path / "companies"
    companies_dir.mkdir(parents=True, exist_ok=True)
    store = CompanyStore(companies_dir)
    store.save(test_company)

    mock_llm = AsyncMock()
    mock_llm.generate_reply.return_value = LLMReply(
        text="По фотографии вижу притёртость переднего бампера. Ремонт и покраска обойдутся от 12 000 руб.",
        lead=None,
    )
    mock_http = AsyncMock()
    mock_http.is_closed = False
    mock_resp = SimpleNamespace(status_code=200, json=lambda: {"idMessage": "wa_msg_out"})
    mock_http.post = AsyncMock(return_value=mock_resp)

    handler = WhatsAppWebhookHandler(
        company_store=store,
        storage=storage,
        llm=mock_llm,
        notifier=AsyncMock(),
        client=mock_http,
    )

    # 1. Test secret check: wrong secret -> error
    bad_secret_payload = {
        "typeWebhook": "incomingMessageReceived",
        "instanceData": {"idInstance": "998877"},
        "idMessage": "msg_sec_1",
        "senderData": {"chatId": "79990001122@c.us", "sender": "79990001122"},
        "messageData": {"typeMessage": "textMessage", "textMessageData": {"textMessage": "Привет"}},
    }
    res_bad = await handler.handle_webhook(bad_secret_payload, company_slug_hint="comp_test_ready", secret="wrong_sec")
    assert res_bad["reason"] == "invalid_webhook_secret"

    # 2. Test Vision: image with caption -> invokes LLM with image_url
    vision_payload = {
        "typeWebhook": "incomingMessageReceived",
        "instanceData": {"idInstance": "998877"},
        "idMessage": "msg_vis_1",
        "senderData": {"chatId": "79990001122@c.us", "sender": "79990001122"},
        "messageData": {
            "typeMessage": "imageMessage",
            "fileMessageData": {
                "downloadUrl": "https://example.com/bumper.jpg",
                "caption": "Сколько стоит покрасить этот бампер?",
            },
        },
    }
    res_vis = await handler.handle_webhook(vision_payload, company_slug_hint="comp_test_ready", secret="wa_wh_sec")
    assert res_vis["status"] == "replied_media"
    assert "притёртость переднего бампера" in res_vis["reply"]
    assert mock_llm.generate_reply.called
    call_kwargs = mock_llm.generate_reply.call_args[1]
    assert call_kwargs["image_url"] == "https://example.com/bumper.jpg"
    assert call_kwargs["user_message"] == "Сколько стоит покрасить этот бампер?"


# 8. Test STT Service Wrapper
@pytest.mark.asyncio
async def test_stt_service_wrapper():
    mock_client = AsyncMock()
    mock_trans = AsyncMock()
    mock_trans.create = AsyncMock(return_value=SimpleNamespace(text="  Здравствуйте, покраска бампера интересует  "))
    mock_client.audio = SimpleNamespace(transcriptions=mock_trans)

    stt = SpeechToTextService(api_key="mock_key", client=mock_client)
    res = await stt.transcribe(b"fake_audio_bytes", filename="voice.ogg")
    assert res == "Здравствуйте, покраска бампера интересует"
    assert mock_trans.create.called
