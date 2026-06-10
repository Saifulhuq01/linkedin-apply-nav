"""
Apply-Nav — LinkedIn Job Application Automation Dashboard (v2)

Refactored backend integrating:
- config.yaml/config.local.yaml configuration (no hardcoded PII)
- SQLite state persistence (application history, dedup, rate limits)
- Resume manager (PDF upload + text extraction)
- LLM adapter (Gemini / Ollama / heuristic fallback)
- ATS router (Easy Apply / Workday / Greenhouse / HITL fallback)
- Error recovery with browser cleanup
"""

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

from fastapi import FastAPI, WebSocket, WebSocketDisconnect, HTTPException, UploadFile, File
from fastapi.responses import HTMLResponse, FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel
import uvicorn
import yaml

# ─── Local Modules ───
import db
from resume_manager import (
    save_uploaded_resume,
    get_resume_text,
    get_resume_pdf_path,
    has_resume,
    update_resume_text,
    migrate_legacy_resume,
)
from llm_adapter import LLMAdapter
from ats_router import detect_ats_type, get_handler_for_ats, ApplyResult

# Add the mcp server directory to path
sys.path.append(str(Path(__file__).parent / "linkedin-mcp-server"))
from linkedin_mcp_server.scraping.extractor import LinkedInExtractor

# CRITICAL: Set PLAYWRIGHT_BROWSERS_PATH before importing patchright
LINKEDIN_MCP_DIR = Path.home() / ".linkedin-mcp"
os.environ["PLAYWRIGHT_BROWSERS_PATH"] = str(LINKEDIN_MCP_DIR / "patchright-browsers")

from patchright.async_api import async_playwright


logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")
logger = logging.getLogger("apply_nav")

app = FastAPI(title="Apply-Nav — LinkedIn Job Application Dashboard")

WORKSPACE_DIR = Path(__file__).parent
TEMPLATES_DIR = WORKSPACE_DIR / "templates"
SOURCE_PROFILE_DIR = LINKEDIN_MCP_DIR / "profile"

# Static file serving
app.mount("/templates", StaticFiles(directory=str(TEMPLATES_DIR)), name="templates")


# ─── Configuration ───

def load_config() -> Dict[str, Any]:
    """Load configuration from config.local.yaml, falling back to config.yaml."""
    local_config = WORKSPACE_DIR / "config.local.yaml"
    default_config = WORKSPACE_DIR / "config.yaml"

    config_path = local_config if local_config.exists() else default_config

    try:
        with open(config_path, "r", encoding="utf-8") as f:
            config = yaml.safe_load(f) or {}
        logger.info("Configuration loaded from %s", config_path.name)
        return config
    except Exception as e:
        logger.warning("Failed to load config from %s: %s — using defaults", config_path, e)
        return {
            "user": {},
            "llm": {"provider": "gemini", "gemini": {"api_key": "", "model": "gemini-2.5-flash"}},
            "search": {"default_keywords": "", "default_location": "", "max_pages": 2},
            "safety": {"max_applies_per_hour": 5, "max_applies_per_day": 25},
            "resume": {"pdf_path": "", "txt_path": ""},
        }


def save_config(config: Dict[str, Any]) -> None:
    """Save configuration to config.local.yaml."""
    local_config = WORKSPACE_DIR / "config.local.yaml"
    with open(local_config, "w", encoding="utf-8") as f:
        yaml.dump(config, f, default_flow_style=False, allow_unicode=True)
    logger.info("Configuration saved to %s", local_config.name)


# ─── Global State ───
CONFIG = load_config()
LLM = LLMAdapter(CONFIG)

active_connections: List[WebSocket] = []
current_apply_task: Optional[asyncio.Task] = None
question_event = asyncio.Event()
user_answer = ""
apply_status = "idle"
active_jobs = []

# Shared browser session — reused across search and apply
_shared_pw = None
_shared_context = None
_shared_page = None
_session_valid = False
_session_lock = asyncio.Lock()


# ─── Pydantic Models ───

class SearchRequest(BaseModel):
    keywords: str
    location: str
    max_pages: int = 2
    gemini_key: Optional[str] = None

class ApplyRequest(BaseModel):
    job_id: str
    gemini_key: Optional[str] = None

class ApplyExternalRequest(BaseModel):
    job_id: str
    apply_url: str
    gemini_key: Optional[str] = None

