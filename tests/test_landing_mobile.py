import pytest
from pathlib import Path
from starlette.testclient import TestClient
from fastapi import FastAPI

from app.dashboard import get_landing_html, build_dashboard_router
from app.company_store import CompanyStore
from app.config import Settings
from app.storage import Storage
from app.auth import AuthService


def test_landing_mobile_viewport_and_meta():
    """Verify viewport and mobile web app meta tags for modern smartphones (iPhone/Android)."""
    html = get_landing_html("Zayavka24AI_bot")
    assert 'name="viewport"' in html
    assert "viewport-fit=cover" in html
    assert 'name="theme-color"' in html
    assert 'name="apple-mobile-web-app-capable"' in html


def test_landing_mobile_css_safari_zoom_fix_and_touch():
    """Verify iOS Safari auto-zoom fix (font-size: 16px on inputs) and touch action manipulation."""
    html = get_landing_html("Zayavka24AI_bot")
    # 1. 16px font-size prevents unwanted iOS Safari zoom on input focus
    assert ".messenger-input input" in html
    assert "font-size: 16px;" in html
    # 2. Fast responsive taps without 300ms delay
    assert "touch-action: manipulation;" in html
    # 3. No horizontal scroll
    assert "overflow-x: hidden;" in html
    assert "max-width: 100vw;" in html


def test_landing_mobile_sticky_bar():
    """Verify floating sticky action bar on mobile with direct link to Telegram demo."""
    html = get_landing_html("Zayavka24AI_bot")
    assert "mobile-sticky-bar" in html
    assert "mobile-sticky-btn" in html
    assert "https://t.me/Zayavka24AI_bot?start=demo" in html
    assert "env(safe-area-inset-bottom" in html


def test_landing_mobile_breakpoints():
    """Verify responsive breakpoints for small (320-360px), medium (375-430px) and tablet screens."""
    html = get_landing_html("Zayavka24AI_bot")
    assert "@media (max-width: 768px)" in html
    assert "@media (max-width: 640px)" in html
    assert "@media (max-width: 360px)" in html
    # Hero buttons stack on mobile
    assert ".hero-ctas" in html
    assert "flex-direction: column;" in html


def test_landing_endpoint_serves_mobile_optimized_html(tmp_path: Path):
    """Test /landing HTTP endpoint returns 200 with full mobile markup."""
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

    resp = client.get("/landing")
    assert resp.status_code == 200
    assert "viewport-fit=cover" in resp.text
    assert "mobile-sticky-bar" in resp.text
    assert "Заявка 24/7" in resp.text
