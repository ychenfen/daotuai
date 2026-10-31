import json, os
from playwright.sync_api import sync_playwright

OUT = r"C:\Users\纪松本\Desktop\daotuai\screens"
CHROME = r"C:\Users\纪松本\AppData\Local\ms-playwright\chromium-1228\chrome-win64\chrome.exe"
state = json.load(open(r"C:\Users\纪松本\Desktop\daotuai\browser\state.json", encoding="utf-8"))
ck = [c for c in state["cookies"] if c["name"] == "daotu_company_session"][0]

with sync_playwright() as p:
    b = p.chromium.launch(executable_path=CHROME, headless=True, args=["--no-sandbox", "--disable-gpu", "--hide-scrollbars"])
    ctx = b.new_context(viewport={"width": 1680, "height": 1000}, locale="zh-CN")
    ctx.add_cookies([{"name": ck["name"], "value": ck["value"], "domain": ck["domain"], "path": "/",
                      "expires": int(ck["expires"]), "httpOnly": True, "secure": True, "sameSite": "Lax"}])
    page = ctx.new_page()
    page.goto("https://www.daotu.vip/", wait_until="domcontentloaded", timeout=90000)
    page.wait_for_timeout(8000)
    page.evaluate("""() => {
      const b = [...document.querySelectorAll('button')].find(x => x.textContent.trim().startsWith('视频') && x.getClientRects().length);
      b && b.click();
    }""")
    page.wait_for_timeout(15000)
    info = page.evaluate("""() => {
      const keys = new Set();
      document.querySelectorAll('*').forEach(el => {
        for (const a of el.attributes || []) if (a.name.startsWith('data-')) keys.add(a.name);
      });
      return [...keys].filter(k => /video|shot|card/.test(k)).slice(0, 30);
    }""")
    print("data-attrs:", info, flush=True)
    r = page.evaluate("""() => {
      const el = [...document.querySelectorAll('*')].find(e => e.getClientRects().length && /^1-1$/.test(e.textContent.trim()) && e.children.length === 0);
      if (!el) return 'none';
      (el.closest('button,[data-video-open],[data-shot-card]') || el).click();
      return 'clicked';
    }""")
    print("card click:", r, flush=True)
    page.wait_for_timeout(6000)
    page.screenshot(path=os.path.join(OUT, "15-视频卡片-生成参数.png"), full_page=True)
    print("shot 15", flush=True)
    b.close()
print("DONE5")
