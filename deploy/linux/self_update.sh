#!/usr/bin/env bash
# 服务器自动更新（daotuai-update.timer 每 10 分钟调用一次）：
#   GitHub main 有新提交 → 拉到暂存目录 → 装依赖 → 跑全部测试（临时库、强制模拟）
#   → 全过才替换代码并重启 → 健康检查不过就回滚到上一版。结果写 data/update/status.json（工作台首页可见）。
# 需要一次性在 GitHub 仓库 Settings → Deploy keys 加上 /opt/daotuai/.deploy/id_ed25519.pub（只读）。
set -uo pipefail
R="${DAOTUAI_ROOT:-/opt/daotuai}"
RESTART="${DAOTUAI_RESTART:-sudo systemctl restart daotuai-aggregator daotuai-workflow}"
U=$R/data/update
KEY=$R/.deploy/id_ed25519
REPO="${DAOTUAI_REPO:-git@github.com:ychenfen/daotuai.git}"
BRANCH="${DAOTUAI_BRANCH:-main}"
mkdir -p "$U"
exec 9>"$U/lock"; flock -n 9 || exit 0          # 上一次还没跑完
log() { echo "$(date '+%F %T') $*" >> "$R/data/logs/update.log"; }
status() {  # status <result> <detail> [rev]
  python3 - "$U/status.json" "$1" "$2" "${3:-}" "$(cat "$R/REVISION" 2>/dev/null)" <<'PY'
import json, sys, time
p, result, detail, rev, cur = sys.argv[1:6]
json.dump({"result": result, "detail": detail[-1500:], "candidate": rev, "running": cur,
           "at": time.strftime("%Y-%m-%d %H:%M:%S")}, open(p, "w"), ensure_ascii=False)
PY
}
export GIT_SSH_COMMAND="ssh -i $KEY -o IdentitiesOnly=yes -o BatchMode=yes -o StrictHostKeyChecking=accept-new"
if [ ! -f "$KEY" ]; then status "no_key" "还没有部署密钥：运行 deploy/linux/install.sh 生成"; exit 0; fi
if [ ! -d "$U/repo/.git" ]; then
  rm -rf "$U/repo"
  if ! out=$(git clone -q --depth 30 --branch "$BRANCH" "$REPO" "$U/repo" 2>&1); then
    status "no_access" "拉不到代码（多半是还没在 GitHub 加部署公钥）：$out"; exit 0
  fi
fi
cd "$U/repo" || exit 1
if ! out=$(git fetch -q origin "$BRANCH" 2>&1); then status "fetch_failed" "$out"; exit 0; fi
NEW=$(git rev-parse "origin/$BRANCH")
CUR=$(cat "$R/REVISION" 2>/dev/null || echo none)
if [ "$NEW" = "$CUR" ]; then status "up_to_date" "已是最新" "$NEW"; exit 0; fi
if [ -f "$U/failed_rev" ] && [ "$(cat "$U/failed_rev")" = "$NEW" ]; then exit 0; fi   # 同一版本测试没过，不反复试
log "发现新版本 ${NEW:0:7}（当前 ${CUR:0:7}）"
STAGE=$U/stage; rm -rf "$STAGE"; mkdir -p "$STAGE"
git archive "$NEW" | tar x -C "$STAGE"
"$R/venv/bin/pip" install -q -r "$STAGE/aggregator/requirements.txt" >> "$R/data/logs/update.log" 2>&1
a=$(cd "$STAGE/aggregator" && "$R/venv/bin/python" tests/run_tests.py 2>&1 | tail -1)
w=$(cd "$STAGE" && "$R/venv/bin/python" workflow/tests/run_tests.py 2>&1 | grep -E "FAIL|PASS [0-9]" | tail -5)
case "$a$w" in
  *"FAIL 0"*"FAIL 0"*) ;;
  *) echo "$NEW" > "$U/failed_rev"; log "测试没过，保持旧版本：$a / $w"; status "tests_failed" "聚合层 $a；工作流 $w" "$NEW"; exit 0;;
esac
# 备份当前代码（只含代码，不含数据），用于回滚
BK=$U/prev.tar
(cd "$R" && tar cf "$BK" --exclude=./venv --exclude=./media --exclude=./data --exclude=./.env --exclude=./.deploy \
   --exclude=./workflow/data --exclude=./aggregator/data --exclude=./ACCESS.txt .) 2>/dev/null
git archive "$NEW" | tar x -C "$R"
echo "$NEW" > "$R/REVISION"
$RESTART
ok=1
for p in 8010 8011; do
  for i in $(seq 1 60); do curl -fs "http://127.0.0.1:$p/healthz" >/dev/null && break; sleep 0.5; done
  curl -fs "http://127.0.0.1:$p/healthz" >/dev/null || ok=0
done
if [ $ok = 1 ]; then
  rm -f "$U/failed_rev"; log "已更新到 ${NEW:0:7}"; status "updated" "已更新：$(git log -1 --format=%s "$NEW")" "$NEW"
else
  log "新版本起不来，回滚"
  tar xf "$BK" -C "$R"; echo "$CUR" > "$R/REVISION"; echo "$NEW" > "$U/failed_rev"
  $RESTART
  status "rolled_back" "新版本 ${NEW:0:7} 启动失败，已回滚到 ${CUR:0:7}" "$NEW"
fi
