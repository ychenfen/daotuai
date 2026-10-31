import json, os
from playwright.sync_api import sync_playwright

OUT = r"C:\Users\纪松本\Desktop\daotuai\screens"
CHROME = r"C:\Users\纪松本\AppData\Local\ms-playwright\chromium-1228\chrome-win64\chrome.exe"
state = json.load(open(r"C:\Users\纪松本\Desktop\daotuai\browser\state.json", encoding="utf-8"))
ck = [c for c in state["cookies"] if c["name"] == "daotu_company_session"][0]

def shot(page, name):
    page.screenshot(path=os.path.join(OUT, f"{name}.png"), full_page=True)
    print("SHOT", name, flush=True)

def js_click(page, sel, idx=0):
    return page.evaluate("""([sel, idx]) => {
      const els = [...document.querySelectorAll(sel)].filter(e => e.getClientRects().length);
      const el = els[idx];
      if (!el) return 'none';
      el.scrollIntoView({block:'center'});
      el.click();
      return 'ok';
    }""", [sel, idx])

with sync_playwright() as p:
    b = p.chromium.launch(executable_path=CHROME, headless=True, args=["--no-sandbox", "--disable-gpu", "--hide-scrollbars"])
    ctx = b.new_context(viewport={"width": 1680, "height": 1000}, locale="zh-CN")
    ctx.add_cookies([{"name": ck["name"], "value": ck["value"], "domain": ck["domain"], "path": "/",
                      "expires": int(ck["expires"]), "httpOnly": True, "secure": True, "sameSite": "Lax"}])
    page = ctx.new_page()
    page.set_default_timeout(25000)
    page.goto("https://www.daotu.vip/", wait_until="domcontentloaded", timeout=90000)
    page.wait_for_timeout(9000)

    # --- 视频卡片弹层 ---
    page.locator("button:has-text('视频')").first.click()
    page.wait_for_timeout(8000)
    print("video create els:", page.locator("[data-video-create]").count(), flush=True)
    r = js_click(page, "[data-video-create]")
    print("js click video card:", r, flush=True)
    page.wait_for_timeout(6000)
    shot(page, "15-视频卡片-生成参数")
    page.keyboard.press("Escape")
    page.wait_for_timeout(1500)

    # --- 分镜卡片 ---
    page.locator("button:has-text('分镜')").first.click()
    page.wait_for_timeout(8000)
    for sel in ["[data-shot-open]", "[data-storyboard-open]", ".storyboard-card", "[data-shot-id]"]:
        if page.locator(sel).count():
            print("shot sel", sel, page.locator(sel).count(), flush=True)
            js_click(page, sel)
            page.wait_for_timeout(5000)
            shot(page, "18-分镜卡片")
            break
    page.keyboard.press("Escape")
    page.wait_for_timeout(1200)

    # --- 生成设置 ---
    page.locator("button:has-text('资产')").first.click()
    page.wait_for_timeout(7000)
    print("gen btns:", page.locator("button:has-text('生成设置')").count(), flush=True)
    r = js_click(page, "button", 0) if False else None
    ok = page.evaluate("""() => {
      const b = [...document.querySelectorAll('button')].find(x => x.textContent.trim() === '生成设置' && x.getClientRects().length);
      if (!b) return 'none';
      b.click(); return 'ok';
    }""")
    print("gen click:", ok, flush=True)
    page.wait_for_timeout(3500)
    shot(page, "17-生成设置")

    # --- 提示词全屏编辑 ---
    page.keyboard.press("Escape")
    page.wait_for_timeout(1200)
    page.locator("button:has-text('提示词')").first.click()
    page.wait_for_timeout(8000)
    ok = page.evaluate("""() => {
      const b = [...document.querySelectorAll('button,[data-fullscreen],[data-prompt-open]')]
        .find(x => /全屏|放大|编辑/.test(x.textContent) && x.getClientRects().length);
      if (!b) return 'none';
      b.click(); return b.textContent.trim().slice(0,12);
    }""")
    print("prompt fullscreen:", ok, flush=True)
    page.wait_for_timeout(4000)
    shot(page, "19-提示词编辑")
    b.close()
print("DONE3")
