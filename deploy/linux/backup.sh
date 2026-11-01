#!/usr/bin/env bash
# 每天备份两个数据库（SQLite 在线备份，不停服）到 data/daily/<日期>/，保留 14 天。
# 素材（media）太大不在这里备份；需要异地备份时用 rclone/对象存储另配。
set -euo pipefail
R=/opt/daotuai
D=$R/data/daily/$(date +%Y%m%d)
mkdir -p "$D"
"$R/venv/bin/python" - "$R" "$D" <<'PY'
import os, sqlite3, sys
r, d = sys.argv[1], sys.argv[2]
for rel in ("workflow/data/studio.db", "aggregator/data/agg.db"):
    src = os.path.join(r, rel)
    if os.path.exists(src):
        dst = os.path.join(d, os.path.basename(rel))
        a, b = sqlite3.connect(src), sqlite3.connect(dst)
        a.backup(b); b.close(); a.close()
        print(rel, os.path.getsize(dst) // 1024, "KB")
PY
find "$R/data/daily" -mindepth 1 -maxdepth 1 -type d -mtime +14 -exec rm -rf {} +
echo "$(date '+%F %T') 备份完成 $D" >> "$R/data/logs/backup.log"
