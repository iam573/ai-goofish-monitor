"""iPhone 采集配置：只保存 Appium/WDA 连接参数，不保存任何登录凭据。"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path


def _int_env(name: str, default: int) -> int:
    try:
        return int(str(os.getenv(name, "")).strip() or default)
    except ValueError:
        return default


def _path_env(name: str, default: str = "") -> Path | None:
    value = str(os.getenv(name, default)).strip()
    return Path(value).expanduser().resolve() if value else None


@dataclass(frozen=True)
class IPhoneSettings:
    appium_server_url: str
    wda_bundle_id: str
    wda_derived_data_path: Path | None
    wda_local_port: int
    wda_team_id: str
    wda_project_path: Path | None
    udid: str
    goofish_bundle_id: str
    appium_bin: Path | None
    appium_home: Path | None
    screens_per_page: int
    lock_timeout_seconds: int
    proxy_host: str = "0.0.0.0"
    proxy_port: int = 8899
    proxy_runtime: Path = Path("data/iphone-proxy").resolve()
    proxy_trusted_ca_dir: Path | None = None

    @property
    def proxy_socket_path(self) -> Path:
        return self.proxy_runtime / "events.sock"

    @property
    def proxy_certificate(self) -> Path:
        return self.proxy_runtime / "ca" / "mitmproxy-ca-cert.cer"

    @classmethod
    def from_env(cls) -> "IPhoneSettings":
        return cls(
            appium_server_url=str(os.getenv("APPIUM_SERVER_URL", "http://127.0.0.1:4723")).strip().rstrip("/"),
            wda_bundle_id=str(os.getenv("IOS_WDA_BUNDLE_ID", "")).strip(),
            wda_derived_data_path=_path_env("IOS_WDA_DERIVED_DATA_PATH", "/tmp/goofish-wda-probe"),
            wda_local_port=_int_env("IOS_WDA_LOCAL_PORT", 8101),
            wda_team_id=str(os.getenv("IOS_WDA_TEAM_ID", "")).strip(),
            wda_project_path=_path_env("IOS_WDA_PROJECT_PATH"),
            udid=str(os.getenv("IPHONE_UDID", "")).strip(),
            goofish_bundle_id=str(os.getenv("GOOFISH_BUNDLE_ID", "com.taobao.fleamarket")).strip(),
            appium_bin=_path_env("APPIUM_BIN"),
            appium_home=_path_env("APPIUM_HOME"),
            screens_per_page=max(1, _int_env("IPHONE_SCREENS_PER_PAGE", 3)),
            lock_timeout_seconds=max(0, _int_env("IPHONE_LOCK_TIMEOUT", 1800)),
            proxy_host=str(os.getenv("IPHONE_PROXY_HOST", "")).strip() or "0.0.0.0",
            proxy_port=_int_env("IPHONE_PROXY_PORT", 8899),
            proxy_runtime=_path_env("IPHONE_PROXY_RUNTIME", "data/iphone-proxy"),
            proxy_trusted_ca_dir=_path_env("IPHONE_PROXY_TRUSTED_CA_DIR"),
        )
