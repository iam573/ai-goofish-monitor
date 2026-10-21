"""Run Swift OCR with an optional project-specific macOS SDK."""
from __future__ import annotations

import os
import subprocess
from pathlib import Path
from typing import Iterable


def run_swift(script: Path, arguments: Iterable[str], *, sdk: str = "", timeout: int = 180):
    command = ["swift"]
    environment = None
    if sdk:
        sdk_path = Path(sdk).expanduser()
        if not sdk_path.is_dir():
            raise RuntimeError(f"APP_MONITOR_SWIFT_SDK 指定的 SDK 不存在：{sdk_path}")
        command.extend(["-sdk", str(sdk_path)])
        # Explicit CLI selection and inherited SDKROOT must describe the same SDK.
        environment = dict(os.environ, SDKROOT=str(sdk_path))
    command.extend([str(script), *arguments])
    return subprocess.run(command, env=environment, capture_output=True, text=True, timeout=timeout, check=False)
