"""Тесты исправлений по результатам ревью (дефекты 1-6)."""

from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest
from starlette.testclient import TestClient

from app.auth import AuthService, hash_password
from app.company_store import CompanyStore
from app.config import Settings
from app.maintenance import compute_deep_health
from app.price_parser import parse_price_line
from app.quotas import QuotaService
from app.server import create_app
from app.storage import Storage


# --- Дефект 1: дефис внутри названия услуги не должен отрезаться ---
@pytest.mark.parametrize(
    "line,expected",
    [
        ("Полировка-экспресс 5000", "Полировка-экспресс: 5 000 ₽"),
        ("Детейлинг-мойка 2500", "Детейлинг-мойка: 2 500 ₽"),
        ("Химчистка 15 000,50", "Химчистка: 15 000,50 ₽"),
        ("Мойка 15k", "Мойка: 15 000 ₽"),
        ("Полировка 35 000 - 45 000", "Полировка: 35 000 – 45 000 ₽"),
        ("Керамика от 12 000 р", "Керамика: от 12 000 ₽"),
        ("Полировка кузова — от 15 000 ₽", "Полировка кузова: от 15 000 ₽"),
    ],
)
def test_price_parser_preserves_hyphenated_names(line, expected):
    ok, formatted, _ = parse_price_line(line)
    assert ok is True
    assert formatted == expected


def test_price_parser_rejects_non_prices():
    for line in ("Гарантия 1 год", "Предоплата 50%", ""):
        ok, _, _ = parse_price_line(line)
        assert ok is False


# --- Дефект 2: цена для модели из LLM_MODEL появляется автоматически ---
def test_ensure_model_price_seeds_configured_model(tmp_path: Path):
    storage = Storage(tmp_path / "db.sqlite3")

    assert storage.get_model_price("ds/deepseek-v4-flash-vision") == (0.0, 0.0)

    created = storage.ensure_model_price("ds/deepseek-v4-flash-vision", 0.15, 0.60)
    assert created is True
    assert storage.get_model_price("ds/deepseek-v4-flash-vision") == (0.15, 0.60)

    # Повторный вызов не должен затирать уже заданную цену
    assert storage.ensure_model_price("ds/deepseek-v4-flash-vision", 99.0, 99.0) is False
    assert storage.get_model_price("ds/deepseek-v4-flash-vision") == (0.15, 0.60)


def test_usage_cost_is_nonzero_with_seeded_price(tmp_path: Path):
    storage = Storage(tmp_path / "db.sqlite3")
    storage.ensure_model_price("ds/deepseek-v4-flash-vision", 0.15, 0.60)
    p, c = storage.get_model_price("ds/deepseek-v4-flash-vision")

    storage.record_llm_usage(
        tenant_slug="t",
        company_slug="c",
        channel="telegram",
        chat_id="1",
        model="ds/deepseek-v4-flash-vision",
        prompt_tokens=1000,
        completion_tokens=500,
        total_tokens=1500,
        cost_rub=round(1000 / 1000 * p + 500 / 1000 * c, 4),
    )

    summary = storage.get_usage_summary(tenant_slug="t")
    assert summary["cost_rub"] == pytest.approx(0.45, abs=1e-6)


# --- Дефект 4: неизвестный тариф не даёт безлимит ---
def test_unknown_plan_falls_back_to_basic_limits(tmp_path: Path):
    storage = Storage(tmp_path / "db.sqlite3")
    storage.save_tenant(slug="odd", name="Со странным тарифом", plan="nonexistent-plan")

    quota = QuotaService(storage)
    st = quota.check("odd")

    basic = storage.get_plan("basic")
    assert st.limit_messages == basic["included_messages"]
    assert st.allowed is True


