import asyncio
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest
from starlette.testclient import TestClient

from app.auth import AuthService, hash_password
from app.company_store import CompanyStore
from app.config import Settings
from app.llm import LLMService
from app.models import CompanyProfile, ConversationMessage
from app.quotas import QuotaService
from app.server import create_app
from app.storage import Storage


@pytest.mark.asyncio
async def test_usage_recorded_after_llm_call(tmp_path: Path):
    db_file = tmp_path / "test.db"
    storage = Storage(db_file)

    company = CompanyProfile(
        slug="test_comp",
        company_name="Авто Тест",
        city="Москва",
        tenant_slug="client_alpha",
    )

    fake_client = MagicMock()
    fake_choice = MagicMock()
    fake_choice.message.content = "Здравствуйте! Чем могу помочь?"
    fake_choice.message.tool_calls = None

    fake_usage = MagicMock()
    fake_usage.prompt_tokens = 450
    fake_usage.completion_tokens = 120
    fake_usage.total_tokens = 570

    fake_resp = MagicMock()
    fake_resp.choices = [fake_choice]
    fake_resp.usage = fake_usage

    fake_client.chat.completions.create = AsyncMock(return_value=fake_resp)

    llm = LLMService(
        api_key="sk-fake",
        base_url="https://api.openai.com/v1",
        model="gpt-4o-mini",
        storage=storage,
        client=fake_client,
    )

    reply = await llm.generate_reply(
        company=company,
        history=[],
        user_message="Сколько стоит полировка?",
        channel="telegram",
        chat_id="chat_100",
    )

    assert reply.text == "Здравствуйте! Чем могу помочь?"

    summary = storage.get_usage_summary(tenant_slug="client_alpha", days=30)
    assert summary["messages"] == 1
    assert summary["prompt_tokens"] == 450
    assert summary["completion_tokens"] == 120
    assert summary["total_tokens"] == 570
    assert len(summary["by_model"]) == 1
    assert summary["by_model"][0]["model"] == "gpt-4o-mini"


def test_cost_calculation(tmp_path: Path):
    db_file = tmp_path / "test.db"
    storage = Storage(db_file)

    # Устанавливаем цену: 0.10 ₽ за 1k prompt, 0.30 ₽ за 1k completion
    storage.save_model_price("gpt-4o-mini", prompt_rub_per_1k=0.10, completion_rub_per_1k=0.30)
    p_price, c_price = storage.get_model_price("gpt-4o-mini")
    assert p_price == 0.10
    assert c_price == 0.30

    prompt_tokens = 1000
    completion_tokens = 500
    cost = round((prompt_tokens / 1000.0) * p_price + (completion_tokens / 1000.0) * c_price, 4)
    # 1.0 * 0.10 + 0.5 * 0.30 = 0.10 + 0.15 = 0.25 руб.
    assert cost == 0.25

    # Записываем расход в базу и проверяем сводку
    storage.record_llm_usage(
        tenant_slug="tenant_x",
        company_slug="comp_x",
        channel="telegram",
        chat_id="chat_1",
        model="gpt-4o-mini",
        prompt_tokens=prompt_tokens,
        completion_tokens=completion_tokens,
        total_tokens=1500,
        cost_rub=cost,
    )

    summary = storage.get_usage_summary(tenant_slug="tenant_x")
    assert summary["cost_rub"] == 0.25
    assert summary["total_tokens"] == 1500


def test_quota_blocks_over_limit(tmp_path: Path):
    db_file = tmp_path / "test.db"
    storage = Storage(db_file)

    # 1. Создаем тенанта с лимитом 2 сообщения
    storage.save_plan(
        slug="test_plan_2",
        name="Лимит 2 сообщения",
        monthly_rub=500,
        included_messages=2,
        included_tokens=None,
    )
    storage.save_tenant(slug="limited_client", name="Ограниченный клиент", plan="test_plan_2")

    quota = QuotaService(storage)

    # Начальное состояние: 0 сообщений
    st1 = quota.check("limited_client")
    assert st1.allowed is True
    assert st1.used_messages == 0
    assert st1.soft_limit is False

    # Записываем 1 сообщение
    storage.record_llm_usage(
        tenant_slug="limited_client",
        company_slug="comp",
        channel="telegram",
        chat_id="1",
        model="gpt-4o-mini",
        prompt_tokens=100,
        completion_tokens=50,
        total_tokens=150,
        cost_rub=0.01,
    )

    st2 = quota.check("limited_client")
    assert st2.allowed is True
    assert st2.used_messages == 1

    # Записываем 2-е сообщение
    storage.record_llm_usage(
        tenant_slug="limited_client",
        company_slug="comp",
        channel="telegram",
        chat_id="2",
        model="gpt-4o-mini",
        prompt_tokens=100,
        completion_tokens=50,
        total_tokens=150,
        cost_rub=0.01,
    )

    st3 = quota.check("limited_client")
    # Достигнут лимит 2/2 -> allowed = False
    assert st3.allowed is False
    assert st3.used_messages == 2
    assert "Превышен лимит сообщений" in st3.reason


