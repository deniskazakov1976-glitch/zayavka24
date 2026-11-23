import json
import pytest
from unittest.mock import AsyncMock, MagicMock, patch
from pathlib import Path

from app.company_store import CompanyStore
from app.models import CompanyProfile, ConversationMessage
from app.llm import (
    LLMService,
    extract_car_model,
    match_company_service,
    build_contextual_fallback,
)
from app.config import Settings


@pytest.fixture
def demo_company() -> CompanyProfile:
    cs = CompanyStore(Path("companies"))
    comp = cs.get("demo_detailing")
    assert comp is not None
    return comp


def test_car_extraction():
    cases = [
        ("Сколько стоит покрасить передний бампер на Toyota Camry?", "Toyota Camry"),
        ("Оклейка зон риска на Geely Monjaro — сколько и как долго?", "Geely Monjaro"),
        ("Сколько стоит полировка фар на BMW X5?", "BMW X5"),
        ("Сколько стоит покраска на камри?", "Камри"),
        ("Полировка кузова на Лада Веста", "Лада Веста"),
        ("Просто вопрос без машины", None),
    ]
    for text, expected in cases:
        result = extract_car_model(text)
        if expected is None:
            assert result is None, f"Expected None for '{text}', got '{result}'"
        else:
            assert result is not None, f"Expected '{expected}' for '{text}', got None"
            assert expected.lower() in result.lower()


def test_service_matching(demo_company: CompanyProfile):
    services = demo_company.services
    
    # Bumper painting
    match1 = match_company_service("Сколько стоит покрасить передний бампер на Toyota Camry?", services)
    assert match1 is not None
    assert "13 000" in match1 or "бампер" in match1.lower()

    # Risk zones PPF
    match2 = match_company_service("Оклейка зон риска на Geely Monjaro — сколько и как долго?", services)
    assert match2 is not None
    assert "45 000" in match2 or "зон риска" in match2.lower()

    # Polishing
    match3 = match_company_service("Хочу полировку кузова", services)
    assert match3 is not None
    assert "полировка" in match3.lower()

    # Dry cleaning
    match4 = match_company_service("Глубокая химчистка салона с разбором", services)
    assert match4 is not None
    assert "химчистка" in match4.lower()


def test_contextual_fallback_does_not_ask_for_car_if_already_provided(demo_company: CompanyProfile):
    query = "Сколько стоит покрасить передний бампер на Toyota Camry?"
    reply = build_contextual_fallback(query, demo_company)
    
    assert "Toyota Camry" in reply
    assert "13 000" in reply or "22 000" in reply
    # Must NOT ask stupid question about what car they have:
    assert "какой у вас автомобиль и что планируете сделать?" not in reply
    assert demo_company.manager_name in reply


def test_contextual_fallback_handles_schedule_and_discount(demo_company: CompanyProfile):
    # Schedule
    schedule_reply = build_contextual_fallback("Работаете в воскресенье? Хочу на осмотр.", demo_company)
    assert "работаем" in schedule_reply.lower()
    assert demo_company.address in schedule_reply or "Автозаводская" in schedule_reply

    # Discount / price objection
    discount_reply = build_contextual_fallback("Сделайте скидку 50%, иначе уйду к конкурентам!", demo_company)
    assert "10%" in discount_reply or "скидк" in discount_reply.lower()
    assert demo_company.manager_name in discount_reply


@pytest.mark.asyncio
async def test_multimodel_fallback_failover_on_503(demo_company: CompanyProfile):
    """If primary model fails with 503, LLMService must automatically try fallback model."""
    mock_client = MagicMock()

    # Primary model throws 503, fallback model succeeds
    first_call_error = Exception("Error code: 503 - {'error': {'message': 'Модель сейчас занята'}}")
    second_call_response = MagicMock(
        choices=[
            MagicMock(
                message=MagicMock(
                    content="Покраска бампера на Camry обойдётся в 15 000 ₽.",
                    tool_calls=None,
                )
            )
        ]
    )

    mock_client.chat.completions.create = AsyncMock(
        side_effect=[first_call_error, second_call_response]
    )

    service = LLMService(
        api_key="mock",
        base_url="http://mock",
        model="primary-busy-503",
        fallback_models=["fallback-fast-model"],
        client=mock_client,
    )

    reply = await service.generate_reply(
        company=demo_company,
        history=[],
        user_message="Сколько стоит покрасить передний бампер на Toyota Camry?",
        channel="telegram",
        chat_id="test_chat",
    )

    assert reply.text == "Покраска бампера на Camry обойдётся в 15 000 ₽."
    assert mock_client.chat.completions.create.call_count == 2
    # Verify first call used primary model and second call used fallback model
    calls = mock_client.chat.completions.create.call_args_list
    assert calls[0].kwargs["model"] == "primary-busy-503"
    assert calls[1].kwargs["model"] == "fallback-fast-model"


@pytest.mark.asyncio
async def test_emergency_offline_fallback_phone_lead_capture(demo_company: CompanyProfile):
    """When all models are completely offline and user sends phone, lead must be captured."""
    mock_client = MagicMock()
    mock_client.chat.completions.create = AsyncMock(side_effect=Exception("Connection refused"))

    service = LLMService(
        api_key="mock",
        base_url="http://mock",
        model="primary",
        fallback_models=["fallback"],
        client=mock_client,
    )

    reply = await service.generate_reply(
        company=demo_company,
        history=[],
        user_message="Покраска бампера Camry, мой номер +7 (926) 999-88-77, перезвоните!",
        channel="telegram",
        chat_id="lead_offline_chat",
    )

    assert reply.lead is not None
    assert reply.lead.phone == "+7 (926) 999-88-77"
    assert "Записал ваш номер" in reply.text


def test_settings_fallback_models_parsing():
    s = Settings(
        llm_model="gc/gemini-2.5-flash",
        llm_fallback_models="cx/gpt-6-luna, ds/deepseek-v4-flash-vision",
    )
    assert s.llm_model == "gc/gemini-2.5-flash"
    assert s.llm_fallback_models == ["cx/gpt-6-luna", "ds/deepseek-v4-flash-vision"]