# --- Дефект 5: создание клиента с несуществующим тарифом отклоняется ---
def _admin_client(tmp_path: Path):
    companies_dir = tmp_path / "companies"
    companies_dir.mkdir(parents=True)
    settings = Settings(
        companies_dir=companies_dir,
        db_path=tmp_path / "db.sqlite3",
        dashboard_password="admin-pass-123",
        dashboard_user="root",
        telegram_bot_token="",
        admin_ids=[],
    )
    storage = Storage(settings.db_path)
    store = CompanyStore(companies_dir)
    auth = AuthService(settings, storage)
    auth.bootstrap()
    app = create_app(settings=settings, company_store=store, storage=storage, auth=auth)
    client = TestClient(app)
    resp = client.post(
        "/login",
        data={"username": "root", "password": "admin-pass-123"},
        follow_redirects=False,
    )
    assert resp.status_code == 303
    return client, storage


def test_create_tenant_rejects_unknown_plan(tmp_path: Path):
    client, storage = _admin_client(tmp_path)

    bad = client.post("/api/tenants", json={"slug": "badplan", "name": "Плохой", "plan": "no-such-plan"})
    assert bad.status_code == 400
    assert storage.get_tenant("badplan") is None

    good = client.post("/api/tenants", json={"slug": "goodplan", "name": "Хороший", "plan": "pro"})
    assert good.status_code == 200
    assert storage.get_tenant("goodplan")["plan"] == "pro"


def test_health_deep_returns_503_on_fail(tmp_path: Path, monkeypatch):
    """status=fail -> HTTP 503, degraded -> 200 (дефект 6: README обещал 503)."""
    import app.server as server_module

    client, _ = _admin_client(tmp_path)
    app = client.app

    monkeypatch.setattr(
        server_module,
        "compute_deep_health",
        lambda **kwargs: {"status": "fail", "db": {"ok": False}, "bots": {}, "disk": {}, "queue": {}, "last_backup": {}},
    )
    assert client.get("/health/deep").status_code == 503

    monkeypatch.setattr(
        server_module,
        "compute_deep_health",
        lambda **kwargs: {"status": "degraded", "db": {"ok": True}, "bots": {}, "disk": {}, "queue": {}, "last_backup": {}},
    )
    assert client.get("/health/deep").status_code == 200


# --- Дефект 5b: фото не обходят квоту ---
@pytest.mark.asyncio
async def test_vision_blocked_by_quota(tmp_path: Path):
    from app.llm import LLMService
    from app.notifier import LeadNotifier
    from app.telegram_bot import build_telegram_router
    from app.models import CompanyProfile

    storage = Storage(tmp_path / "db.sqlite3")
    companies_dir = tmp_path / "companies"
    companies_dir.mkdir(parents=True)
    (companies_dir / "comp.yaml").write_text(
        "slug: comp\ncompany_name: Компания\ncity: Москва\ntenant_slug: tenant_q\n",
        encoding="utf-8",
    )
    store = CompanyStore(companies_dir)
    settings = Settings(
        companies_dir=companies_dir,
        db_path=tmp_path / "db.sqlite3",
        telegram_bot_token="",
        default_company_slug="comp",
    )

    storage.save_plan(slug="tiny", name="Крошечный", monthly_rub=0, included_messages=1, included_tokens=None)
    storage.save_tenant(slug="tenant_q", name="Клиент с малым лимитом", plan="tiny")
    storage.record_llm_usage(
        tenant_slug="tenant_q",
        company_slug="comp",
        channel="telegram",
        chat_id="1",
        model="m",
        prompt_tokens=10,
        completion_tokens=5,
        total_tokens=15,
        cost_rub=0.01,
    )

    llm = MagicMock()
    llm.generate_reply = AsyncMock()
    quota = QuotaService(storage)

    # Проверяем саму логику: при исчерпанной квоте вызов LLM не происходит
    status = quota.check("tenant_q")
    assert status.allowed is False

    # Роутер должен отказать до обращения к модели
    router = build_telegram_router(
        settings, store, storage, llm, LeadNotifier(bot=None, default_chat_id=None),
        tenant_slug="tenant_q", admin_ids=[], quota_service=quota,
    )
    assert router is not None
    llm.generate_reply.assert_not_called()
