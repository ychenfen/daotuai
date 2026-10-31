# 部署到 Linux 服务器

工作台在服务器上的样子：

| 项 | 值 |
|---|---|
| 目录 | `/opt/daotuai`（代码、`venv/`、`media/`、两个数据库、`data/logs/`） |
| 服务 | `daotuai-aggregator`（127.0.0.1:8010）、`daotuai-workflow`（127.0.0.1:8011），systemd 管，开机自启、挂了自动拉起 |
| 对外 | nginx：`https://<域名>`，HTTPS + 访问密码（`/etc/nginx/daotuai.htpasswd`），后端端口不对外 |
| 密钥 | `/opt/daotuai/.env`（600 权限），从纪总电脑的环境变量直接写过去，不进 git |
| 访问密码 | 第一次部署时随机生成，写在服务器 `/opt/daotuai/ACCESS.txt` |

## 挂在已有站点的子路径下（当前用法）

现在挂在 `https://stock.yuchenxu.cn/studio/`，不用新加域名解析，用量化站已有的证书：

```bash
bash deploy/push_from_windows.sh stock.yuchenxu.cn --path /studio
```

nginx 只做两件事：写 `/etc/nginx/snippets/daotuai-studio.conf`，并在 `stock.yuchenxu.cn` 的 443 server 块里加一行
`include`（改前备份到 `/opt/daotuai/data/nginx-backup-*`，`nginx -t` 不过自动恢复）。前端按页面地址自动识别前缀，
本机直接开 `/ui/` 照常可用。

## 第一次部署 / 更新代码

在纪总电脑上（Git Bash，`daotuai` 目录下）：

```bash
bash deploy/push_from_windows.sh studio.yuchenxu.cn --with-data   # 第一次：连数据一起搬
bash deploy/push_from_windows.sh studio.yuchenxu.cn               # 以后：只更新代码和密钥
```

指定访问账号（只传哈希，不传明文）：先 `openssl passwd -apr1 <密码>` 得到哈希，再
`DAOTUAI_HTPASSWD='用户名:$apr1$...' bash deploy/push_from_windows.sh ...`。不指定时第一次部署随机生成。

服务器在短时间多次 SSH 连接时会直接断开，脚本每次 SSH 都带退避重试（最多 6 次）。

脚本会：推代码 → 写 `.env` → （可选）数据库快照 + `media` → 服务器上 `deploy/linux/install.sh`：
装 `python3-venv`/`ffmpeg` → 建 venv → **跑全部测试（没全过就停）** → 数据库里的 Windows 路径改成 `/opt/daotuai/...`
→ 重启服务 → 域名解析到本机时配 nginx 和证书。

域名还没解析时，脚本会停在最后一步并提示加 A 记录；在那之前可以用 SSH 隧道访问：
`ssh -L 8011:127.0.0.1:8011 ubuntu@43.137.12.89`，然后开 `http://127.0.0.1:8011`。

## 日常

```bash
sudo systemctl status daotuai-workflow daotuai-aggregator
tail -f /opt/daotuai/data/logs/workflow.log
sudo systemctl restart daotuai-workflow daotuai-aggregator
```

数据库每次启动自动备份到 `workflow/data/backups`、`aggregator/data/backups`（各留 10 份）。

## 已知限制

- LibTV 登录回调地址仍是 `127.0.0.1:8010`，在服务器上授权要另外配（暂未做）。
