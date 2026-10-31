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
# 服务器在短时间多次 SSH 连接时会直接断开（2026-10-31 实测），所以每次调用都带退避重试；
# 要传的数据先落到临时文件，重试时可以重新读。
sx() {   # sx <远程命令> [本地输入文件]
  local i rc
  for i in 1 2 3 4 5 6; do
    rc=0
    if [ -n "${2:-}" ]; then "${S[@]}" "$1" < "$2" || rc=$?; else "${S[@]}" "$1" < /dev/null || rc=$?; fi
    [ $rc -ne 255 ] && return $rc
    echo "  （SSH 被断开，$((i * 10)) 秒后重试）" >&2
    sleep $((i * 10))
  done
  return 255
}
STAGE=$(umask 077; mktemp -d)
trap 'rm -rf "$STAGE"' EXIT
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"
PY="py -3.12"

echo "== 连接 $HOST"
sx 'echo "  $(hostname) ok"; sudo mkdir -p /opt/daotuai && sudo chown ubuntu:ubuntu /opt/daotuai'

echo "== 推代码（$(git log --oneline -1)）"
git -c core.autocrlf=false archive --format=tar -o "$STAGE/code.tar" HEAD
sx 'tar xf - -C /opt/daotuai' "$STAGE/code.tar"

echo "== 写密钥和默认设置（只显示名字）"
keys=$(grep -oE 'api_key_env:\s*[A-Z0-9_]+' aggregator/providers.yaml | awk '{print $2}' | sort -u)
{
  for k in $keys; do v="${!k:-}"; [ -n "$v" ] && printf '%s=%s\n' "$k" "$v"; done
  [ -f .env ] && grep -E '^(WORKFLOW_(TEXT|IMAGE|VIDEO)_MODEL|WORKFLOW_RESOLUTION|WORKFLOW_BUDGET_RMB|WORKFLOW_LLM)=' .env || true
  echo "WORKFLOW_MEDIA=/opt/daotuai/media"
} > "$STAGE/env"
sx 'umask 077; cat > /opt/daotuai/.env.new && mv /opt/daotuai/.env.new /opt/daotuai/.env && chmod 600 /opt/daotuai/.env' "$STAGE/env"
if [ -n "${DAOTUAI_HTPASSWD:-}" ]; then   # 指定访问账号："用户名:$apr1$..."（只传哈希，不传明文）
  printf '%s\n' "$DAOTUAI_HTPASSWD" > "$STAGE/htpasswd"
  sx 'sudo tee /etc/nginx/daotuai.htpasswd >/dev/null && sudo chown root:www-data /etc/nginx/daotuai.htpasswd && sudo chmod 640 /etc/nginx/daotuai.htpasswd && echo "  访问账号已设置：$(cut -d: -f1 /etc/nginx/daotuai.htpasswd)"' "$STAGE/htpasswd"
fi
for k in $keys; do [ -n "${!k:-}" ] && printf '  %s\n' "$k"; done

MIG=""
if [ "$WITH_DATA" = 1 ]; then
  has=$(sx 'test -s /opt/daotuai/workflow/data/studio.db && echo yes || echo no')
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
  tar cf "$STAGE/db.tar" -C "$T" workflow aggregator
  tar cf "$STAGE/media.tar" media
  sx 'sudo systemctl stop daotuai-workflow daotuai-aggregator 2>/dev/null || true'
  sx 'tar xf - -C /opt/daotuai' "$STAGE/db.tar"
  sx 'tar xf - -C /opt/daotuai && du -sh /opt/daotuai/media | sed "s/^/  media /"' "$STAGE/media.tar"
  rm -rf "$T"
  MIG="--migrate-from auto"
fi

echo "== 服务器安装"
sx "bash /opt/daotuai/deploy/linux/install.sh '$DOMAIN' $MIG"
