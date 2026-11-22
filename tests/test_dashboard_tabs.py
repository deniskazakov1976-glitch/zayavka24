import re
from pathlib import Path
import pytest
from starlette.testclient import TestClient

from app.auth import (
    ROLE_CLIENT_ADMIN,
    ROLE_CLIENT_VIEWER,
    ROLE_PLATFORM_ADMIN,
    AuthService,
    hash_password,
)
from app.company_store import CompanyStore
from app.config import Settings
from app.models import KnowledgeGap, LeadInfo
from app.server import create_app
from app.storage import Storage


@pytest.fixture
def dashboard_env(tmp_path: Path):
    companies_dir = tmp_path / "companies"
    companies_dir.mkdir(parents=True, exist_ok=True)
    (companies_dir / "detailing_co.yaml").write_text(
        """slug: detailing_co
company_name: "Премиум Детейлинг"
city: "Москва"
niche: "детейлинг"
manager_name: "Денис"
manager_role: "старший мастер"
tenant_slug: default
services:
  - "Полировка кузова: от 15 000 ₽"
  - "Оклейка полиуретаном: от 50 000 ₽"
""",
        encoding="utf-8",
    )

    settings = Settings(
        companies_dir=companies_dir,
        db_path=tmp_path / "bot_test.db",
        dashboard_enabled=True,
        dashboard_password="admin-secret-pwd",
        dashboard_user="admin",
        telegram_bot_token="123456:TEST_TOKEN",
        admin_ids=[123456],
        default_notify_chat_id=123456,
    )
    storage = Storage(settings.db_path)
    store = CompanyStore(companies_dir)
    auth = AuthService(settings, storage)
    auth.bootstrap()

    # Prepopulate database with leads, sessions, gaps, and guard events
    storage.save_lead(
        LeadInfo(
            channel="telegram",
            chat_id="tg_101",
            company_slug="detailing_co",
            phone="+79991234567",
            client_name="Виктор",
            car_model="Toyota Camry",
            service_interest="Оклейка зон риска",
            estimated_price="от 50 000 ₽",
        )
    )

    storage.upsert_session("telegram", "tg_101", "detailing_co")
    storage.add_message("telegram", "tg_101", "user", "Сколько стоит оклейка?")
    storage.add_message("telegram", "tg_101", "assistant", "Оклейка от 50 000 ₽")

    storage.save_knowledge_gap(
        KnowledgeGap(
            channel="telegram",
            chat_id="tg_101",
            company_slug="detailing_co",
            question="Делаете ли вы PDR удаление вмятин?",
            customer_message="У меня вмятина на крыле, выпрямляете без покраски?",
        )
    )

    storage.record_guard_event(
        channel="telegram",
        chat_id="tg_101",
        company_slug="detailing_co",
        original_reply="Сделаем за 500 рублей",
        safe_reply="Точную стоимость уточню у мастера",
        blocked_terms="500",
    )

    # Save a second tenant to verify tenant management
    storage.save_tenant(
        slug="auto-pro",
        name="АвтоПро",
        telegram_bot_token="999888:BOT_TOKEN",
        owner_telegram_id="998877",
    )

    app = create_app(
        settings=settings,
        company_store=store,
        storage=storage,
        get_bot_username=lambda: "Zayavka24AI_bot",
        auth=auth,
    )

    return {
        "settings": settings,
        "storage": storage,
        "store": store,
        "auth": auth,
        "app": app,
    }


def login_client(client: TestClient, username: str = "admin", password: str = "admin-secret-pwd") -> None:
    resp = client.post("/login", data={"username": username, "password": password}, follow_redirects=False)
    assert resp.status_code == 303, f"Login failed: {resp.status_code} {resp.text}"


