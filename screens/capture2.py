import json, os
from playwright.sync_api import sync_playwright

OUT = r"C:\Users\纪松本\Desktop\daotuai\screens"
CHROME = r"C:\Users\纪松本\AppData\Local\ms-playwright\chromium-1228\chrome-win64\chrome.exe"
state = json.load(open(r"C:\Users\纪松本\Desktop\daotuai\browser\state.json", encoding="utf-8"))
ck = [c for c in state["cookies"] if c["name"] == "daotu_company_session"][0]
PID = "e2a92745-846b-4c4b-8c2f-bf924f2db1b5"  # 妖王

def shot(page, name):
    page.screenshot(path=os.path.join(OUT, f"{name}.png"), full_page=True)
    print("SHOT", name, flush=True)

def select_project(page):
    page.locator("#projectSelectBtn").click()
    page.wait_for_timeout(2500)
    page.screenshot(path=os.path.join(OUT, "04-项目选择器.png"), full_page=True)
    clicked = False
    for sel in [f'[data-project-id="{PID}"]', 'text=妖王']:
        try:
            el = page.locator(sel).first
            if el.count():
                el.click(timeout=5000)
                clicked = True
                break
        except Exception as e:
            print("select try fail", sel, type(e).__name__, flush=True)
    page.wait_for_timeout(9000)
    header = page.inner_text("body")[:160].replace("\n", " ")
    print("selected:", clicked, "| header:", header.encode("unicode_escape").decode()[:160], flush=True)

with sync_playwright() as p:
    b = p.chromium.launch(executable_path=CHROME, headless=True,
                          args=["--no-sandbox", "--disable-gpu", "--hide-scrollbars"])
    ctx = b.new_context(viewport={"width": 1680, "height": 1000}, locale="zh-CN")
    ctx.add_cookies([{"name": ck["name"], "value": ck["value"], "domain": ck["domain"], "path": "/",
                      "expires": int(ck["expires"]), "httpOnly": True, "secure": True, "sameSite": "Lax"}])
    page = ctx.new_page()
    page.set_default_timeout(25000)
    page.goto("https://www.daotu.vip/", wait_until="domcontentloaded", timeout=90000)
    page.wait_for_timeout(9000)
    # 若当前不是妖王：切过去
    if "妖王" not in page.inner_text("body")[:200]:
        select_project(page)
    else:
        print("already on 妖王", flush=True)

    # 资产页（先确认有内容）
    page.locator("button:has-text('资产')").first.click()
    page.wait_for_timeout(9000)
    shot(page, "07-资产库")

    # 资产卡片详情
    try:
        cards = page.locator("[data-asset-open], .asset-card, [data-asset-id]")
        print("asset cards:", cards.count(), flush=True)
        if cards.count():
            cards.first.click()
            page.wait_for_timeout(5000)
            shot(page, "16-资产详情")
            page.keyboard.press("Escape")
            page.wait_for_timeout(1200)
    except Exception as e:
        print("asset detail fail", type(e).__name__, flush=True)

    for label, name in [("剧本", "06-剧本管理"), ("分镜", "08-分镜脚本"), ("站位图", "09-站位图"),
                        ("提示词", "10-视频提示词"), ("视频", "11-视频任务"), ("对话", "12-AI对话"),
                        ("教程", "13-教程"), ("首页", "14-首页-回看")]:
        try:
            page.locator(f"button:has-text('{label}')").first.click()
            page.wait_for_timeout(9000)
            shot(page, name)
        except Exception as e:
            print("page fail", label, type(e).__name__, flush=True)

    # 视频卡片弹层
    try:
        page.locator("button:has-text('视频')").first.click()
        page.wait_for_timeout(7000)
        cards = page.locator("[data-video-create]")
        print("video cards:", cards.count(), flush=True)
        if cards.count():
            cards.first.click()
            page.wait_for_timeout(6000)
            shot(page, "15-视频卡片-生成参数")
            page.keyboard.press("Escape")
            page.wait_for_timeout(1200)
    except Exception as e:
        print("video card fail", type(e).__name__, flush=True)

    # 生成设置
    try:
        page.locator("button:has-text('资产')").first.click()
        page.wait_for_timeout(7000)
        btns = page.locator("button:has-text('生成设置')")
        print("gen-setting btns:", btns.count(), flush=True)
        if btns.count():
            btns.first.click()
            page.wait_for_timeout(3000)
            shot(page, "17-生成设置")
    except Exception as e:
        print("gen settings fail", type(e).__name__, flush=True)

    b.close()
print("DONE2")
