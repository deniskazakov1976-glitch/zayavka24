"""Р СћР ВµРЎРѓРЎвЂљРЎвЂ№ Р СРЎС“Р В»РЎРЉРЎвЂљР С‘РЎвЂљР ВµР Р…Р В°Р Р…РЎвЂљР Р…Р С•РЎРѓРЎвЂљР С‘ (Р СРЎС“Р В»РЎРЉРЎвЂљР С‘Р В±Р С•РЎвЂљ) Р С‘ Р С”Р В°Р В±Р С‘Р Р…Р ВµРЎвЂљР В° Р С”Р В»Р С‘Р ВµР Р…РЎвЂљР В°.

Р СџР С•Р С”РЎР‚РЎвЂ№Р Р†Р В°РЎР‹РЎвЂљ:
    * Р С‘Р В·Р С•Р В»РЎРЏРЎвЂ Р С‘РЎР‹ Р Т‘Р В°Р Р…Р Р…РЎвЂ№РЎвЂ¦ Р С—Р С• Р С”Р В»Р С‘Р ВµР Р…РЎвЂљР В°Р С (Р В»Р С‘Р Т‘РЎвЂ№, РЎРѓР ВµРЎРѓРЎРѓР С‘Р С‘, Р С—РЎР‚Р С•Р В±Р ВµР В»РЎвЂ№, РЎРѓРЎвЂљР В°РЎвЂљР С‘РЎРѓРЎвЂљР С‘Р С”Р В°);
    * Р Р†РЎвЂ¦Р С•Р Т‘ Р С—Р С• Р В»Р С•Р С–Р С‘Р Р…РЎС“/Р С—Р В°РЎР‚Р С•Р В»РЎР‹ РЎРѓ cookie-РЎРѓР ВµРЎРѓРЎРѓР С‘Р ВµР в„–;
    * РЎР‚Р С•Р В»Р С‘ (Р Р†Р В»Р В°Р Т‘Р ВµР В»Р ВµРЎвЂ  Р С—Р В»Р В°РЎвЂљРЎвЂћР С•РЎР‚Р СРЎвЂ№ / Р В°Р Т‘Р СР С‘Р Р… Р С”Р В»Р С‘Р ВµР Р…РЎвЂљР В° / Р Р…Р В°Р В±Р В»РЎР‹Р Т‘Р В°РЎвЂљР ВµР В»РЎРЉ);
    * РЎС“Р С—РЎР‚Р В°Р Р†Р В»Р ВµР Р…Р С‘Р Вµ Р С”Р В»Р С‘Р ВµР Р…РЎвЂљР В°Р СР С‘ Р С‘ Р С‘РЎвЂ¦ Telegram-Р В±Р С•РЎвЂљР В°Р СР С‘;
    * Р С•РЎвЂљР С”Р В°Р В· Р Р† Р Т‘Р С•РЎРѓРЎвЂљРЎС“Р С—Р Вµ Р С” РЎвЂЎРЎС“Р В¶Р С‘Р С Р С”Р С•Р СР С—Р В°Р Р…Р С‘РЎРЏР С.
"""

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.auth import (
    ROLE_CLIENT_ADMIN,
    ROLE_CLIENT_VIEWER,
    ROLE_PLATFORM_ADMIN,
    AuthService,
    hash_password,
    verify_password,
)
from app.company_store import CompanyStore
from app.config import Settings
from app.models import CompanyProfile, KnowledgeGap, LeadInfo
from app.server import create_app
from app.storage import Storage


