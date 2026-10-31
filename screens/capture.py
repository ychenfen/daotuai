import json, os, re, traceback
from playwright.sync_api import sync_playwright

OUT = r"C:\Users\纪松本\Desktop\daotuai\screens"
CHROME = r"C:\Users\纪松本\AppData\Local\ms-playwright\chromium-1228\chrome-win64\chrome.exe"
state = json.load(open(r"C:\Users\纪松本\Desktop\daotuai\browser\state.json", encoding="utf-8"))
ck = [c for c in state["cookies"] if c["name"] == "daotu_company_session"][0]
PID = "e2a92745-846b-4c4b-8c2f-bf924f2db1b5"  # 妖王：资产/分镜/提示词齐全
os.makedirs(OUT, exist_ok=True)

manifest = []
def shot(page, name, full=True):
    path = os.path.join(OUT, f"{name}.png")
    try:
        page.screenshot(path=path, full_page=full)
        manifest.append({"name": name, "file": os.path.basename(path), "w": page.viewport_size["width"]})
        print("SHOT", name, flush=True)
    except Exception as e:
        print("SHOT-FAIL", name, type(e).__name__, flush=True)

def safe(fn, label):
    try:
        fn()
    except Exception as e:
        print("STEP-FAIL", label, type(e).__name__, str(e)[:120], flush=True)

with sync_playwright() as p:
    b = p.chromium.launch(executable_path=CHROME, headless=True,
                          args=["--no-sandbox", "--disable-gpu", "--hide-scrollbars"])
    ctx = b.new_context(viewport={"width": 1680, "height": 1000}, device_scale_factor=1, locale="zh-CN")
    ctx.add_cookies([{"name": ck["name"], "value": ck["value"], "domain": ck["domain"], "path": "/",
                      "expires": int(ck["expires"]), "httpOnly": True, "secure": True, "sameSite": "Lax"}])
    page = ctx.new_page()
    page.set_default_timeout(20000)

    # 0. 未登录态（用无 cookie 的新上下文）
    anon = b.new_context(viewport={"width": 1680, "height": 1000}, locale="zh-CN")
    ap = anon.new_page()
    safe(lambda: ap.goto("https://www.daotu.vip/", wait_until="domcontentloaded", timeout=60000), "anon-goto")
    ap.wait_for_timeout(7000)
    shot(ap, "00-登录页")
    anon.close()

    # 1. 登录态进入
    page.goto("https://www.daotu.vip/", wait_until="domcontentloaded", timeout=90000)
    page.wait_for_timeout(4500)
    page.evaluate("id => localStorage.setItem('daotu_project_id', id)", PID)
    page.reload(wait_until="domcontentloaded")
    page.wait_for_timeout(7000)
    shot(page, "01-首页-项目概览")

    # 顶部弹层
    def notif():
        page.locator("#notificationBtn").click()
        page.wait_for_timeout(2500)
        shot(page, "02-通知中心")
        page.keyboard.press("Escape")
        page.wait_for_timeout(800)
    safe(notif, "notif")

    def usermenu():
        page.locator("#userMenuBtn").click()
        page.wait_for_timeout(2000)
        shot(page, "03-账号菜单")
        page.keyboard.press("Escape")
        page.wait_for_timeout(600)
    safe(usermenu, "usermenu")

    def projectsel():
        page.locator("#projectSelectBtn").click()
        page.wait_for_timeout(2500)
        shot(page, "04-项目选择器")
        page.keyboard.press("Escape")
        page.wait_for_timeout(800)
    safe(projectsel, "projectsel")

    def newproj():
        page.locator("#newProjectBtn").click()
        page.wait_for_timeout(2500)
        shot(page, "05-新建项目")
        page.keyboard.press("Escape")
        page.wait_for_timeout(800)
    safe(newproj, "newproj")

    # 2. 左侧各页
    pages = [
        ("剧本", "06-剧本管理"),
        ("资产", "07-资产库"),
        ("分镜", "08-分镜脚本"),
        ("站位图", "09-站位图"),
        ("提示词", "10-视频提示词"),
        ("视频", "11-视频任务"),
        ("对话", "12-AI对话"),
        ("教程", "13-教程"),
        ("首页", "14-首页-回看"),
    ]
    for label, name in pages:
        def go(label=label, name=name):
            page.locator(f"button:has-text('{label}')").first.click()
            page.wait_for_timeout(9000)
            shot(page, name)
        safe(go, f"page-{label}")

    # 3. 视频页细节：卡片弹层
    def video_card():
        page.locator("button:has-text('视频')").first.click()
        page.wait_for_timeout(6000)
        cards = page.locator("[data-video-create]")
        if cards.count():
            cards.first.click()
            page.wait_for_timeout(5000)
            shot(page, "15-视频卡片-生成参数")
            page.keyboard.press("Escape")
            page.wait_for_timeout(1000)
    safe(video_card, "video-card")

    # 4. 资产页细节：打开第一个资产的编辑/历史版本
    def asset_card():
        page.locator("button:has-text('资产')").first.click()
        page.wait_for_timeout(7000)
        cards = page.locator("[data-asset-open], .asset-card, [data-asset-id]")
        if cards.count():
            cards.first.click()
            page.wait_for_timeout(4000)
            shot(page, "16-资产详情")
            page.keyboard.press("Escape")
            page.wait_for_timeout(1000)
    safe(asset_card, "asset-card")

    # 5. 生成设置弹层（各页右上角）
    def gen_settings():
        page.locator("button:has-text('资产')").first.click()
        page.wait_for_timeout(6000)
        btns = page.locator("button:has-text('生成设置')")
        if btns.count():
            btns.first.click()
            page.wait_for_timeout(2500)
            shot(page, "17-生成设置")
            page.keyboard.press("Escape")
            page.wait_for_timeout(800)
    safe(gen_settings, "gen-settings")

    json.dump(manifest, open(os.path.join(OUT, "manifest.json"), "w", encoding="utf-8"), ensure_ascii=False, indent=1)
    b.close()
print("DONE", len(manifest))