class AnswerRequest(BaseModel):
    answer: str

class ConfigUpdateRequest(BaseModel):
    user: Optional[Dict[str, str]] = None
    search: Optional[Dict[str, Any]] = None
    llm: Optional[Dict[str, Any]] = None


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


# ─── Config Endpoints ───

@app.get("/api/config")
async def get_config():
    """Return non-secret configuration."""
    cfg = load_config()
    # Strip API keys from response
    safe_cfg = {
        "user": cfg.get("user", {}),
        "search": cfg.get("search", {}),
        "safety": cfg.get("safety", {}),
        "llm": {
            "provider": cfg.get("llm", {}).get("provider", "gemini"),
        },
        "has_gemini_key": bool(cfg.get("llm", {}).get("gemini", {}).get("api_key", "") or os.environ.get("GEMINI_API_KEY", "")),
        "has_resume": has_resume(),
    }
    return safe_cfg

@app.post("/api/config")
async def update_config(request: ConfigUpdateRequest):
    """Update configuration."""
    global CONFIG, LLM
    cfg = load_config()

    if request.user:
        cfg["user"] = {**cfg.get("user", {}), **request.user}
    if request.search:
        cfg["search"] = {**cfg.get("search", {}), **request.search}
    if request.llm:
        # Only update non-key fields from this endpoint
        llm_cfg = cfg.get("llm", {})
        if "provider" in request.llm:
            llm_cfg["provider"] = request.llm["provider"]
        cfg["llm"] = llm_cfg

    save_config(cfg)
    CONFIG = cfg
    LLM = LLMAdapter(CONFIG)
    return {"status": "success"}


# ─── Resume Endpoints ───

@app.get("/api/resume")
async def get_resume():
    text = get_resume_text()
    if text:
        return {"text": text, "has_resume": True}
    return {"text": "No resume uploaded yet. Upload a PDF to get started.", "has_resume": False}

@app.post("/api/resume")
async def update_resume(data: Dict[str, str]):
    update_resume_text(data.get("text", ""))
    return {"status": "success"}

@app.post("/api/resume/upload")
async def upload_resume(file: UploadFile = File(...)):
    """Handle resume PDF upload."""
    if not file.filename.lower().endswith(".pdf"):
        raise HTTPException(400, "Only PDF files are supported.")

    contents = await file.read()
    if len(contents) > 10 * 1024 * 1024:  # 10MB limit
        raise HTTPException(400, "File too large. Maximum 10MB.")

    pdf_path, txt_path, extracted_text = save_uploaded_resume(contents, file.filename)

    # Update config with paths
    cfg = load_config()
    cfg["resume"] = {
        "pdf_path": str(pdf_path),
        "txt_path": str(txt_path),
    }
    save_config(cfg)

    return {
        "status": "success",
        "text": extracted_text,
        "chars_extracted": len(extracted_text),
        "filename": file.filename,
    }

@app.get("/api/resume/download")
async def download_resume():
    pdf_path = get_resume_pdf_path()
    if pdf_path and pdf_path.exists():
        return FileResponse(str(pdf_path), media_type="application/pdf", filename="resume.pdf")
    raise HTTPException(404, "No resume uploaded.")


# ─── History & Stats Endpoints ───

@app.get("/api/history")
async def get_history(limit: int = 50, offset: int = 0, status: Optional[str] = None, ats_type: Optional[str] = None):
    return {
        "applications": db.get_application_history(limit, offset, status, ats_type),
        "total": len(db.get_application_history(1000, 0)),  # Simple count
    }

@app.get("/api/stats")
async def get_stats():
    return db.get_stats()


# ─── Search & Apply Endpoints ───

@app.post("/api/search")
async def search_jobs(request: SearchRequest):
    if apply_status in ["applying", "paused_for_question", "paused_for_review"]:
        raise HTTPException(400, "Apply task in progress. Cancel it first.")
    if apply_status == "searching":
        raise HTTPException(400, "Search already in progress.")

    # Override LLM key if provided from frontend
    if request.gemini_key:
        LLM.override_api_key(request.gemini_key)

    asyncio.create_task(run_search_background(request))
    return {"status": "searching"}