# ==============================================================================
# TEST 1: All 9 Tab Navigation Buttons & Badges (Microcopy & Geometry verification)
# ==============================================================================
def test_all_9_tab_buttons_markup(dashboard_env):
    """Проверяет наличие всех 9 кнопок закладок в панели навигации, их компактные названия и бейджи."""
    client = TestClient(dashboard_env["app"])
    login_client(client)
    resp = client.get("/")
    assert resp.status_code == 200
    html = resp.text

    # Extract all tab-btn full button tags
    btn_matches = re.findall(r'<button[^>]*class="[^"]*tab-btn[^"]*"[^>]*>.*?</button>', html, re.DOTALL)
    assert len(btn_matches) == 9, f"Expected 9 tab buttons, found {len(btn_matches)}"

    # 1. Заявки (было 'Лиды и заявки')
    assert 'onclick="switchTab(\'leads\')"' in html
    assert 'id="count-leads"' in html
    btn_leads = [b for b in btn_matches if "switchTab('leads')" in b][0]
    assert "Заявки" in btn_leads

    # 2. Диалоги (было 'Активные диалоги')
    assert 'onclick="switchTab(\'sessions\')"' in html
    assert 'id="count-sessions"' in html
    btn_sessions = [b for b in btn_matches if "switchTab('sessions')" in b][0]
    assert "Диалоги" in btn_sessions

    # 3. База знаний (было 'База самообучения')
    assert 'onclick="switchTab(\'gaps\')"' in html
    assert 'id="count-gaps"' in html
    btn_gaps = [b for b in btn_matches if "switchTab('gaps')" in b][0]
    assert "База знаний" in btn_gaps

    # 4. Компании (было 'Компании и ссылки')
    assert 'onclick="switchTab(\'companies\')"' in html
    btn_companies = [b for b in btn_matches if "switchTab('companies')" in b][0]
    assert "Компании" in btn_companies

    # 5. + Компания (было 'Добавить компанию')
    assert 'onclick="switchTab(\'create\')"' in html
    btn_create = [b for b in btn_matches if "switchTab('create')" in b][0]
    assert "+ Компания" in btn_create

    # 6. Авито Pro
    assert 'onclick="switchTab(\'avito\')"' in html
    btn_avito = [b for b in btn_matches if "switchTab('avito')" in b][0]
    assert "Авито Pro" in btn_avito

    # 7. Защита цен
    assert 'onclick="switchTab(\'guard\')"' in html
    btn_guard = [b for b in btn_matches if "switchTab('guard')" in b][0]
    assert "Защита цен" in btn_guard

    # 8. Расходы
    assert 'id="tab-usage-btn"' in html
    assert 'onclick="switchTab(\'usage\')"' in html
    btn_usage = [b for b in btn_matches if "tab-usage-btn" in b or "switchTab('usage')" in b][0]
    assert "Расходы" in btn_usage

    # 9. Клиенты и боты (для платформенного администратора)
    assert 'id="tab-tenants-btn"' in html
    assert 'onclick="switchTab(\'tenants\')"' in html
    assert 'id="count-tenants"' in html
    btn_tenants = [b for b in btn_matches if "tab-tenants-btn" in b or "switchTab('tenants')" in b][0]
    assert "Клиенты и боты" in btn_tenants


# ==============================================================================
# TEST 2: All 9 Panels Presence and Required Content Containers
# ==============================================================================
def test_all_9_panels_content_structure(dashboard_env):
    """Проверяет наличие и целостность контейнеров и таблиц всех 9 панелей."""
    client = TestClient(dashboard_env["app"])
    login_client(client)
    resp = client.get("/")
    assert resp.status_code == 200
    html = resp.text

    # Panel 1: Leads
    assert 'id="panel-leads"' in html
    assert 'id="table-leads-body"' in html

    # Panel 2: Sessions
    assert 'id="panel-sessions"' in html
    assert 'id="table-sessions-body"' in html

    # Panel 3: Knowledge Gaps (База знаний)
    assert 'id="panel-gaps"' in html
    assert 'id="table-gaps-body"' in html

    # Panel 4: Companies
    assert 'id="panel-companies"' in html
    assert 'id="table-companies-body"' in html

    # Panel 5: Create Company
    assert 'id="panel-create"' in html
    assert 'id="create-company-form"' in html
    assert 'id="fc-name"' in html
    assert 'id="fc-slug"' in html
    assert 'id="fc-city"' in html
    assert 'id="fc-services"' in html

    # Panel 6: Avito Pro
    assert 'id="panel-avito"' in html
    assert 'id="avito-client-id"' in html
    assert 'id="avito-client-secret"' in html
    assert 'id="avito-user-id"' in html
    assert 'id="avito-webhook-url"' in html

    # Panel 7: Guardrails
    assert 'id="panel-guard"' in html
    assert 'id="table-guard-body"' in html

    # Panel 8: Usage & Expenses
    assert 'id="panel-usage"' in html
    assert 'id="table-usage-models-body"' in html
    assert 'id="table-usage-tenants-body"' in html
    assert 'id="table-model-prices-body"' in html

    # Panel 9: Tenants & Bots
    assert 'id="panel-tenants"' in html
    assert 'id="table-tenants-body"' in html
    assert 'id="form-tenant"' in html


