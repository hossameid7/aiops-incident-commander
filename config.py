"""
config.py - Centralised configuration management using pydantic-settings.
All settings are loaded from environment variables or the .env file.
"""

import logging
import sys
from functools import lru_cache

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Application-wide settings resolved from environment variables."""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
    )

    # --- LLM ---
    groq_api_key: str = Field(..., description="Groq Cloud API key")
    groq_model: str = Field("llama-3.3-70b-versatile", description="Groq model identifier")

    # --- Telegram ---
    telegram_bot_token: str = Field(..., description="Telegram Bot API token")
    telegram_chat_id: str = Field("", description="Default Telegram chat ID for alerts")

    # --- FastAPI ---
    api_host: str = Field("0.0.0.0", description="Uvicorn bind address")
    api_port: int = Field(8000, description="Uvicorn bind port")

    # --- ChromaDB ---
    chroma_persist_dir: str = Field("./chroma_db", description="ChromaDB on-disk persistence path")
    chroma_collection_name: str = Field("runbooks", description="ChromaDB collection name")

    # --- Remediation ---
    dry_run: bool = Field(True, description="When True, commands are logged but not executed")
    ansible_playbook_dir: str = Field("./playbooks", description="Directory containing Ansible playbooks")

    # --- Logging ---
    log_level: str = Field("INFO", description="Python logging level")


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Return a cached singleton Settings instance."""
    return Settings()


def configure_logging(settings: Settings) -> None:
    """Configure the root logger based on settings."""
    numeric_level = getattr(logging, settings.log_level.upper(), logging.INFO)
    logging.basicConfig(
        level=numeric_level,
        format="%(asctime)s | %(levelname)-8s | %(name)s | %(message)s",
        datefmt="%Y-%m-%dT%H:%M:%S",
        stream=sys.stdout,
    )
    # Silence noisy third-party loggers
    for noisy in ("httpx", "httpcore", "urllib3", "chromadb.telemetry"):
        logging.getLogger(noisy).setLevel(logging.WARNING)
