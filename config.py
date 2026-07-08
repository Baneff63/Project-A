import json
import os
from datetime import datetime
from pathlib import Path

from dotenv import load_dotenv

try:
    from zoneinfo import ZoneInfo
except ImportError:
    from backports.zoneinfo import ZoneInfo

BASE_DIR = Path(__file__).resolve().parent
load_dotenv(BASE_DIR / ".env")


def _env(name: str, default: str = "") -> str:
    return os.getenv(name, default).strip()


_persist_dir = _env("DATA_DIR")
if _persist_dir:
    DATA_DIR = Path(_persist_dir)
elif Path("/data").is_dir():
    DATA_DIR = Path("/data")
else:
    DATA_DIR = BASE_DIR / "data"
DATA_DIR.mkdir(parents=True, exist_ok=True)

SESSION_FILE = BASE_DIR / "playwright-state.json"
DB_FILE = DATA_DIR / "applications.db"
SITE_CONFIG_FILE = BASE_DIR / "site_config.json"


SITE_URL = _env("SITE_URL")
SITE_LOGIN_URL = _env("SITE_LOGIN_URL") or SITE_URL
LOGIN_USERNAME = _env("LOGIN_USERNAME")
LOGIN_PASSWORD = _env("LOGIN_PASSWORD")
TOTP_SECRET = _env("TOTP_SECRET")

NOTIFY_LOG_FILE = DATA_DIR / "notifications.log"

NOTIFY_MODE = _env("NOTIFY_MODE", "telegram").lower()
TELEGRAM_BOT_TOKEN = _env("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT_ID = _env("TELEGRAM_CHAT_ID")
TELEGRAM_PROXY = _env("TELEGRAM_PROXY")

CHECK_INTERVAL_SECONDS = int(_env("CHECK_INTERVAL_SECONDS", "60"))
HEADLESS = _env("HEADLESS", "true").lower() in {"1", "true", "yes", "on"}



APP_TZ_NAME = _env("APP_TIMEZONE", "Europe/Samara")
APP_TZ = ZoneInfo(APP_TZ_NAME)


def now_local() -> datetime:

    return datetime.now(APP_TZ).replace(tzinfo=None)



samara_now = now_local


def load_site_config() -> dict:
    if not SITE_CONFIG_FILE.exists():
        example = BASE_DIR / "site_config.example.json"
        if example.exists():
            return json.loads(example.read_text(encoding="utf-8"))
        return {}
    return json.loads(SITE_CONFIG_FILE.read_text(encoding="utf-8"))
