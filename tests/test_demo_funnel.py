import pytest
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock
from aiogram.filters import CommandObject
from aiogram.types import InlineKeyboardMarkup

from app.config import Settings
from app.company_store import CompanyStore
from app.models import CompanyProfile, LeadInfo
from app.notifier import LeadNotifier
from app.storage import Storage
from app.telegram_bot import (
    _platform_menu_keyboard,
    _test_scenarios_keyboard,
    _trial_cta_keyboard,
    _contact_keyboard,
    build_telegram_router,
)


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
    # 1. Main platform menu has 4 distinct options
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
    assert any("воскресенье" in t for t in scenario_texts)
    assert any("скидку 50%" in t for t in scenario_texts)
    assert any("Главное меню" in t for t in scenario_texts)

    # 3. Trial CTA keyboard
    trial = _trial_cta_keyboard()
    trial_texts = [btn.text for row in trial.inline_keyboard for btn in row]
    assert any("Запустить тест-драйв" in t for t in trial_texts)
    assert any("Главное меню" in t for t in trial_texts)

    # 4. Contact keyboard has direct Telegram link to Denis
    contact = _contact_keyboard()
    contact_buttons = [btn for row in contact.inline_keyboard for btn in row]
    assert any(btn.url == "https://t.me/Zayavka24AI" for btn in contact_buttons if btn.url)
    assert any("Запустить тест-драйв" in btn.text for btn in contact_buttons)
    assert any("Главное меню" in btn.text for btn in contact_buttons)


def _setup_test_router(tmp_path: Path, tenant_slug=None):
    companies_dir = tmp_path / "companies"
    companies_dir.mkdir(parents=True, exist_ok=True)
    (companies_dir / "detailing_premium.yaml").write_text(
        "slug: detailing_premium\n"
        "company_name: Премиум Детейлинг\n"
        "city: Москва\n"
        "greeting: 'Привет из студии Премиум Детейлинг!'\n",
        encoding="utf-8",
    )
    (companies_dir / "demo_detailing.yaml").write_text(
        "slug: demo_detailing\n"
        "company_name: Демо Автосервис\n"
        "city: Москва\n"
        "greeting: 'Здравствуйте! Чем помочь?'\n",
        encoding="utf-8",
    )
    store = CompanyStore(companies_dir)
    storage = Storage(tmp_path / "db.sqlite3")
    settings = Settings(
        companies_dir=companies_dir,
        db_path=tmp_path / "db.sqlite3",
        telegram_bot_token="",
        default_company_slug="detailing_premium",
    )
    llm = MagicMock()
    llm.generate_reply = AsyncMock()
    mock_reply = MagicMock()
    mock_reply.text = "Локальная покраска бампера Camry от 8 000 руб."
    mock_reply.lead = None
    llm.generate_reply.return_value = mock_reply

    notifier = MagicMock()
    notifier.notify_demo_started_platform = AsyncMock()
    notifier.notify_demo_started = AsyncMock()
    notifier.notify_saas_trial_request = AsyncMock()

    router = build_telegram_router(
        settings=settings,
        company_store=store,
        storage=storage,
        llm=llm,
        notifier=notifier,
        tenant_slug=tenant_slug,
        admin_ids=[],
    )

    msg_handlers = {h.callback.__name__: h.callback for h in router.message.handlers}
    cb_handlers = {h.callback.__name__: h.callback for h in router.callback_query.handlers}

    return {
        "router": router,
        "msg_handlers": msg_handlers,
        "cb_handlers": cb_handlers,
        "storage": storage,
        "notifier": notifier,
        "llm": llm,
    }


@pytest.mark.asyncio
async def test_cmd_start_with_demo_param(tmp_path: Path):
    """User lands from the website button: /start demo -> platform welcome menu."""
    ctx = _setup_test_router(tmp_path)
    cmd_start = ctx["msg_handlers"]["cmd_start"]

    mock_msg = AsyncMock()
    mock_msg.chat.id = 555111
    mock_msg.from_user.id = 555111
    mock_msg.from_user.full_name = "Алексей Смирнов"
    mock_msg.from_user.username = "smirnov_auto"

    command = CommandObject(command="start", args="demo")
    await cmd_start(mock_msg, command)

    # 1. Answer called with welcome text
    assert mock_msg.answer.called
    ans_text = mock_msg.answer.call_args[0][0]
    assert "Добро пожаловать на демо-стенд платформы" in ans_text
    assert "Запустить" in ans_text
    reply_markup = mock_msg.answer.call_args[1].get("reply_markup")
    assert isinstance(reply_markup, InlineKeyboardMarkup)

    # 2. Session stored as demo
    assert ctx["storage"].get_session_company("telegram", "555111") == "demo"
    assert ctx["notifier"].notify_demo_started_platform.called


