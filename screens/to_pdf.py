import os, pathlib
from playwright.sync_api import sync_playwright

OUT = r"C:\Users\纪松本\Desktop\daotuai\screens"
CHROME = r"C:\Users\纪松本\AppData\Local\ms-playwright\chromium-1228\chrome-win64\chrome.exe"
url = pathlib.Path(OUT, "index.html").as_uri()

with sync_playwright() as p:
    b = p.chromium.launch(executable_path=CHROME, headless=True, args=["--no-sandbox"])
    page = b.new_page(viewport={"width": 1680, "height": 1000})
    page.goto(url, wait_until="load", timeout=90000)
    # 等所有图片解码完成
    page.wait_for_function("""() => {
        const imgs = [...document.images];
        return imgs.length > 0 && imgs.every(i => i.complete);
    }""", timeout=120000)
    page.wait_for_timeout(2000)
    pdf = os.path.join(OUT, "刀徒AI-工作流-截图册.pdf")
    page.pdf(path=pdf, format="A3", landscape=True, print_background=True,
             margin={"top": "8mm", "bottom": "8mm", "left": "8mm", "right": "8mm"})
    print("pdf", pdf, round(os.path.getsize(pdf) / 1024 / 1024, 2), "MB")
    b.close()