def test_quota_ignores_platform_admin(tmp_path: Path):
    db_file = tmp_path / "test.db"
    storage = Storage(db_file)

    storage.save_tenant(slug="internal_client", name="Внутренний клиент", plan="internal")
    quota = QuotaService(storage)

    # Записываем 100 сообщений
    for i in range(10):
        storage.record_llm_usage(
            tenant_slug="internal_client",
            company_slug="comp",
            channel="telegram",
            chat_id=str(i),
            model="gpt-4o-mini",
            prompt_tokens=100,
            completion_tokens=50,
            total_tokens=150,
            cost_rub=0.01,
        )

    st = quota.check("internal_client")
    assert st.allowed is True
    assert st.limit_messages is None

    # Проверка для платформенного администратора
    st_admin = quota.check("platform_admin")
    assert st_admin.allowed is True
    assert st_admin.limit_messages is None


def test_usage_api_scoped(tmp_path: Path):
    db_file = tmp_path / "test.db"
    storage = Storage(db_file)
    companies_dir = tmp_path / "companies"
    companies_dir.mkdir(parents=True)
    company_store = CompanyStore(companies_dir)

    settings = Settings(db_path=db_file, companies_dir=companies_dir)
    auth = AuthService(settings, storage)

    # Создаём тенантов
    storage.save_tenant(slug="tenant_a", name="Клиент А")
    storage.save_tenant(slug="tenant_b", name="Клиент Б")

    # Создаём пользователей
    storage.save_user("user_a", hash_password("pass_user_a_123"), role="client_admin", tenant_slug="tenant_a")
    storage.save_user("user_b", hash_password("pass_user_b_123"), role="client_admin", tenant_slug="tenant_b")
    storage.save_user("superadmin", hash_password("pass_superadmin_123"), role="platform_admin", tenant_slug=None)

    # Создаём сессии
    token_a = "token_a_123"
    token_b = "token_b_456"
    token_admin = "token_admin_789"
    storage.create_user_session(token_a, storage.get_user_by_username("user_a")["id"])
    storage.create_user_session(token_b, storage.get_user_by_username("user_b")["id"])
    storage.create_user_session(token_admin, storage.get_user_by_username("superadmin")["id"])

    # Пишем расходы для А и Б
    storage.record_llm_usage(
        tenant_slug="tenant_a",
        company_slug="comp_a",
        channel="telegram",
        chat_id="1",
        model="gpt-4o-mini",
        prompt_tokens=1000,
        completion_tokens=200,
        total_tokens=1200,
        cost_rub=10.50,
    )
    storage.record_llm_usage(
        tenant_slug="tenant_b",
        company_slug="comp_b",
        channel="telegram",
        chat_id="2",
        model="gpt-4o-mini",
        prompt_tokens=2000,
        completion_tokens=400,
        total_tokens=2400,
        cost_rub=25.00,
    )

    app = create_app(
        settings=settings,
        company_store=company_store,
        storage=storage,
        auth=auth,
    )
    client = TestClient(app)

    # 1. Запрос от клиента А: видит только 10.50 ₽ и 1200 токенов
    client.cookies.set("zayavka_session", token_a)
    resp_a = client.get("/api/usage")
    assert resp_a.status_code == 200
    data_a = resp_a.json()
    assert data_a["messages"] == 1
    assert data_a["total_tokens"] == 1200
    assert data_a["cost_rub"] == 10.50

    # 2. Запрос от клиента Б: видит только 25.00 ₽ и 2400 токенов
    client.cookies.set("zayavka_session", token_b)
    resp_b = client.get("/api/usage")
    assert resp_b.status_code == 200
    data_b = resp_b.json()
    assert data_b["messages"] == 1
    assert data_b["total_tokens"] == 2400
    assert data_b["cost_rub"] == 25.00

    # 3. Клиент А не имеет доступа к сводке по всем тенантам
    client.cookies.set("zayavka_session", token_a)
    resp_forbidden = client.get("/api/usage/tenants")
    assert resp_forbidden.status_code == 403

    # 4. Платформенный администратор видит сводку по всем тенантам
    client.cookies.set("zayavka_session", token_admin)
    resp_all = client.get("/api/usage/tenants")
    assert resp_all.status_code == 200
    data_all = resp_all.json()
    assert len(data_all) >= 2
    slugs = [t["tenant_slug"] for t in data_all]
    assert "tenant_a" in slugs
    assert "tenant_b" in slugs
