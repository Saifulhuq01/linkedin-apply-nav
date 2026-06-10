import sys
import os
import json
import logging
import asyncio
import random
import shutil
import time
from pathlib import Path
from typing import List, Dict, Any, Optional

from fastapi import FastAPI, WebSocket, WebSocketDisconnect, HTTPException
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel
import uvicorn

# Add the mcp server directory to path
sys.path.append(str(Path(__file__).parent / "linkedin-mcp-server"))
from linkedin_mcp_server.scraping.extractor import LinkedInExtractor

# CRITICAL: Set PLAYWRIGHT_BROWSERS_PATH before importing patchright
LINKEDIN_MCP_DIR = Path.home() / ".linkedin-mcp"
os.environ["PLAYWRIGHT_BROWSERS_PATH"] = str(LINKEDIN_MCP_DIR / "patchright-browsers")

from patchright.async_api import async_playwright


logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")
logger = logging.getLogger("job_applier_dashboard")

app = FastAPI(title="LinkedIn Easy Apply Automation Dashboard")

WORKSPACE_DIR = Path(__file__).parent
TEMPLATES_DIR = WORKSPACE_DIR / "templates"
RESUME_TXT_PATH = WORKSPACE_DIR / "Mohammed_Saifulhuq_Resume.txt"
RESUME_PDF_PATH = WORKSPACE_DIR / "Mohammed_Saifulhuq_Resume.pdf"
SOURCE_PROFILE_DIR = LINKEDIN_MCP_DIR / "profile"
COOKIES_PATH = LINKEDIN_MCP_DIR / "cookies.json"

app.mount("/templates", StaticFiles(directory=str(TEMPLATES_DIR)), name="templates")

# ─── Global State ───
active_connections: List[WebSocket] = []
current_apply_task: Optional[asyncio.Task] = None
question_event = asyncio.Event()
user_answer = ""
apply_status = "idle"
active_jobs = []

# Shared browser session — reused across search and apply
_shared_pw = None
_shared_context = None
_shared_page = None      # headless page for search/scraping
_session_valid = False
_session_lock = asyncio.Lock()


class SearchRequest(BaseModel):
    keywords: str
    location: str
    max_pages: int = 2
    gemini_key: Optional[str] = None

class ApplyRequest(BaseModel):
    job_id: str
    gemini_key: Optional[str] = None

class AnswerRequest(BaseModel):
    answer: str


# ─── Shared Browser Session ───