# ==============================================================================
# TEST 3: TAB 1 (Заявки) API & Data Verification
# ==============================================================================
def test_tab_1_leads_content_and_api(dashboard_env):
    client = TestClient(dashboard_env["app"])
    login_client(client)
    resp = client.get("/api/leads")
    assert resp.status_code == 200
    leads = resp.json()
    assert len(leads) >= 1
    lead = leads[0]
    assert lead["phone"] == "+79991234567"
    assert lead["car_model"] == "Toyota Camry"
    assert lead["service_interest"] == "Оклейка зон риска"
    assert lead["company_slug"] == "detailing_co"


# ==============================================================================
# TEST 4: TAB 2 (Диалоги) API & Data Verification
# ==============================================================================
def test_tab_2_sessions_content_and_api(dashboard_env):
    client = TestClient(dashboard_env["app"])
    login_client(client)
    resp = client.get("/api/sessions")
    assert resp.status_code == 200
    sessions = resp.json()
    assert len(sessions) >= 1
    session = sessions[0]
    assert session["channel"] == "telegram"
    assert session["chat_id"] == "tg_101"
    assert session["company_slug"] == "detailing_co"
    assert session["is_paused"] is False

    # Check history sub-route for this session
    resp_hist = client.get(f"/api/sessions/{session['channel']}/{session['chat_id']}/history")
    assert resp_hist.status_code == 200
    messages = resp_hist.json()
    assert len(messages) == 2
    assert messages[0]["content"] == "Сколько стоит оклейка?"


# ==============================================================================
# TEST 5: TAB 3 (База знаний / Обучение) API & Data Verification
# ==============================================================================
def test_tab_3_gaps_content_and_api(dashboard_env):
    client = TestClient(dashboard_env["app"])
    login_client(client)
    resp = client.get("/api/gaps")
    assert resp.status_code == 200
    gaps = resp.json()
    assert len(gaps) >= 1
    gap = gaps[0]
    assert "PDR" in gap["question"]
    assert gap["status"] == "pending"

    # Test learn gap flow
    resp_learn = client.post(
        f"/api/gaps/{gap['id']}/learn",
        json={"answer": "Да, делаем PDR выпрямление от 3 000 ₽ за элемент."},
    )
    assert resp_learn.status_code == 200
    assert resp_learn.json()["status"] == "ok"


# ==============================================================================
# TEST 6: TAB 4 (Компании) API & Data Verification
# ==============================================================================
def test_tab_4_companies_content_and_api(dashboard_env):
    client = TestClient(dashboard_env["app"])
    login_client(client)
    resp = client.get("/api/companies")
    assert resp.status_code == 200
    companies = resp.json()
    assert len(companies) >= 1
    comp = [c for c in companies if c["slug"] == "detailing_co"][0]
    assert comp["company_name"] == "Премиум Детейлинг"
    assert comp["city"] == "Москва"
    assert "Zayavka24AI_bot" in comp["demo_link"]


# ==============================================================================
# TEST 7: TAB 5 (+ Компания) API Creation Verification
# ==============================================================================
def test_tab_5_create_company_api(dashboard_env):
    client = TestClient(dashboard_env["app"])
    login_client(client)
    new_company_payload = {
        "slug": "bodyshop_spb",
        "company_name": "Кузовной СПБ",
        "city": "Санкт-Петербург",
        "niche": "кузовной ремонт",
        "manager_name": "Игорь",
        "manager_role": "мастер-колорист",
        "services": [
            "Локальный окрас: от 8 000 ₽",
            "Стапель и геометрия: от 20 000 ₽",
        ],
        "work_hours": "Пн-Сб с 9:00 до 20:00",
        "discount_offer": "скидка 10% на кузовные работы",
    }
    resp = client.post("/api/companies", json=new_company_payload)
    assert resp.status_code == 200
    data = resp.json()
    assert data["status"] == "ok"
    assert data["slug"] == "bodyshop_spb"

    # Verify company is now listable
    resp_list = client.get("/api/companies")
    slugs = [c["slug"] for c in resp_list.json()]
    assert "bodyshop_spb" in slugs


