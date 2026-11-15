#!/bin/bash
set -euo pipefail
if [ "$#" -lt 1 ] || [ "$#" -gt 2 ]; then
  printf '%s\n' '用法：./test.command <测试 URL> [SOCKS 端口]' >&2
  exit 1
fi
task_url="$1"
task_port="${2:-10808}"
case "$task_url" in
  http://*|https://*) ;;
  *) printf '%s\n' '测试 URL 必须以 http:// 或 https:// 开头。' >&2; exit 1 ;;
esac
case "$task_port" in
  ''|*[!0-9]*) printf '%s\n' '端口必须是整数。' >&2; exit 1 ;;
esac
if [ "$task_port" -lt 1 ] || [ "$task_port" -gt 65535 ]; then
  printf '%s\n' '端口必须在 1 到 65535 之间。' >&2
  exit 1
fi
/usr/bin/curl \
  --proxy "socks5h://127.0.0.1:$task_port" \
  --noproxy '' \
  --head --silent --show-error \
  --connect-timeout 6 --max-time 12 \
  --output /dev/null \
  --write-out '目标网站 HTTP 状态：%{http_code}\n' \
  "$task_url"
