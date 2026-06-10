"""Quick diagnostic: launch profile-dashboard and see where LinkedIn redirects."""
import os
import sys
import asyncio
from pathlib import Path

LINKEDIN_MCP_DIR = Path.home() / ".linkedin-mcp"
os.environ["PLAYWRIGHT_BROWSERS_PATH"] = str(LINKEDIN_MCP_DIR / "patchright-browsers")

from patchright.async_api import async_playwright

async def main():
    pw = await async_playwright().start()
    
    profile_dir = LINKEDIN_MCP_DIR / "profile-dashboard"
    print(f"Profile dir exists: {profile_dir.exists()}")
    
    try:
        context = await pw.chromium.launch_persistent_context(
            user_data_dir=str(profile_dir),
            headless=True,
            viewport={"width": 1280, "height": 720},
            locale="en-US",
        )
        print("Context launched OK")
        
        page = context.pages[0] if context.pages else await context.new_page()
        
        # Try navigating to feed
        print("Navigating to /feed/...")
        try:
            response = await page.goto("https://www.linkedin.com/feed/", wait_until="domcontentloaded", timeout=20000)
            print(f"  Response status: {response.status if response else 'None'}")
        except Exception as e:
            print(f"  Navigation error: {e}")
        
        await asyncio.sleep(3)
        
        final_url = page.url
        title = await page.title()
        print(f"  Final URL: {final_url}")
        print(f"  Page title: {title}")
        
        # Check cookies
        cookies = await context.cookies()
        li_cookies = [c for c in cookies if "linkedin.com" in c.get("domain", "")]
        print(f"  LinkedIn cookies count: {len(li_cookies)}")
        li_at = [c for c in li_cookies if c["name"] == "li_at"]
        print(f"  Has li_at: {bool(li_at)}")
        if li_at:
            print(f"  li_at value prefix: {li_at[0]['value'][:20]}...")
        
        # Try a different URL
        print("\nNavigating to linkedin.com (base)...")
        try:
            response = await page.goto("https://www.linkedin.com/", wait_until="domcontentloaded", timeout=20000)
            print(f"  Response status: {response.status if response else 'None'}")
        except Exception as e:
            print(f"  Navigation error: {e}")
        
        await asyncio.sleep(2)
        final_url = page.url
        title = await page.title()
        print(f"  Final URL: {final_url}")
        print(f"  Page title: {title}")
        
        # Check if auth is valid by looking at page content
        is_logged_in = await page.evaluate("""() => {
            return {
                hasNav: !!document.querySelector('nav, .global-nav'),
                hasLoginForm: !!document.querySelector('form.login__form, #session_key'),
                hasAuthwall: !!document.querySelector('.authwall-join-form'),
                bodyText: document.body?.innerText?.substring(0, 500)
            }
        }""")
        print(f"  Auth check: {is_logged_in}")
        
        await context.close()
    except Exception as e:
        print(f"Error: {e}")
    finally:
        await pw.stop()

asyncio.run(main())
