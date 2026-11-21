from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest

from app.crm import CRMDispatcher
from app.models import CompanyProfile, LeadInfo
from app.notifier import LeadNotifier


@pytest.fixture
def sample_lead():
    return LeadInfo(
        channel="telegram",
        chat_id="tg_12345",
        company_slug="demo_detailing",
        client_name="Иван",
        phone="+79991234567",
        car_model="BMW X5",
        service_interest="Оклейка зон риска",
        estimated_price="от 45 000 ₽",
        summary="Хочет оклеить капот и фары на этой неделе",
    )


@pytest.mark.asyncio
async def test_crm_skipped_when_disabled(sample_lead):
    company = CompanyProfile(
        slug="demo_detailing",
        company_name="БлескАвто",
        city="Москва",
        crm_type="none",
    )
    dispatcher = CRMDispatcher()
    res = await dispatcher.dispatch(sample_lead, company)
    assert res["status"] == "skipped"
    assert res["reason"] == "crm_disabled"


@pytest.mark.asyncio
async def test_crm_webhook_success(sample_lead):
    company = CompanyProfile(
        slug="demo_detailing",
        company_name="БлескАвто",
        city="Москва",
        crm_type="webhook",
        crm_webhook_url="https://hooks.example.com/crm/leads",
        crm_api_token="secret_key_123",
    )

    mock_client = AsyncMock()
    mock_resp = SimpleNamespace(status_code=200, text="OK")
    mock_client.post.return_value = mock_resp
    mock_client.is_closed = False

    dispatcher = CRMDispatcher(client=mock_client)
    res = await dispatcher.dispatch(sample_lead, company)

    assert res["status"] == "ok"
    assert res["provider"] == "webhook"
    assert mock_client.post.called
    call_args = mock_client.post.call_args
    assert call_args[0][0] == "https://hooks.example.com/crm/leads"
    headers = call_args[1]["headers"]
    assert "X-CRM-Token" in headers
    assert "X-Webhook-Signature" in headers


@pytest.mark.asyncio
async def test_crm_amocrm_dispatch(sample_lead):
    company = CompanyProfile(
        slug="demo_detailing",
        company_name="БлескАвто",
        city="Москва",
        crm_type="amocrm",
        crm_subdomain="bleskauto",
        crm_api_token="amo_long_lived_token_xyz",
    )

    mock_client = AsyncMock()
    mock_resp = SimpleNamespace(status_code=200, text="{}")
    mock_client.post.return_value = mock_resp
    mock_client.is_closed = False

    dispatcher = CRMDispatcher(client=mock_client)
    res = await dispatcher.dispatch(sample_lead, company)

    assert res["status"] == "ok"
    assert res["provider"] == "amocrm"
    call_args = mock_client.post.call_args
    assert "https://bleskauto.amocrm.ru/api/v4/leads/complex" == call_args[0][0]
    payload = call_args[1]["json"]
    assert len(payload) == 1
    lead_obj = payload[0]
    assert "BMW X5" in lead_obj["name"] or "Оклейка" in lead_obj["name"]
    contact_data = lead_obj["_embedded"]["contacts"][0]
    assert contact_data["custom_fields_values"][0]["values"][0]["value"] == "+79991234567"


@pytest.mark.asyncio
async def test_crm_bitrix24_dispatch(sample_lead):
    company = CompanyProfile(
        slug="demo_detailing",
        company_name="БлескАвто",
        city="Москва",
        crm_type="bitrix24",
        crm_webhook_url="https://bleskauto.bitrix24.ru/rest/1/webhook_token",
    )

    mock_client = AsyncMock()
    mock_resp = SimpleNamespace(status_code=200, text="{}")
    mock_client.post.return_value = mock_resp
    mock_client.is_closed = False

    dispatcher = CRMDispatcher(client=mock_client)
    res = await dispatcher.dispatch(sample_lead, company)

    assert res["status"] == "ok"
    assert res["provider"] == "bitrix24"
    call_args = mock_client.post.call_args
    assert "https://bleskauto.bitrix24.ru/rest/1/webhook_token/crm.lead.add.json" == call_args[0][0]
    fields = call_args[1]["json"]["fields"]
    assert fields["PHONE"][0]["VALUE"] == "+79991234567"


@pytest.mark.asyncio
async def test_lead_notifier_triggers_crm_dispatch(sample_lead):
    company = CompanyProfile(
        slug="demo_detailing",
        company_name="БлескАвто",
        city="Москва",
        crm_type="webhook",
        crm_webhook_url="https://hooks.example.com/test",
    )

    mock_bot = AsyncMock()
    mock_bot.send_message = AsyncMock(return_value=True)

    mock_crm = AsyncMock()
    mock_crm.dispatch = AsyncMock(return_value={"status": "ok"})

    notifier = LeadNotifier(bot=mock_bot, default_chat_id=123456, crm_dispatcher=mock_crm)
    notified = await notifier.notify(sample_lead, company)

    assert notified is True
    assert mock_bot.send_message.called
    assert mock_crm.dispatch.called
