import asyncio
import io
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch
import pytest

from app.bot_manager import BotManager
from app.config import Settings
from app.guardrails import validate_reply, _extract_numbers
from app.models import CompanyProfile, ConversationMessage, KnowledgeGap, LeadInfo
from app.storage import Storage
from app.avito import AvitoClient, AvitoWebhookHandler
from app.whatsapp import WhatsAppWebhookHandler
from app.company_store import CompanyStore


@pytest.fixture
def storage(tmp_path: Path) -> Storage:
    db_file = tmp_path / "test_v3.db"
    return Storage(str(db_file))


@pytest.fixture
def sample_company() -> CompanyProfile:
    return CompanyProfile(
        company_name="Премиум Авто",
        city="Москва",
        slug="premium_auto",
        tenant_slug="client_1",
        services=["Полировка кузова 15 000 руб", "Оклейка зон риска 45 000 руб"],
        advantages=["Опыт 10 лет", "Гарантия 1 год"],
        discount_offer="Скидка 10% на первую полировку",
        media_reply_promise="в течение 15 минут",
        whatsapp_enabled=True,
        whatsapp_instance_id="1101",
        whatsapp_api_token="token_wa",
    )


# 1. Test BotManager stop_tenant lock fix
@pytest.mark.asyncio
async def test_bot_manager_stop_tenant_lock(storage: Storage):
    settings = Settings()
    company_store = MagicMock()
    llm = MagicMock()
    notifier = MagicMock()
    bm = BotManager(settings, company_store, storage, llm, notifier)

    # Calling stop_tenant for non-existent tenant should gracefully return False without AttributeError
    result = await bm.stop_tenant("non_existent_tenant")
    assert result is False


# 2. Test Storage delete_subject_data tenant isolation
def test_delete_subject_data_tenant_isolation(storage: Storage):
    # Insert leads for two different tenants / companies
    lead1 = LeadInfo(
        channel="telegram",
        chat_id="chat_100",
        company_slug="comp_alpha",
        client_name="Иван",
        phone="+79991112233",
        service_interest="Полировка",
    )
    lead2 = LeadInfo(
        channel="telegram",
        chat_id="chat_200",
        company_slug="comp_beta",
        client_name="Петр",
        phone="+79991112233",  # same phone number!
        service_interest="Химчистка",
    )
    storage.save_lead(lead1)
    storage.save_lead(lead2)

    # Tenant alpha deletes subject data with phone "+79991112233"
    deleted = storage.delete_subject_data("+79991112233", company_slugs=["comp_alpha"])
    assert deleted >= 1

    # comp_alpha lead must be deleted
    assert not storage.has_lead("telegram", "chat_100", "+79991112233")
    # comp_beta lead must STILL exist!
    assert storage.has_lead("telegram", "chat_200", "+79991112233")


# 3. Test Avito quota check & multi-tenant client
@pytest.mark.asyncio
async def test_avito_quota_check_and_multitenant(storage: Storage, sample_company: CompanyProfile):
    company_store = MagicMock()
    company_store.get_or_default.return_value = sample_company

    llm = AsyncMock()
    notifier = AsyncMock()
    avito_client = AsyncMock(spec=AvitoClient)
    avito_client.send_message.return_value = {"id": "sent_123"}

    # Mock quota service exceeding limit
    quota_service = MagicMock()
    quota_status = MagicMock()
    quota_status.allowed = False
    quota_status.reason = "Лимит исчерпан"
    quota_status.soft_limit = False
    quota_service.check.return_value = quota_status

    handler = AvitoWebhookHandler(
        avito_client=avito_client,
        company_store=company_store,
        storage=storage,
        llm=llm,
        notifier=notifier,
        default_company_slug="premium_auto",
        quota_service=quota_service,
    )

    payload = {
        "payload": {
            "type": "message",
            "value": {
                "id": "msg_001",
                "chat_id": "u2i-test-chat",
                "type": "text",
                "content": {"text": "Здравствуйте, сколько стоит оклейка?"},
            },
        }
    }

    res = await handler.handle_event(payload)
    assert res["status"] == "quota_blocked"

    # Verify fallback message was sent and guard event recorded without argument error
    avito_client.send_message.assert_awaited_once_with(
        "u2i-test-chat", "Передал ваш запрос менеджеру, он свяжется с вами."
    )
    events = storage.list_guard_events(limit=10)
    assert len(events) >= 1
    assert events[0]["company_slug"] == "premium_auto"