@pytest.fixture()
def env(tmp_path: Path):
    companies_dir = tmp_path / "companies"
    companies_dir.mkdir()
    (companies_dir / "alpha.yaml").write_text(
        "slug: alpha\ncompany_name: Alpha\ncity: Р СљР С•РЎРѓР С”Р Р†Р В°\ntenant_slug: acme\n",
        encoding="utf-8",
    )
    (companies_dir / "beta.yaml").write_text(
        "slug: beta\ncompany_name: Beta\ncity: Р С™Р В°Р В·Р В°Р Р…РЎРЉ\ntenant_slug: globex\n",
        encoding="utf-8",
    )

    settings = Settings(
        companies_dir=companies_dir,
        db_path=tmp_path / "db.sqlite3",
        dashboard_enabled=True,
        dashboard_password="platform-secret",
        dashboard_user="root",
        telegram_bot_token="",
        admin_ids=[],
    )
    storage = Storage(settings.db_path)
    store = CompanyStore(companies_dir)
    auth = AuthService(settings, storage)
    auth.bootstrap()

    storage.save_tenant(slug="acme", name="Acme", telegram_bot_token="111:AAA")
    storage.save_tenant(slug="globex", name="Globex", telegram_bot_token="222:BBB")
    storage.save_user("acme_admin", hash_password("acme-pass-1"), ROLE_CLIENT_ADMIN, "acme")
    storage.save_user("acme_viewer", hash_password("acme-pass-2"), ROLE_CLIENT_VIEWER, "acme")
    storage.save_user("globex_admin", hash_password("globex-pass"), ROLE_CLIENT_ADMIN, "globex")

    # Р вЂќР В°Р Р…Р Р…РЎвЂ№Р Вµ Р Т‘Р Р†РЎС“РЎвЂ¦ Р С”Р В»Р С‘Р ВµР Р…РЎвЂљР С•Р Р†
    storage.upsert_session("telegram", "1", "alpha")
    storage.add_message("telegram", "1", "user", "РЎвЂ¦Р С•РЎвЂЎРЎС“ Р С—Р С•Р В»Р С‘РЎР‚Р С•Р Р†Р С”РЎС“")
    storage.save_lead(
        LeadInfo(channel="telegram", chat_id="1", company_slug="alpha", phone="+79990000001")
    )
    storage.save_knowledge_gap(
        KnowledgeGap(channel="telegram", chat_id="1", company_slug="alpha", question="Р В¦Р ВµР Р…Р В°?")
    )

    storage.upsert_session("telegram", "2", "beta")
    storage.add_message("telegram", "2", "user", "Р Р…РЎС“Р В¶Р Р…Р В° Р С—Р С•Р С”РЎР‚Р В°РЎРѓР С”Р В°")
    storage.save_lead(
        LeadInfo(channel="telegram", chat_id="2", company_slug="beta", phone="+79990000002")
    )

    app = create_app(
        settings=settings,
        company_store=store,
        storage=storage,
        get_bot_username=lambda: "alpha_bot",
        auth=auth,
    )
    return {
        "settings": settings,
        "storage": storage,
        "store": store,
        "auth": auth,
        "app": app,
    }


def login(client: TestClient, username: str, password: str) -> None:
    resp = client.post("/login", data={"username": username, "password": password}, follow_redirects=False)
    assert resp.status_code == 303


# === Р СџР В°РЎР‚Р С•Р В»Р С‘ ===
def test_password_hash_roundtrip():
    hashed = hash_password("s3cret-pass")
    assert hashed.startswith("pbkdf2_sha256$")
    assert verify_password("s3cret-pass", hashed)
    assert not verify_password("wrong", hashed)
    assert not verify_password("", hashed)


# === Р ВР В·Р С•Р В»РЎРЏРЎвЂ Р С‘РЎРЏ Р Р† РЎвЂ¦РЎР‚Р В°Р Р…Р С‘Р В»Р С‘РЎвЂ°Р Вµ ===
def test_storage_scoping_by_company(env):
    storage: Storage = env["storage"]

    all_leads = storage.list_recent_leads(limit=10)
    assert len(all_leads) == 2

    acme_leads = storage.list_recent_leads(limit=10, company_slugs=["alpha"])
    assert [lead.company_slug for lead in acme_leads] == ["alpha"]

    assert storage.list_recent_leads(limit=10, company_slugs=[]) == []

    acme_stats = storage.get_stats(company_slugs=["alpha"])
    assert acme_stats["total_sessions"] == 1
    assert acme_stats["total_leads"] == 1

    gaps = storage.list_knowledge_gaps(limit=10, company_slugs=["alpha"])
    assert len(gaps) == 1
    assert storage.list_knowledge_gaps(limit=10, company_slugs=["beta"]) == []


def test_company_store_tenant_helpers(env):
    store: CompanyStore = env["store"]
    assert [p.slug for p in store.list_for_tenant("acme")] == ["alpha"]
    assert [p.slug for p in store.list_for_tenant("globex")] == ["beta"]
    assert len(store.list_for_tenant(None)) == 2

    assert store.owns_company("acme", "alpha")
    assert not store.owns_company("acme", "beta")
    assert store.owns_company(None, "beta")

    assert store.slugs_for_tenant("acme") == ["alpha"]
    assert store.slugs_for_tenant(None) is None


