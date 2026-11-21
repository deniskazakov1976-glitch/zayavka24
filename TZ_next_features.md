# ТЗ: бэкапы/мониторинг, учёт токенов с тарифами, мастер онбординга

Документ для исполнителя (ИИ-агента или разработчика), который продолжит работу над
`BotAvitoTelegram`. Написан так, чтобы не требовать доступа к истории предыдущей сессии:
всё нужное — в разделах 0–1.

---

## 0. Контекст: что уже есть

Проект: **`F:\BotAvitoTelegram`** (локальная копия) → прод на сервере `77.221.155.74`
в `/opt/BotAvitoTelegram`, запуск через Docker Compose.

### Стек

- Python 3.12, aiogram 3.x, FastAPI + uvicorn, SQLite (WAL), pydantic-settings, openai SDK.
- Тесты: `pytest`, `pytest-asyncio`. Сейчас **44 теста, все зелёные**.
- Запуск локально: `.\.venv\Scripts\python.exe main.py` или `start.bat`.

### Структура

| Файл | Назначение |
|---|---|
| `main.py` | сборка приложения: Storage, LLMService, AuthService, BotManager, uvicorn |
| `app/config.py` | `Settings` (pydantic-settings, читает `.env`) + `update_env_file()` |
| `app/storage.py` | вся работа с SQLite; миграции v1–v3 |
| `app/models.py` | pydantic-модели: `CompanyProfile` (в т.ч. `tenant_slug`), `LeadInfo`, `KnowledgeGap` |
| `app/company_store.py` | YAML-профили компаний; `list_for_tenant()`, `slugs_for_tenant()` |
| `app/auth.py` | `AuthService`, роли, PBKDF2-пароли, cookie-сессии |
| `app/bot_manager.py` | `BotManager`: свой Bot+Dispatcher на каждого тенанта, `restart(slug)` |
| `app/telegram_bot.py` | хендлеры Telegram, `create_dispatcher(..., tenant_slug=, admin_ids=)` |
| `app/avito.py` | Avito Messenger API + вебхук |
| `app/llm.py` | `LLMService.generate_reply()` — вызов LLM с tool-calling и guardrails |
| `app/dashboard.py` | все HTTP-роуты кабинета (`/api/*`) + рендер HTML |
| `app/server.py` | `create_app()` — FastAPI, вебхук Авито, `/health` |
| `app/templates/` | `dashboard.html`, `login.html`, `landing.html` |
| `tests/` | `test_core.py`, `test_hardening.py`, `test_multitenant.py` |

### Схема БД (актуальная)

```
schema_migrations(version, name, applied_at)
sessions(channel, chat_id, company_slug, paused_until, updated_at)   PK(channel, chat_id)
messages(id, channel, chat_id, role, content, created_at)
leads(id, channel, chat_id, company_slug, client_name, phone, car_model, service_interest,
      estimated_price, preferred_time, summary, avito_chat_url, created_at)
knowledge_gaps(id, channel, chat_id, company_slug, question, customer_message,
               suggested_answer, status, created_at)
processed_events(channel, message_id, status, created_at)            PK(channel, message_id)
bot_sent_messages(id, channel, chat_id, message_id, text_hash, status, created_at)
avito_accounts(id, company_slug, user_id, client_id, client_secret, webhook_secret,
               enabled, last_event_at, created_at)
guard_events(id, channel, chat_id, company_slug, original_reply, safe_reply, blocked_terms, created_at)
tenants(id, slug UNIQUE, name, brand_name, telegram_bot_token, bot_username,
        owner_telegram_id, plan, status, created_at)
users(id, username UNIQUE, password_hash, role, tenant_slug, is_active, created_at)
user_sessions(token PK, user_id, created_at, expires_at)
```

### Роли и изоляция

- `platform_admin` — всё; `client_admin` — свой тенант; `client_viewer` — только чтение.
- Изоляция выполняется в SQL через `_scope(company_slugs)` в `app/storage.py`
  и `company_store.slugs_for_tenant(tenant_slug)` в `app/dashboard.py`.

### Прод-окружение