# 4. Test WhatsApp webhook handler: upsert_session, pending echo, knowledge_gap
@pytest.mark.asyncio
async def test_whatsapp_webhook_session_and_gap(storage: Storage, sample_company: CompanyProfile):
    company_store = MagicMock()
    company_store.get.return_value = sample_company
    company_store.list_all.return_value = [sample_company]

    from app.models import LLMReply
    llm = AsyncMock()
    llm.generate_reply.return_value = LLMReply(
        text="Полировка стоит 15 000 руб.",
        knowledge_gap=KnowledgeGap(
            channel="whatsapp",
            chat_id="79998887766@c.us",
            company_slug="premium_auto",
            question="Делаете ли жидкое стекло?",
            customer_message="А жидкое стекло делаете?",
            suggested_answer="Да, наносим в 2 слоя",
        ),
    )

    notifier = AsyncMock()

    handler = WhatsAppWebhookHandler(
        company_store=company_store,
        storage=storage,
        llm=llm,
        notifier=notifier,
    )

    with patch("app.whatsapp.WhatsAppClient.send_message", new_callable=AsyncMock) as mock_send:
        mock_send.return_value = {"idMessage": "wa_msg_999"}

        payload = {
            "typeWebhook": "incomingMessageReceived",
            "instanceData": {"idInstance": "1101"},
            "idMessage": "evt_wa_101",
            "senderData": {
                "chatId": "79998887766@c.us",
                "sender": "79998887766@c.us",
                "senderName": "Алексей",
            },
            "messageData": {
                "typeMessage": "textMessage",
                "textMessageData": {"textMessage": "А жидкое стекло делаете?"},
            },
        }

        res = await handler.handle_webhook(payload, company_slug_hint="premium_auto")
        assert res["status"] == "replied"

        # Check session was upserted
        session_comp = storage.get_session_company("whatsapp", "79998887766@c.us")
        assert session_comp == "premium_auto"

        # Check knowledge gap was recorded and notified
        gaps = storage.list_knowledge_gaps(company_slug="premium_auto")
        assert len(gaps) >= 1
        assert gaps[0].question == "Делаете ли жидкое стекло?"
        notifier.notify_knowledge_gap.assert_awaited_once()


# 5. Test Guardrails false positives
def test_guardrails_mileage_and_ranges(sample_company: CompanyProfile):
    # Mileage with "тыс км" should NOT trigger hallucination
    mileage_text = "При пробеге 150 тыс км рекомендуем сделать глубокую полировку за 15 000 руб."
    res = validate_reply(mileage_text, sample_company)
    assert res.is_safe is True
    assert len(res.violations) == 0

    # Range where lower bound is hallucinated (e.g. "от 2000 до 45000 руб" where 2000 is not in allowed numbers)
    range_text = "Стоимость оклейки составляет от 2 000 до 45 000 руб."
    res_range = validate_reply(range_text, sample_company)
    assert res_range.is_safe is False
    assert any("2000" in v or "2 000" in v for v in res_range.violations)


# 6. Test Dashboard landing lead
@pytest.mark.asyncio
async def test_dashboard_landing_lead(storage: Storage):
    from app.dashboard import build_dashboard_router, LandingLeadRequest
    from starlette.requests import Request

    company_store = MagicMock()
    notifier = AsyncMock()
    settings = Settings()

    router = build_dashboard_router(
        settings=settings,
        company_store=company_store,
        storage=storage,
        notifier=notifier,
    )

    # Directly find api_landing_lead endpoint
    endpoint = None
    for route in router.routes:
        if getattr(route, "path", "") == "/api/landing/lead":
            endpoint = route.endpoint
            break
    assert endpoint is not None

    req = LandingLeadRequest(
        name="Дмитрий",
        phone="+79123456789",
        service="Тест-драйв автосервиса",
    )
    mock_request = MagicMock(spec=Request)
    mock_request.headers = {}
    mock_request.client = MagicMock()
    mock_request.client.host = "127.0.0.1"

    res = await endpoint(req, mock_request)
    assert res["status"] == "ok"

    # Verify lead saved with company_slug="default" and channel="web"
    leads = storage.list_recent_leads(limit=5)
    assert len(leads) >= 1
    lead = leads[0]
    assert lead.channel == "web"
    assert lead.company_slug == "default"
    assert lead.phone == "+79123456789"

    # Verify founder telegram alert was invoked
    notifier.notify_saas_trial_request.assert_awaited_once()


# 7. Test Onboarding HTML javascript fix
def test_onboarding_html_js_order():
    html_path = Path("app/templates/onboarding.html")
    content = html_path.read_text(encoding="utf-8")

    # In nextStep(): collectCurrentStepData() and saveStep(currentStep) must be called BEFORE currentStep++
    next_step_pos = content.find("function nextStep()")
    assert next_step_pos != -1
    next_step_block = content[next_step_pos:next_step_pos + 400]

    collect_idx = next_step_block.find("collectCurrentStepData();")
    save_idx = next_step_block.find("saveStep(currentStep);")
    increment_idx = next_step_block.find("currentStep++;")

    assert collect_idx != -1, "collectCurrentStepData() should be present in nextStep()"
    assert save_idx != -1, "saveStep() should be present in nextStep()"
    assert increment_idx != -1, "currentStep++ should be present in nextStep()"

    assert collect_idx < increment_idx, "collectCurrentStepData() must be called BEFORE currentStep++"
    assert save_idx < increment_idx, "saveStep() must be called BEFORE currentStep++"