@pytest.mark.asyncio
async def test_cmd_start_bare_on_platform_bot(tmp_path: Path):
    """User finds @Zayavka24AI_bot directly (no args) -> routes to platform welcome, NOT car polish!"""
    ctx = _setup_test_router(tmp_path, tenant_slug=None)
    cmd_start = ctx["msg_handlers"]["cmd_start"]

    mock_msg = AsyncMock()
    mock_msg.chat.id = 777222
    mock_msg.from_user.id = 777222
    mock_msg.from_user.full_name = "Михаил Директор"
    mock_msg.from_user.username = "mikhail_sto"

    command = CommandObject(command="start", args="")
    await cmd_start(mock_msg, command)

    assert mock_msg.answer.called
    ans_text = mock_msg.answer.call_args[0][0]
    assert "Добро пожаловать на демо-стенд платформы" in ans_text
    # Should NOT greet as B2C detailing customer
    assert "Привет из студии Премиум Детейлинг" not in ans_text
    assert ctx["storage"].get_session_company("telegram", "777222") == "demo"


@pytest.mark.asyncio
async def test_demo_callbacks_menu_test_pricing_contact(tmp_path: Path):
    """Test all navigation callback buttons in the demo funnel."""
    ctx = _setup_test_router(tmp_path)
    handle_demo_callback = ctx["cb_handlers"]["handle_demo_callback"]

    # 1. demo:menu
    cb_menu = AsyncMock()
    cb_menu.message.chat.id = 1001
    cb_menu.data = "demo:menu"
    await handle_demo_callback(cb_menu)
    assert "Главное меню платформы" in cb_menu.message.answer.call_args[0][0]

    # 2. demo:test
    cb_test = AsyncMock()
    cb_test.message.chat.id = 1001
    cb_test.data = "demo:test"
    await handle_demo_callback(cb_test)
    assert "Режим тестирования как клиент" in cb_test.message.answer.call_args[0][0]

    # 3. demo:pricing
    cb_pricing = AsyncMock()
    cb_pricing.message.chat.id = 1001
    cb_pricing.data = "demo:pricing"
    await handle_demo_callback(cb_pricing)
    ans_pricing = cb_pricing.message.answer.call_args[0][0]
    assert "Тарифы платформы" in ans_pricing
    assert "12 900" in ans_pricing
    assert "24 900" in ans_pricing
    assert "Окупаемость" in ans_pricing

    # 4. demo:contact
    cb_contact = AsyncMock()
    cb_contact.message.chat.id = 1001
    cb_contact.data = "demo:contact"
    await handle_demo_callback(cb_contact)
    ans_contact = cb_contact.message.answer.call_args[0][0]
    assert "Денис, основатель платформы" in ans_contact
    assert "@Zayavka24AI" in ans_contact
    contact_markup = cb_contact.message.answer.call_args[1].get("reply_markup")
    assert any(btn.url == "https://t.me/Zayavka24AI" for row in contact_markup.inline_keyboard for btn in row)

    # 5. demo:q1 (Camry bumper query)
    cb_q1 = AsyncMock()
    cb_q1.message.chat.id = 1001
    cb_q1.data = "demo:q1"
    await handle_demo_callback(cb_q1)
    ans_q1 = cb_q1.message.answer.call_args[0][0]
    assert "Сколько стоит покрасить передний бампер на Toyota Camry?" in ans_q1
    assert "Бот-ассистент" in ans_q1
    assert ctx["llm"].generate_reply.called


