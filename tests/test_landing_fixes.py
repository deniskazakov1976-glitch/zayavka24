import pytest
from pathlib import Path
from starlette.testclient import TestClient
from fastapi import FastAPI

from app.dashboard import get_landing_html, build_dashboard_router
from app.company_store import CompanyStore
from app.config import Settings
from app.storage import Storage
from app.auth import AuthService


def test_landing_audit_fixes_html():
    """Verify all issues highlighted in the comprehensive CRO/tech audit are resolved in HTML."""
    html = get_landing_html("Zayavka24AI_bot")

    # 1. No broken 404 links on base path
    assert 'href="#top" class="nav-logo"' in html
    assert 'href="/login"' in html
    assert 'id="top"' in html

    # 2. H1 targeted specifically to auto businesses
    assert "ИИ-менеджер для автосервисов, кузовного ремонта и детейлинга" in html

    # 3. Tagline consistent numbers with average tickets
    assert "от 25 000 до 45 000 ₽" in html

    # 4. Step 02 has no contradiction about read-only
    assert "только к чтению сообщений" not in html
    assert "Avito Messenger API v3 с доступом только к диалогам" in html

    # 5. WhatsApp FAQ safe wording
    assert "риск санкций со стороны WhatsApp сведен к нулю" not in html
    assert "Риск блокировки минимален, так как бот работает исключительно с входящими обращениями" in html

    # 6. Simulator reset button and race condition protection
    assert "sim-reset-btn" in html
    assert "resetSimulator()" in html
    assert "setSimulatorDisabled" in html
    assert "isSimProcessing" in html
    assert "insertAdjacentHTML" in html

    # 7. Real social proof section (#proof)
    assert 'id="proof"' in html
    assert "proof-cases-grid" in html

    # 8. Pricing tariffs section (#pricing)
    assert 'id="pricing"' in html
    assert "12 900 ₽" in html
    assert "24 900 ₽" in html

    # 9. Alternative lead capture web form
    assert "lead-web-form" in html
    assert "submitWebLead" in html

    # 10. Legal requisites and 152-FZ Privacy modal
    assert "ИП Казаков Денис Сергеевич" in html
    assert "772156845210" in html
    assert "legal-modal" in html
    assert "152-ФЗ" in html


def test_landing_lead_api_endpoint(tmp_path: Path):
    """Test /api/landing/lead endpoint accepts form submissions and persists lead."""
    companies_dir = tmp_path / "companies"
    companies_dir.mkdir(parents=True, exist_ok=True)
    db_path = tmp_path / "test.db"

    store = CompanyStore(companies_dir)
    settings = Settings(
        db_path=db_path,
        companies_dir=companies_dir,
        dashboard_user="admin",
        dashboard_password="password123",
        telegram_bot_token="",
    )
    storage = Storage(db_path)
    auth = AuthService(settings, storage)
    auth.bootstrap()

    router = build_dashboard_router(
        settings=settings,
        company_store=store,
        storage=storage,
        auth=auth,
        llm=None,
    )
    app = FastAPI()
    app.include_router(router)
    client = TestClient(app)

    # Submit lead from landing page
    resp = client.post(
        "/api/landing/lead",
        json={"phone": "+7 (999) 777-66-55", "name": "Кузовной Цех Москва", "service": "Тест-драйв 3 дня"}
    )
    assert resp.status_code == 200
    data = resp.json()
    assert data["status"] == "ok"
    assert "Заявка успешно принята" in data["message"]

    # Verify saved to database
    recent = storage.list_recent_leads(limit=10)
    assert len(recent) >= 1
    assert any(lead.phone == "+7 (999) 777-66-55" for lead in recent)
