"""Diagnostic: test cookie bridge with ALL cookies, not just bridge subset."""
import os
import sys
import json
import asyncio
from pathlib import Path

LINKEDIN_MCP_DIR = Path.home() / ".linkedin-mcp"
os.environ["PLAYWRIGHT_BROWSERS_PATH"] = str(LINKEDIN_MCP_DIR / "patchright-browsers")

from patchright.async_api import async_playwright

COOKIES_PATH = LINKEDIN_MCP_DIR / "cookies.json"

async def main():
    pw = await async_playwright().start()
    
    # Load ALL cookies from portable file
    cookies_data = json.loads(COOKIES_PATH.read_text(encoding="utf-8"))
    print(f"Total cookies in file: {len(cookies_data)}")
    
    li_cookies = [c for c in cookies_data if "linkedin.com" in c.get("domain", "")]
    print(f"LinkedIn cookies: {len(li_cookies)}")
    for c in li_cookies:
        print(f"  {c['name']}: domain={c['domain']} expires={c.get('expires', 'session')}")
    
    # Launch fresh browser (not persistent context)
    browser = await pw.chromium.launch(headless=True)
    context = await browser.new_context(
        viewport={"width": 1280, "height": 720},
        locale="en-US",
    )
    
    # Import ALL cookies (not just bridge subset)
    normalized = []
    for c in li_cookies:
        cookie = {**c}
        domain = cookie.get("domain", "")
        if "www.linkedin.com" in domain:
            cookie["domain"] = ".linkedin.com"
        normalized.append(cookie)
    
    await context.add_cookies(normalized)
    print(f"\nImported {len(normalized)} cookies")
    
    # Verify cookies after import
    ctx_cookies = await context.cookies("https://www.linkedin.com")
    print(f"Context cookies for linkedin.com: {len(ctx_cookies)}")
    li_at_found = [c for c in ctx_cookies if c["name"] == "li_at"]
    print(f"li_at present in context: {bool(li_at_found)}")
    
    page = await context.new_page()
    
    # Navigate to linkedin.com base first
    print("\n--- Test 1: Navigate to linkedin.com ---")
    try:
        resp = await page.goto("https://www.linkedin.com/", wait_until="domcontentloaded", timeout=15000)
        print(f"Status: {resp.status if resp else 'None'}")
    except Exception as e:
        print(f"Error: {e}")
    await asyncio.sleep(2)
    print(f"URL: {page.url}")
    print(f"Title: {await page.title()}")
    
    # Check if we're logged in
    check = await page.evaluate("""() => ({
        url: window.location.href,
        hasGlobalNav: !!document.querySelector('.global-nav, [data-test-global-nav]'),
        hasLoginForm: !!document.querySelector('#session_key, .login__form'),
        body200: document.body?.innerText?.substring(0, 200)
    })""")
    print(f"Auth check: {json.dumps(check, indent=2)}")
    
    # Try navigating to feed
    print("\n--- Test 2: Navigate to /feed/ ---")
    try:
        resp = await page.goto("https://www.linkedin.com/feed/", wait_until="domcontentloaded", timeout=15000)
        print(f"Status: {resp.status if resp else 'None'}")
    except Exception as e:
        print(f"Error: {e}")
    await asyncio.sleep(2)
    print(f"URL: {page.url}")
    print(f"Title: {await page.title()}")
    
    # Try job search URL directly
    print("\n--- Test 3: Navigate to job search ---")
    try:
        resp = await page.goto("https://www.linkedin.com/jobs/search/?keywords=Java&location=Chennai&f_EA=true", wait_until="domcontentloaded", timeout=15000)
        print(f"Status: {resp.status if resp else 'None'}")
    except Exception as e:
        print(f"Error: {e}")
    await asyncio.sleep(2)
    print(f"URL: {page.url}")
    print(f"Title: {await page.title()}")
    
    await context.close()
    await browser.close()
    await pw.stop()

asyncio.run(main())
