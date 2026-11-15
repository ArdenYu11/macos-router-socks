#!/bin/bash
set -euo pipefail
task_dir="$(cd "$(dirname "$0")" && pwd)"
task_python="${PYTHON_BIN:-$(command -v python3 || true)}"
if [ -z "$task_python" ]; then
  printf '%s\n' '未找到 Python 3，请安装 Python 3.9 或更高版本。' >&2
  exit 1
fi
exec "$task_python" - "$task_dir" "$@" <<'PY'
from pathlib import Path
import socket
import subprocess
import sys
import time

task_dir = Path(sys.argv[1])
script = str(task_dir / "router_socks.py")
pid_file = task_dir / "socks.pid"
log_file = task_dir / "socks.log"

# start.command 参数依次为：物理网卡、路由器 DNS 地址、SOCKS 端口。
# 双击启动使用下列默认值；请按自己的网络调整。
interface = sys.argv[2] if len(sys.argv) > 2 else "en1"
router_dns = sys.argv[3] if len(sys.argv) > 3 else "192.168.1.1"
try:
    port = int(sys.argv[4]) if len(sys.argv) > 4 else 10808
    if not 1 <= port <= 65535:
        raise ValueError
except ValueError:
    sys.exit("端口必须是 1 到 65535 之间的整数。")
if len(sys.argv) > 5:
    sys.exit("用法：./start.command [物理网卡] [路由器 DNS] [端口]")

if pid_file.exists():
    try:
        pid = int(pid_file.read_text().strip())
        process = subprocess.run(["/bin/ps", "-p", str(pid), "-o", "args="],
                                 capture_output=True, text=True)
        if pid > 1 and script in process.stdout:
            print("SOCKS 已在运行，PID=%d；如需更换参数，请先运行 stop.command。" % pid)
            sys.exit(0)
    except ValueError:
        pass

with socket.socket() as check:
    try:
        check.bind(("127.0.0.1", port))
    except OSError:
        sys.exit("%d 端口已被占用；未启动新服务。" % port)

with log_file.open("ab") as log:
    process = subprocess.Popen(
        [sys.executable, "-u", script, "--interface", interface,
         "--router-dns", router_dns, "--port", str(port)],
        stdin=subprocess.DEVNULL, stdout=log, stderr=subprocess.STDOUT,
        start_new_session=True, cwd=str(task_dir))
    pid_file.write_text(str(process.pid) + "\n")

for _ in range(30):
    if process.poll() is not None:
        pid_file.unlink(missing_ok=True)
        sys.exit("服务启动失败，请检查：%s" % log_file)
    try:
        with socket.create_connection(("127.0.0.1", port), timeout=0.1):
            print("SOCKS 已启动：127.0.0.1:%d，PID=%d" % (port, process.pid))
            print("物理网卡：%s；路由器 DNS：%s" % (interface, router_dns))
            print("日志：%s" % log_file)
            break
    except OSError:
        time.sleep(0.1)
else:
    process.terminate()
    pid_file.unlink(missing_ok=True)
    sys.exit("SOCKS 未能开始监听，请检查日志。")
PY