# ==============================================================================
# TEST 8: TAB 6 (Авито Pro) Config API Verification
# ==============================================================================
def test_tab_6_avito_config_and_accounts_api(dashboard_env):
    client = TestClient(dashboard_env["app"])
    login_client(client)
    resp = client.get("/api/avito/config")
    assert resp.status_code == 200
    cfg = resp.json()
    assert "avito_enabled" in cfg
    assert "webhook_path" in cfg

    resp_accs = client.get("/api/avito/accounts")
    assert resp_accs.status_code == 200
    assert isinstance(resp_accs.json(), list)


# ==============================================================================
# TEST 9: TAB 7 (Защита цен) Guard Events API Verification
# ==============================================================================
def test_tab_7_guard_events_api(dashboard_env):
    client = TestClient(dashboard_env["app"])
    login_client(client)
    resp = client.get("/api/guard-events")
    assert resp.status_code == 200
    events = resp.json()
    assert len(events) >= 1
    ev = events[0]
    assert ev["channel"] == "telegram"
    assert "500" in ev["original_reply"]
    assert "Точную стоимость" in ev["safe_reply"]


# ==============================================================================
# TEST 10: TAB 8 (Расходы) Usage Summary & Model Prices API Verification
# ==============================================================================
def test_tab_8_usage_and_prices_api(dashboard_env):
    client = TestClient(dashboard_env["app"])
    login_client(client)

    # 1. Usage summary
    resp_usage = client.get("/api/usage?days=30")
    assert resp_usage.status_code == 200
    usage_data = resp_usage.json()
    assert "messages" in usage_data
    assert "total_tokens" in usage_data
    assert "cost_rub" in usage_data

    # 2. Tenants usage breakdown
    resp_tenants_usage = client.get("/api/usage/tenants?days=30")
    assert resp_tenants_usage.status_code == 200
    assert isinstance(resp_tenants_usage.json(), list)

    # 3. Model prices
    resp_prices = client.get("/api/model-prices")
    assert resp_prices.status_code == 200
    assert isinstance(resp_prices.json(), list)


# ==============================================================================
# TEST 11: TAB 9 (Клиенты и боты) Tenants API Verification
# ==============================================================================
def test_tab_9_tenants_and_bots_api(dashboard_env):
    client = TestClient(dashboard_env["app"])
    login_client(client)

    resp = client.get("/api/tenants")
    assert resp.status_code == 200
    tenants = resp.json()
    assert len(tenants) >= 1
    tenant_slugs = [t["slug"] for t in tenants]
    assert "auto-pro" in tenant_slugs

    # Create new tenant via API
    resp_create = client.post(
        "/api/tenants",
        json={
            "slug": "tuning-kzn",
            "name": "Тюнинг Казань",
            "telegram_bot_token": "555444:NEW_BOT_TOKEN",
            "owner_telegram_id": "776655",
            "admin_username": "tuning_admin",
            "admin_password": "tuning_password_123",
        },
    )
    assert resp_create.status_code == 200
    assert resp_create.json()["status"] == "ok"


# ==============================================================================
# TEST 12: JavaScript switchTab Client Routing Function Verification
# ==============================================================================
def test_switch_tab_js_function_integrity(dashboard_env):
    """Проверяет целостность JavaScript-функции switchTab и наличие переходов по всем 9 вкладкам."""
    client = TestClient(dashboard_env["app"])
    login_client(client)
    resp = client.get("/")
    assert resp.status_code == 200
    html = resp.text

    # Verify switchTab function is defined
    assert "function switchTab(name)" in html

    # Verify each of the 9 tabs has its loader triggered inside switchTab
    expected_handlers = [
        ("leads", "loadLeads()"),
        ("sessions", "loadSessions()"),
        ("gaps", "loadGaps()"),
        ("companies", "loadCompanies()"),
        ("avito", "loadAvitoConfig()"),
        ("guard", "loadGuardEvents()"),
        ("tenants", "loadTenants()"),
        ("usage", "loadUsage()"),
    ]
    for tab_name, handler_call in expected_handlers:
        assert f"if (name === '{tab_name}') {handler_call}" in html, f"Missing handler for {tab_name}"
