import os, json, html
from PIL import Image, ImageDraw

OUT = r"C:\Users\纪松本\Desktop\daotuai\screens"
files = sorted([f for f in os.listdir(OUT) if f.lower().endswith(".png") and not f.startswith("contact")])

items = []
for f in files:
    p = os.path.join(OUT, f)
    with Image.open(p) as im:
        w, h = im.size
    title = os.path.splitext(f)[0]
    num, _, name = title.partition("-")
    items.append({"file": f, "num": num, "name": name or title, "w": w, "h": h, "kb": round(os.path.getsize(p) / 1024)})

# ---------- index.html ----------
cards = []
for it in items:
    cards.append(f"""
    <figure>
      <a href="{html.escape(it['file'])}" target="_blank"><img loading="lazy" src="{html.escape(it['file'])}" alt="{html.escape(it['name'])}"></a>
      <figcaption><b>{html.escape(it['num'])} · {html.escape(it['name'])}</b><span>{it['w']}×{it['h']} · {it['kb']} KB</span></figcaption>
    </figure>""")

doc = f"""<!doctype html>
<html lang="zh-CN"><head><meta charset="utf-8">
<title>刀徒AI 短剧工作流 · 全流程截图册</title>
<style>
  :root {{ color-scheme: dark; }}
  body {{ margin:0; padding:32px 40px 80px; background:#0b0f16; color:#e7eef8;
         font:14px/1.6 -apple-system,"Segoe UI","Microsoft YaHei",sans-serif; }}
  h1 {{ font-size:26px; margin:0 0 6px; }}
  .sub {{ color:#8aa0bd; margin-bottom:28px; }}
  .grid {{ display:grid; grid-template-columns:repeat(auto-fill,minmax(520px,1fr)); gap:28px; }}
  figure {{ margin:0; background:#121a26; border:1px solid #1e2b3d; border-radius:12px; overflow:hidden; }}
  figure img {{ width:100%; display:block; background:#000; }}
  figcaption {{ display:flex; justify-content:space-between; gap:12px; padding:10px 14px; font-size:13px; }}
  figcaption span {{ color:#7d93b2; white-space:nowrap; }}
</style></head><body>
<h1>刀徒AI 短剧工作流 · 全流程截图册</h1>
<div class="sub">www.daotu.vip · V26.10.839 · 项目「妖王」· 共 {len(items)} 张 · 生成于 2026-10-31</div>
<div class="grid">{''.join(cards)}</div>
</body></html>"""
open(os.path.join(OUT, "index.html"), "w", encoding="utf-8").write(doc)

# ---------- contact sheet ----------
cols, thumb_w = 4, 520
thumbs = []
for it in items:
    with Image.open(os.path.join(OUT, it["file"])) as im:
        im = im.convert("RGB")
        ratio = thumb_w / im.width
        thumbs.append((it, im.resize((thumb_w, max(1, int(im.height * ratio))), Image.LANCZOS)))

pad, cap_h = 18, 34
rows = [thumbs[i:i + cols] for i in range(0, len(thumbs), cols)]
row_heights = [max(t[1].height for t in r) + cap_h for r in rows]
W = cols * thumb_w + (cols + 1) * pad
H = sum(row_heights) + (len(rows) + 1) * pad + 60
sheet = Image.new("RGB", (W, H), (11, 15, 22))
draw = ImageDraw.Draw(sheet)
draw.text((pad, 20), f"daotu.vip workflow  |  {len(items)} shots", fill=(200, 220, 245))
y = 60 + pad
for r, rh in zip(rows, row_heights):
    x = pad
    for it, im in r:
        sheet.paste(im, (x, y))
        draw.text((x + 2, y + im.height + 8), f"{it['num']} {it['name']}"[:60], fill=(150, 175, 205))
        x += thumb_w + pad
    y += rh + pad
sheet.save(os.path.join(OUT, "contact-sheet.jpg"), quality=82)
print("items", len(items), "sheet", sheet.size)
json.dump(items, open(os.path.join(OUT, "manifest.json"), "w", encoding="utf-8"), ensure_ascii=False, indent=1)