@app.post("/api/apply")
async def apply_job(request: ApplyRequest):
    global current_apply_task
    if apply_status == "searching":
        raise HTTPException(400, "Search in progress. Wait for it to finish.")
    if apply_status in ["applying", "paused_for_question", "paused_for_review"]:
        raise HTTPException(400, "Apply task in progress.")

    # Check duplicate
    if db.is_already_applied(request.job_id):
        raise HTTPException(400, "Already applied to this job.")

    # Check rate limits
    safety = CONFIG.get("safety", {})
    rate_msg = db.check_rate_limit(
        safety.get("max_applies_per_hour", 5),
        safety.get("max_applies_per_day", 25),
    )
    if rate_msg:
        raise HTTPException(429, rate_msg)

    if request.gemini_key:
        LLM.override_api_key(request.gemini_key)

    question_event.clear()
    current_apply_task = asyncio.create_task(run_apply_background(request))
    return {"status": "applying"}

@app.post("/api/apply-external")
async def apply_external(request: ApplyExternalRequest):
    """Start an external ATS application (Workday, Greenhouse, etc.)."""
    global current_apply_task
    if apply_status in ["applying", "paused_for_question", "paused_for_review"]:
        raise HTTPException(400, "Apply task in progress.")

    if db.is_already_applied(request.job_id):
        raise HTTPException(400, "Already applied to this job.")

    safety = CONFIG.get("safety", {})
    rate_msg = db.check_rate_limit(
        safety.get("max_applies_per_hour", 5),
        safety.get("max_applies_per_day", 25),
    )
    if rate_msg:
        raise HTTPException(429, rate_msg)

    if request.gemini_key:
        LLM.override_api_key(request.gemini_key)

    question_event.clear()
    current_apply_task = asyncio.create_task(run_external_apply_background(request))
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


# ─── Search Background Task ───

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

        # Record search in DB
        db.record_search(request.keywords, request.location, len(job_ids))

        active_jobs = []
        await broadcast_json({"type": "search_results", "jobs": []})

        resume_text = get_resume_text()

        for job_id in job_ids:
            try:
                # Check if already applied
                existing_status = db.get_application_status(job_id)

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
                    "location": location, "description": desc[:2000], "analysis": None,
                    "already_applied": existing_status in ("applied", "pending"),
                    "ats_type": "easy_apply",
                }
                active_jobs.append(record)
                await broadcast_json({"type": "search_results", "jobs": active_jobs})

                # Score inline
                await score_single_job(record, resume_text)

                await asyncio.sleep(0.5)
            except Exception as e:
                await broadcast_log(f"Error on {job_id}: {e}", "warning")

        await broadcast_log(f"Done. {len(active_jobs)} jobs loaded.", "success")

    except Exception as e:
        await broadcast_log(f"Search failed: {e}", "error")
    finally:
        await broadcast_status("idle")


# ─── Scoring ───

async def score_single_job(job: dict, resume_text: str):
    """Score a job using the LLM adapter."""
    job_id = job["job_id"]
    desc = job["description"]

    try:
        analysis = await LLM.score_job(resume_text, desc)
    except Exception as e:
        analysis = {"score": 0, "rationale": f"Scoring error: {e}", "matched_skills": [], "missing_skills": [], "outreach_note": ""}

    job["analysis"] = analysis
    await broadcast_json({"type": "score_result", "job_id": job_id, "analysis": analysis})


# ─── HITL Screening Question Helper ───

async def solve_screening_question(field: dict, suggested: str = "") -> str:
    """Broadcast a screening question to the UI and wait for user answer."""
    global user_answer, question_event

    await broadcast_json({"type": "question", "question": {
        "id": field["index"], "text": field["labelText"],
        "type": field["type"], "options": field.get("radios", []),
        "suggested": suggested
    }})
    await broadcast_status("paused_for_question")
    question_event.clear()
    await question_event.wait()
    return user_answer


# ─── Easy Apply Background Task ───

