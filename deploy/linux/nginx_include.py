"""在 nginx 站点文件里，给 server_name 含 <域名> 且 listen 443 的 server 块插一行 include（已存在就不动）。
    sudo python3 nginx_include.py <站点文件> <域名> <include 路径>
只改这一行；调用方负责备份、nginx -t 和失败回滚。"""
import re
import sys

path, domain, inc = sys.argv[1:4]
text = open(path, encoding="utf-8").read()
line = f"include {inc};"
if line in text:
    print("  include 已存在"); sys.exit(0)


def blocks(t):
    """顶层 server { ... } 的 (起, 止) 位置。"""
    out, i = [], 0
    for m in re.finditer(r"(?m)^\s*server\s*\{", t):
        if m.start() < i:
            continue
        depth, j = 0, m.end() - 1
        while j < len(t):
            if t[j] == "{":
                depth += 1
            elif t[j] == "}":
                depth -= 1
                if depth == 0:
                    break
            j += 1
        out.append((m.start(), j)); i = j
    return out


for a, b in blocks(text):
    body = text[a:b]
    sn = re.search(r"(?m)^(\s*)server_name\s+([^;]+);.*$", body)
    if sn and domain in sn.group(2).split() and re.search(r"listen\s+443", body):
        pos = a + sn.end()
        text = text[:pos] + f"\n{sn.group(1)}{line}   # daotuai 工作台" + text[pos:]
        open(path, "w", encoding="utf-8").write(text)
        print(f"  已在 {domain} 的 443 server 块加入 {line}"); sys.exit(0)
print(f"没找到 {domain} 的 443 server 块", file=sys.stderr); sys.exit(2)
