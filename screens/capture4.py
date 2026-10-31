import json, os, re
from playwright.sync_api import sync_playwright

OUT = r"C:\Users\纪松本\Desktop\daotuai\screens"
CHROME = r"C:\Users\纪松本\AppData\Local\ms-playwright\chromium-1228\chrome-win64\chrome.exe"
state = json.load(open(r"C:\Users\纪松本\Desktop\daotuai\browser\state.json", encoding="utf-8"))
ck = [c for c in state["cookies"] if c["name"] == "daotu_company_session"][0]
NAV = ["首页", "剧本", "资产", "分镜", "站位图", "提示词", "视频", "对话", "教程"]

def shot(page, name):
    page.screenshot(path=os.path.join(OUT, f"{name}.png"), full_page=True)
    print("SHOT", name, flush=True)

def nav(page, label):
    """点击左侧导航：按精确文本 + JS 兜底，并验证页面切换"""
    ok = page.evaluate("""(label) => {
      const btns = [...document.querySelectorAll('button')].filter(b => b.getClientRects().length && b.textContent.trim().startsWith(label));
      const b = btns[0];
      if (!b) return 'none';
      b.scrollIntoView({block:'center'});
      b.click();
      return 'clicked';
    }""", label)
    return ok

def current_nav(page):
    return page.evaluate("""() => {
      const btns = [...document.querySelectorAll('button')].filter(b => b.getClientRects().length);
      const active = btns.find(b => /active|primary|selected/.test(b.className) && b.textContent.trim());
      return active ? active.textContent.trim().slice(0,8) : '';
    }""")

with sync_playwright() as p:
    b = p.chromium.launch(executable_path=CHROME, headless=True, args=["--no-sandbox", "--disable-gpu", "--hide-scrollbars"])
    ctx = b.new_context(viewport={"width": 1680, "height": 1000}, locale="zh-CN")
    ctx.add_cookies([{"name": ck["name"], "value": ck["value"], "domain": ck["domain"], "path": "/",
                      "expires": int(ck["expires"]), "httpOnly": True, "secure": True, "sameSite": "Lax"}])
    page = ctx.new_page()
    page.set_default_timeout(25000)
    page.goto("https://www.daotu.vip/", wait_until="domcontentloaded", timeout=90000)
    page.wait_for_timeout(9000)

    targets = [("对话", "12-AI对话"), ("教程", "13-教程"), ("首页", "14-首页-回看"), ("站位图", "09-站位图")]
    for label, name in targets:
        for attempt in range(3):
            nav(page, label)
            page.wait_for_timeout(7000)
            active = current_nav(page)
            print("nav", label, "->", active, "attempt", attempt, flush=True)
            if label in active or active.startswith(label):
                break
            page.wait_for_timeout(2000)
        shot(page, name)

    # 视频卡片弹层：等数据齐再点
    nav(page, "视频")
    page.wait_for_timeout(14000)
    n = page.evaluate("() => document.querySelectorAll('[data-video-create]').length")
    print("video-create els:", n, flush=True)
    r = page.evaluate("""() => {
      const el = [...document.querySelectorAll('[data-video-create]')].find(e => e.getClientRects().length);
      if (!el) return 'none';
      el.scrollIntoView({block:'center'});
      el.click();
      return 'clicked';
    }""")
    print("video card click:", r, flush=True)
    page.wait_for_timeout(7000)
    shot(page, "15-视频卡片-生成参数")
    page.keyboard.press("Escape")
    page.wait_for_timeout(1500)

    # 提示词：双击放大编辑
    nav(page, "提示词")
    page.wait_for_timeout(10000)
    r = page.evaluate("""() => {
      const t = document.querySelector('#videoPromptText, textarea, [contenteditable]');
      if (!t) return 'none';
      const r = t.getBoundingClientRect();
      t.dispatchEvent(new MouseEvent('dblclick', {bubbles:true, clientX:r.left+20, clientY:r.top+20}));
      return 'dblclick';
    }""")
    print("prompt dblclick:", r, flush=True)
    page.wait_for_timeout(5000)
    shot(page, "19-提示词编辑")
    b.close()
print("DONE4")
