from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from dotenv import dotenv_values


ROOT = Path(__file__).resolve().parents[2]


def _load_dotenv(path: Path) -> None:
    if not path.exists():
        return
    for key, value in dotenv_values(path).items():
        if value is not None:
            os.environ.setdefault(key, value)


_load_dotenv(ROOT / ".env")


def _path_env(name: str, default: str) -> Path:
    value = os.environ.get(name, default)
    path = Path(value).expanduser()
    return path if path.is_absolute() else ROOT / path


@dataclass(frozen=True)
class Settings:
    host: str = os.environ.get("APP_MONITOR_HOST", "127.0.0.1")
    port: int = int(os.environ.get("APP_MONITOR_PORT", "8765"))
    timezone: str = os.environ.get("APP_MONITOR_TIMEZONE", "Asia/Shanghai")
    database: Path = _path_env("APP_MONITOR_DATABASE", "data/app-monitor.sqlite3")
    quote_hour: int = int(os.environ.get("APP_MONITOR_QUOTE_HOUR", "9"))
    quote_minute: int = int(os.environ.get("APP_MONITOR_QUOTE_MINUTE", "0"))
    quote_api_root: str = os.environ.get("APP_MONITOR_QUOTE_API_ROOT", "https://api.zuphone.com/biji-product/api/home").rstrip("/")
    quote_app_id: str = os.environ.get("APP_MONITOR_QUOTE_APP_ID", "wx5d70a654307f7bbf").strip()
    quote_platform: str = os.environ.get("APP_MONITOR_QUOTE_PLATFORM", "weapp-lite-aiji-report").strip()
    bundle_id: str = os.environ.get("GOOFISH_BUNDLE_ID", "com.taobao.fleamarket")
    track_db: Path = _path_env(
        "GOOFISH_TRACK_DB",
        "~/Library/Containers/com.taobao.fleamarket/Data/Library/walle_ut_user_track.db",
    )
    appium_server_url: str = os.environ.get("APPIUM_SERVER_URL", "http://127.0.0.1:4723").rstrip("/")
    ios_wda_bundle_id: str = os.environ.get("IOS_WDA_BUNDLE_ID", "").strip()
    ios_wda_derived_data_path: Path = _path_env("IOS_WDA_DERIVED_DATA_PATH", "/tmp/goofish-wda-probe")
    proxy_runtime: Path = _path_env("APP_MONITOR_PROXY_RUNTIME", ".runtime/proxy")
    proxy_listen_host: str = os.environ.get("APP_MONITOR_PROXY_HOST", "0.0.0.0")
    proxy_port: int = int(os.environ.get("APP_MONITOR_PROXY_PORT", "8899"))
    ai_api_key: str = os.environ.get("APP_MONITOR_AI_API_KEY", "").strip()
    swift_sdk: str = os.environ.get("APP_MONITOR_SWIFT_SDK", "").strip()
    ntfy_topic_url: str = os.environ.get("NTFY_TOPIC_URL", "").strip()
    bark_url: str = os.environ.get("BARK_URL", "").strip()
    webhook_url: str = os.environ.get("WEBHOOK_URL", "").strip()
    webhook_method: str = os.environ.get("WEBHOOK_METHOD", "POST").strip().upper()
    webhook_headers: str = os.environ.get("WEBHOOK_HEADERS", "").strip()
    webhook_content_type: str = os.environ.get("WEBHOOK_CONTENT_TYPE", "JSON").strip().upper()
    webhook_query_parameters: str = os.environ.get("WEBHOOK_QUERY_PARAMETERS", "").strip()
    webhook_body: str = os.environ.get("WEBHOOK_BODY", "").strip()

    @property
    def data_dir(self) -> Path:
        return self.database.parent

    @property
    def quote_dir(self) -> Path:
        return self.data_dir / "daily-quotes"


settings = Settings()
