"""把数据库里记录的绝对路径从一台机器换到另一台（例：Windows 电脑 → Linux 服务器）。

    python3 tools/migrate_paths.py --old "C:\\Users\\纪松本\\Desktop\\daotuai" --new /opt/daotuai \
        workflow/data/studio.db aggregator/data/agg.db [--dry-run]

素材、配音、质检帧等路径存的是绝对路径（普通列里是原样，JSON 列里反斜杠是转义过的 \\\\）。
本脚本扫描所有表的所有文本列，把 old 根目录开头的路径换成 new，路径分隔符统一成 /。
改之前自动备份为 <db>.before-migrate；结束时报告还残留的 Windows 路径条数（应为 0）。
"""
from __future__ import annotations

import argparse
import re
import shutil
import sqlite3
import sys

SEP = r"(?:\\\\|\\|/)"          # JSON 转义的 \\、普通的 \、或 /


def root_pattern(old: str) -> re.Pattern:
    parts = [p for p in re.split(r"[\\/]+", old.strip()) if p]
    if not parts:
        raise SystemExit("--old 不能为空")
    head = parts[0]
    if re.fullmatch(r"[A-Za-z]:", head):
        head_re = f"[{head[0].upper()}{head[0].lower()}]:"
    else:
        head_re = re.escape(head)
    body = SEP.join([head_re] + [re.escape(p) for p in parts[1:]])
    return re.compile(body + r"(?P<rest>(?:" + SEP + r"[^\"'\r\n\\/]*)*)")


def rewrite(text: str, pat: re.Pattern, new: str) -> str:
    new = new.rstrip("/")
    return pat.sub(lambda m: new + m.group("rest").replace("\\\\", "/").replace("\\", "/"), text)


def migrate(db: str, pat: re.Pattern, new: str, dry: bool) -> tuple[int, int]:
    if not dry:
        shutil.copy2(db, db + ".before-migrate")
    con = sqlite3.connect(db)
    changed = left = 0
    tables = [r[0] for r in con.execute("SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'")]
    for t in tables:
        cols = [r[1] for r in con.execute(f'PRAGMA table_info("{t}")') if (r[2] or "").upper() in ("TEXT", "")]
        if not cols:
            continue
        for row in con.execute(f'SELECT rowid, {", ".join(chr(34) + c + chr(34) for c in cols)} FROM "{t}"').fetchall():
            upd = {}
            for c, v in zip(cols, row[1:]):
                if isinstance(v, str) and v:
                    nv = rewrite(v, pat, new)
                    if nv != v:
                        upd[c] = nv
            if upd:
                changed += 1
                if not dry:
                    sets = ", ".join(f'"{c}"=?' for c in upd)
                    con.execute(f'UPDATE "{t}" SET {sets} WHERE rowid=?', (*upd.values(), row[0]))
    if not dry:
        con.commit()
    # 残留检查：还有没有 X:\ 或 X:/ 开头的路径
    for t in tables:
        cols = [r[1] for r in con.execute(f'PRAGMA table_info("{t}")')]
        for c in cols:
            n = con.execute(f'SELECT COUNT(*) FROM "{t}" WHERE typeof("{c}")=\'text\' AND ("{c}" LIKE \'%:\\%\' OR "{c}" LIKE \'%:/Users%\')').fetchone()[0]
            if n:
                left += n
                print(f"  残留 {t}.{c}: {n} 行", file=sys.stderr)
    con.close()
    return changed, left


def detect_old_root(dbs: list[str]) -> str:
    """从库里找 Windows 绝对路径，取 …\\media\\ 前面那段作为旧根目录（出现最多的那个）。"""
    from collections import Counter
    pat = re.compile(r"([A-Za-z]:(?:\\\\|\\|/)[^\"'\r\n]*?)(?:\\\\|\\|/)+media(?:\\\\|\\|/)")
    seen: Counter = Counter()
    for db in dbs:
        con = sqlite3.connect(db)
        for (t,) in con.execute("SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'"):
            cols = [r[1] for r in con.execute(f'PRAGMA table_info("{t}")')]
            for c in cols:
                for (v,) in con.execute(f'SELECT "{c}" FROM "{t}" WHERE typeof("{c}")=\'text\' AND "{c}" LIKE \'%media%\''):
                    for m in pat.finditer(v):
                        seen[re.sub(r"(?:\\\\|\\|/)+", "\\\\", m.group(1))] += 1
        con.close()
    return seen.most_common(1)[0][0] if seen else ""


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--old", required=True, help="原机器上的项目根目录；写 auto 则从库里自动识别")
    ap.add_argument("--new", required=True, help="新机器上的项目根目录")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("dbs", nargs="+")
    a = ap.parse_args()
    if a.old == "auto":
        a.old = detect_old_root(a.dbs)
        if not a.old:
            print("库里没有找到 Windows 路径，不用迁移")
            return 0
        print(f"旧根目录（自动识别）：{a.old}")
    pat = root_pattern(a.old)
    bad = 0
    for db in a.dbs:
        changed, left = migrate(db, pat, a.new, a.dry_run)
        print(f"{db}: 改了 {changed} 行，残留 Windows 路径 {left} 处{'（试运行，未写入）' if a.dry_run else ''}")
        bad += left
    return 1 if bad and not a.dry_run else 0


if __name__ == "__main__":
    sys.exit(main())