@pytest.mark.asyncio
async def test_saas_trial_wizard_full_flow(tmp_path: Path):
    """Test 4-step SaaS onboarding wizard for a new auto service client."""
    ctx = _setup_test_router(tmp_path)
    handle_demo_callback = ctx["cb_handlers"]["handle_demo_callback"]
    handle_text = ctx["msg_handlers"]["handle_text"]
    mock_bot = AsyncMock()

    chat_id = 90011

    # Step 0: click "demo:trial"
    cb_trial = AsyncMock()
    cb_trial.message.chat.id = chat_id
    cb_trial.data = "demo:trial"
    await handle_demo_callback(cb_trial)
    assert "Шаг 1 из 4" in cb_trial.message.answer.call_args[0][0]

    # Step 1: User enters city & company name
    msg1 = AsyncMock()
    msg1.chat.id = chat_id
    msg1.text = "Екатеринбург, Авто-Бокс 96"
    msg1.from_user.id = chat_id
    msg1.from_user.full_name = "Дмитрий"
    msg1.from_user.username = "dmitry_ekb"
    await handle_text(msg1, mock_bot)
    assert "Шаг 2 из 4" in msg1.answer.call_args[0][0]

    # Step 2: User chooses niche (1 = body repair)
    msg2 = AsyncMock()
    msg2.chat.id = chat_id
    msg2.text = "1"
    msg2.from_user.id = chat_id
    msg2.from_user.full_name = "Дмитрий"
    await handle_text(msg2, mock_bot)
    assert "Шаг 3 из 4" in msg2.answer.call_args[0][0]

    # Step 3: User sends price list hint / Avito link
    msg3 = AsyncMock()
    msg3.chat.id = chat_id
    msg3.text = "Покраска детали от 10 000 руб, рихтовка от 3 000 руб"
    msg3.from_user.id = chat_id
    msg3.from_user.full_name = "Дмитрий"
    await handle_text(msg3, mock_bot)
    assert "Шаг 4 из 4" in msg3.answer.call_args[0][0]

    # Step 4: User provides phone number
    msg4 = AsyncMock()
    msg4.chat.id = chat_id
    msg4.text = "+7 (922) 555-44-33"
    msg4.from_user.id = chat_id
    msg4.from_user.full_name = "Дмитрий"
    msg4.from_user.username = "dmitry_ekb"
    await handle_text(msg4, mock_bot)

    # Verification: Wizard finished, confirmation sent, notifier triggered
    ans_final = msg4.answer.call_args[0][0]
    assert "Заявка принята!" in ans_final
    assert "3 дня бесплатно" in ans_final

    ctx["notifier"].notify_saas_trial_request.assert_called_once_with(
        user_label="Дмитрий @dmitry_ekb",
        chat_id=str(chat_id),
        city_name="Екатеринбург, Авто-Бокс 96",
        niche="Кузовной ремонт и покраска",
        price_hint="Покраска детали от 10 000 руб, рихтовка от 3 000 руб",
        owner_phone="+7 (922) 555-44-33",
    )


@pytest.mark.asyncio
async def test_saas_wizard_cancellation_by_word_and_command(tmp_path: Path):
    """Test cancellation of SaaS wizard via text ('отмена') and /cancel command."""
    ctx = _setup_test_router(tmp_path)
    handle_demo_callback = ctx["cb_handlers"]["handle_demo_callback"]
    handle_text = ctx["msg_handlers"]["handle_text"]
    cmd_cancel = ctx["msg_handlers"]["cmd_cancel"]
    mock_bot = AsyncMock()

    chat_id = 90022

    # 1. Cancel via word "отмена"
    cb_trial = AsyncMock()
    cb_trial.message.chat.id = chat_id
    cb_trial.data = "demo:trial"
    await handle_demo_callback(cb_trial)

    msg_cancel_word = AsyncMock()
    msg_cancel_word.chat.id = chat_id
    msg_cancel_word.text = "отмена"
    msg_cancel_word.from_user.id = chat_id
    await handle_text(msg_cancel_word, mock_bot)
    assert "Заполнение заявки отменено" in msg_cancel_word.answer.call_args[0][0]

    # 2. Cancel via command /cancel
    await handle_demo_callback(cb_trial)
    msg_cancel_cmd = AsyncMock()
    msg_cancel_cmd.chat.id = chat_id
    await cmd_cancel(msg_cancel_cmd)
    assert "Заполнение заявки отменено" in msg_cancel_cmd.answer.call_args[0][0]


@pytest.mark.asyncio
async def test_wow_effect_presentation(tmp_path: Path):
    """Test WOW effect lead card formatting when LLM detects a customer phone."""
    ctx = _setup_test_router(tmp_path)
    handle_demo_callback = ctx["cb_handlers"]["handle_demo_callback"]

    # Configure LLM to return a lead
    lead = LeadInfo(
        phone="+79991112233",
        client_name="Константин",
        car_model="Toyota Camry",
        service_interest="Покраска бампера",
        estimated_price="8 000–12 000 ₽",
        company_slug="demo_detailing",
        channel="telegram",
        chat_id="3003",
    )
    mock_reply = MagicMock()
    mock_reply.text = "Записал вас на покраску бампера!"
    mock_reply.lead = lead
    ctx["llm"].generate_reply.return_value = mock_reply

    cb_q = AsyncMock()
    cb_q.message.chat.id = 3003
    cb_q.data = "demo:q1"
    await handle_demo_callback(cb_q)

    # Second answer call is the WOW card
    answers = [call[0][0] for call in cb_q.message.answer.call_args_list]
    wow_card = next((a for a in answers if "ВОТ ТАК ЭТО РАБОТАЕТ" in a), None)
    assert wow_card is not None
    assert "Toyota Camry" in wow_card
    assert "Покраска бампера" in wow_card
    assert "+79991112233" in wow_card
    assert "Новый лид" in wow_card
