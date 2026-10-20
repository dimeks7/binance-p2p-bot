import os
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv

BASE_DIR = Path(__file__).resolve().parent

# Локальний .env (корисно при запуску на своєму ПК)
env_path = BASE_DIR / ".env"
if env_path.exists():
    load_dotenv(env_path)


@dataclass
class Config:
    bot_token: str
    admin_ids: list[int]
    db_path: str
    log_path: str
    binance_base_url: str
    binance_timeout: float
    binance_max_retries: int
    binance_max_pages: int
    monitor_worker_count: int
    monitor_default_interval: int
    history_retention_days: int


def load_config() -> Config:
    # Читаємо BOT_TOKEN тільки з ENV (dotenv вже міг його підставити локально)
    bot_token = os.getenv("BOT_TOKEN", "").strip()
    if not bot_token:
        raise RuntimeError(
            "BOT_TOKEN не задано в змінних середовища (ENV). "
            "На локальній машині задайте його в .env, а на сервері — в Environment/Variables."
        )

    admin_ids_raw = os.getenv("ADMIN_IDS", "").strip()
    admin_ids: list[int] = []
    if admin_ids_raw:
        for part in admin_ids_raw.split(","):
            part = part.strip()
            if part:
                try:
                    admin_ids.append(int(part))
                except ValueError:
                    continue

    db_path = os.getenv("DB_PATH", str(BASE_DIR / "bot.db"))
    log_path = os.getenv("LOG_PATH", str(BASE_DIR / "logs"))
    binance_base_url = os.getenv("BINANCE_BASE_URL", "https://p2p.binance.com")
    binance_timeout = float(os.getenv("BINANCE_TIMEOUT", "10"))
    binance_max_retries = int(os.getenv("BINANCE_MAX_RETRIES", "3"))
    binance_max_pages = int(os.getenv("BINANCE_MAX_PAGES", "5"))
    monitor_worker_count = int(os.getenv("MONITOR_WORKER_COUNT", "1"))
    monitor_default_interval = int(os.getenv("MONITOR_DEFAULT_INTERVAL", "60"))
    history_retention_days = int(os.getenv("HISTORY_RETENTION_DAYS", "7"))

    return Config(
        bot_token=bot_token,
        admin_ids=admin_ids,
        db_path=db_path,
        log_path=log_path,
        binance_base_url=binance_base_url,
        binance_timeout=binance_timeout,
        binance_max_retries=binance_max_retries,
        binance_max_pages=binance_max_pages,
        monitor_worker_count=monitor_worker_count,
        monitor_default_interval=monitor_default_interval,
        history_retention_days=history_retention_days,
    )