async def run_apply_background(request: ApplyRequest):
    """Run the LinkedIn Easy Apply flow using the refactored handler."""
    global user_answer, question_event
    await broadcast_status("applying")
    await broadcast_log(f"Starting Easy Apply for Job {request.job_id}...", "info")

    user_data = CONFIG.get("user", {})
    resume_text = get_resume_text()
    resume_pdf = get_resume_pdf_path()

    if not resume_pdf:
        await broadcast_log("No resume uploaded. Upload a PDF first.", "error")
        await broadcast_status("error")
        return

    if not user_data.get("first_name"):
        await broadcast_log("User profile not configured. Fill in your details in Settings.", "error")
        await broadcast_status("error")
        return

    # Record pending application in DB
    job_info = next((j for j in active_jobs if j["job_id"] == request.job_id), {})
    db.record_application(
        request.job_id,
        title=job_info.get("title", ""),
        company=job_info.get("company", ""),
        location=job_info.get("location", ""),
        description=job_info.get("description", ""),
        ats_type="easy_apply",
        status="pending",
        score=job_info.get("analysis", {}).get("score"),
        matched_skills=job_info.get("analysis", {}).get("matched_skills"),
        missing_skills=job_info.get("analysis", {}).get("missing_skills"),
    )

    # Close shared headless session — Apply needs a VISIBLE browser
    await close_shared_session()

    pw = None
    context = None
    handler = None

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
            await page.goto("https://www.linkedin.com/feed/", wait_until="domcontentloaded", timeout=20000)
            await asyncio.sleep(2.0)
        except Exception as e:
            await broadcast_log(f"Session validation failed: {e}", "error")
            db.update_application_status(request.job_id, "failed", str(e))
            await broadcast_status("error")
            return

        if any(x in page.url for x in ["/login", "/authwall"]):
            await broadcast_log("Session expired. Run: linkedin-mcp-server --login", "error")
            db.update_application_status(request.job_id, "failed", "Session expired")
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
                    db.update_application_status(request.job_id, "failed", str(e2))
                    await broadcast_status("error")
                    return
            else:
                await broadcast_log(f"Navigation failed: {e}", "error")
                db.update_application_status(request.job_id, "failed", str(e))
                await broadcast_status("error")
                return

        await asyncio.sleep(3.0)

        if any(x in page.url for x in ["/login", "/authwall"]):
            await broadcast_log("Redirected to login.", "error")
            db.update_application_status(request.job_id, "failed", "Auth redirect")
            await broadcast_status("error")
            return

        # Detect apply type — check for external apply
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
            db.update_application_status(request.job_id, "skipped", "No apply button")
            await broadcast_status("idle")
            return

        # If external apply, detect ATS and route
        if apply_info["type"] == "external_apply":
            external_url = apply_info.get("href", "")
            ats_type = detect_ats_type(external_url)
            await broadcast_log(f"External application detected: {ats_type.upper()} — {external_url[:80]}", "info")

            # Navigate to external URL
            await page.goto(external_url, wait_until="domcontentloaded", timeout=30000)
            await asyncio.sleep(3)

            # Get appropriate handler
            handler_class = get_handler_for_ats(ats_type)
            handler = handler_class()

            result = await handler.fill_form(
                page=page,
                user_data=user_data,
                resume_pdf_path=resume_pdf,
                resume_text=resume_text,
                llm=LLM,
                broadcast=broadcast_log,
                question_callback=lambda f, s: solve_screening_question(f, s),
            )

            db.update_application_status(request.job_id, result.status, result.message)
            await broadcast_log(result.message, "success" if result.status == "applied" else "warning")
            await broadcast_status("completed" if result.status == "applied" else "idle")
            return

        # Easy Apply flow
        from ats_handlers.easy_apply import EasyApplyHandler
        handler = EasyApplyHandler()

        async def question_cb(field, suggested=""):
            return await solve_screening_question(field, suggested)

        result = await handler.fill_form(
            page=page,
            user_data=user_data,
            resume_pdf_path=resume_pdf,
            resume_text=resume_text,
            llm=LLM,
            broadcast=broadcast_log,
            question_callback=question_cb,
        )

        if result.status == "review":
            # Pause for final review — same HITL behavior as before
            await broadcast_status("paused_for_review")
            question_event.clear()
            await question_event.wait()

            if user_answer == "__SUBMIT__":
                submit_btn = page.get_by_role("button", name="Submit application", exact=False)
                try:
                    await submit_btn.first.click()
                    await asyncio.sleep(4)
                    await broadcast_log("✅ Application submitted!", "success")
                    db.update_application_status(request.job_id, "applied")
                    await broadcast_status("completed")
                except Exception as se:
                    await broadcast_log(f"Submit failed: {se}", "error")
                    db.update_application_status(request.job_id, "failed", str(se))
                    await broadcast_status("error")
            else:
                await broadcast_log("Cancelled.", "error")
                db.update_application_status(request.job_id, "cancelled")
                await broadcast_status("idle")
        elif result.status == "applied":
            db.update_application_status(request.job_id, "applied")
            await broadcast_status("completed")
        elif result.status == "cancelled":
            db.update_application_status(request.job_id, "cancelled")
            await broadcast_status("idle")
        else:
            db.update_application_status(request.job_id, result.status, result.message)
            await broadcast_status("idle")

    except asyncio.CancelledError:
        await broadcast_log("Cancelled.", "error")
        db.update_application_status(request.job_id, "cancelled")
    except Exception as e:
        await broadcast_log(f"Error: {e}", "error")
        db.update_application_status(request.job_id, "failed", str(e))
        await broadcast_status("error")
    finally:
        # Error recovery: cleanup handler state
        if handler and context:
            try:
                page = context.pages[0] if context.pages else None
                if page:
                    await handler.cleanup(page, broadcast_log)
            except Exception:
                pass
        if context:
            try: await context.close()
            except Exception: pass
        if pw:
            try: await pw.stop()
            except Exception: pass
        if apply_status not in ["completed", "error"]:
            await broadcast_status("idle")


