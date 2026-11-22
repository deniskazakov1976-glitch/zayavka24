import html
from pathlib import Path
import pytest
from fastapi.testclient import TestClient

from app.auth import (
    COOKIE_NAME,
    ROLE_CLIENT_ADMIN,
    ROLE_PLATFORM_ADMIN,
    AuthService,
    hash_password,
)
from app.company_store import CompanyStore
from app.config import Settings
from app.guardrails import validate_reply
from app.models import CompanyProfile, LeadInfo
from app.notifier import format_lead_message
from app.server import create_app
from app.storage import Storage
from scripts.migrate_sqlite_to_postgres import TABLES_TO_MIGRATE


@pytest.fixture()
def audit_env(tmp_path: Path):
    companies_dir = tmp_path / "companies"
    companies_dir.mkdir()
    (companies_dir / "alpha.yaml").write_text(
        "slug: alpha\ncompany_name: Alpha Detailing\ncity: Москва\ntenant_slug: acme\n"
        "services:\n  - 'Полировка кузова: 15 000 ₽'\n",
        encoding="utf-8",
    )
    (companies_dir / "beta.yaml").write_text(
        "slug: beta\ncompany_name: Beta Motors\ncity: Казань\ntenant_slug: globex\n",
        encoding="utf-8",
    )

    settings = Settings(
        companies_dir=companies_dir,
        db_path=tmp_path / "test_audit.db",
        dashboard_enabled=True,
        dashboard_password="AuditSecurePass2026!",
        dashboard_user="admin",
        telegram_bot_token="token_default",
        admin_ids=[100, 200],
    )
    storage = Storage(settings.db_path)
    store = CompanyStore(companies_dir)
    auth = AuthService(settings, storage)
    auth.bootstrap()

    storage.save_tenant(slug="acme", name="Acme Motors", telegram_bot_token="token_acme", owner_telegram_id=300)
    storage.save_tenant(slug="globex", name="Globex Auto", telegram_bot_token="token_globex", owner_telegram_id=400)
    storage.save_user("acme_admin", hash_password("Pass123!"), ROLE_CLIENT_ADMIN, "acme")

    # Add leads for both companies
    storage.save_lead(LeadInfo(channel="telegram", chat_id="chat_a", company_slug="alpha", phone="+79991112233", summary="Полировка"))
    storage.save_lead(LeadInfo(channel="telegram", chat_id="chat_b", company_slug="beta", phone="+79994445566", summary="Кузовной"))

    app = create_app(settings=settings, storage=storage, company_store=store, auth=auth)
    return {
        "app": app,
        "settings": settings,
        "storage": storage,
        "store": store,
        "auth": auth,
    }


def test_guardrails_multiplier_tys_and_k():
    company = CompanyProfile(
        slug="alpha",
        company_name="Alpha Detailing",
        city="Москва",
        services=["Комплекс: 15 000 ₽"],
    )
    # 15 тыс should be recognized as 15000 and allowed because 15000 is in services
    res_allowed = validate_reply("Сделаем полировку за 15 тыс рублей", company)
    assert res_allowed.is_safe is True
    assert len(res_allowed.violations) == 0

    # 5 тыс or 20к should be recognized as 5000 / 20000 and blocked because neither is in services
    res_blocked_5k = validate_reply("Покрасим бампер за 5 тыс рублей", company)
    assert res_blocked_5k.is_safe is False
    assert any("5 тыс" in v for v in res_blocked_5k.violations)

    res_blocked_20k = validate_reply("Защитим кузов за 20к", company)
    assert res_blocked_20k.is_safe is False
    assert any("20к" in v for v in res_blocked_20k.violations)


def test_cors_credentials_disabled(audit_env):
    client = TestClient(audit_env["app"])
    resp = client.options(
        "/api/simulator/chat",
        headers={"Origin": "https://attacker.com", "Access-Control-Request-Method": "POST"},
    )
    # Access-Control-Allow-Credentials must NOT be true when wildcard origins are allowed
    assert resp.headers.get("access-control-allow-credentials") != "true"


def test_toggle_pause_both_urls(audit_env):
    client = TestClient(audit_env["app"], follow_redirects=False)
    # Login as admin
    login_resp = client.post("/login", data={"username": "admin", "password": "AuditSecurePass2026!"})
    assert login_resp.status_code == 303

    storage: Storage = audit_env["storage"]
    storage.upsert_session("telegram", "chat_pause_test", "alpha")

    # 1. /toggle-pause endpoint
    r1 = client.post("/api/sessions/telegram/chat_pause_test/toggle-pause")
    assert r1.status_code == 200
    assert r1.json()["is_paused"] is True

    # 2. /pause alias endpoint
    r2 = client.post("/api/sessions/telegram/chat_pause_test/pause")
    assert r2.status_code == 200
    assert r2.json()["is_paused"] is False


def test_simulator_company_slug_whitelist(audit_env):
    client = TestClient(audit_env["app"])
    # Passing an arbitrary private slug must not crash and fallback to demo profile
    resp = client.post(
        "/api/simulator/chat",
        json={"message": "Здравствуйте", "company_slug": "secret_private_client", "history": []},
    )
    assert resp.status_code == 200
    data = resp.json()
    assert data["status"] == "ok"


def test_format_lead_message_whatsapp_label():
    lead = LeadInfo(channel="whatsapp", chat_id="wa_1", company_slug="alpha", phone="+79998887766")
    msg = format_lead_message(lead)
    assert "WhatsApp" in msg
    assert "Telegram (Демо)" not in msg


def test_migrate_postgres_all_tables_included():
    # Verify all actual storage tables are in TABLES_TO_MIGRATE
    required_tables = [
        "schema_migrations",
        "tenants",
        "users",
        "user_sessions",
        "plans",
        "sessions",
        "messages",
        "leads",
        "knowledge_gaps",
        "processed_events",
        "bot_sent_messages",
        "avito_accounts",
        "guard_events",
        "backup_runs",
        "llm_usage",
        "model_prices",
        "onboarding",
    ]
    for tbl in required_tables:
        assert tbl in TABLES_TO_MIGRATE, f"Table '{tbl}' missing from TABLES_TO_MIGRATE"


def test_x_real_ip_used_and_header_spoof_prevented(audit_env):
    auth: AuthService = audit_env["auth"]
    from starlette.requests import Request

    scope = {
        "type": "http",
        "client": ("127.0.0.1", 12345),
        "headers": [
            (b"x-real-ip", b"203.0.113.195"),
            (b"x-forwarded-for", b"1.1.1.1, 203.0.113.195"),
        ],
    }
    req = Request(scope)
    assert auth.client_ip(req) == "203.0.113.195"
