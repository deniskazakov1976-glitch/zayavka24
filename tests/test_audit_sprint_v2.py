import asyncio
import pytest
from unittest.mock import AsyncMock, MagicMock

from app.company_store import CompanyStore
from app.config import Settings
from app.crm import CRMDispatcher
from app.debounce import MessageDebouncer
from app.guardrails import (
    extract_allowed_numbers,
    extract_allowed_price_ranges,
    is_number_allowed,
    validate_reply,
)
from app.llm import build_contextual_fallback
from app.models import CompanyProfile, KnowledgeGap, LeadInfo
from app.stt import SpeechToTextService
from app.storage import Storage
from app.telegram_bot import RateLimiter


@pytest.fixture
def sample_company():
    return CompanyProfile(
        slug="test_detailing",
        company_name="АвтоБлеск Детейлинг",
        city="Москва",
        niche="детейлинг и покраска",
        manager_name="Александр",
        services=[
            "Локальная покраска детали с подбором цвета: от 9 000 до 15 000 ₽ за элемент",
            "Полная покраска элемента (бампер, крыло, дверь) под ключ: от 13 000 до 22 000 ₽",
            "Оклейка зон риска полиуретановой плёнкой: от 45 000 до 75 000 ₽",
            "Оклейка фар полиуретаном: от 5 000 до 8 000 ₽ за пару",
        ],
        faq=[
            "Сколько стоит химчистка? — от 10 000 до 18 000 ₽ в зависимости от класса.",
        ],
        discount_offer="скидку 10% на первый визит",
        media_reply_promise="в течение 15 минут",
        crm_type="amocrm",
        crm_subdomain="testcrm",
        crm_api_token="test_token",
    )


def test_build_contextual_fallback_with_car_and_service(sample_company):
    """When a customer asks for a specific car and service (e.g. Camry bumper painting),
    fallback MUST recognize both car and service, quoting the price fork instead of asking generic question.
    """
    query = "Сколько стоит покрасить передний бампер на Toyota Camry?"
    reply = build_contextual_fallback(query, sample_company)

    # Must contain car model
    assert "Toyota Camry" in reply
    # Must contain service price fork from profile
    assert "13 000" in reply or "22 000" in reply
    # Must NOT ask generic "какой у вас автомобиль"
    assert "какой у вас автомобиль" not in reply.lower()
    # Must soft-capture phone
    assert "телефон" in reply.lower()


def test_guardrails_price_range_tolerance(sample_company):
    """Numbers falling inside approved service price ranges (e.g. 18 000 between 13 000 and 22 000)
    must NOT be rejected as hallucinations.
    """
    ranges = extract_allowed_price_ranges(sample_company)
    assert len(ranges) >= 3

    # 18 000 falls in 13 000..22 000 (painting bumper)
    allowed_nums = extract_allowed_numbers(sample_company)
    assert is_number_allowed(18000, allowed_nums, ranges)

    reply = "Покраска переднего бампера на ваш седан выйдет около 18 000 ₽ под ключ с гарантией."
    res = validate_reply(reply, sample_company, mode="enforce")
    assert res.is_safe is True
    assert "18 000" in res.text


def test_guardrails_bare_price_hallucination_detected(sample_company):
    """Hallucinated price outside approved ranges without currency symbol after price trigger (e.g. 'будет стоить 89999')
    must be flagged and replaced in enforce mode.
    """
    reply = "Для вашего автомобиля покраска будет стоить 89999 с учетом подбора краски."
    res = validate_reply(reply, sample_company, mode="enforce")
    assert res.is_safe is False
    assert any("Выдуманная цена без валюты" in v for v in res.violations)
    assert "89999" not in res.text


def test_guardrails_pairwise_sum_tolerance(sample_company):
    """Bundled services sum (e.g. bumper painting 13000 + headlights 5000 = 18000)
    must be permitted by allowed_numbers.
    """
    allowed_nums = extract_allowed_numbers(sample_company)
    assert (13000 + 5000) in allowed_nums


