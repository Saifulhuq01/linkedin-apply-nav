"""Diagnostic: Check the actual LinkedIn Easy Apply button HTML."""
import os
import asyncio
from pathlib import Path

LINKEDIN_MCP_DIR = Path.home() / ".linkedin-mcp"
os.environ["PLAYWRIGHT_BROWSERS_PATH"] = str(LINKEDIN_MCP_DIR / "patchright-browsers")

from patchright.async_api import async_playwright

async def main():
    pw = await async_playwright().start()
    
    try:
        context = await pw.chromium.launch_persistent_context(
            user_data_dir=str(LINKEDIN_MCP_DIR / "profile"),
            headless=False,  # Visual so we can see
            viewport={"width": 1280, "height": 720},
        )
        
        page = context.pages[0] if context.pages else await context.new_page()
        
        # Navigate to a known Easy Apply job from search
        url = "https://www.linkedin.com/jobs/view/4417488966/"
        print(f"Navigating to {url}...")
        await page.goto(url, wait_until="domcontentloaded", timeout=20000)
        await asyncio.sleep(5)
        
        print(f"Final URL: {page.url}")
        print(f"Title: {await page.title()}")
        
        # Look for ALL buttons on the page
        buttons = await page.evaluate("""() => {
            const btns = Array.from(document.querySelectorAll('button'));
            return btns.map(b => ({
                text: b.innerText.trim(),
                class: b.className,
                ariaLabel: b.getAttribute('aria-label'),
                id: b.id,
                visible: b.offsetParent !== null
            })).filter(b => b.text.length > 0);
        }""")
        
        print(f"\n=== ALL VISIBLE BUTTONS ({len(buttons)}) ===")
        for b in buttons:
            if b.get("visible"):
                print(f"  '{b['text']}' class='{b['class'][:80]}' aria='{b.get('ariaLabel', '')}'")
        
        # Look specifically for anything "apply" related
        apply_buttons = [b for b in buttons if "apply" in b["text"].lower() or "apply" in (b.get("ariaLabel") or "").lower()]
        print(f"\n=== APPLY-RELATED BUTTONS ({len(apply_buttons)}) ===")
        for b in apply_buttons:
            print(f"  TEXT: '{b['text']}' CLASS: '{b['class']}' ARIA: '{b.get('ariaLabel')}'")
        
        # Also look for apply-related elements (not just buttons)
        apply_elements = await page.evaluate("""() => {
            const allEls = document.querySelectorAll('[class*="apply"], [aria-label*="apply"], [data-control-name*="apply"]');
            return Array.from(allEls).map(el => ({
                tag: el.tagName,
                text: el.innerText.trim().substring(0, 100),
                class: el.className.substring(0, 100),
                ariaLabel: el.getAttribute('aria-label'),
                visible: el.offsetParent !== null
            }));
        }""")
        print(f"\n=== APPLY-RELATED ELEMENTS ({len(apply_elements)}) ===")
        for el in apply_elements:
            print(f"  <{el['tag']}> text='{el['text']}' class='{el['class']}'")
        
        # Take a screenshot for reference
        await page.screenshot(path="diag_apply_btn.png")
        print("\nScreenshot saved to diag_apply_btn.png")
        
        await context.close()
    except Exception as e:
        print(f"Error: {e}")
    
    await pw.stop()

asyncio.run(main())
