#!/usr/bin/env bash
# 在 Linux 服务器上安装 / 更新工作台。代码和数据已经放在 /opt/daotuai（由 deploy/push_from_windows.sh 推上来）。
#   bash /opt/daotuai/deploy/linux/install.sh <域名> [--migrate-from '<旧机器项目根目录>']
# 幂等：可以反复跑。不会动服务器上其他站点；nginx 配置检查不过就撤掉本站点配置。
set -euo pipefail
DOMAIN="${1:?用法: install.sh <域名> [--path /studio] [--migrate-from 旧根目录|auto]}"; shift
OLD_ROOT=""; SUBPATH=""
while [ $# -gt 0 ]; do
  case "$1" in
    --migrate-from) OLD_ROOT="${2:-}"; shift 2;;
    --path) SUBPATH="/${2#/}"; SUBPATH="${SUBPATH%/}"; shift 2;;
    *) shift;;
  esac
done
R=/opt/daotuai
cd "$R"
mkdir -p data/logs workflow/data aggregator/data media

echo "== 1/6 系统依赖"
need=()
dpkg -s python3-venv >/dev/null 2>&1 || need+=(python3-venv)
command -v ffmpeg >/dev/null || need+=(ffmpeg)
command -v openssl >/dev/null || need+=(openssl)
ldconfig -p 2>/dev/null | grep -q libmediainfo || need+=(libmediainfo0v5)   # 剪映草稿导出读素材信息用
if [ ${#need[@]} -gt 0 ]; then
  sudo DEBIAN_FRONTEND=noninteractive apt-get update -qq
  sudo DEBIAN_FRONTEND=noninteractive apt-get install -y -qq "${need[@]}" >/dev/null
fi
ffmpeg -version | head -1

echo "== 2/6 Python 环境"
[ -x venv/bin/python ] || python3 -m venv venv
venv/bin/pip install -q --upgrade pip >/dev/null
venv/bin/pip install -q -r aggregator/requirements.txt
venv/bin/python --version

echo "== 3/6 测试（临时库、强制模拟，不花钱、不碰真实数据）"
a=$(cd aggregator && ../venv/bin/python tests/run_tests.py 2>&1 | tail -1); echo "  聚合层 $a"
w=$(venv/bin/python workflow/tests/run_tests.py 2>&1 | grep -E "FAIL|PASS [0-9]" | tail -5); echo "  工作流 $w"
case "$a$w" in *"FAIL 0"*"FAIL 0"*) ;; *) echo "测试没全过，停止部署（旧服务不受影响）"; exit 1;; esac

