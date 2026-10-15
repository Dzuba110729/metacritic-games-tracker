from functools import lru_cache

from pydantic import SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    anthropic_api_key: SecretStr = SecretStr("")
    llm_model: str = "claude-haiku-5-5"
    llm_daily_budget_usd: float = 3.0
    # Цена Haiku 5.5 за 1M токенов (промт до 100K токенов).
    llm_price_input_per_mtok: float = 0.10
    llm_price_output_per_mtok: float = 0.50
    tz: str = "Europe/Moscow"
    database_url: str = "sqlite:///data/games.db"
    schedule_minutes: int = 60
    force_run_cooldown_min: int = 10
    log_level: str = "INFO"
    # Публичный ключ, вшитый во фронтенд metacritic.com (не секрет).
    metacritic_api_key: str = "1MOZgmNFxvmljaQR1X9KAij9Mo4xAY3u"


@lru_cache
def get_settings() -> Settings:
    return Settings()