async def get_shared_session(log_fn=None):
    """Get or create a shared persistent browser context for search operations."""
    global _shared_pw, _shared_context, _shared_page, _session_valid
    
    async def log(msg, level="info"):
        if log_fn:
            await log_fn(msg, level)
        logger.info(msg)
    
    async with _session_lock:
        if _shared_context and _session_valid:
            return _shared_context, _shared_page
        
        # Close old session
        if _shared_context:
            try: await _shared_context.close()
            except Exception: pass
            _shared_context = None
            _shared_page = None
        if _shared_pw:
            try: await _shared_pw.stop()
            except Exception: pass
            _shared_pw = None
        
        await log("Opening shared LinkedIn browser session...")
        
        _shared_pw = await async_playwright().start()
        
        try:
            _shared_context = await _shared_pw.chromium.launch_persistent_context(
                user_data_dir=str(SOURCE_PROFILE_DIR),
                headless=True,
                viewport={"width": 1280, "height": 720},
                locale="en-US",
                args=["--disable-blink-features=AutomationControlled", "--no-sandbox"],
            )
            await log("✓ Browser session opened!", "success")
        except Exception as e:
            await log(f"Failed to open browser: {e}", "error")
            raise
        
        _shared_page = _shared_context.pages[0] if _shared_context.pages else await _shared_context.new_page()
        
        # Validate session
        await log("Validating LinkedIn session...")
        try:
            response = await _shared_page.goto("https://www.linkedin.com/feed/", wait_until="domcontentloaded", timeout=20000)
            status = response.status if response else 0
            await asyncio.sleep(2.0)
        except Exception as e:
            if "ERR_TOO_MANY_REDIRECTS" in str(e):
                await log("Rate limited. Waiting 15s and retrying...", "warning")
                await asyncio.sleep(15)
                try:
                    response = await _shared_page.goto("https://www.linkedin.com/feed/", wait_until="domcontentloaded", timeout=20000)
                    status = response.status if response else 0
                    await asyncio.sleep(2.0)
                except Exception:
                    await log("Still rate limited. Session may need re-login.", "error")
                    raise
            else:
                await log(f"Navigation error: {e}", "error")
                raise
        
        url = _shared_page.url
        if status == 429:
            await log("LinkedIn 429. Waiting 15s...", "warning")
            await asyncio.sleep(15)
            try:
                response = await _shared_page.goto("https://www.linkedin.com/feed/", wait_until="domcontentloaded", timeout=20000)
                url = _shared_page.url
            except Exception: pass
        
        if any(x in url for x in ["/login", "/authwall", "/checkpoint"]):
            is_login = await _shared_page.evaluate(
                "() => !!document.querySelector('#session_key, .login__form, form[action*=\"login\"]')"
            )
            if is_login:
                _session_valid = False
                raise RuntimeError("LinkedIn session expired. Run: linkedin-mcp-server --login")
        
        if status == 999:
            _session_valid = False
            raise RuntimeError("LinkedIn anti-bot detection. Need fresh session.")
        
        _session_valid = True
        await log("LinkedIn session active!", "success")
        return _shared_context, _shared_page


async def close_shared_session():
    """Close the shared browser session."""
    global _shared_pw, _shared_context, _shared_page, _session_valid
    async with _session_lock:
        _session_valid = False
        if _shared_context:
            try: await _shared_context.close()
            except Exception: pass
            _shared_context = None
            _shared_page = None
        if _shared_pw:
            try: await _shared_pw.stop()
            except Exception: pass
            _shared_pw = None


# ─── WebSocket & Broadcasting ───

@app.websocket("/ws")
async def websocket_endpoint(websocket: WebSocket):
    await websocket.accept()
    active_connections.append(websocket)
    logger.info("WebSocket connected.")
    try:
        await websocket.send_json({"type": "status", "status": apply_status})
        if active_jobs:
            await websocket.send_json({"type": "search_results", "jobs": active_jobs})
            for job in active_jobs:
                if job.get("analysis"):
                    await websocket.send_json({"type": "score_result", "job_id": job["job_id"], "analysis": job["analysis"]})
        while True:
            await websocket.receive_text()
    except WebSocketDisconnect:
        if websocket in active_connections:
            active_connections.remove(websocket)

async def broadcast_log(message: str, level: str = "info"):
    logger.info(f"[{level}] {message}")
    dead = []
    for conn in active_connections:
        try:
            await conn.send_json({"type": "log", "message": message, "level": level})
        except Exception:
            dead.append(conn)
    for d in dead:
        if d in active_connections: active_connections.remove(d)

async def broadcast_status(status: str):
    global apply_status
    apply_status = status
    dead = []
    for conn in active_connections:
        try:
            await conn.send_json({"type": "status", "status": status})
        except Exception:
            dead.append(conn)
    for d in dead:
        if d in active_connections: active_connections.remove(d)

async def broadcast_json(data: dict):
    dead = []
    for conn in active_connections:
        try:
            await conn.send_json(data)
        except Exception:
            dead.append(conn)
    for d in dead:
        if d in active_connections: active_connections.remove(d)


# ─── API Endpoints ───

@app.get("/")
async def get_index():
    index_path = TEMPLATES_DIR / "index.html"
    if index_path.exists():
        return HTMLResponse(content=index_path.read_text(encoding="utf-8"))
    return HTMLResponse("index.html not found.", status_code=404)

@app.get("/api/resume")
async def get_resume():
    if RESUME_TXT_PATH.exists():
        return {"text": RESUME_TXT_PATH.read_text(encoding="utf-8")}
    return {"text": "No resume text found."}

