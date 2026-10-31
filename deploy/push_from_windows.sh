#!/usr/bin/env bash
# 在纪总的 Windows 电脑上（Git Bash）把工作台推到 Linux 服务器并安装。
#   bash deploy/push_from_windows.sh <域名> [--with-data] [--force-data]
#   --with-data   把电脑上的数据库和 media 一起搬过去（服务器上已有数据时拒绝，除非加 --force-data）
# 密钥：从本机环境变量读 providers.yaml 里声明的 *_API_KEY，经 SSH 直接写进服务器 /opt/daotuai/.env（600 权限）；
#       不打印、不进 git、不进日志。
set -euo pipefail
DOMAIN="${1:?用法: push_from_windows.sh <域名> [--with-data] [--force-data]}"; shift
WITH_DATA=0; FORCE=0
for x in "$@"; do case "$x" in --with-data) WITH_DATA=1;; --force-data) FORCE=1;; esac; done
HOST="${DAOTUAI_SERVER:-ubuntu@43.137.12.89}"
KEY="${DAOTUAI_SSH_KEY:-$USERPROFILE/.ssh/tmp_key}"
S=(ssh -i "$KEY" -o IdentitiesOnly=yes -o BatchMode=yes -o ConnectTimeout=15 -o StrictHostKeyChecking=accept-new
   -o UserKnownHostsFile="$USERPROFILE/.ssh/known_hosts" "$HOST")
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"
PY="py -3.12"

echo "== 连接 $HOST"
"${S[@]}" 'echo "  $(hostname) ok"; sudo mkdir -p /opt/daotuai && sudo chown ubuntu:ubuntu /opt/daotuai'

echo "== 推代码（$(git log --oneline -1)）"
git -c core.autocrlf=false archive --format=tar HEAD | "${S[@]}" 'tar xf - -C /opt/daotuai'

echo "== 写密钥和默认设置（只显示名字）"
keys=$(grep -oE 'api_key_env:\s*[A-Z0-9_]+' aggregator/providers.yaml | awk '{print $2}' | sort -u)
{
  for k in $keys; do v="${!k:-}"; [ -n "$v" ] && printf '%s=%s\n' "$k" "$v"; done
  [ -f .env ] && grep -E '^(WORKFLOW_(TEXT|IMAGE|VIDEO)_MODEL|WORKFLOW_RESOLUTION|WORKFLOW_BUDGET_RMB|WORKFLOW_LLM)=' .env || true
  echo "WORKFLOW_MEDIA=/opt/daotuai/media"
} | "${S[@]}" 'umask 077; cat > /opt/daotuai/.env.new && mv /opt/daotuai/.env.new /opt/daotuai/.env && chmod 600 /opt/daotuai/.env'
for k in $keys; do [ -n "${!k:-}" ] && printf '  %s\n' "$k"; done

MIG=""
if [ "$WITH_DATA" = 1 ]; then
  has=$("${S[@]}" 'test -s /opt/daotuai/workflow/data/studio.db && echo yes || echo no')
  if [ "$has" = yes ] && [ "$FORCE" != 1 ]; then
    echo "服务器上已经有数据库，不覆盖（确实要覆盖加 --force-data）"; exit 1
  fi
  echo "== 搬数据：数据库快照 + media"
  T=$(mktemp -d)
  mkdir -p "$T/workflow/data" "$T/aggregator/data"
  $PY - "$T" <<'PYEOF'
import sqlite3, sys, os
t = sys.argv[1]
for src, dst in (("workflow/data/studio.db", "workflow/data/studio.db"), ("aggregator/data/agg.db", "aggregator/data/agg.db")):
    if os.path.exists(src):
        a, b = sqlite3.connect(src), sqlite3.connect(os.path.join(t, dst))
        a.backup(b); b.close(); a.close()
        print("  快照", src, os.path.getsize(os.path.join(t, dst)) // 1024, "KB")
PYEOF
  [ -f aggregator/data/libtv_auth.json ] && cp aggregator/data/libtv_auth.json "$T/aggregator/data/" || true
  "${S[@]}" 'sudo systemctl stop daotuai-workflow daotuai-aggregator 2>/dev/null || true'
  tar cf - -C "$T" workflow aggregator | "${S[@]}" 'tar xf - -C /opt/daotuai'
  tar cf - media | "${S[@]}" 'tar xf - -C /opt/daotuai'
  "${S[@]}" 'du -sh /opt/daotuai/media | sed "s/^/  media /"'
  rm -rf "$T"
  MIG="--migrate-from auto"
fi

echo "== 服务器安装"
"${S[@]}" "bash /opt/daotuai/deploy/linux/install.sh '$DOMAIN' $MIG"
