from pathlib import Path
import pytest
from starlette.testclient import TestClient

from app.auth import AuthService, hash_password
from app.company_store import CompanyStore
from app.config import Settings
from app.price_parser import parse_price_list
from app.server import create_app
from app.storage import Storage


def test_price_parser_handles_formats():
    raw_text = """
    # Мойка и кузов
    Комплексная мойка — 2 500 ₽
    Полировка кузова: от 15000 руб
    Химчистка салона | 8.000
    Защитная плёнка (капот) .... 25000р
    Шумоизоляция дверей - от 12 000
    Керамика 9H\t35000
    Рихтовка вмятин: 4500
    Бессрочная гарантия на оклейку
    Официальные сертифицированные мастера
    """
    res = parse_price_list(raw_text)
    services = res["services"]
    extra = res["extra_lines"]

    assert len(services) == 7
    assert "Комплексная мойка: 2 500 ₽" in services
    assert "Полировка кузова: от 15 000 ₽" in services
    assert "Химчистка салона: 8 000 ₽" in services
    assert "Защитная плёнка (капот): 25 000 ₽" in services
    assert "Шумоизоляция дверей: от 12 000 ₽" in services
    assert "Керамика 9H: 35 000 ₽" in services
    assert "Рихтовка вмятин: 4 500 ₽" in services

    assert len(extra) == 2
    assert "Бессрочная гарантия на оклейку" in extra
    assert "Официальные сертифицированные мастера" in extra


def test_onboarding_state_persists(tmp_path: Path):
    db_file = tmp_path / "test.db"
    storage = Storage(db_file)
    companies_dir = tmp_path / "companies"
    companies_dir.mkdir(parents=True)
    company_store = CompanyStore(companies_dir)

    settings = Settings(db_path=db_file, companies_dir=companies_dir)
    auth = AuthService(settings, storage)

    storage.save_tenant(slug="tenant_test", name="Тестовый сервис")
    storage.save_user("user_admin", hash_password("pass_user_admin_123"), role="client_admin", tenant_slug="tenant_test")
    token = "session_token_123"
    storage.create_user_session(token, storage.get_user_by_username("user_admin")["id"])

    app = create_app(
        settings=settings,
        company_store=company_store,
        storage=storage,
        auth=auth,
    )
    client = TestClient(app)
    client.cookies.set("zayavka_session", token)

    # 1. Первоначальный GET -> step 1, empty draft
    r1 = client.get("/api/onboarding")
    assert r1.status_code == 200
    d1 = r1.json()
    assert d1["step"] == 1
    assert d1["draft"] == {}

    # 2. POST step 1 -> сохраняем данные компании
    r2 = client.post(
        "/api/onboarding/step",
        json={"step": 2, "data": {"company_name": "Детейлинг Элит", "city": "Казань"}},
    )
    assert r2.status_code == 200
    d2 = r2.json()
    assert d2["step"] == 2
    assert d2["draft"]["company_name"] == "Детейлинг Элит"
    assert d2["draft"]["city"] == "Казань"

    # 3. Повторный GET -> данные сохранены
    r3 = client.get("/api/onboarding")
    assert r3.status_code == 200
    d3 = r3.json()
    assert d3["step"] == 2
    assert d3["draft"]["company_name"] == "Детейлинг Элит"
    assert d3["draft"]["city"] == "Казань"


def test_onboarding_complete_creates_company(tmp_path: Path):
    db_file = tmp_path / "test.db"
    storage = Storage(db_file)
    companies_dir = tmp_path / "companies"
    companies_dir.mkdir(parents=True)
    company_store = CompanyStore(companies_dir)

    settings = Settings(db_path=db_file, companies_dir=companies_dir)
    auth = AuthService(settings, storage)

    storage.save_tenant(slug="tenant_complete", name="Компания Онбординга")
    storage.save_user("admin_comp", hash_password("pass_admin_comp_123"), role="client_admin", tenant_slug="tenant_complete")
    token = "token_complete_123"
    storage.create_user_session(token, storage.get_user_by_username("admin_comp")["id"])

    app = create_app(settings=settings, company_store=company_store, storage=storage, auth=auth)
    client = TestClient(app)
    client.cookies.set("zayavka_session", token)

    # Сохраняем черновик со всеми шагами
    client.post(
        "/api/onboarding/step",
        json={
            "step": 5,
            "data": {
                "company_name": "АвтоSPA Премиум",
                "city": "Санкт-Петербург",
                "niche": "детейлинг",
                "services": ["Полировка: от 12 000 ₽", "Керамика: от 20 000 ₽"],
                "manager_name": "Дмитрий",
                "work_hours": "Ежедневно с 09:00 до 21:00",
            },
        },
    )

    # Завершаем онбординг
    resp = client.post("/api/onboarding/complete")
    assert resp.status_code == 200
    data = resp.json()
    assert data["status"] == "ok"
    slug = data["slug"]
    assert slug.startswith("avtospa-premium")

    # Проверяем, что CompanyProfile создан и доступен
    profile = company_store.get(slug)
    assert profile is not None
    assert profile.company_name == "АвтоSPA Премиум"
    assert profile.city == "Санкт-Петербург"
    assert profile.tenant_slug == "tenant_complete"
    assert len(profile.services) == 2
    assert "Полировка: от 12 000 ₽" in profile.services

    # Проверяем статус завершения в storage
    assert storage.is_onboarding_completed("tenant_complete") is True