@app.post("/api/resume")
async def update_resume(data: Dict[str, str]):
    RESUME_TXT_PATH.write_text(data.get("text", ""), encoding="utf-8")
    return {"status": "success"}

@app.post("/api/search")
async def search_jobs(request: SearchRequest):
    if apply_status in ["applying", "paused_for_question", "paused_for_review"]:
        raise HTTPException(400, "Apply task in progress. Cancel it first.")
    if apply_status == "searching":
        raise HTTPException(400, "Search already in progress.")
    asyncio.create_task(run_search_background(request))
    return {"status": "searching"}

@app.post("/api/apply")
async def apply_job(request: ApplyRequest):
    global current_apply_task
    if apply_status == "searching":
        raise HTTPException(400, "Search in progress. Wait for it to finish.")
    if apply_status in ["applying", "paused_for_question", "paused_for_review"]:
        raise HTTPException(400, "Apply task in progress.")
    question_event.clear()
    current_apply_task = asyncio.create_task(run_apply_background(request))
    return {"status": "applying"}

@app.post("/api/submit-answer")
async def submit_answer(request: AnswerRequest):
    global user_answer
    user_answer = request.answer
    question_event.set()
    return {"status": "accepted"}

@app.post("/api/confirm-submit")
async def confirm_submit():
    global user_answer
    user_answer = "__SUBMIT__"
    question_event.set()
    return {"status": "submitted"}

@app.post("/api/cancel-apply")
async def cancel_apply():
    global user_answer, current_apply_task
    user_answer = "__CANCEL__"
    question_event.set()
    if current_apply_task:
        current_apply_task.cancel()
    await broadcast_status("idle")
    await broadcast_log("Cancelled.", "error")
    return {"status": "cancelled"}

@app.on_event("shutdown")
async def shutdown():
    await close_shared_session()


# ─── Search ───

async def run_search_background(request: SearchRequest):
    global active_jobs
    await broadcast_status("searching")
    await broadcast_log(f"Searching '{request.keywords}' in '{request.location}'...", "info")
    
    try:
        context, page = await get_shared_session(broadcast_log)
        
        extractor = LinkedInExtractor(page)
        
        search_result = await extractor.search_jobs(
            keywords=request.keywords,
            location=request.location,
            max_pages=request.max_pages,
            easy_apply=True
        )
        
        job_ids = search_result.get("job_ids", [])
        await broadcast_log(f"Found {len(job_ids)} Easy Apply jobs.", "success")
        
        active_jobs = []
        await broadcast_json({"type": "search_results", "jobs": []})
        
        resume_text = RESUME_TXT_PATH.read_text(encoding="utf-8") if RESUME_TXT_PATH.exists() else ""
        
        for job_id in job_ids:
            try:
                await broadcast_log(f"Fetching Job {job_id}...", "info")
                detail = await extractor.scrape_job(job_id)
                posting = detail.get("sections", {}).get("job_posting", "")
                
                lines = [l.strip() for l in posting.split("\n") if l.strip()]
                title = lines[0] if lines else f"Job {job_id}"
                company = lines[1] if len(lines) > 1 else "Unknown"
                location = lines[2] if len(lines) > 2 else request.location
                desc = "\n".join(lines[3:]) if len(lines) > 3 else posting
                
                record = {
                    "job_id": job_id, "title": title, "company": company,
                    "location": location, "description": desc[:2000], "analysis": None
                }
                active_jobs.append(record)
                await broadcast_json({"type": "search_results", "jobs": active_jobs})
                
                # Score inline (synchronously) to avoid race conditions
                await score_single_job(record, resume_text, request.gemini_key)
                
                await asyncio.sleep(0.5)
            except Exception as e:
                await broadcast_log(f"Error on {job_id}: {e}", "warning")
        
        await broadcast_log(f"Done. {len(active_jobs)} jobs loaded.", "success")
        
    except Exception as e:
        await broadcast_log(f"Search failed: {e}", "error")
    finally:
        await broadcast_status("idle")


