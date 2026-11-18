# 部署到 Linux 服务器

工作台在服务器上的样子：

| 项 | 值 |
|---|---|
| 目录 | `/opt/daotuai`（代码、`venv/`、`media/`、两个数据库、`data/logs/`） |
| 服务 | `daotuai-aggregator`（127.0.0.1:8010）、`daotuai-workflow`（127.0.0.1:8011），systemd 管，开机自启、挂了自动拉起 |
| 对外 | nginx：`https://<域名>`，HTTPS + 访问密码（`/etc/nginx/daotuai.htpasswd`），后端端口不对外 |
| 密钥 | `/opt/daotuai/.env`（600 权限），从本机环境变量直接写过去，不进 git |
| 访问密码 | 第一次部署时随机生成，写在服务器 `/opt/daotuai/ACCESS.txt` |

## 挂在已有站点的子路径下（当前用法）

例如挂在 `https://example.com/studio/`，共用主站已有证书：

```bash
bash deploy/push_from_windows.sh example.com --path /studio
```

nginx 只做两件事：写 `/etc/nginx/snippets/daotuai-studio.conf`，并在 `example.com` 的 443 server 块里加一行
`include`（改前备份到 `/opt/daotuai/data/nginx-backup-*`，`nginx -t` 不过自动恢复）。前端按页面地址自动识别前缀，
本机直接开 `/ui/` 照常可用。

## 第一次部署 / 更新代码

在本地电脑上（Git Bash，`daotuai` 目录下）：

```bash
bash deploy/push_from_windows.sh studio.example.com --with-data   # 第一次：连数据一起搬
bash deploy/push_from_windows.sh studio.example.com               # 以后：只更新代码和密钥
```

指定访问账号（只传哈希，不传明文）：先 `openssl passwd -apr1 <密码>` 得到哈希，再
`DAOTUAI_HTPASSWD='用户名:$apr1$...' bash deploy/push_from_windows.sh ...`。不指定时第一次部署随机生成。

服务器在短时间多次 SSH 连接时会直接断开，脚本每次 SSH 都带退避重试（最多 6 次）。

脚本会：推代码 → 写 `.env` → （可选）数据库快照 + `media` → 服务器上 `deploy/linux/install.sh`：
装 `python3-venv`/`ffmpeg` → 建 venv → **跑全部测试（没全过就停）** → 数据库里的 Windows 路径改成 `/opt/daotuai/...`
→ 重启服务 → 域名解析到本机时配 nginx 和证书。

域名还没解析时，脚本会停在最后一步并提示加 A 记录；在那之前可以用 SSH 隧道访问：
`ssh -L 8011:127.0.0.1:8011 ubuntu@your-server-ip`，然后开 `http://127.0.0.1:8011`。

## 日常

```bash
sudo systemctl status daotuai-workflow daotuai-aggregator
tail -f /opt/daotuai/data/logs/workflow.log
sudo systemctl restart daotuai-workflow daotuai-aggregator
```

数据库每次启动自动备份到 `workflow/data/backups`、`aggregator/data/backups`（各留 10 份）。

## 自动更新与每日备份

`install.sh` 会装两个定时任务：

| 定时任务 | 做什么 |
|---|---|
| `daotuai-update.timer`（每 10 分钟） | GitHub `main` 有新提交 → 拉到 `data/update/stage` → 装依赖 → 跑全部测试（临时库、强制模拟）→ **全过才**替换代码并重启 → 两个服务健康检查不过就回滚到上一版。同一个失败版本不会反复试。结果写 `data/update/status.json`，工作台首页“存储与运行状态”里能看到 |
| `daotuai-backup.timer`（每天 04:30） | 两个数据库在线备份到 `data/daily/<日期>/`，留 14 天（media 太大不在这里备份） |

自动更新需要一次性在 GitHub 加只读部署公钥：`install.sh` 跑完会打印 `/opt/daotuai/.deploy/id_ed25519.pub`，
到 `ychenfen/daotuai` → Settings → Deploy keys → Add deploy key，粘贴，不勾 “Allow write access”。
之后推到 `main` 的代码 10 分钟内自动上线，不再需要电脑开机。

```bash
systemctl list-timers 'daotuai-*'
cat /opt/daotuai/data/update/status.json
tail -f /opt/daotuai/data/logs/update.log
sudo systemctl start daotuai-update.service      # 立即检查一次
```

## LibTV 授权

浏览器打开工作台的“生成设置 → 连接 LibTV”，地址走 `https://<域名><前缀>/libtv/login`，授权后回跳
`https://<域名><前缀>/libtv/callback`（工作台转发给只监听本机的聚合层）。回跳地址由 `push_from_windows.sh`
写进服务器 `.env` 的 `LIBTV_REDIRECT_URI`。

## 已知限制

