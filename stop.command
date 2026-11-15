#!/bin/bash
set -euo pipefail
task_dir="$(cd "$(dirname "$0")" && pwd)"
task_python="${PYTHON_BIN:-$(command -v python3 || true)}"
if [ -z "$task_python" ]; then
  printf '%s\n' '未找到 Python 3。' >&2
  exit 1
fi
exec "$task_python" - "$task_dir" <<'PY'
from pathlib import Path
import os
import signal
import subprocess
import sys
import time

task_dir = Path(sys.argv[1])
pid_file = task_dir / "socks.pid"
script = str(task_dir / "router_socks.py")
if not pid_file.exists():
    sys.exit("没有本脚本记录的服务进程。")
try:
    pid = int(pid_file.read_text().strip())
except ValueError:
    sys.exit("PID 文件无效，未停止任何进程。")
process = subprocess.run(["/bin/ps", "-p", str(pid), "-o", "args="],
                         capture_output=True, text=True)
if pid <= 1 or script not in process.stdout:
    pid_file.unlink(missing_ok=True)
    sys.exit("记录的服务已停止，已清理 PID 文件。")
try:
    os.kill(pid, signal.SIGTERM)
except ProcessLookupError:
    pass

for _ in range(30):
    process = subprocess.run(["/bin/ps", "-p", str(pid), "-o", "args="],
                             capture_output=True, text=True)
    if script not in process.stdout:
        pid_file.unlink(missing_ok=True)
        print("SOCKS 已停止。")
        break
    time.sleep(0.1)
else:
    sys.exit("服务尚未退出，请检查 PID=%d；已保留 PID 文件。" % pid)
PY
