#!/bin/zsh
set -euo pipefail

script_dir="${0:A:h}"
venv_dir="${TMPDIR:-/tmp}/goofish-multi-clone-venv"
local_config="$script_dir/.local.env"

if [[ -f "$local_config" ]]; then
  set -a
  source "$local_config"
  set +a
fi

if [[ ! -x "$venv_dir/bin/python" ]]; then
  python3 -m venv "$venv_dir"
fi

if ! "$venv_dir/bin/python" -c 'import lief, PIL' >/dev/null 2>&1; then
  "$venv_dir/bin/python" -m pip install --disable-pip-version-check 'lief==1.0.0' 'Pillow>=10,<13'
fi

exec "$venv_dir/bin/python" "$script_dir/clone_goofish.py" "$@"
