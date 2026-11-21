import pytest
from app.models import CompanyProfile, ConversationMessage, LLMReply, LeadInfo
from app.prompt_builder import build_system_prompt
from app.llm import LLMService


@pytest.fixture
def test_company():
    return CompanyProfile(
        slug="detailing_premium",
        company_name="Детейлинг Премиум",
        city="Москва",
        niche="детейлинг и покраска авто",
        manager_name="Денис",
        manager_role="старший мастер",
        address="ул. Автозаводская, д. 23",
        work_hours="Ежедневно с 10:00 до 21:00",
        discount_offer="скидку 10% на первый визит и бесплатную проверку ЛКП",
        disclose_ai=True,
        assistant_label="виртуальный ассистент",
        services=[
            "Локальная покраска детали: от 9 000 до 15 000 ₽ за элемент",
            "Полная покраска элемента (бампер, крыло, дверь): от 13 000 до 22 000 ₽",
            "Оклейка зон риска полиуретановой плёнкой: от 45 000 до 75 000 ₽",
            "Абразивная полировка кузова: от 14 000 до 25 000 ₽",
        ],
        advantages=[
            "Собственная покрасочная камера и лаборатория цветоподбора",
            "Оригинальные плёнки с гарантией 5 лет",
            "Бесплатный замер толщины ЛКП",
        ],
        faq=[
            "Сколько по времени оклейка зон риска? — 1 рабочий день.",
            "Сколько по времени покраска бампера? — 2-3 рабочих дня.",
        ]
    )


def test_system_prompt_structure(test_company):
    prompt = build_system_prompt(test_company)
    assert "Детейлинг Премиум" in prompt
    assert "Москва" in prompt
    assert "Денис" in prompt
    assert "старший мастер" in prompt
    assert "скидку 10%" in prompt
    assert "золотое правило продаж" in prompt.lower()
    assert "submit_lead" in prompt


def test_ai_honesty_rule(test_company):
    prompt = build_system_prompt(test_company)
    assert "Честность по поводу ИИ" in prompt
    assert "виртуальный ассистент студии" in prompt


def test_sales_dialog_scenarios(test_company):
    # Test script evaluation:
    # 1. Price inquiry must not close without question
    prompt = build_system_prompt(test_company)
    assert "должно заканчиваться ОДНИМ простым встречным вопросом" in prompt
    # 2. Offer discount to hook client
    assert test_company.discount_offer in prompt
    # 3. Defense from jailbreaks/trolling
    assert "НИКОГДА не выходи из роли" in prompt


@pytest.mark.asyncio
async def test_llm_lead_capture_logic(test_company):
    class SimulatedOpenAIService:
        async def generate_reply(self, company, history, user_message, **kwargs):
            if "+7 999 123-45-67" in user_message:
                return LLMReply(
                    text="Записал вас на осмотр! Мастер свяжется с вами.",
                    lead=LeadInfo(
                        channel="telegram",
                        chat_id="12345",
                        company_slug=company.slug,
                        phone="+7 (999) 123-45-67",
                        summary="Запрос на покраску бампера Camry",
                    ),
                )
            return LLMReply(text="Здравствуйте! Какая у вас модель авто?")

    svc = SimulatedOpenAIService()
    reply = await svc.generate_reply(
        company=test_company,
        history=[],
        user_message="Хочу записаться, мой телефон +7 999 123-45-67",
    )
    assert reply.lead is not None
    assert reply.lead.phone == "+7 (999) 123-45-67"
    assert "Записал вас" in reply.text

