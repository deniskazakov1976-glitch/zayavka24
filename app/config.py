from pathlib import Path
from typing import List, Union

from pydantic import field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    # LLM (ProxyAPI / VseGPT / OpenAI / AnyModel)
    llm_base_url: str = "https://api.proxyapi.ru/openai/v1"
    llm_api_key: str = ""
    llm_model: str = "gc/gemini-2.5-flash"
    llm_fallback_models: Union[List[str], str] = ["cx/gpt-6-luna", "ds/deepseek-v4-flash-vision"]
    llm_temperature: float = 0.5

    # Telegram
    telegram_bot_token: str = ""
    admin_ids: List[int] = []
    default_notify_chat_id: int | None = None
    default_company_slug: str = "demo_detailing"

    # Avito Messenger API
    avito_enabled: bool = False
    avito_client_id: str = ""
    avito_client_secret: str = ""
    avito_user_id: int | None = None
    avito_default_company_slug: str = "demo_detailing"
    avito_manual_pause_hours: int = 24

    # Webhook server & Dashboard
    webhook_host: str = "0.0.0.0"
    webhook_port: int = 8080
    dashboard_enabled: bool = True
    dashboard_user: str = "admin"
    dashboard_password: str = ""
    avito_webhook_secret: str = ""
    public_base_url: str = ""

    # Reliability & Guardrails
    llm_timeout_sec: float = 25.0
    guardrails_mode: str = "enforce"
    disclose_ai: bool = True
    vision_enabled: bool = True

    # 152-FZ & Retention
    data_retention_days: int = 90
    lead_retention_days: int = 365

    # Storage
    db_path: Path = Path("data/bot.db")
    companies_dir: Path = Path("companies")
    max_history_messages: int = 14

    # Backups & Alerting
    backup_interval_hours: int = 6
    backup_keep: int = 14
    backup_dir: Path = Path("data/backups")
    backup_remote: str = "local"
    alerts_enabled: bool = True
    alert_cooldown_minutes: int = 30
    disk_warn_pct: int = 90
    disk_crit_free_mb: int = 500

    # Цены LLM за 1k токенов (рубли). Используются как значение по умолчанию
    # для модели из LLM_MODEL, если в таблице model_prices нет своей строки.
    llm_price_prompt_rub_per_1k: float = 0.15
    llm_price_completion_rub_per_1k: float = 0.60

    @field_validator("avito_user_id", "default_notify_chat_id", mode="before")
    @classmethod
    def empty_str_to_none(cls, value: object) -> object:
        if value == "":
            return None
        return value

    @field_validator("admin_ids", mode="before")
    @classmethod
    def parse_admin_ids(cls, value: object) -> List[int]:
        if value is None or value == "":
            return []
        if isinstance(value, int):
            return [value]
        if isinstance(value, str):
            return [int(item.strip()) for item in value.split(",") if item.strip()]
        if isinstance(value, list):
            return [int(item) for item in value]
        return []

    @field_validator("llm_fallback_models", mode="after")
    @classmethod
    def parse_fallback_models(cls, value: object) -> List[str]:
        if value is None or value == "":
            return []
        if isinstance(value, str):
            return [item.strip() for item in value.split(",") if item.strip()]
        if isinstance(value, list):
            return [str(item).strip() for item in value if str(item).strip()]
        return []


def get_settings() -> Settings:
    return Settings()


def update_env_file(updates: dict[str, str], env_path: Path = Path(".env")) -> None:
    """Updates or appends key-value pairs in the .env file while preserving comments."""
    if not env_path.exists():
        lines = []
    else:
        lines = env_path.read_text(encoding="utf-8").splitlines()

    new_lines = []
    seen = set()
    for line in lines:
        stripped = line.strip()
        if stripped and not stripped.startswith("#") and "=" in stripped:
            key = stripped.split("=", 1)[0].strip()
            if key in updates:
                new_lines.append(f"{key}={updates[key]}")
                seen.add(key)
                continue
        new_lines.append(line)

    for k, v in updates.items():
        if k not in seen:
            new_lines.append(f"{k}={v}")

    env_path.write_text("\n".join(new_lines) + "\n", encoding="utf-8")