@pytest.mark.asyncio
async def test_message_debouncer():
    """Rapid messages within 0.1s must be grouped and flushed once with combined text."""
    debouncer = MessageDebouncer(delay_seconds=0.1)
    flushed_texts = []

    async def on_flush(text: str):
        flushed_texts.append(text)

    await debouncer.buffer_message("telegram", "chat_1", "Первое сообщение", on_flush)
    await debouncer.buffer_message("telegram", "chat_1", "Второе сообщение", on_flush)

    assert len(flushed_texts) == 0
    await asyncio.sleep(0.2)

    assert len(flushed_texts) == 1
    assert "Первое сообщение\nВторое сообщение" == flushed_texts[0]


def test_rate_limiter_ttl_cleanup():
    """RateLimiter cleanup removes entries older than 3600s and avoids memory leaks."""
    limiter = RateLimiter(min_interval_sec=0.1, max_per_hour=10)
    now = 1000.0

    limiter.allow("chat_old", now=now)
    assert "chat_old" in limiter._timestamps

    # 3601 seconds later, cleanup removes stale chat
    removed = limiter.cleanup(now=now + 3601.0)
    assert removed == 1
    assert "chat_old" not in limiter._timestamps


def test_knowledge_gap_deduplication(tmp_path):
    """Saving identical pending question for same company must return existing ID."""
    storage = Storage(db_path=str(tmp_path / "test.db"))
    gap1 = KnowledgeGap(
        id=None,
        channel="telegram",
        chat_id="test_chat",
        company_slug="demo_detailing",
        question="Делаете ли вы бронирование лобового стекла пленкой?",
        customer_message="Интересует бронь лобового",
        status="pending",
    )
    id1 = storage.save_knowledge_gap(gap1)
    assert id1 > 0

    gap2 = KnowledgeGap(
        id=None,
        channel="telegram",
        chat_id="other_chat",
        company_slug="demo_detailing",
        question="Делаете ли вы бронирование лобового стекла пленкой?",
        customer_message="Хочу забронировать лобовое",
        status="pending",
    )
    id2 = storage.save_knowledge_gap(gap2)
    assert id1 == id2


@pytest.mark.asyncio
async def test_amocrm_send_notes(sample_company):
    """amoCRM lead creation dispatches lead first, then calls /notes endpoint with qualification note."""
    lead = LeadInfo(
        channel="telegram",
        chat_id="123",
        company_slug="test_detailing",
        client_name="Иван",
        phone="+79991234567",
        car_model="BMW X5",
        service_interest="Оклейка",
        notes="Квалификация: BMW X5, Оклейка, 55 000 ₽",
    )

    mock_client = AsyncMock()
    mock_client.is_closed = False
    mock_post_lead = MagicMock(status_code=200)
    mock_post_lead.json.return_value = {
        "_embedded": {"leads": [{"id": 998877}]}
    }
    mock_post_note = MagicMock(status_code=200)
    mock_post_note.json.return_value = {}

    mock_client.post.side_effect = [mock_post_lead, mock_post_note]

    dispatcher = CRMDispatcher(client=mock_client)
    res = await dispatcher.send_amocrm(
        lead=lead,
        company=sample_company,
    )

    assert res["status"] == "ok"
    assert res["lead_id"] == 998877
    assert mock_client.post.call_count == 2
    # Verify second call was to /notes endpoint
    second_call_url = mock_client.post.call_args_list[1][0][0]
    assert "/api/v4/leads/998877/notes" in second_call_url


@pytest.mark.asyncio
async def test_stt_transcription_mock():
    """SpeechToTextService correctly passes audio bytes to OpenAI audio transcription."""
    mock_openai = AsyncMock()
    mock_openai.audio.transcriptions.create.return_value = MagicMock(text="Сколько стоит покрасить крыло на киа рио")

    stt = SpeechToTextService(api_key="test_key", client=mock_openai)
    text = await stt.transcribe(b"dummy_ogg_audio_data", filename="voice.ogg")

    assert text == "Сколько стоит покрасить крыло на киа рио"
    assert mock_openai.audio.transcriptions.create.call_count == 1
