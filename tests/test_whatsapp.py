from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from app.company_store import CompanyStore
from app.models import CompanyProfile, LLMReply, LeadInfo
from app.storage import Storage
from app.whatsapp import WhatsAppClient, WhatsAppWebhookHandler


@pytest.fixture
def wa_company():
    return CompanyProfile(
        slug="wa_studio",
        company_name="Ватсап Детейлинг",
        city="Москва",
        niche="детейлинг",
        whatsapp_enabled=True,
        whatsapp_instance_id="1101823456",
        whatsapp_api_token="wa_secret_token_abc",
        services=["Оклейка авто: от 50 000 ₽"],
    )


@pytest.fixture
def wa_handler(tmp_path: Path, wa_company):
    storage = Storage(tmp_path / "wa.db")
    comp_dir = tmp_path / "companies"
    comp_dir.mkdir(parents=True, exist_ok=True)
    comp_file = comp_dir / "wa_studio.yaml"
    import yaml
    comp_file.write_text(yaml.dump(wa_company.model_dump()), encoding="utf-8")
    store = CompanyStore(comp_dir)

    mock_llm = SimpleNamespace(
        generate_reply=AsyncMock(
            return_value=LLMReply(
                text="Здравствуйте! Оклейка авто у нас стоит от 50 000 ₽. Какая у вас марка автомобиля?",
                lead=None,
            )
        )
    )
    mock_notifier = SimpleNamespace(
        notify=AsyncMock(return_value=True),
    )

    mock_http = AsyncMock()
    mock_http.is_closed = False
    mock_resp = SimpleNamespace(status_code=200, json=lambda: {"idMessage": "out_msg_1"})
    mock_http.post = AsyncMock(return_value=mock_resp)

    handler = WhatsAppWebhookHandler(
        company_store=store,
        storage=storage,
        llm=mock_llm,
        notifier=mock_notifier,
        manual_pause_hours=24,
        client=mock_http,
    )
    return handler, storage, mock_llm, mock_notifier, mock_http


@pytest.mark.asyncio
async def test_whatsapp_text_message_flow(wa_handler):
    handler, storage, mock_llm, mock_notifier, mock_http = wa_handler

    incoming_payload = {
        "typeWebhook": "incomingMessageReceived",
        "instanceData": {"idInstance": 1101823456},
        "timestamp": 1795123456,
        "idMessage": "wa_msg_1",
        "senderData": {
            "chatId": "79991234567@c.us",
            "sender": "79991234567",
            "senderName": "Алексей",
        },
        "messageData": {
            "typeMessage": "textMessage",
            "textMessageData": {"textMessage": "Сколько стоит оклеить машину?"},
        },
    }

    res = await handler.handle_webhook(incoming_payload, company_slug_hint="wa_studio")
    assert res["status"] == "replied"
    assert "от 50 000 ₽" in res["reply"]
    assert mock_llm.generate_reply.called
    assert mock_http.post.called


@pytest.mark.asyncio
async def test_whatsapp_media_reply_flow(wa_handler):
    handler, storage, mock_llm, mock_notifier, mock_http = wa_handler

    media_payload = {
        "typeWebhook": "incomingMessageReceived",
        "instanceData": {"idInstance": 1101823456},
        "timestamp": 1795123457,
        "idMessage": "wa_img_1",
        "senderData": {
            "chatId": "79991234567@c.us",
            "sender": "79991234567",
            "senderName": "Алексей",
        },
        "messageData": {
            "typeMessage": "imageMessage",
            "fileMessageData": {"downloadUrl": "https://example.com/photo.jpg"},
        },
    }

    res = await handler.handle_webhook(media_payload, company_slug_hint="wa_studio")
    assert res["status"] == "replied_media"
    assert "фото" in res["reply"]
    assert mock_http.post.called


@pytest.mark.asyncio
async def test_whatsapp_human_reply_triggers_autopause(wa_handler):
    handler, storage, mock_llm, mock_notifier, mock_http = wa_handler

    # Human owner replies from phone app
    owner_payload = {
        "typeWebhook": "outgoingMessageReceived",
        "instanceData": {"idInstance": 1101823456},
        "timestamp": 1795123458,
        "idMessage": "wa_owner_1",
        "senderData": {
            "chatId": "79991234567@c.us",
            "sender": "79990000000",
        },
        "messageData": {
            "typeMessage": "textMessage",
            "textMessageData": {"textMessage": "Привет! Я мастер, сейчас в боксе, перезвоню через 5 минут."},
        },
    }

    res = await handler.handle_webhook(owner_payload, company_slug_hint="wa_studio")
    assert res["status"] == "paused_by_human"
    assert storage.is_paused("whatsapp", "79991234567@c.us") is True

    # Next incoming customer message should be ignored because chat is paused
    next_customer_payload = {
        "typeWebhook": "incomingMessageReceived",
        "instanceData": {"idInstance": 1101823456},
        "timestamp": 1795123459,
        "idMessage": "wa_cust_2",
        "senderData": {
            "chatId": "79991234567@c.us",
            "sender": "79991234567",
        },
        "messageData": {
            "typeMessage": "textMessage",
            "textMessageData": {"textMessage": "Хорошо, жду звонка"},
        },
    }
    res2 = await handler.handle_webhook(next_customer_payload, company_slug_hint="wa_studio")
    assert res2["status"] == "paused"


@pytest.mark.asyncio
async def test_whatsapp_lead_capture_and_notification(wa_handler):
    handler, storage, mock_llm, mock_notifier, mock_http = wa_handler

    mock_llm.generate_reply = AsyncMock(
        return_value=LLMReply(
            text="Записал вас на осмотр!",
            lead=LeadInfo(
                channel="whatsapp",
                chat_id="79991234567@c.us",
                company_slug="wa_studio",
                client_name="Алексей",
                phone="+79991234567",
                car_model="Porsche Cayenne",
                service_interest="Оклейка",
            ),
        )
    )

    lead_payload = {
        "typeWebhook": "incomingMessageReceived",
        "instanceData": {"idInstance": 1101823456},
        "timestamp": 1795123460,
        "idMessage": "wa_lead_1",
        "senderData": {
            "chatId": "79991234567@c.us",
            "sender": "79991234567",
            "senderName": "Алексей",
        },
        "messageData": {
            "typeMessage": "textMessage",
            "textMessageData": {"textMessage": "Мой номер +79991234567, машина Porsche Cayenne"},
        },
    }

    res = await handler.handle_webhook(lead_payload, company_slug_hint="wa_studio")
    assert res["status"] == "replied"
    assert res["has_lead"] is True
    assert mock_notifier.notify.called