if [ -n "$OLD_ROOT" ]; then
  echo "== 4/6 数据库路径迁移：$OLD_ROOT → $R"
  dbs=()
  for d in workflow/data/studio.db aggregator/data/agg.db; do [ -f "$d" ] && dbs+=("$d"); done
  [ ${#dbs[@]} -gt 0 ] && venv/bin/python tools/migrate_paths.py --old "$OLD_ROOT" --new "$R" "${dbs[@]}"
else
  echo "== 4/6 不迁移数据"
fi

echo "== 5/6 后台服务 + 定时任务（自动更新、每日备份）"
sudo cp deploy/linux/daotuai-aggregator.service deploy/linux/daotuai-workflow.service \
        deploy/linux/daotuai-update.service deploy/linux/daotuai-update.timer \
        deploy/linux/daotuai-backup.service deploy/linux/daotuai-backup.timer /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable -q daotuai-aggregator daotuai-workflow
sudo systemctl enable -q --now daotuai-update.timer daotuai-backup.timer
command -v git >/dev/null || sudo DEBIAN_FRONTEND=noninteractive apt-get install -y -qq git >/dev/null
if [ ! -f "$R/.deploy/id_ed25519" ]; then
  mkdir -p "$R/.deploy" && chmod 700 "$R/.deploy"
  ssh-keygen -q -t ed25519 -N "" -C "daotuai-server-deploy" -f "$R/.deploy/id_ed25519"
fi
echo "  自动更新用的部署公钥（在 GitHub 仓库 Settings → Deploy keys 添加，只读即可）："
sed 's/^/    /' "$R/.deploy/id_ed25519.pub"
sudo systemctl restart daotuai-aggregator daotuai-workflow
for p in 8010 8011; do
  for i in $(seq 1 40); do curl -fs "http://127.0.0.1:$p/healthz" >/dev/null && break; sleep 0.5; done
  curl -fs "http://127.0.0.1:$p/healthz" >/dev/null && echo "  127.0.0.1:$p ok" || { echo "  127.0.0.1:$p 没起来"; tail -20 data/logs/*.log; exit 1; }
done

if [ -n "$SUBPATH" ]; then
  echo "== 6/6 nginx：挂到 https://$DOMAIN$SUBPATH/（用该站点已有证书）"
  [ -f /etc/nginx/daotuai.htpasswd ] || { echo "  还没有访问账号（/etc/nginx/daotuai.htpasswd），先设置再挂出去"; exit 1; }
  SITE=$(sudo grep -lE "server_name[^;]*\b$DOMAIN\b" /etc/nginx/sites-enabled/* | head -1)
  [ -n "$SITE" ] || { echo "  没找到 $DOMAIN 的站点配置"; exit 1; }
  BK="$R/data/nginx-backup-$(date +%Y%m%d-%H%M%S)"; mkdir -p "$BK"
  sudo cp -a "$SITE" "$BK/"; [ -f /etc/nginx/snippets/daotuai-studio.conf ] && sudo cp -a /etc/nginx/snippets/daotuai-studio.conf "$BK/" || true
  echo "  站点文件 $SITE，已备份到 $BK"
  sudo mkdir -p /etc/nginx/snippets
  sed "s#__PATH__#$SUBPATH#g" deploy/linux/nginx-daotuai-path.conf | sudo tee /etc/nginx/snippets/daotuai-studio.conf >/dev/null
  sudo python3 deploy/linux/nginx_include.py "$SITE" "$DOMAIN" /etc/nginx/snippets/daotuai-studio.conf
  if ! sudo nginx -t -q; then
    sudo cp -a "$BK/$(basename "$SITE")" "$SITE"; sudo rm -f /etc/nginx/snippets/daotuai-studio.conf
    [ -f "$BK/daotuai-studio.conf" ] && sudo cp -a "$BK/daotuai-studio.conf" /etc/nginx/snippets/ || true
    sudo nginx -t -q && echo "  nginx 配置检查不过，已原样恢复" ; exit 1
  fi
  sudo systemctl reload nginx
  sleep 1
  for u in "$SUBPATH" "$SUBPATH/ui/" "$SUBPATH/healthz" "/"; do
    printf '  https://%s%s 不带密码 → %s\n' "$DOMAIN" "$u" "$(curl -s -o /dev/null -w '%{http_code}' "https://$DOMAIN$u")"
  done
  echo "完成：https://$DOMAIN$SUBPATH/"
  exit 0
fi

echo "== 6/6 nginx + HTTPS：$DOMAIN"
MYIP=$(curl -fs --max-time 5 http://metadata.tencentyun.com/latest/meta-data/public-ipv4 || curl -fs --max-time 5 https://ifconfig.me || true)
DNSIP=$(getent ahostsv4 "$DOMAIN" | awk 'NR==1{print $1}' || true)
echo "  本机公网 IP=$MYIP  $DOMAIN 解析到=${DNSIP:-（没有解析）}"
if [ -z "$DNSIP" ] || [ "$DNSIP" != "$MYIP" ]; then
  echo "  [待办] 先在域名解析里加一条 A 记录：$DOMAIN → $MYIP，生效后重跑本脚本。"
  echo "  在此之前可以用 SSH 隧道访问：ssh -L 8011:127.0.0.1:8011 ubuntu@$MYIP  然后浏览器开 http://127.0.0.1:8011"
  exit 0
fi
if [ ! -f /etc/nginx/daotuai.htpasswd ]; then
  PASS=$(openssl rand -base64 12 | tr -dc 'A-Za-z0-9' | cut -c1-12)
  printf 'jizong:%s\n' "$(openssl passwd -apr1 "$PASS")" | sudo tee /etc/nginx/daotuai.htpasswd >/dev/null
  sudo chmod 640 /etc/nginx/daotuai.htpasswd; sudo chown root:www-data /etc/nginx/daotuai.htpasswd
  umask 077; printf '网址: https://%s\n用户名: jizong\n密码: %s\n' "$DOMAIN" "$PASS" > "$R/ACCESS.txt"
  echo "  已生成访问密码（写在服务器 $R/ACCESS.txt，不在日志里显示）"
fi
SITE=/etc/nginx/sites-available/daotuai
if [ ! -f "/etc/letsencrypt/live/$DOMAIN/fullchain.pem" ]; then
  # 先只开 80 端口拿证书
  printf 'server { listen 80; listen [::]:80; server_name %s; location /.well-known/acme-challenge/ { root /var/www/html; } location / { return 404; } }\n' "$DOMAIN" | sudo tee "$SITE" >/dev/null
  sudo ln -sf "$SITE" /etc/nginx/sites-enabled/daotuai
  if ! sudo nginx -t -q; then sudo rm -f /etc/nginx/sites-enabled/daotuai; echo "nginx 配置检查不过，已撤回"; exit 1; fi
  sudo systemctl reload nginx
  sudo mkdir -p /var/www/html
  sudo certbot certonly --webroot -w /var/www/html -d "$DOMAIN" -n --agree-tos --register-unsafely-without-email --keep-until-expiring
fi
sed "s/__DOMAIN__/$DOMAIN/g" deploy/linux/nginx-daotuai.conf | sudo tee "$SITE" >/dev/null
sudo ln -sf "$SITE" /etc/nginx/sites-enabled/daotuai
if ! sudo nginx -t -q; then sudo rm -f /etc/nginx/sites-enabled/daotuai; echo "nginx 配置检查不过，已撤回"; exit 1; fi
sudo systemctl reload nginx
code=$(curl -s -o /dev/null -w '%{http_code}' "https://$DOMAIN/healthz")
echo "  https://$DOMAIN/healthz 不带密码 → $code（应为 401）"
echo "完成：https://$DOMAIN"