# === Р вЂ™РЎвЂ¦Р С•Р Т‘ Р Р† Р С”Р В°Р В±Р С‘Р Р…Р ВµРЎвЂљ ===
def test_login_wrong_password_and_rate_limit(env):
    client = TestClient(env["app"])
    resp = client.post("/login", data={"username": "acme_admin", "password": "nope"}, follow_redirects=False)
    assert resp.status_code == 303
    assert resp.headers["location"].endswith("error=1")

    # Р вЂР ВµР В· cookie Р С”Р В°Р В±Р С‘Р Р…Р ВµРЎвЂљ Р Р…Р ВµР Т‘Р С•РЎРѓРЎвЂљРЎС“Р С—Р ВµР Р…
    assert client.get("/", follow_redirects=False).status_code in (302, 303)
    assert client.get("/api/me").status_code == 401


def test_login_logout_flow(env):
    client = TestClient(env["app"])
    login(client, "acme_admin", "acme-pass-1")

    me = client.get("/api/me").json()
    assert me["username"] == "acme_admin"
    assert me["role"] == ROLE_CLIENT_ADMIN
    assert me["tenant_slug"] == "acme"
    assert me["can_write"] is True

    client.get("/logout", follow_redirects=False)
    assert client.get("/api/me").status_code == 401


# === Р ВР В·Р С•Р В»РЎРЏРЎвЂ Р С‘РЎРЏ РЎвЂЎР ВµРЎР‚Р ВµР В· API ===
def test_client_sees_only_own_data(env):
    client = TestClient(env["app"])
    login(client, "acme_admin", "acme-pass-1")

    companies = client.get("/api/companies").json()
    assert [c["slug"] for c in companies] == ["alpha"]
    assert companies[0]["demo_link"] == ""  # бот клиента ещё не запускался — ссылку не выдаём

    leads = client.get("/api/leads").json()
    assert len(leads) == 1
    assert leads[0]["company_slug"] == "alpha"

    sessions = client.get("/api/sessions").json()
    assert {s["company_slug"] for s in sessions} == {"alpha"}

    stats = client.get("/api/stats").json()
    assert stats["total_leads"] == 1
    assert stats["companies_count"] == 1


def test_client_cannot_read_foreign_session(env):
    client = TestClient(env["app"])
    login(client, "acme_admin", "acme-pass-1")
    assert client.get("/api/sessions/telegram/2/history").status_code == 404


def test_viewer_is_read_only(env):
    client = TestClient(env["app"])
    login(client, "acme_viewer", "acme-pass-2")

    assert client.get("/api/leads").status_code == 200

    resp = client.post(
        "/api/companies",
        json={"slug": "gamma", "company_name": "Gamma", "city": "Р РЋР С•РЎвЂЎР С‘"},
    )
    assert resp.status_code == 403

    resp = client.post("/api/sessions/telegram/1/toggle-pause")
    assert resp.status_code == 403


def test_platform_admin_sees_everything(env):
    client = TestClient(env["app"])
    login(client, "root", "platform-secret")

    assert len(client.get("/api/companies").json()) == 2
    assert len(client.get("/api/leads").json()) == 2

    tenants = client.get("/api/tenants").json()
    slugs = {t["slug"] for t in tenants}
    assert {"default", "acme", "globex"} <= slugs
    # РўРѕРєРµРЅС‹ Р±РѕС‚РѕРІ РЅР°СЂСѓР¶Сѓ РЅРµ РѕС‚РґР°СЋС‚СЃСЏ
    for tenant in tenants:
        assert ":" not in (tenant.get("telegram_bot_token") or "")

    users = client.get("/api/users").json()
    assert {u["username"] for u in users} >= {"root", "acme_admin", "globex_admin"}