# ─── External Apply Background Task ───

async def run_external_apply_background(request: ApplyExternalRequest):
    """Run an external ATS application flow."""
    global user_answer, question_event
    await broadcast_status("applying")

    user_data = CONFIG.get("user", {})
    resume_text = get_resume_text()
    resume_pdf = get_resume_pdf_path()

    ats_type = detect_ats_type(request.apply_url)
    await broadcast_log(f"External apply: {ats_type.upper()} — {request.apply_url[:80]}", "info")

    # Record in DB
    job_info = next((j for j in active_jobs if j["job_id"] == request.job_id), {})
    db.record_application(
        request.job_id,
        title=job_info.get("title", ""),
        company=job_info.get("company", ""),
        ats_type=ats_type,
        status="pending",
        apply_url=request.apply_url,
    )

    await close_shared_session()
    pw = None
    context = None
    handler = None

    try:
        pw = await async_playwright().start()
        context = await pw.chromium.launch_persistent_context(
            user_data_dir=str(SOURCE_PROFILE_DIR),
            headless=False,
            slow_mo=300,
            viewport={"width": 1280, "height": 720},
            locale="en-US",
            args=["--disable-blink-features=AutomationControlled", "--no-sandbox"],
        )

        page = context.pages[0] if context.pages else await context.new_page()
        await page.goto(request.apply_url, wait_until="domcontentloaded", timeout=30000)
        await asyncio.sleep(3)

        handler_class = get_handler_for_ats(ats_type)
        handler = handler_class()

        result = await handler.fill_form(
            page=page,
            user_data=user_data,
            resume_pdf_path=resume_pdf,
            resume_text=resume_text,
            llm=LLM,
            broadcast=broadcast_log,
        )

        db.update_application_status(request.job_id, result.status, result.message)
        await broadcast_log(result.message, "success" if result.status == "applied" else "warning")
        await broadcast_status("completed" if result.status == "applied" else "idle")

    except asyncio.CancelledError:
        db.update_application_status(request.job_id, "cancelled")
    except Exception as e:
        await broadcast_log(f"Error: {e}", "error")
        db.update_application_status(request.job_id, "failed", str(e))
        await broadcast_status("error")
    finally:
        if handler and context:
            try:
                page = context.pages[0] if context.pages else None
                if page:
                    await handler.cleanup(page, broadcast_log)
            except Exception:
                pass
        if context:
            try: await context.close()
            except Exception: pass
        if pw:
            try: await pw.stop()
            except Exception: pass
        if apply_status not in ["completed", "error"]:
            await broadcast_status("idle")


# ─── Startup ───

@app.on_event("startup")
async def startup():
    """Initialize database and migrate legacy files on startup."""
    db.init_db()
    logger.info("Database initialized.")

    # Migrate legacy resume files if present
    if not has_resume():
        if migrate_legacy_resume(WORKSPACE_DIR):
            logger.info("Legacy resume files migrated to data/resumes/")


if __name__ == "__main__":
    uvicorn.run("job_applier_dashboard:app", host="127.0.0.1", port=8000, reload=False)
