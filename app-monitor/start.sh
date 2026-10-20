#!/usr/bin/env bash

set -euo pipefail

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"

if ! command -v uv >/dev/null 2>&1; then
  echo "未找到 uv，请先安装：https://docs.astral.sh/uv/getting-started/installation/" >&2
  exit 1
fi

exec uv run app-monitor "$@"
