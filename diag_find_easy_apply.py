"""Find actual Easy Apply jobs by checking the job page buttons."""
import os
import asyncio
from pathlib import Path

LINKEDIN_MCP_DIR = Path.home() / ".linkedin-mcp"
os.environ["PLAYWRIGHT_BROWSERS_PATH"] = str(LINKEDIN_MCP_DIR / "patchright-browsers")

import sys
sys.path.append(str(Path(__file__).parent / "linkedin-mcp-server"))
from linkedin_mcp_server.scraping.extractor import LinkedInExtractor
from patchright.async_api import async_playwright

async def main():
    pw = await async_playwright().start()
    
    context = await pw.chromium.launch_persistent_context(
        user_data_dir=str(LINKEDIN_MCP_DIR / "profile"),
        headless=True,
        viewport={"width": 1280, "height": 720},
    )
    
    page = context.pages[0] if context.pages else await context.new_page()
    
    # Navigate to feed first
    await page.goto("https://www.linkedin.com/feed/", wait_until="domcontentloaded", timeout=20000)
    await asyncio.sleep(2)
    
    extractor = LinkedInExtractor(page)
    
    search_result = await extractor.search_jobs(
        keywords="Java",
        location="Chennai",
        max_pages=1,
        easy_apply=True
    )
    
    job_ids = search_result.get("job_ids", [])
    print(f"Search returned {len(job_ids)} 'Easy Apply' jobs")
    
    # Check each job's actual button
    for job_id in job_ids[:5]:
        await page.goto(f"https://www.linkedin.com/jobs/view/{job_id}/", wait_until="domcontentloaded", timeout=20000)
        await asyncio.sleep(3)
        
        buttons = await page.evaluate("""() => {
            const btns = Array.from(document.querySelectorAll('button, a'));
            return btns
                .filter(b => b.offsetParent !== null)
                .filter(b => {
                    const text = b.innerText?.toLowerCase() || '';
                    return text.includes('apply') || text.includes('easy');
                })
                .map(b => ({
                    tag: b.tagName,
                    text: b.innerText.trim(),
                    class: b.className?.substring(0, 80) || '',
                    href: b.href || null,
                    ariaLabel: b.getAttribute('aria-label')
                }));
        }""")
        
        is_easy_apply = any("easy apply" in b["text"].lower() for b in buttons)
        has_external_apply = any(b["text"].strip() == "Apply" or "apply" in (b.get("href") or "") for b in buttons if "easy" not in b["text"].lower())
        
        print(f"\nJob {job_id}:")
        for b in buttons:
            print(f"  <{b['tag']}> '{b['text']}' href={b.get('href', 'N/A')}")
        print(f"  → Easy Apply: {is_easy_apply}, External Apply: {has_external_apply}")
    
    await context.close()
    await pw.stop()

asyncio.run(main())