# ─── Scoring ───

async def score_single_job(job: dict, resume_text: str, api_key: str | None):
    job_id = job["job_id"]
    desc = job["description"]
    
    if not api_key:
        score = 50
        matched, gaps = [], []
        keywords_map = {
            "java": "Java", "spring": "Spring Boot", "kafka": "Kafka",
            "postgresql": "PostgreSQL", "angular": "Angular", "aws": "AWS",
            "gcp": "GCP", "azure": "Azure", "docker": "Docker", "kubernetes": "Kubernetes",
            "react": "React", "node": "Node.js", "python": "Python",
            "microservices": "Microservices", "rest": "REST API", "sql": "SQL",
            "mongodb": "MongoDB", "redis": "Redis", "jenkins": "Jenkins",
            "ci/cd": "CI/CD", "git": "Git", "typescript": "TypeScript",
        }
        dl, rl = desc.lower(), resume_text.lower()
        for k, d in keywords_map.items():
            if k in dl:
                if k in rl: matched.append(d); score += 5
                else: gaps.append(d); score -= 3
        score = max(10, min(100, score))
        analysis = {
            "score": score,
            "rationale": "Keyword heuristic. Add Gemini API key for AI analysis.",
            "matched_skills": matched, "missing_skills": gaps,
            "outreach_note": f"Hi, I'm excited about {job['title']}. I have experience with {', '.join(matched[:3]) if matched else 'backend development'} and built DLQ Revive, an open-source Kafka recovery platform."
        }
    else:
        try:
            from google import genai
            from google.genai import types
            client = genai.Client(api_key=api_key)
            response = client.models.generate_content(
                model='gemini-2.5-flash',
                contents=f"Evaluate job match.\nResume:\n{resume_text}\n\nJob:\n{desc}\n\nOutput JSON: score(0-100), rationale, matched_skills[], missing_skills[], outreach_note",
                config=types.GenerateContentConfig(
                    response_mime_type="application/json",
                    response_schema=types.Schema(
                        type=types.Type.OBJECT,
                        properties={
                            "score": types.Schema(type=types.Type.INTEGER),
                            "rationale": types.Schema(type=types.Type.STRING),
                            "matched_skills": types.Schema(type=types.Type.ARRAY, items=types.Schema(type=types.Type.STRING)),
                            "missing_skills": types.Schema(type=types.Type.ARRAY, items=types.Schema(type=types.Type.STRING)),
                            "outreach_note": types.Schema(type=types.Type.STRING),
                        }, required=["score","rationale","matched_skills","missing_skills"]
                    )
                )
            )
            analysis = json.loads(response.text)
        except Exception as e:
            analysis = {"score": 0, "rationale": f"AI error: {e}", "matched_skills": [], "missing_skills": [], "outreach_note": ""}
    
    job["analysis"] = analysis
    await broadcast_json({"type": "score_result", "job_id": job_id, "analysis": analysis})


# ─── Screening Questions ───

async def get_ai_suggestion(question, field_type, options, resume_text, api_key):
    try:
        from google import genai
        client = genai.Client(api_key=api_key)
        resp = client.models.generate_content(
            model='gemini-2.5-flash',
            contents=f'Answer this screening question for Mohammed Saifulhuq.\nResume: {resume_text[:2000]}\nQuestion: "{question}"\nType: {field_type}\nOptions: {", ".join(options) if options else "N/A"}\nOutput ONLY the answer. Mohammed is in Chennai, fully authorized to work in India, no visa needed. Experience: ~1-2 years.'
        )
        return resp.text.strip()
    except Exception:
        return ""