# === Р Р€Р С—РЎР‚Р В°Р Р†Р В»Р ВµР Р…Р С‘Р Вµ Р С”Р В»Р С‘Р ВµР Р…РЎвЂљР В°Р СР С‘ ===
def test_create_tenant_with_admin_user(env):
    client = TestClient(env["app"])
    login(client, "root", "platform-secret")

    resp = client.post(
        "/api/tenants",
        json={
            "slug": "initech",
            "name": "Initech",
            "brand_name": "Initech AI",
            "admin_username": "initech_admin",
            "admin_password": "initech-pass",
        },
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["admin_username"] == "initech_admin"

    # Р СњР С•Р Р†РЎвЂ№Р в„– Р С”Р В»Р С‘Р ВµР Р…РЎвЂљ РЎРѓРЎР‚Р В°Р В·РЎС“ Р СР С•Р В¶Р ВµРЎвЂљ Р Р†Р С•Р в„–РЎвЂљР С‘ Р С‘ Р Р…Р Вµ Р Р†Р С‘Р Т‘Р С‘РЎвЂљ РЎвЂЎРЎС“Р В¶Р С‘РЎвЂ¦ Р С”Р С•Р СР С—Р В°Р Р…Р С‘Р в„–
    other = TestClient(env["app"])
    login(other, "initech_admin", "initech-pass")
    me = other.get("/api/me").json()
    assert me["tenant_slug"] == "initech"
    assert other.get("/api/companies").json() == []
    assert other.get("/api/leads").json() == []


def test_client_admin_cannot_create_tenants(env):
    client = TestClient(env["app"])
    login(client, "acme_admin", "acme-pass-1")
    resp = client.post("/api/tenants", json={"slug": "evil", "name": "Evil"})
    assert resp.status_code == 403


def test_company_belongs_to_tenant_on_create(env):
    client = TestClient(env["app"])
    login(client, "acme_admin", "acme-pass-1")

    resp = client.post(
        "/api/companies",
        json={"slug": "alpha2", "company_name": "Alpha 2", "city": "Р СљР С•РЎРѓР С”Р Р†Р В°"},
    )
    assert resp.status_code == 200
    profile = env["store"].get("alpha2")
    assert profile.tenant_slug == "acme"


def test_client_cannot_take_foreign_slug(env):
    client = TestClient(env["app"])
    login(client, "acme_admin", "acme-pass-1")

    resp = client.post(
        "/api/companies",
        json={"slug": "beta", "company_name": "Hijack", "city": "Р СљР С•РЎРѓР С”Р Р†Р В°"},
    )
    assert resp.status_code == 409


def test_delete_tenant_is_blocked_for_default(env):
    client = TestClient(env["app"])
    login(client, "root", "platform-secret")
    assert client.delete("/api/tenants/default").status_code == 400


# === Р РЋР ВµРЎРѓРЎРѓР С‘Р С‘ Р С—Р С•Р В»РЎРЉР В·Р С•Р Р†Р В°РЎвЂљР ВµР В»Р ВµР в„– ===
def test_user_sessions_expire_and_purge(env):
    storage: Storage = env["storage"]
    user = storage.get_user_by_username("acme_admin")
    storage.create_user_session("token-ok", int(user["id"]), ttl_hours=1)
    assert storage.get_user_session("token-ok") is not None

    storage.create_user_session("token-old", int(user["id"]), ttl_hours=-1)
    assert storage.get_user_session("token-old") is None

    storage.purge_expired_sessions()
    assert storage.get_user_session("token-ok") is not None


def test_bootstrap_is_idempotent(env):
    storage: Storage = env["storage"]
    users_before = len(storage.list_users())
    env["auth"].bootstrap()
    assert len(storage.list_users()) == users_before
    assert storage.get_tenant("default") is not None





def test_demo_link_appears_when_client_bot_known(env):
    client = TestClient(env["app"])
    login(client, "acme_admin", "acme-pass-1")

    env["storage"].update_tenant_bot_username("acme", "acme_bot")
    companies = client.get("/api/companies").json()
    assert companies[0]["demo_link"] == "https://t.me/acme_bot?start=alpha"

    stats = client.get("/api/stats").json()
    assert stats["has_own_bot"] is True
    assert stats["bot_username"] == "acme_bot"

def test_platform_admin_company_goes_to_default_tenant(env):
    client = TestClient(env["app"])
    login(client, "root", "platform-secret")

    resp = client.post(
        "/api/companies",
        json={"slug": "platform-co", "company_name": "Platform Co", "city": "Москва"},
    )
    assert resp.status_code == 200
    assert env["store"].get("platform-co").tenant_slug == "default"

    # Компания платформы видна только платформе, не клиентам
    other = TestClient(env["app"])
    login(other, "acme_admin", "acme-pass-1")
    assert "platform-co" not in {c["slug"] for c in other.get("/api/companies").json()}
