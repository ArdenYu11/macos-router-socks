#!/usr/bin/env python3
"""Manage the per-user macOS LaunchAgent for router_socks.py."""

import argparse
import ipaddress
import json
import os
from pathlib import Path
import plistlib
import re
import shutil
import socket
import subprocess
import sys
import time


LABEL = "io.github.ardenyu11.router-socks"
SUPPORT_DIR = Path.home() / "Library" / "Application Support" / "macos-router-socks"
PLIST_PATH = Path.home() / "Library" / "LaunchAgents" / (LABEL + ".plist")
SETTINGS_PATH = SUPPORT_DIR / "settings.json"
RUNTIME_SCRIPT = SUPPORT_DIR / "router_socks.py"
PID_PATH = SUPPORT_DIR / "socks.pid"
LOG_PATH = SUPPORT_DIR / "socks.log"
DOMAIN = "gui/%d" % os.getuid()
TARGET = DOMAIN + "/" + LABEL
DEFAULTS = {"interface": "auto", "router_dns": "192.168.1.1", "port": 10808}


def launchctl(*arguments, check=True):
    result = subprocess.run(["/bin/launchctl", *arguments],
                            capture_output=True, text=True, timeout=15)
    if check and result.returncode:
        raise RuntimeError(result.stderr.strip() or result.stdout.strip()
                           or "launchctl failed: %s" % " ".join(arguments))
    return result


def agent_state():
    result = launchctl("print", TARGET, check=False)
    if result.returncode:
        return {"loaded": False, "pid": None, "state": "unloaded"}
    pid = re.search(r"^\s*pid = (\d+)\s*$", result.stdout, re.MULTILINE)
    state = re.search(r"^\s*state = (.+)$", result.stdout, re.MULTILINE)
    return {"loaded": True, "pid": int(pid.group(1)) if pid else None,
            "state": state.group(1).strip() if state else "loaded"}


def settings():
    config = DEFAULTS.copy()
    if SETTINGS_PATH.exists():
        config.update(json.loads(SETTINGS_PATH.read_text()))
    return config


def validate(config):
    config["router_dns"] = str(ipaddress.IPv4Address(config["router_dns"]))
    config["port"] = int(config["port"])
    if not 1 <= config["port"] <= 65535:
        raise ValueError("端口必须是 1 到 65535 之间的整数。")
    if not re.fullmatch(r"auto|[a-zA-Z][a-zA-Z0-9]*", config["interface"]):
        raise ValueError("网卡参数应为 auto 或物理网卡名称。")
    return config


def desired_settings(args):
    config = settings()
    for key in DEFAULTS:
        if getattr(args, key, None) is not None:
            config[key] = getattr(args, key)
    return validate(config)


def check_port_free(port):
    with socket.socket() as sock:
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            sock.bind(("127.0.0.1", port))
        except OSError as exc:
            raise RuntimeError("%d 端口被其他服务占用，请先停止旧 SOCKS 服务。" % port) from exc


def agent_plist(config, python):
    return {
        "Label": LABEL,
        "ProgramArguments": [python, "-u", str(RUNTIME_SCRIPT),
                             "--interface", config["interface"],
                             "--router-dns", config["router_dns"],
                             "--port", str(config["port"]),
                             "--pid-file", str(PID_PATH),
                             "--log-file", str(LOG_PATH)],
        "WorkingDirectory": str(SUPPORT_DIR),
        "RunAtLoad": True,
        "KeepAlive": True,
        "ThrottleInterval": 5,
        "ExitTimeOut": 5,
        "ProcessType": "Background",
        "Umask": 0o077,
        "StandardOutPath": str(SUPPORT_DIR / "launchd.log"),
        "StandardErrorPath": str(SUPPORT_DIR / "launchd.log"),
    }


def stop_loaded_agent():
    if agent_state()["loaded"]:
        launchctl("bootout", TARGET)
        for _ in range(50):
            if not agent_state()["loaded"]:
                return
            time.sleep(0.1)
        raise RuntimeError("LaunchAgent 尚未停止。")


def wait_for_socks(port):
    deadline = time.monotonic() + 12
    while time.monotonic() < deadline:
        state = agent_state()
        if state["pid"]:
            try:
                recorded_pid = int(PID_PATH.read_text().strip())
                if recorded_pid == state["pid"]:
                    with socket.create_connection(("127.0.0.1", port), timeout=1) as sock:
                        sock.sendall(b"\x05\x01\x00")
                        if sock.recv(2) == b"\x05\x00":
                            return state["pid"]
            except (OSError, ValueError):
                pass
        time.sleep(0.2)
    raise RuntimeError("服务未能启动，请检查 %s 和 %s。" % (LOG_PATH, SUPPORT_DIR / "launchd.log"))


