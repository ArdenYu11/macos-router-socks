#!/bin/bash
set -euo pipefail
task_dir="$(cd "$(dirname "$0")" && pwd)"
task_python="${PYTHON_BIN:-$(command -v python3 || true)}"
if [ -z "$task_python" ]; then
  printf '%s\n' '未找到 Python 3，请安装 Python 3.9 或更高版本。' >&2
  exit 1
fi
exec "$task_python" "$task_dir/service_control.py" start "$@"
