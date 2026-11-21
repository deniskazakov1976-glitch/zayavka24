import pytest
from unittest.mock import AsyncMock, MagicMock
from app.models import CompanyProfile, LeadInfo
from app.notifier import LeadNotifier


@pytest.mark.asyncio
async def test_notify_demo_started_platform():
    mock_bot = AsyncMock()
    notifier = LeadNotifier(bot=mock_bot, default_chat_id=123456)
    res = await notifier.notify_demo_started_platform(
        user_label="Иван Иванов (@ivan_auto)",
        chat_id="7890",
    )
    assert res is True
    assert mock_bot.send_message.called
    call_args = mock_bot.send_message.call_args[1]
    assert call_args["chat_id"] == 123456
    assert "Новый потенциальный клиент на демо-стенде платформы!" in call_args["text"]
    assert "Иван Иванов" in call_args["text"]


@pytest.mark.asyncio
async def test_notify_saas_trial_request():
    mock_bot = AsyncMock()
    notifier = LeadNotifier(bot=mock_bot, default_chat_id=123456)
    res = await notifier.notify_saas_trial_request(
        user_label="Сергей (@sergey_detailing)",
        chat_id="998877",
        city_name="Казань, Блеск116",
        niche="Детейлинг и оклейка",
        price_hint="Оклейка от 50к, полировка от 15к",
        owner_phone="+7 (917) 000-11-22",
    )
    assert res is True
    assert mock_bot.send_message.called
    call_args = mock_bot.send_message.call_args[1]
    assert call_args["chat_id"] == 123456
    assert "НОВАЯ ЗАЯВКА НА ТЕСТ-ДРАЙВ!" in call_args["text"]
    assert "Казань, Блеск116" in call_args["text"]
    assert "Детейлинг и оклейка" in call_args["text"]
    assert "+7 (917) 000-11-22" in call_args["text"]


def test_platform_keyboards():
    from app.telegram_bot import (
        _platform_menu_keyboard,
        _test_scenarios_keyboard,
        _trial_cta_keyboard,
    )
    # 1. Main platform menu has 4 options
    menu = _platform_menu_keyboard()
    button_texts = [btn.text for row in menu.inline_keyboard for btn in row]
    assert any("Протестировать диалог" in t for t in button_texts)
    assert any("Запустить тест-драйв" in t for t in button_texts)
    assert any("Тарифы и окупаемость" in t for t in button_texts)
    assert any("Связаться с основателем" in t for t in button_texts)

    # 2. Test scenarios keyboard has concrete auto queries
    scenarios = _test_scenarios_keyboard()
    scenario_texts = [btn.text for row in scenarios.inline_keyboard for btn in row]
    assert any("Toyota Camry" in t for t in scenario_texts)
    assert any("Geely Monjaro" in t for t in scenario_texts)
    assert any("Главное меню" in t for t in scenario_texts)

    # 3. Trial CTA keyboard
    trial = _trial_cta_keyboard()
    trial_texts = [btn.text for row in trial.inline_keyboard for btn in row]
    assert any("Запустить тест-драйв" in t for t in trial_texts)
