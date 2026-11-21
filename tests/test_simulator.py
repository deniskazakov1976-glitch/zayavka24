import pytest
from starlette.testclient import TestClient

from app.auth import AuthService
from app.company_store import CompanyStore
from app.config import Settings
from app.dashboard import build_dashboard_router
from app.models import CompanyProfile, LLMReply, LeadInfo
from app.storage import Storage
from fastapi import FastAPI


class DummyLLM:
    async def generate_reply(self, company, history, user_message, **kwargs):
        if "покрас" in user_message.lower():
            return LLMReply(text="Покрасим качественно в камере! Какая у вас марка авто?", lead=None, gap=None)
        if "999" in user_message:
            return LLMReply(
                text="Записал вас на осмотр! Скоро перезвоним.",
                lead=LeadInfo(
                    channel="telegram",
                    chat_id="demo",
                    company_slug=company.slug,
                    phone="+7 (999) 111-22-33",
                    summary="Запись на покраску"
                ),
                gap=None
            )
        return LLMReply(text="Здравствуйте! Чем могу помочь?", lead=None, gap=None)


@pytest.fixture
def test_app(tmp_path):
    companies_dir = tmp_path / "companies"
    companies_dir.mkdir()
    db_path = tmp_path / "test.db"

    store = CompanyStore(companies_dir)
    store.save(CompanyProfile(
        slug="detailing_premium",
        company_name="Детейлинг Премиум",
        city="Москва",
        niche="детейлинг и покраска",
        manager_name="Денис",
        manager_role="старший мастер",
        services=["Локальная покраска: от 9 000 ₽", "Оклейка: от 45 000 ₽"],
    ))

    settings = Settings(
        db_path=db_path,
        companies_dir=companies_dir,
        dashboard_user="admin",
        dashboard_password="password123",
    )
    storage = Storage(db_path)
    auth = AuthService(settings, storage)
    auth.bootstrap()

    router = build_dashboard_router(
        settings=settings,
        company_store=store,
        storage=storage,
        auth=auth,
        llm=DummyLLM(),
    )

    app = FastAPI()
    app.include_router(router)
    return app


def test_simulator_chat_reply(test_app):
    client = TestClient(test_app)
    resp = client.post("/api/simulator/chat", json={"message": "ПОКРАСЬ МАШИНУ!"})
    assert resp.status_code == 200
    data = resp.json()
    assert data["status"] == "ok"
    assert "Покрасим" in data["reply"]
    assert data["lead_captured"] is False


def test_simulator_chat_phone_capture(test_app):
    client = TestClient(test_app)
    resp = client.post("/api/simulator/chat", json={"message": "Мой телефон +7 999 111-22-33, перезвоните"})
    assert resp.status_code == 200
    data = resp.json()
    assert data["status"] == "ok"
    assert data["lead_captured"] is True
    assert "+7 (999) 111-22-33" in data["phone"] or "999" in data["phone"]


def test_simulator_chat_fallback_no_llm(tmp_path):
    companies_dir = tmp_path / "companies"
    companies_dir.mkdir()
    db_path = tmp_path / "test.db"
    store = CompanyStore(companies_dir)
    settings = Settings(db_path=db_path, companies_dir=companies_dir)
    storage = Storage(db_path)
    auth = AuthService(settings, storage)

    router = build_dashboard_router(
        settings=settings,
        company_store=store,
        storage=storage,
        auth=auth,
        llm=None,  # No LLM
    )
    app = FastAPI()
    app.include_router(router)
    client = TestClient(app)

    resp = client.post("/api/simulator/chat", json={"message": "Сколько стоит покраска?"})
    assert resp.status_code == 200
    data = resp.json()
    assert data["status"] == "ok"
    assert "Здравствуйте" in data["reply"]