async def solve_screening_question(field, resume_text, gemini_key):
    global user_answer, question_event
    suggested = ""
    if gemini_key:
        opts = [r["text"] for r in field.get("radios", [])]
        suggested = await get_ai_suggestion(field["labelText"], field["type"], opts, resume_text, gemini_key)
        await broadcast_log(f"AI suggested: '{suggested}'", "system")
    
    await broadcast_json({"type": "question", "question": {
        "id": field["index"], "text": field["labelText"],
        "type": field["type"], "options": field.get("radios", []),
        "suggested": suggested
    }})
    await broadcast_status("paused_for_question")
    question_event.clear()
    await question_event.wait()
    return user_answer


# ─── Easy Apply ───

async def run_apply_background(request: ApplyRequest):
    global user_answer, question_event
    await broadcast_status("applying")
    await broadcast_log(f"Starting Easy Apply for Job {request.job_id}...", "info")
    
    resume_text = RESUME_TXT_PATH.read_text(encoding="utf-8") if RESUME_TXT_PATH.exists() else ""
    
    # Close shared headless session — Apply needs a VISIBLE browser
    await close_shared_session()
    
    pw = None
    context = None
    
    try:
        pw = await async_playwright().start()
        
        await broadcast_log("Launching visible browser...", "info")
        context = await pw.chromium.launch_persistent_context(
            user_data_dir=str(SOURCE_PROFILE_DIR),
            headless=False,
            slow_mo=500,
            viewport={"width": 1280, "height": 720},
            locale="en-US",
            args=["--disable-blink-features=AutomationControlled", "--no-sandbox"],
        )
        await broadcast_log("✓ Browser opened!", "success")
        
        page = context.pages[0] if context.pages else await context.new_page()
        
        # Validate session
        await broadcast_log("Validating LinkedIn session...", "info")
        try:
            response = await page.goto("https://www.linkedin.com/feed/", wait_until="domcontentloaded", timeout=20000)
            await asyncio.sleep(2.0)
        except Exception as e:
            await broadcast_log(f"Session validation failed: {e}", "error")
            await broadcast_status("error")
            return
        
        if any(x in page.url for x in ["/login", "/authwall"]):
            await broadcast_log("Session expired. Run: linkedin-mcp-server --login", "error")
            await broadcast_status("error")
            return
        
        await broadcast_log("Session active!", "success")
        
        # Navigate to job
        job_url = f"https://www.linkedin.com/jobs/view/{request.job_id}/"
        await broadcast_log(f"Opening: {job_url}", "info")
        
        try:
            await page.goto(job_url, wait_until="domcontentloaded", timeout=20000)
        except Exception as e:
            if "ERR_TOO_MANY_REDIRECTS" in str(e):
                await broadcast_log("Rate limited. Retrying in 10s...", "warning")
                await asyncio.sleep(10)
                try:
                    await page.goto(job_url, wait_until="domcontentloaded", timeout=25000)
                except Exception as e2:
                    await broadcast_log(f"Navigation failed: {e2}", "error")
                    await broadcast_status("error")
                    return
            else:
                await broadcast_log(f"Navigation failed: {e}", "error")
                await broadcast_status("error")
                return
        
        await asyncio.sleep(3.0)
        
        if any(x in page.url for x in ["/login", "/authwall"]):
            await broadcast_log("Redirected to login.", "error")
            await broadcast_status("error")
            return
        
        # Detect apply button type
        await broadcast_log("Looking for Easy Apply button...", "info")
        
        apply_info = await page.evaluate("""() => {
            const allLinks = Array.from(document.querySelectorAll('a'));
            const easyApplyLink = allLinks.find(a => 
                a.href && a.href.includes('openSDUIApplyFlow=true')
            );
            if (easyApplyLink) {
                return { type: 'easy_apply_link', text: easyApplyLink.innerText.trim(), href: easyApplyLink.href };
            }
            const allButtons = Array.from(document.querySelectorAll('button'));
            const easyApplyBtn = allButtons.find(b => 
                b.innerText.toLowerCase().includes('easy apply') && b.offsetParent !== null
            );
            if (easyApplyBtn) {
                return { type: 'easy_apply_button', text: easyApplyBtn.innerText.trim() };
            }
            const applyLink = allLinks.find(a => {
                const text = a.innerText?.trim().toLowerCase() || '';
                return text === 'apply' && a.offsetParent !== null;
            });
            if (applyLink) {
                return { type: 'external_apply', text: applyLink.innerText.trim(), href: applyLink.href };
            }
            return null;
        }""")
        
        if not apply_info:
            await broadcast_log("No apply button found. You may have already applied.", "error")
            await broadcast_status("idle")
            return
        
        if apply_info["type"] in ("external_apply",):
            await broadcast_log(f"External apply job (not Easy Apply). URL: {apply_info.get('href', 'N/A')}", "warning")
            await broadcast_status("idle")
            return
        
        # Click Easy Apply
        if apply_info["type"] == "easy_apply_link":
            await broadcast_log("Found Easy Apply link! Clicking...", "success")
            easy_link = page.locator("a[href*='openSDUIApplyFlow=true']")
            await easy_link.first.click()
        else:
            await broadcast_log("Found Easy Apply button! Clicking...", "success")
            easy_btn = page.locator("button:has-text('Easy Apply')")
            await easy_btn.first.click()
        
        await asyncio.sleep(3.0)
        
        # Wait for modal/dialog
        modal = page.locator("[role='dialog'], .jobs-easy-apply-modal, .artdeco-modal")
        try:
            await modal.wait_for(state="visible", timeout=10000)
            await broadcast_log("Easy Apply form opened!", "success")
        except Exception:
            if "apply" in page.url:
                await broadcast_log("Apply form opened in new view.", "success")
                modal = page.locator("main, [role='main'], form")
            else:
                await broadcast_log("Apply form didn't appear. Check browser.", "error")
                await broadcast_status("idle")
                return
        
        # Form automation loop
        for step in range(1, 16):
            if await modal.count() == 0:
                await broadcast_log("Form closed.", "info")
                break
            
            submit_btn = page.get_by_role("button", name="Submit application", exact=False)
            review_btn = page.get_by_role("button", name="Review", exact=False)
            next_btn = page.get_by_role("button", name="Next", exact=False)
            
            fields = await page.evaluate("""() => {
                const fields = [];
                document.querySelectorAll('.fb-dash-form-element, fieldset, .jobs-easy-apply-form-section').forEach((el, i) => {
                    const lbl = el.querySelector('label, legend, .fb-dash-form-element__label, .fb-form-element-label');
                    if (!lbl) return;
                    const t = el.querySelector('input[type="text"], input[type="tel"], input[type="email"], input[type="number"]');
                    const ta = el.querySelector('textarea');
                    const sel = el.querySelector('select');
                    const radios = Array.from(el.querySelectorAll('input[type="radio"]')).map(r => ({
                        value: r.value, text: (el.querySelector(`label[for="${r.id}"]`) || r.parentElement)?.innerText?.trim() || r.value, id: r.id
                    }));
                    const cb = el.querySelector('input[type="checkbox"]');
                    fields.push({
                        index: i, labelText: lbl.innerText.trim(),
                        type: t?'text':(ta?'textarea':(sel?'select':(radios.length?'radio':(cb?'checkbox':'unknown')))),
                        elementId: t?.id||ta?.id||sel?.id||cb?.id||null,
                        radios, currentValue: t?.value||ta?.value||sel?.value||(cb?cb.checked:null)
                    });
                });
                return fields;
            }""")
            
            # Resume upload
            file_inputs = page.locator("input[type='file']")
            if await file_inputs.count() > 0 and RESUME_PDF_PATH.exists():
                for fi in range(await file_inputs.count()):
                    try:
                        inp = file_inputs.nth(fi)
                        if await inp.is_visible():
                            await broadcast_log("Uploading resume...", "info")
                            await inp.set_input_files(str(RESUME_PDF_PATH))
                            await asyncio.sleep(2)
                    except Exception: pass
            
            if fields:
                await broadcast_log(f"Step {step}: {len(fields)} fields", "info")
                for f in fields:
                    lbl = f["labelText"].lower()
                    if f["currentValue"] and f["type"] in ["text","textarea"]: continue
                    
                    val = None
                    if "first name" in lbl: val = "Mohammed"
                    elif "last name" in lbl: val = "Saifulhuq"
                    elif "email" in lbl: val = "mohammed.saifulhuq@gmail.com"
                    elif "phone" in lbl or "mobile" in lbl: val = "9025281608"
                    elif "city" in lbl or "location" in lbl: val = "Chennai"
                    
                    if val:
                        if f["elementId"]:
                            try: await page.locator(f"#{f['elementId']}").fill(val)
                            except Exception: pass
                        await broadcast_log(f"✓ {f['labelText']} → {val}", "success")
                        continue
                    
                    ans = await solve_screening_question(f, resume_text, request.gemini_key)
                    if ans == "__CANCEL__": raise asyncio.CancelledError()
                    if ans == "__SKIP__": continue
                    
                    try:
                        if f["type"] in ["text","textarea"] and f["elementId"]:
                            await page.locator(f"#{f['elementId']}").fill(ans)
                        elif f["type"] == "select" and f["elementId"]:
                            await page.locator(f"#{f['elementId']}").select_option(label=ans)
                        elif f["type"] == "radio":
                            rid = None
                            for r in f["radios"]:
                                if r["text"].lower() == ans.lower() or ans.lower() in r["text"].lower():
                                    rid = r["id"]; break
                            if not rid and f["radios"]: rid = f["radios"][0]["id"]
                            if rid: await page.locator(f"label[for='{rid}']").click()
                        elif f["type"] == "checkbox" and f["elementId"]:
                            if any(k in ans.lower() for k in ["yes","true"]):
                                await page.locator(f"#{f['elementId']}").check()
                    except Exception as fe:
                        await broadcast_log(f"Fill error: {fe}", "warning")
                    
                    await broadcast_log(f"✓ Filled: {ans}", "success")
                    await asyncio.sleep(random.uniform(0.5, 1.2))
            
            await broadcast_status("applying")
            await asyncio.sleep(1.5)
            
            try:
                if await submit_btn.count() > 0 and await submit_btn.first.is_visible():
                    await broadcast_log("Ready to submit! Confirm in dashboard.", "warning")
                    await broadcast_status("paused_for_review")
                    question_event.clear()
                    await question_event.wait()
                    if user_answer == "__SUBMIT__":
                        await submit_btn.first.click()
                        await asyncio.sleep(4)
                        await broadcast_log("✅ Application submitted!", "success")
                        await broadcast_status("completed")
                    else:
                        await broadcast_log("Cancelled.", "error")
                        await broadcast_status("idle")
                    break
                elif await review_btn.count() > 0 and await review_btn.first.is_visible():
                    await review_btn.first.click()
                elif await next_btn.count() > 0 and await next_btn.first.is_visible():
                    await next_btn.first.click()
                else:
                    await broadcast_log("Check browser for warnings/errors.", "warning")
                    await broadcast_status("paused_for_review")
                    question_event.clear()
                    await question_event.wait()
                    if user_answer == "__SUBMIT__":
                        try:
                            await submit_btn.first.click()
                            await asyncio.sleep(4)
                            await broadcast_log("✅ Submitted!", "success")
                            await broadcast_status("completed")
                        except Exception: pass
                    break
            except Exception as ne:
                await broadcast_log(f"Nav error: {ne}", "warning")
            
            await asyncio.sleep(random.uniform(1.5, 3.0))
    
    except asyncio.CancelledError:
        await broadcast_log("Cancelled.", "error")
    except Exception as e:
        await broadcast_log(f"Error: {e}", "error")
        await broadcast_status("error")
    finally:
        if context:
            try: await context.close()
            except Exception: pass
        if pw:
            try: await pw.stop()
            except Exception: pass
        if apply_status not in ["completed","error"]:
            await broadcast_status("idle")


if __name__ == "__main__":
    uvicorn.run("job_applier_dashboard:app", host="127.0.0.1", port=8000, reload=False)
