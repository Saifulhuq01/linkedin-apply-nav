"""Test using the source profile directly (no copy, no cookie bridge)."""
import os
import sys
import json
import asyncio
from pathlib import Path

LINKEDIN_MCP_DIR = Path.home() / ".linkedin-mcp"
os.environ["PLAYWRIGHT_BROWSERS_PATH"] = str(LINKEDIN_MCP_DIR / "patchright-browsers")

from patchright.async_api import async_playwright

async def main():
    pw = await async_playwright().start()
    
    profile_dir = LINKEDIN_MCP_DIR / "profile"
    print(f"Profile dir: {profile_dir}")
    print(f"Profile exists: {profile_dir.exists()}")
    
    try:
        context = await pw.chromium.launch_persistent_context(
            user_data_dir=str(profile_dir),
            headless=True,
            viewport={"width": 1280, "height": 720},
            locale="en-US",
        )
        print("Context launched OK with source profile!")
        
        page = context.pages[0] if context.pages else await context.new_page()
        
        # Test 1: LinkedIn feed
        print("\n--- Test 1: /feed/ ---")
        try:
            resp = await page.goto("https://www.linkedin.com/feed/", wait_until="domcontentloaded", timeout=20000)
            print(f"Status: {resp.status if resp else 'None'}")
        except Exception as e:
            print(f"Error: {e}")
        
        await asyncio.sleep(3)
        print(f"URL: {page.url}")
        print(f"Title: {await page.title()}")
        
        logged_in = "login" not in page.url and "authwall" not in page.url
        print(f"Logged in: {logged_in}")
        
        if logged_in:
            # Test 2: Job search
            print("\n--- Test 2: Job search ---")
            try:
                resp = await page.goto("https://www.linkedin.com/jobs/search/?keywords=Java&location=Chennai&f_EA=true", wait_until="domcontentloaded", timeout=20000)
                print(f"Status: {resp.status if resp else 'None'}")
            except Exception as e:
                print(f"Error: {e}")
            await asyncio.sleep(3)
            print(f"URL: {page.url}")
            print(f"Title: {await page.title()}")
        
        # Check cookies
        cookies = await context.cookies()
        li_at = [c for c in cookies if c["name"] == "li_at" and "linkedin.com" in c.get("domain", "")]
        print(f"\nHas li_at: {bool(li_at)}")
        
        await context.close()
        print("\nContext closed cleanly.")
        
    except Exception as e:
        print(f"ERROR: {e}")
    
    await pw.stop()
    print("Done.")

asyncio.run(main())
