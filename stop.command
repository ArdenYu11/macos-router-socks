#!/bin/bash
set -euo pipefail
task_dir="$(cd "$(dirname "$0")" && pwd)"
task_python="${PYTHON_BIN:-$(command -v python3 || true)}"
if [ -z "$task_python" ]; then
  printf '%s\n' '未找到 Python 3。' >&2
  exit 1
fi
exec "$task_python" "$task_dir/service_control.py" stop "$@"