def test_onboarding_scoped_by_tenant(tmp_path: Path):
    db_file = tmp_path / "test.db"
    storage = Storage(db_file)
    companies_dir = tmp_path / "companies"
    companies_dir.mkdir(parents=True)
    company_store = CompanyStore(companies_dir)

    settings = Settings(db_path=db_file, companies_dir=companies_dir)
    auth = AuthService(settings, storage)

    storage.save_tenant(slug="tenant_alpha", name="Альфа")
    storage.save_tenant(slug="tenant_beta", name="Бета")

    storage.save_user("user_alpha", hash_password("pass_user_alpha_123"), role="client_admin", tenant_slug="tenant_alpha")
    storage.save_user("user_beta", hash_password("pass_user_beta_123"), role="client_admin", tenant_slug="tenant_beta")

    token_a = "token_alpha_123"
    token_b = "token_beta_123"
    storage.create_user_session(token_a, storage.get_user_by_username("user_alpha")["id"])
    storage.create_user_session(token_b, storage.get_user_by_username("user_beta")["id"])

    app = create_app(settings=settings, company_store=company_store, storage=storage, auth=auth)
    client = TestClient(app)

    # Клиент Alpha сохраняет черновик
    client.cookies.set("zayavka_session", token_a)
    client.post("/api/onboarding/step", json={"step": 2, "data": {"company_name": "Секреты Альфы"}})

    # Клиент Beta читает онбординг: не должен видеть черновик Alpha
    client.cookies.set("zayavka_session", token_b)
    resp_b = client.get("/api/onboarding")
    assert resp_b.status_code == 200
    data_b = resp_b.json()
    assert data_b["draft"] == {}


def test_onboarding_slug_collision_gets_suffix(tmp_path: Path):
    db_file = tmp_path / "test.db"
    storage = Storage(db_file)
    companies_dir = tmp_path / "companies"
    companies_dir.mkdir(parents=True)
    company_store = CompanyStore(companies_dir)

    settings = Settings(db_path=db_file, companies_dir=companies_dir)
    auth = AuthService(settings, storage)

    # Создаем первого клиента
    storage.save_tenant(slug="client_1", name="Клиент 1")
    storage.save_user("user_1", hash_password("pass_user_1_12345"), role="client_admin", tenant_slug="client_1")
    token_1 = "tok_1_123"
    storage.create_user_session(token_1, storage.get_user_by_username("user_1")["id"])

    app = create_app(settings=settings, company_store=company_store, storage=storage, auth=auth)
    client = TestClient(app)
    client.cookies.set("zayavka_session", token_1)

    client.post(
        "/api/onboarding/step",
        json={"step": 5, "data": {"company_name": "Автомойка Люкс", "city": "Самара", "services": ["Мойка: 500 ₽"]}},
    )
    r1 = client.post("/api/onboarding/complete")
    assert r1.status_code == 200
    slug1 = r1.json()["slug"]
    assert slug1 == "avtomoyka-lyuks"

    # Создаем второго клиента с ТАКИМ ЖЕ названием компании
    storage.save_tenant(slug="client_2", name="Клиент 2")
    storage.save_user("user_2", hash_password("pass_user_2_12345"), role="client_admin", tenant_slug="client_2")
    token_2 = "tok_2_456"
    storage.create_user_session(token_2, storage.get_user_by_username("user_2")["id"])

    client.cookies.set("zayavka_session", token_2)
    client.post(
        "/api/onboarding/step",
        json={"step": 5, "data": {"company_name": "Автомойка Люкс", "city": "Тверь", "services": ["Мойка: 700 ₽"]}},
    )
    r2 = client.post("/api/onboarding/complete")
    assert r2.status_code == 200
    slug2 = r2.json()["slug"]
    assert slug2 == "avtomoyka-lyuks-2"


def test_viewer_cannot_onboard(tmp_path: Path):
    db_file = tmp_path / "test.db"
    storage = Storage(db_file)
    companies_dir = tmp_path / "companies"
    companies_dir.mkdir(parents=True)
    company_store = CompanyStore(companies_dir)

    settings = Settings(db_path=db_file, companies_dir=companies_dir)
    auth = AuthService(settings, storage)

    storage.save_tenant(slug="tenant_view", name="Только просмотр")
    storage.save_user("user_viewer", hash_password("pass_viewer_12345"), role="client_viewer", tenant_slug="tenant_view")
    token = "token_viewer_789"
    storage.create_user_session(token, storage.get_user_by_username("user_viewer")["id"])

    app = create_app(settings=settings, company_store=company_store, storage=storage, auth=auth)
    client = TestClient(app)
    client.cookies.set("zayavka_session", token)

    # 1. Попытка сохранить шаг
    r_step = client.post("/api/onboarding/step", json={"step": 2, "data": {"company_name": "Тест"}})
    assert r_step.status_code == 403

    # 2. Попытка завершить онбординг
    r_comp = client.post("/api/onboarding/complete")
    assert r_comp.status_code == 403
