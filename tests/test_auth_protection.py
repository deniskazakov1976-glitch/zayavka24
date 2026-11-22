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
from app.server import create_app
from app.storage import Storage


@pytest.fixture()
def app_env(tmp_path: Path):
    companies_dir = tmp_path / "companies"
    companies_dir.mkdir()
    (companies_dir / "alpha.yaml").write_text(
        "slug: alpha\ncompany_name: Alpha Detailing\ncity: Москва\ntenant_slug: acme\n",
        encoding="utf-8",
    )

    settings = Settings(
        companies_dir=companies_dir,
        db_path=tmp_path / "test_auth.db",
        dashboard_enabled=True,
        dashboard_password="SecretAdminPass2026!",
        dashboard_user="admin",
        telegram_bot_token="",
        admin_ids=[],
    )
    storage = Storage(settings.db_path)
    store = CompanyStore(companies_dir)
    auth = AuthService(settings, storage)
    auth.bootstrap()

    storage.save_tenant(slug="acme", name="Acme Motors", telegram_bot_token="token_acme")
    storage.save_user("acme_admin", hash_password("ClientPass123!"), ROLE_CLIENT_ADMIN, "acme")

    app = create_app(settings=settings, storage=storage, company_store=store, auth=auth)
    return {
        "app": app,
        "settings": settings,
        "storage": storage,
        "auth": auth,
    }


def test_unauthenticated_dashboard_redirects_to_login(app_env):
    client = TestClient(app_env["app"], follow_redirects=False)
    resp = client.get("/")
    assert resp.status_code in (302, 303, 307)
    assert resp.headers["location"] == "/login"


def test_unauthenticated_onboarding_redirects_to_login(app_env):
    client = TestClient(app_env["app"], follow_redirects=False)
    resp = client.get("/onboarding")
    assert resp.status_code in (302, 303, 307)
    assert resp.headers["location"] == "/login"


def test_login_page_renders_form(app_env):
    client = TestClient(app_env["app"])
    resp = client.get("/login")
    assert resp.status_code == 200
    assert 'action="/login"' in resp.text
    assert 'name="username"' in resp.text
    assert 'name="password"' in resp.text
    assert "Панель управления" in resp.text


def test_login_invalid_credentials(app_env):
    client = TestClient(app_env["app"], follow_redirects=False)
    resp = client.post("/login", data={"username": "admin", "password": "WrongPassword"})
    assert resp.status_code == 303
    assert resp.headers["location"] == "/login?error=1"
    assert COOKIE_NAME not in resp.cookies


def test_login_success_platform_admin(app_env):
    client = TestClient(app_env["app"], follow_redirects=False)
    resp = client.post(
        "/login",
        data={"username": "admin", "password": "SecretAdminPass2026!"},
    )
    assert resp.status_code == 303
    assert resp.headers["location"] == "/"
    assert COOKIE_NAME in resp.cookies

    # Now accessing / with this session returns dashboard
    dash_resp = client.get("/")
    assert dash_resp.status_code == 200
    assert "Заявка 24/7" in dash_resp.text


def test_logout_clears_session_and_prevents_reentry(app_env):
    client = TestClient(app_env["app"], follow_redirects=False)
    # Login first
    client.post(
        "/login",
        data={"username": "admin", "password": "SecretAdminPass2026!"},
    )
    assert client.get("/").status_code == 200

    # Logout
    logout_resp = client.get("/logout")
    assert logout_resp.status_code == 303
    assert logout_resp.headers["location"] == "/login"

    # Immediately verify that / redirects to /login again
    after_resp = client.get("/")
    assert after_resp.status_code in (302, 303)
    assert after_resp.headers["location"] == "/login"


def test_platform_admin_vs_client_admin_permissions(app_env):
    # 1. Platform admin has full access to tenants management
    client_admin = TestClient(app_env["app"])
    client_admin.post(
        "/login",
        data={"username": "admin", "password": "SecretAdminPass2026!"},
    )
    tenants_resp = client_admin.get("/api/tenants")
    assert tenants_resp.status_code == 200
    tenants_data = tenants_resp.json()
    assert any(t["slug"] == "acme" for t in tenants_data)

    # 2. Client admin is forbidden from platform administration
    client_client = TestClient(app_env["app"])
    client_client.post(
        "/login",
        data={"username": "acme_admin", "password": "ClientPass123!"},
    )
    forbidden_resp = client_client.get("/api/tenants")
    assert forbidden_resp.status_code == 403
    assert "Только для владельца платформы" in forbidden_resp.json()["detail"]


def test_landing_header_links_to_login(app_env):
    client = TestClient(app_env["app"])
    resp = client.get("/landing")
    assert resp.status_code == 200
    assert '<a href="/login"' in resp.text
    assert "Панель управления" in resp.text
