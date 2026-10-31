import json, os, re
from playwright.sync_api import sync_playwright

OUT = r"C:\Users\纪松本\Desktop\daotuai\probe_api"
CHROME = r"C:\Users\纪松本\AppData\Local\ms-playwright\chromium-1228\chrome-win64\chrome.exe"
state = json.load(open(r"C:\Users\纪松本\Desktop\daotuai\browser\state.json", encoding="utf-8"))
ck = [c for c in state["cookies"] if c["name"] == "daotu_company_session"][0]
PID = "e2a92745-846b-4c4b-8c2f-bf924f2db1b5"
os.makedirs(OUT, exist_ok=True)

records = []
seen = set()

def on_response(r):
    try:
        url = r.url
        if "daotu.vip/api/" not in url:
            return
        req = r.request
        path = url.split("daotu.vip")[1]
        key = f"{req.method} {path.split('?')[0]}"
        rec = {"method": req.method, "path": path, "status": r.status, "resource": req.resource_type}
        pd = req.post_data
        if pd:
            rec["request"] = pd[:4000]
        ct = (r.headers or {}).get("content-type", "")
        if "json" in ct and r.status < 400:
            try:
                body = r.text()
                rec["response"] = body[:12000]
                rec["response_size"] = len(body)
            except Exception:
                pass
        records.append(rec)
        seen.add(key)
    except Exception:
        pass

with sync_playwright() as p:
    b = p.chromium.launch(executable_path=CHROME, headless=True,
                          args=["--no-sandbox", "--disable-gpu", "--hide-scrollbars"])
    ctx = b.new_context(viewport={"width": 1680, "height": 1000}, locale="zh-CN")
    ctx.add_cookies([{"name": ck["name"], "value": ck["value"], "domain": ck["domain"], "path": "/",
                      "expires": int(ck["expires"]), "httpOnly": True, "secure": True, "sameSite": "Lax"}])
    page = ctx.new_page()
    page.on("response", on_response)
    page.goto("https://www.daotu.vip/", wait_until="domcontentloaded", timeout=90000)
    page.wait_for_timeout(9000)
    page.evaluate("id => localStorage.setItem('daotu_project_id', id)", PID)
    page.reload(wait_until="domcontentloaded")
    page.wait_for_timeout(9000)

    def nav(label):
        page.evaluate("""(label) => {
          const b = [...document.querySelectorAll('button')].find(x => x.getClientRects().length && x.textContent.trim().startsWith(label));
          b && b.click();
        }""", label)

    for label in ["剧本", "资产", "分镜", "站位图", "提示词", "视频", "对话", "首页"]:
        nav(label)
        page.wait_for_timeout(8000)

    # 打开生成设置，读模型目录
    nav("资产")
    page.wait_for_timeout(6000)
    page.evaluate("""() => { const b=[...document.querySelectorAll('button')].find(x=>x.textContent.trim()==='生成设置'&&x.getClientRects().length); b&&b.click(); }""")
    page.wait_for_timeout(4000)
    page.keyboard.press("Escape")
    page.wait_for_timeout(1500)

    json.dump(records, open(os.path.join(OUT, "api_capture.json"), "w", encoding="utf-8"), ensure_ascii=False, indent=1)
    print("records", len(records), "unique", len(seen))
    b.close()