- `docker compose` cервисы: `app` (внутри 8080) и `caddy` (80 → внешний, `127.0.0.1:9443` → 443 внутрь).
- Публичный вход: `https://77.221.155.74.sslip.io` (сертификат Let's Encrypt, автообновление).
- На 443 снаружи стоит **Xray (VLESS+Reality)** и проксирует обычный HTTPS в Caddy на `127.0.0.1:9443`.
  **Не занимать 443 и не менять конфиг Xray.**
- Сторож: `/etc/cron.d/bot-watchdog` + `/usr/local/bin/bot-watchdog.sh` — каждые 5 минут
  проверяет публичный `/health` и перезапускает контейнеры, уважая метку `/opt/BotAvitoTelegram/.maintenance`.
- SSH: `ssh aeza-vps` (ключ `~/.ssh/id_vps_aeza`, пароль не нужен).

---

## 1. Общие правила для исполнителя

1. **Не ломать мультитенантность.** Любой новый запрос к данным обязан фильтроваться по
   `company_slugs` (или `tenant_slug`), если его вызывает не `platform_admin`.
2. **Не ломать обратную совместимость.** HTTP Basic (`DASHBOARD_USER`/`DASHBOARD_PASSWORD`) должен
   продолжать работать: им пользуются скрипты и тесты.
3. **Миграции — только вперёд.** Новые таблицы добавлять как `_migration_v4/5/...`
   в `Storage._init_db()`, не редактируя старые миграции.
4. **Каждая фича — с тестами.** Прогон `pytest -q` перед сдачей; все прежние 44 теста обязаны
   остаться зелёными.
5. **Без новых тяжёлых зависимостей.** Разрешено: `httpx` (уже есть), стандартная библиотека.
   Для S3 — только если явно согласовано.
6. **Тексты интерфейса — на русском**, в стиле существующего дашборда.
7. **Не трогать `.env` на сервере** без явной необходимости: там боевые токены.
8. **Секреты не логировать** (токены ботов, пароли, `client_secret`).

---

## 2. Блок A. Бэкапы и мониторинг

### Цель

Данные клиентов (заявки, переписки) не должны теряться, а падение бота не должно оставаться
незамеченным дольше 10 минут.

### Текущее состояние (факты)

- `scripts/backup.py` умеет `create_backup(db_path, companies_dir, backups_dir, keep_count=14)`:
  делает консистентную копию SQLite (`sqlite3.backup`), пакует вместе с YAML компаний,
  чистит старые архивы. **Нигде не вызывается.**
- `scripts/backup.sh` — обёртка, тоже не подключена.
- `docker-compose.yml` содержит healthcheck на `/health` каждые 30 с, `restart: unless-stopped`.
- Внешний cron-сторож перезапускает контейнеры, но **никому не сообщает** о проблеме.
- Эндпоинт `/health` возвращает только `{"status": "ok"}` — не проверяет БД и ботов.

### Объём работ

**A1. Планировщик бэкапов в приложении.**

- Новый модуль `app/maintenance.py`, класс `BackupScheduler`:
  - asyncio-задача в `main.py`, запускается вместе с приложением;
  - интервал из настроек `BACKUP_INTERVAL_HOURS` (по умолчанию 6), `BACKUP_KEEP` (по умолчанию 14);
  - вызывает существующий `create_backup()` (переиспользовать, не дублировать логику);
  - после успеха пишет строку в новую таблицу `backup_runs` (см. ниже);
  - ошибки логирует и **не роняют** приложение.
- Каталог: `BACKUP_DIR` (по умолчанию `data/backups`, уже смонтирован томом `./data`).

**A2. Выгрузка копии за пределы сервера (обязательно).**

Копия на том же диске не спасает от отказа VPS. Реализовать **один** из вариантов,
выбор — через `BACKUP_REMOTE` в `.env`:

- `BACKUP_REMOTE=telegram` — отправка архива документа в Telegram владельцу платформы
  (`platform_admin` с `owner_telegram_id`, либо `DEFAULT_NOTIFY_CHAT_ID`). Лимит Bot API — 50 МБ,
  поэтому перед отправкой проверять размер и при превышении отправлять только БД без YAML.
- `BACKUP_REMOTE=local` — только локальные архивы (по умолчанию, пока нет хранилища).

**A3. Глубокий healthcheck.**

- Новый эндпоинт `GET /health/deep` (без авторизации, но без утечки деталей; отдаёт только
  статусы), возвращает:

```json
{
  "status": "ok|degraded|fail",
  "db": {"ok": true, "latency_ms": 3},
  "bots": {"total": 2, "alive": 2, "dead": []},
  "disk": {"free_mb": 24000, "used_pct": 20},
  "queue": {"avito_pending": 0},
  "last_backup": {"at": "2026-11-21T08:00:00Z", "age_hours": 5.2}
}
```

- `status = fail`, если БД недоступна или свободно < 500 МБ; `degraded`, если есть мёртвый бот
  или последний бэкап старше `2 × BACKUP_INTERVAL_HOURS`.
- `/health` оставить как есть (его дёргает Docker healthcheck и внешний сторож) — не усложнять.

**A4. Алерты в Telegram.**

- Новый модуль `app/alerting.py`: `AlertManager` с антифлудом (не чаще одного сообщения
  на тип события в 30 минут).
- Источники событий:
  - бот тенанта не поднялся при старте / упал (`BotManager._poll_forever` уже ловит исключения);
  - `health/deep` вернул `fail`/`degraded` при периодической самопроверке (раз в 5 минут, задача в `main.py`);
  - бэкап не удался.
- Получатель: `DEFAULT_NOTIFY_CHAT_ID` (владелец платформы). Отправка — через
  `BotManager.primary_bot()`; если бота нет — только лог.

**A5. Ротация логов.**

- В `docker-compose.yml` логи уже ограничены (`max-size: 10m`, `max-file: 3`) — проверить, что
  для `app` это так; если нет — добавить.
- Внутри приложения не писать файлы логов (только stdout) — сохранить это правило.

### Модель данных

```sql
-- миграция v4
CREATE TABLE IF NOT EXISTS backup_runs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    started_at TEXT NOT NULL,
    finished_at TEXT,
    status TEXT NOT NULL DEFAULT 'running',   -- running | ok | failed | uploaded
    size_bytes INTEGER,
    path TEXT,
    remote TEXT,                              -- local | telegram | none
    error TEXT
);
```

### Настройки (`.env`)

```
BACKUP_INTERVAL_HOURS=6
BACKUP_KEEP=14
BACKUP_DIR=data/backups
BACKUP_REMOTE=local
ALERTS_ENABLED=true
ALERT_COOLDOWN_MINUTES=30
DISK_WARN_PCT=90
```

### Критерии приёмки

1. `pytest` зелёный; новые тесты:
   - `test_backup_creates_readable_sqlite` — после `create_backup()` архив открывается, в нём
     есть таблица `leads` с теми же строками;
   - `test_backup_rotation_keeps_n` — при `keep_count=2` остаётся ровно 2 архива;
   - `test_health_deep_reports_bots` — с `BotManager` из фейка: 2 живых → `ok`, 1 мёртвый → `degraded`;
   - `test_alert_cooldown` — два одинаковых события подряд дают одно сообщение.
2. На проде: `curl https://77.221.155.74.sslip.io/health/deep` отдаёт JSON со всеми полями.
3. Ручной тест бэкапа: `docker exec botavitotelegram-app-1 python -c "from scripts.backup import create_backup; print(create_backup())"` → архив появился в `data/backups`.
4. Восстановление проверено: распаковать архив в отдельную папку, открыть `sqlite3 bot.db ".tables"`
   — таблицы на месте. **Это обязательный шаг, без него блок не принимается.**
5. Убить бота (`docker compose stop app`) → в течение 10 минут в Telegram пришёл алерт.

---

## 3. Блок B. Учёт токенов и тарифы

### Цель

Знать, сколько каждый клиент тратит на LLM, ограничивать расход по тарифу и показывать это
в кабинете. Без этого подписку продавать нельзя.

### Текущее состояние (факты)

- `LLMService.generate_reply()` вызывает `self.client.chat.completions.create(...)` через
  `_call_llm_with_retry()` и **не читает `response.usage`**.
- Учёт токенов отсутствует полностью: `grep -i "usage|tokens"` по `app/llm.py` даёт только
  сигнатуры методов.
- `tenants.plan` — строка (`basic`/`internal`), никак не используется.
- Модель задаётся глобально: `LLM_MODEL` в `.env`.

### Модель данных

```sql
-- миграция v4 (вместе с block A)
CREATE TABLE IF NOT EXISTS llm_usage (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    tenant_slug TEXT NOT NULL,
    company_slug TEXT NOT NULL,
    channel TEXT NOT NULL,          -- telegram | avito
    chat_id TEXT,
    model TEXT NOT NULL,
    prompt_tokens INTEGER NOT NULL DEFAULT 0,
    completion_tokens INTEGER NOT NULL DEFAULT 0,
    total_tokens INTEGER NOT NULL DEFAULT 0,
    cost_rub REAL NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_usage_tenant_time ON llm_usage (tenant_slug, created_at);

CREATE TABLE IF NOT EXISTS model_prices (
    model TEXT PRIMARY KEY,
    prompt_rub_per_1k REAL NOT NULL DEFAULT 0,
    completion_rub_per_1k REAL NOT NULL DEFAULT 0,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS plans (
    slug TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    monthly_rub REAL NOT NULL DEFAULT 0,
    included_messages INTEGER,       -- NULL = без лимита
    included_tokens INTEGER,
    overage_rub_per_1k REAL NOT NULL DEFAULT 0,
    is_active INTEGER NOT NULL DEFAULT 1
);
```

- Заполнить `plans` сидом: `basic` (0 ₽, 1000 сообщений/мес), `pro` (2990 ₽, 5000 сообщений),
  `internal` (без лимитов). Цены — заглушки, владелец платформы поправит.
- `model_prices` сидировать нулями; владелец заполняет фактические цены своего провайдера.

### Объём работ

**B1. Захват usage.**

- В `LLMService.generate_reply()` после успешного ответа читать `response.usage`
  (`prompt_tokens`, `completion_tokens`, `total_tokens`). Если провайдер вернул `None` —
  считать по эвристике `len(text)//4` и помечать `model="unknown-usage"`.
- Записывать через `Storage.record_llm_usage(...)`. `tenant_slug` брать из
  `company.tenant_slug or "default"` (поле уже есть в `CompanyProfile`).
- **Не добавлять сетевых вызовов** в горячий путь: запись в SQLite — единственное действие.

**B2. Стоимость.**

- `Storage.get_model_price(model)` → `(prompt_rub_per_1k, completion_rub_per_1k)`.
- Расчёт: `cost = prompt/1000*p + completion/1000*c`. Округлять до 4 знаков.
- Кэшировать цены в памяти процесса на 5 минут (чтобы не читать БД на каждый запрос).

**B3. Лимиты тарифа.**

- Новый модуль `app/quotas.py`:
  ```python
  class QuotaService:
      def check(self, tenant_slug: str) -> QuotaStatus
      # QuotaStatus(allowed: bool, reason: str, used_messages: int,
      #            limit_messages: int | None, used_tokens: int, limit_tokens: int | None,
      #            used_rub: float, soft_limit: bool)
  ```
- Проверка **перед** вызовом LLM в `telegram_bot.handle_text` и `avito`-хендлере:
  если `allowed is False` — не звонить в LLM, ответить клиенту нейтрально
  («Передал ваш запрос менеджеру, он свяжется с вами») и записать событие `quota_blocked`
  в `guard_events` (переиспользовать таблицу, `blocked_terms='quota'`).
- Мягкий порог: при 80 % лимита — алерт владельцу платформы (через `AlertManager` из блока A),
  при 100 % — блокировка до начала следующего месяца (UTC).
- `platform_admin` и план `internal` — без лимитов.

**B4. API кабинета.**

| Метод | Путь | Доступ | Ответ |
|---|---|---|---|
| GET | `/api/usage?days=30` | любой вошедший | своя сводка: `messages`, `tokens`, `cost_rub`, `by_model[]`, `by_day[]` |
| GET | `/api/usage/tenants?days=30` | `platform_admin` | по всем клиентам + `plan`, `limit_pct` |
| GET | `/api/plans` | `platform_admin` | список тарифов |
| POST | `/api/plans` | `platform_admin` | создать/обновить тариф |
| POST | `/api/tenants/{slug}/plan` | `platform_admin` | `{"plan": "pro"}` — сменить тариф клиента |
| GET | `/api/model-prices` | `platform_admin` | цены моделей |
| POST | `/api/model-prices` | `platform_admin` | `{"model": "...", "prompt_rub_per_1k": 0.1, "completion_rub_per_1k": 0.3}` |

Все запросы — с изоляцией: клиент видит только `tenant_slug = свой`.

**B5. UI.**

- В дашборд добавить вкладку **«Расходы»**:
  - карточки: сообщений за месяц, токенов, стоимость, % от лимита (прогресс-бар);
  - таблица по моделям;
  - для `platform_admin` — таблица по клиентам с сортировкой по расходу.
- В существующую таблицу клиентов (вкладка «Клиенты и боты») добавить колонки
  «Тариф» и «Расход за месяц».

### Критерии приёмки

1. Тесты:
   - `test_usage_recorded_after_llm_call` — фейковый LLM-ответ с `usage` → строка в `llm_usage`
     с верными токенами и `tenant_slug`;
   - `test_cost_calculation` — при цене 0.1/0.3 руб. за 1k и 1000/500 токенов → 0.25 руб.;
   - `test_quota_blocks_over_limit` — лимит 2 сообщения, третье не вызывает LLM и логируется;
   - `test_quota_ignores_platform_admin` — у `internal` лимитов нет;
   - `test_usage_api_scoped` — клиент A не видит расход клиента B.
2. На проде после деплоя: отправить боту 2–3 сообщения, затем
   `GET /api/usage` под админом клиента → ненулевые токены и стоимость.
3. `platform_admin` видит сводку по всем клиентам, `client_admin` — только свою (проверить
   через два разных cookie).

---

## 4. Блок C. Мастер онбординга

### Цель

Подключение нового клиента без участия разработчика: 5 шагов в браузере вместо ручного
заполнения 20+ полей YAML.

### Текущее состояние (факты)

- Компания создаётся через `POST /api/companies` (модель `CreateCompanyRequest`,
  в `app/dashboard.py`) либо копированием `companies/_template.yaml`.
- `CompanyProfile` содержит ~30 полей; клиент физически не знает, что заполнять.
- Нет пошагового состояния: если клиент ушёл на середине, начать придётся заново.

### Объём работ

**C1. Состояние онбординга.**

```sql
-- миграция v5
CREATE TABLE IF NOT EXISTS onboarding (
    tenant_slug TEXT PRIMARY KEY,
    step INTEGER NOT NULL DEFAULT 1,
    draft_json TEXT NOT NULL DEFAULT '{}',
    completed_at TEXT,
    updated_at TEXT NOT NULL
);
```

**C2. Шаги мастера.**

| Шаг | Что спрашиваем | Валидация |
|---|---|---|
| 1. Бизнес | название, город, ниша, адрес | название и город обязательны |
| 2. Прайс | текст/CSV «услуга — цена», либо список вручную | минимум 1 позиция; парсер терпим к `15000`, `15 000 ₽`, `от 15000` |
| 3. Часы | график, обед, выходные, «круглосуточно» | корректные часы 0–23 |
| 4. Тон | имя менеджера, роль, обращение, скидка | по умолчанию из шаблона |
| 5. Проверка | предпросмотр приветствия + тестовый диалог | обязательный клик «Проверить» |

- Шаг можно пропустить назад/вперёд; черновик сохраняется после каждого шага.
- На шаге 5 — кнопка «Отправить тестовое сообщение» (если у тенанта есть бот) и
  «Создать компанию».
- По завершении: создаётся `CompanyProfile` (slug генерируется из названия, транслит,
  при конфликте — суффикс `-2`), `onboarding.completed_at` заполняется, редирект на
  «Компании и ссылки» с тостом.

**C3. Парсер прайса (отдельный модуль `app/price_parser.py`).**

- Вход: многострочный текст. Формат строки: `<название> <разделитель> <цена>`.
- Разделители: `—`, `-`, `:`, `|`, табуляция, две и более точек.
- Цена: `15 000`, `15000`, `15.000`, `от 15 000 ₽`, `15000р`.
- Выход: `list[str]` в формате `"Название: от 15 000 ₽"` (как ждёт `CompanyProfile.services`).
- Строки без цены **не терять**: складывать в `advantages` или в `faq` с пометкой.

**C4. API.**

| Метод | Путь | Назначение |
|---|---|---|
| GET | `/api/onboarding` | текущий шаг + черновик (создаёт запись при первом обращении) |
| POST | `/api/onboarding/step` | `{"step": 2, "data": {...}}` — сохранить шаг и поля |
| POST | `/api/onboarding/parse-price` | `{"text": "..."}` → распознанные услуги |
| POST | `/api/onboarding/test-dialog` | `{"message": "..."}` → ответ LLM по черновику (без сохранения в историю) |
| POST | `/api/onboarding/complete` | создаёт компанию, возвращает `slug` и `demo_link` |

Все — `require_writer`, изоляция по `tenant_slug`.

**C5. UI.**

- Новая страница `/onboarding` (отдельный шаблон `onboarding.html`, не встраивать в 1600-строчный
  `dashboard.html`): прогресс-бар 1–5, форма шага, кнопки «Назад / Далее».
- Если у тенанта ещё нет компаний и онбординг не завершён — при входе в `/` редиректить на
  `/onboarding` (но не блокировать доступ к остальным вкладкам).
- Владелец платформы видит онбординг своих клиентов в их карточке (статус: шаг N / завершён).

### Критерии приёмки

1. Тесты:
   - `test_price_parser_handles_formats` — 6–8 вариантов записи цены, включая «от 15 000 ₽»;
   - `test_onboarding_state_persists` — после `POST /step` и нового запроса `GET` шаг и черновик на месте;
   - `test_onboarding_complete_creates_company` — компания появилась с правильным `tenant_slug`
     и заполненными `services`;
   - `test_onboarding_scoped_by_tenant` — клиент A не может читать/менять черновик B;
   - `test_onboarding_slug_collision_gets_suffix` — второй клиент с тем же названием получает `-2`;
   - `test_viewer_cannot_onboard` — 403.
2. Ручной прогон на проде: создать тестового клиента, пройти 5 шагов, получить рабочую
   демо-ссылку, отправить боту сообщение и увидеть ответ по своим услугам.
3. После завершения онбординга повторный вход в `/` больше не редиректит на мастер.

---

## 5. Порядок выполнения и Definition of Done

Рекомендуемый порядок (каждый блок — отдельный коммит/деплой):

1. **A** (бэкапы + мониторинг) — снимает риск потери данных, делается за 1–1,5 дня.
2. **B** (учёт токенов и тарифы) — 2–3 дня, зависит от A только через `AlertManager`.
3. **C** (мастер онбординга) — 2–3 дня, независим, но удобнее после B (в мастере показывать тариф).

### Definition of Done для каждого блока

- [ ] Миграция БД добавлена и применяется на существующей базе без потери данных.
- [ ] `pytest -q` — все тесты зелёные (было 44 + новые).
- [ ] Новые эндпоинты защищены нужной ролью и изолированы по тенанту.
- [ ] `README.md` обновлён: настройки, новые эндпоинты, как проверить.
- [ ] Задеплоено на `77.221.155.74`, контейнер `healthy`, публичный `/health` = 200.
- [ ] Проверено вживую, результаты приложены в отчёт (что именно дёргали и что получили).
- [ ] Внешний вид не сломан: дашборд открывается, вкладки переключаются, вход/выход работают.

### Деплой (как это делается в проекте)

```powershell
# локально, из F:\BotAvitoTelegram
tar -czf $env:TEMP\botapp.tgz --exclude="__pycache__" app main.py requirements.txt tests
scp $env:TEMP\botapp.tgz aeza-vps:/root/botapp.tgz
Get-Content -Raw scripts\_deploy_multibot.sh | ssh aeza-vps "bash -s"
```

`scripts/_deploy_multibot.sh` делает бэкап кода и БД в `/root/backups`, распаковывает архив,
пересобирает контейнер и ждёт `healthy`. **Не удалять этот скрипт.**

### Что нельзя делать

- Занимать порт 443, менять конфиг Xray, останавливать Caddy.
- Удалять таблицы или переписывать миграции v1–v3.
- Класть токены/пароли в код, тесты, README или git.
- Отключать проверку ролей «чтобы работало».
- Делать `DROP`/`DELETE` боевых данных при отладке.

---

## 6. Как отчитаться

В отчёте по каждому блоку:

1. Список изменённых/добавленных файлов.
2. Вывод `pytest -q` (последняя строка).
3. Вывод проверок на проде (команда + ответ).
4. Что осталось не сделано и почему.

Если что-то в ТЗ противоречит фактическому коду — **сначала уточнить**, потом делать.