def install(args):
    config = desired_settings(args)
    source = Path(__file__).resolve().with_name("router_socks.py")
    if not source.exists():
        raise RuntimeError("未找到 router_socks.py。")
    # Keep the runtime outside Documents so login launch does not depend on
    # access to the checkout or on the checkout's folder name staying fixed.
    SUPPORT_DIR.mkdir(parents=True, exist_ok=True, mode=0o700)
    PLIST_PATH.parent.mkdir(parents=True, exist_ok=True)
    stop_loaded_agent()
    check_port_free(config["port"])
    if source != RUNTIME_SCRIPT:
        temporary = RUNTIME_SCRIPT.with_suffix(".tmp")
        shutil.copyfile(source, temporary)
        temporary.replace(RUNTIME_SCRIPT)
    SETTINGS_PATH.write_text(json.dumps(config, ensure_ascii=False, indent=2) + "\n")
    SETTINGS_PATH.chmod(0o600)
    python = str(Path(sys.executable).resolve())
    temporary = PLIST_PATH.with_suffix(".tmp")
    temporary.write_bytes(plistlib.dumps(agent_plist(config, python)))
    temporary.chmod(0o600)
    temporary.replace(PLIST_PATH)
    launchctl("enable", TARGET)
    launchctl("bootstrap", DOMAIN, str(PLIST_PATH))
    pid = wait_for_socks(config["port"])
    print("已启用登录自启与退出重启：127.0.0.1:%d，PID=%d" % (config["port"], pid))
    print("网卡：%s；路由器 DNS：%s" % (config["interface"], config["router_dns"]))
    print("日志：%s" % LOG_PATH)


def start(args):
    has_arguments = any(getattr(args, key, None) is not None for key in DEFAULTS)
    if not PLIST_PATH.exists() or has_arguments:
        install(args)
        return
    config = validate(settings())
    if not agent_state()["loaded"]:
        check_port_free(config["port"])
        launchctl("enable", TARGET)
        launchctl("bootstrap", DOMAIN, str(PLIST_PATH))
    pid = wait_for_socks(config["port"])
    print("SOCKS 已运行：127.0.0.1:%d，PID=%d；登录自启与守护已启用。" % (config["port"], pid))


def stop(_args):
    stop_loaded_agent()
    print("SOCKS 已停止；下次登录仍会自动启动。永久关闭请运行 disable-autostart.command。")


def uninstall(_args):
    stop_loaded_agent()
    PLIST_PATH.unlink(missing_ok=True)
    print("已停止 SOCKS 并关闭登录自启。配置和日志保留在 %s。" % SUPPORT_DIR)


def status(args):
    info = agent_state()
    info.update(settings())
    info.update({"autostart_installed": PLIST_PATH.exists(), "label": LABEL,
                 "log_file": str(LOG_PATH), "plist": str(PLIST_PATH)})
    if args.json:
        print(json.dumps(info, ensure_ascii=False))
        return
    print("登录自启：%s" % ("已安装" if info["autostart_installed"] else "未安装"))
    print("服务状态：%s；PID=%s" % (info["state"], info["pid"] or "无"))
    print("监听：127.0.0.1:%d；网卡：%s；路由器 DNS：%s" %
          (info["port"], info["interface"], info["router_dns"]))
    print("日志：%s" % LOG_PATH)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    for name in ("install", "start"):
        command = commands.add_parser(name)
        command.add_argument("interface", nargs="?", help="auto or a physical interface name")
        command.add_argument("router_dns", nargs="?", help="Router DNS IPv4 address")
        command.add_argument("port", nargs="?", type=int)
    commands.add_parser("stop")
    commands.add_parser("uninstall")
    commands.add_parser("status").add_argument("--json", action="store_true")
    args = parser.parse_args()
    if sys.platform != "darwin":
        parser.error("此服务管理器仅支持 macOS。")
    try:
        {"install": install, "start": start, "stop": stop,
         "uninstall": uninstall, "status": status}[args.command](args)
    except (OSError, ValueError, RuntimeError, subprocess.TimeoutExpired) as exc:
        print("错误：%s" % exc, file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
