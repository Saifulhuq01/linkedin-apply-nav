"""
Apply-Nav — LinkedIn Job Application Dashboard
Central FastAPI server with all HTTP + WebSocket endpoints.
"""

import sys
import os
import json
import logging
import asyncio
import random
import shutil
import time
from datetime import datetime
from pathlib import Path
from typing import List, Dict, Any, Optional

from fastapi import FastAPI, WebSocket, WebSocketDisconnect, HTTPException, UploadFile, File, Query
from fastapi.responses import HTMLResponse, FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel
import uvicorn
import yaml

# ─── Local Modules ───
import db
from resume_manager import (
    ResumeManager,
    save_uploaded_resume,
    get_resume_text,
    get_resume_pdf_path,
    has_resume,
    update_resume_text,
    migrate_legacy_resume,
)
from llm_adapter import LLMAdapter
from answer_cache import AnswerCache
from ats_router import detect_ats_type, get_handler_for_ats, clean_apply_url, ApplyResult as LegacyApplyResult, ATSRouter

# Add the linkedin-mcp-server directory to path if present
_MCP_SERVER_DIR = Path(__file__).parent / "linkedin-mcp-server"
if _MCP_SERVER_DIR.exists():
    sys.path.insert(0, str(_MCP_SERVER_DIR))

# CRITICAL: Set PLAYWRIGHT_BROWSERS_PATH before importing patchright
LINKEDIN_MCP_DIR = Path.home() / ".linkedin-mcp"
_PATCHRIGHT_BROWSERS = LINKEDIN_MCP_DIR / "patchright-browsers"
if _PATCHRIGHT_BROWSERS.exists():
    os.environ["PLAYWRIGHT_BROWSERS_PATH"] = str(_PATCHRIGHT_BROWSERS)

from patchright.async_api import async_playwright

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s"
)
logger = logging.getLogger("apply_nav")

app = FastAPI(title="Apply-Nav — LinkedIn Job Application Dashboard")

WORKSPACE_DIR = Path(__file__).parent
TEMPLATES_DIR = WORKSPACE_DIR / "templates"
SOURCE_PROFILE_DIR = LINKEDIN_MCP_DIR / "profile"

# Static file serving
app.mount("/templates", StaticFiles(directory=str(TEMPLATES_DIR)), name="templates")


# ─── Configuration ────────────────────────────────────────────

def load_config() -> Dict[str, Any]:
    """Load config.yaml then overlay config.local.yaml."""
    base_cfg: Dict = {}
    for fname in ["config.yaml", "config.local.yaml"]:
        path = WORKSPACE_DIR / fname
        if path.exists():
            try:
                with open(path, "r", encoding="utf-8") as f:
                    data = yaml.safe_load(f) or {}
                # Deep merge
                for key, val in data.items():
                    if isinstance(val, dict) and isinstance(base_cfg.get(key), dict):
                        base_cfg[key] = {**base_cfg[key], **val}
                    else:
                        base_cfg[key] = val
                logger.info("Loaded config from %s", fname)
            except Exception as e:
                logger.warning("Failed to load %s: %s", fname, e)

    if not base_cfg:
        base_cfg = _default_config()
    return base_cfg


def _default_config() -> Dict[str, Any]:
    return {
        "app": {"host": "127.0.0.1", "port": 8000, "log_level": "info"},
        "browser": {"profile_dir": "~/.linkedin-mcp/profile", "headless_search": True, "headless_apply": False},
        "llm": {"provider": "gemini", "gemini_api_key": "", "ollama_model": "llama3", "ollama_url": "http://localhost:11434"},
        "rate_limits": {"max_per_hour": 5, "max_per_day": 25, "circuit_breaker_failures": 3, "circuit_breaker_cooldown_minutes": 5},
        "search": {"default_location": "India", "easy_apply_only": True, "max_jobs_per_search": 50, "profiles": []},
        "scoring": {"min_score_to_queue": 60},
        "candidate": {},
        "user": {},
    }


def save_config(config: Dict[str, Any]) -> None:
    local_path = WORKSPACE_DIR / "config.local.yaml"
    with open(local_path, "w", encoding="utf-8") as f:
        yaml.dump(config, f, default_flow_style=False, allow_unicode=True)
    logger.info("Configuration saved to config.local.yaml")


# ─── Global Singletons ────────────────────────────────────────

CONFIG = load_config()
ANSWER_CACHE = AnswerCache()
LLM = LLMAdapter(CONFIG, answer_cache=ANSWER_CACHE)
RESUME_MANAGER = ResumeManager()
ATS_ROUTER = ATSRouter()

active_connections: List[WebSocket] = []
current_apply_task: Optional[asyncio.Task] = None

apply_status = "idle"
active_jobs: List[dict] = []

# HITL shared events
question_event = asyncio.Event()
submit_event = asyncio.Event()
user_answer = ""

# Shared browser session
_shared_pw = None
_shared_context = None
_shared_page = None
_session_valid = False
_session_lock = asyncio.Lock()

# Circuit breaker (in-memory, also backed by DB)
_circuit_open = False
_circuit_fail_count = 0
_circuit_last_failure = 0.0
_CIRCUIT_MAX_FAILURES = 3
_CIRCUIT_RESET_SECONDS = 300


# ─── Pydantic Models ──────────────────────────────────────────

class SearchRequest(BaseModel):
    keywords: str
    location: str
    max_pages: int = 2
    easy_apply_only: bool = True
    date_posted: str = ""
    experience_level: str = ""
    gemini_key: Optional[str] = None
    easy_apply: Optional[bool] = None  # legacy alias

class ApplyRequest(BaseModel):
    job_id: str
    gemini_key: Optional[str] = None

class ApplyExternalRequest(BaseModel):
    job_id: str
    apply_url: str
    gemini_key: Optional[str] = None

class AnswerRequest(BaseModel):
    answer: str
    question_hash: Optional[str] = None

class SkipJobRequest(BaseModel):
    job_id: str

class SubmitAnswerRequest(BaseModel):
    question_hash: str
    answer: str

class ConfigUpdateRequest(BaseModel):
    user: Optional[Dict[str, Any]] = None
    candidate: Optional[Dict[str, Any]] = None
    search: Optional[Dict[str, Any]] = None
    llm: Optional[Dict[str, Any]] = None
    app: Optional[Dict[str, Any]] = None

class SearchProfile(BaseModel):
    name: str
    keywords: str
    location: str
    max_pages: int = 2
    easy_apply: bool = True

class ResumeActivateRequest(BaseModel):
    filename: str


# ─── Connection Manager ───────────────────────────────────────

class ConnectionManager:
    def __init__(self):
        self._connections: List[WebSocket] = []

    async def connect(self, websocket: WebSocket):
        await websocket.accept()
        self._connections.append(websocket)

    def disconnect(self, websocket: WebSocket):
        if websocket in self._connections:
            self._connections.remove(websocket)

    async def broadcast(self, event_type: str, data: dict):
        msg = json.dumps({
            "event": event_type,
            "data": data,
            "timestamp": datetime.utcnow().isoformat(),
        })
        dead = []
        for ws in self._connections:
            try:
                await ws.send_text(msg)
            except Exception:
                dead.append(ws)
        for d in dead:
            self.disconnect(d)


manager = ConnectionManager()


# ─── WebSocket Endpoint ───────────────────────────────────────

@app.websocket("/ws")
async def websocket_endpoint(websocket: WebSocket):
    await manager.connect(websocket)
    # Also track in global list for legacy broadcast functions
    active_connections.append(websocket)
    logger.info("WebSocket client connected.")
    try:
        # Send current state
        await websocket.send_text(json.dumps({
            "event": "status",
            "data": {"status": apply_status},
            "timestamp": datetime.utcnow().isoformat(),
        }))
        # Also send as legacy format
        await websocket.send_json({"type": "status", "status": apply_status})
        if active_jobs:
            await websocket.send_json({"type": "search_results", "jobs": active_jobs})
        while True:
            await websocket.receive_text()
    except WebSocketDisconnect:
        manager.disconnect(websocket)
        if websocket in active_connections:
            active_connections.remove(websocket)


# ─── Broadcast Helpers ────────────────────────────────────────

async def broadcast_event(event_type: str, data: dict):
    """Broadcast a structured WebSocket event."""
    await manager.broadcast(event_type, data)


async def broadcast_log(message: str, level: str = "info"):
    """Broadcast a log message (both new and legacy format)."""
    logger.info("[%s] %s", level, message)
    dead = []
    for conn in active_connections:
        try:
            await conn.send_json({"type": "log", "message": message, "level": level})
        except Exception:
            dead.append(conn)
    for d in dead:
        if d in active_connections:
            active_connections.remove(d)
    # Also broadcast as structured event
    await manager.broadcast("log", {"message": message, "level": level})


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
        if d in active_connections:
            active_connections.remove(d)
    await manager.broadcast("status", {"status": status})


async def broadcast_json(data: dict):
    """Legacy broadcast for arbitrary JSON."""
    dead = []
    for conn in active_connections:
        try:
            await conn.send_json(data)
        except Exception:
            dead.append(conn)
    for d in dead:
        if d in active_connections:
            active_connections.remove(d)


# ─── Shared Browser Session ───────────────────────────────────

async def get_shared_session(log_fn=None):
    """Get or create a shared persistent browser context."""
    global _shared_pw, _shared_context, _shared_page, _session_valid
    global _circuit_open, _circuit_fail_count, _circuit_last_failure

    async def log(msg, level="info"):
        if log_fn:
            await log_fn(msg, level)
        logger.info(msg)

    # Circuit breaker check
    is_open, open_until = db.get_circuit_state()
    if is_open:
        remaining = int((open_until - datetime.utcnow()).total_seconds()) if open_until else 300
        await log(f"Circuit breaker OPEN — retry in {remaining}s.", "error")
        raise RuntimeError(f"Circuit breaker open. Retry in {remaining}s.")

    async with _session_lock:
        if _shared_context and _session_valid:
            return _shared_context, _shared_page

        # Close stale session
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

        try:
            _shared_pw = await async_playwright().start()

            profile_dir = str(SOURCE_PROFILE_DIR)
            headless = CONFIG.get("browser", {}).get("headless_search", True)

            _shared_context = await _shared_pw.chromium.launch_persistent_context(
                user_data_dir=profile_dir,
                headless=headless,
                no_viewport=False,
                viewport={"width": 1280, "height": 720},
                locale="en-US",
                args=["--disable-blink-features=AutomationControlled", "--no-sandbox"],
            )

            _shared_page = _shared_context.pages[0] if _shared_context.pages else await _shared_context.new_page()
            await log("✓ Browser opened!")

            # Validate session
            try:
                resp = await _shared_page.goto("https://www.linkedin.com/feed/", wait_until="domcontentloaded", timeout=20000)
                await asyncio.sleep(2.0)
                url = _shared_page.url
            except Exception as e:
                await log(f"Navigation error: {e}", "error")
                raise

            if any(x in url for x in ["/login", "/authwall", "/checkpoint"]):
                _session_valid = False
                raise RuntimeError("LinkedIn session expired. Please login via linkedin-mcp-server --login")

            _session_valid = True
            db.record_success()
            _circuit_fail_count = 0
            await log("✓ LinkedIn session active!", "success")
            return _shared_context, _shared_page

        except Exception as e:
            db.record_failure()
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
            raise


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


# ─── Core HTTP Endpoints ──────────────────────────────────────

@app.get("/")
async def get_index():
    index_path = TEMPLATES_DIR / "index.html"
    if index_path.exists():
        return HTMLResponse(content=index_path.read_text(encoding="utf-8"))
    return HTMLResponse("<h1>index.html not found in templates/</h1>", status_code=404)


@app.get("/api/health")
async def get_health():
    is_open, open_until = db.get_circuit_state()
    is_blocked, rate_reason = db.check_rate_limit()
    return {
        "status": "ok",
        "circuit_state": {"open": is_open, "open_until": open_until.isoformat() if open_until else None},
        "session_ok": _session_valid,
        "active_resume": RESUME_MANAGER.get_active_resume(),
        "rate_limit_status": {"blocked": is_blocked, "reason": rate_reason},
    }


# ─── Search Endpoints ─────────────────────────────────────────

@app.post("/api/search")
async def search_jobs(request: SearchRequest):
    if apply_status in ["applying", "paused_for_question", "paused_for_review"]:
        raise HTTPException(400, "Apply task in progress. Cancel it first.")
    if apply_status == "searching":
        raise HTTPException(400, "Search already in progress.")

    is_open, _ = db.get_circuit_state()
    if is_open:
        raise HTTPException(503, "Circuit breaker is open. Session has failed repeatedly.")

    if request.gemini_key:
        LLM.override_api_key(request.gemini_key)

    task_id = f"search_{int(time.time())}"
    asyncio.create_task(run_search_background(request))
    return {"task_id": task_id, "status": "started"}


# ─── Apply Endpoints ──────────────────────────────────────────

@app.post("/api/apply")
async def apply_job(request: ApplyRequest):
    global current_apply_task

    if apply_status in ["applying", "paused_for_question", "paused_for_review"]:
        raise HTTPException(400, "Apply task already in progress.")

    is_open, _ = db.get_circuit_state()
    if is_open:
        raise HTTPException(503, "Circuit breaker is open.")

    if db.check_duplicate(request.job_id):
        raise HTTPException(409, "Already applied to this job.")

    is_blocked, reason = db.check_rate_limit()
    if is_blocked:
        raise HTTPException(429, reason)

    if request.gemini_key:
        LLM.override_api_key(request.gemini_key)

    question_event.clear()
    submit_event.clear()
    current_apply_task = asyncio.create_task(run_apply_background(request))
    return {"status": "started"}


@app.post("/api/submit-answer")
async def submit_answer(request: SubmitAnswerRequest):
    """Store answer in cache, resume HITL handler."""
    global user_answer
    ANSWER_CACHE.set(request.question_hash or "unknown", request.answer)
    user_answer = request.answer
    question_event.set()
    return {"status": "accepted"}


@app.post("/api/confirm-submit")
async def confirm_submit(body: dict = None):
    """User confirmed final submission."""
    global user_answer
    user_answer = "__SUBMIT__"
    submit_event.set()
    question_event.set()
    return {"status": "confirmed"}


@app.post("/api/skip-job")
async def skip_job(request: SkipJobRequest):
    db.update_job_status(request.job_id, "skipped")
    return {"status": "skipped"}


@app.post("/api/cancel-apply")
async def cancel_apply():
    global user_answer, current_apply_task
    user_answer = "__CANCEL__"
    question_event.set()
    submit_event.set()
    if current_apply_task:
        current_apply_task.cancel()
    await broadcast_status("idle")
    return {"status": "cancelled"}


# ─── Resume Endpoints ─────────────────────────────────────────

@app.post("/api/resume/upload")
async def upload_resume(file: UploadFile = File(...)):
    if not file.filename.lower().endswith(".pdf"):
        raise HTTPException(400, "Only PDF files are supported.")

    contents = await file.read()
    if len(contents) > 10 * 1024 * 1024:
        raise HTTPException(400, "File too large. Maximum 10MB.")

    record = RESUME_MANAGER.save_resume(contents, file.filename)
    return {"status": "success", "resume": record}


@app.post("/api/resume/activate")
async def activate_resume(request: ResumeActivateRequest):
    try:
        RESUME_MANAGER.set_active_resume(request.filename)
        record = RESUME_MANAGER.get_active_resume()
        return {"status": "success", "resume": record}
    except FileNotFoundError as e:
        raise HTTPException(404, str(e))


@app.get("/api/resume/list")
async def list_resumes():
    return RESUME_MANAGER.list_resumes()


@app.get("/api/resume")
async def get_resume():
    text = RESUME_MANAGER.get_plain_text()
    return {"text": text or "No resume uploaded yet.", "has_resume": bool(text)}


@app.post("/api/resume")
async def update_resume_endpoint(data: dict):
    update_resume_text(data.get("text", ""))
    return {"status": "success"}


@app.post("/api/resume/upload-legacy")
async def upload_resume_legacy(file: UploadFile = File(...)):
    """Legacy endpoint — same as /api/resume/upload."""
    return await upload_resume(file)


@app.get("/api/resume/download")
async def download_resume():
    pdf = get_resume_pdf_path()
    if pdf and pdf.exists():
        return FileResponse(str(pdf), media_type="application/pdf", filename="resume.pdf")
    raise HTTPException(404, "No resume uploaded.")


# ─── History & Statistics Endpoints ──────────────────────────

@app.get("/api/history")
async def get_history(
    limit: int = Query(100, ge=1, le=500),
    status: Optional[str] = None,
    date_from: Optional[str] = None,
    ats_type: Optional[str] = None,
):
    records = db.get_application_history(limit, 0, status, ats_type)
    return {"applications": records, "total": len(records)}


@app.get("/api/statistics")
async def get_statistics():
    return db.get_statistics()


@app.get("/api/stats")
async def get_stats_legacy():
    return db.get_statistics()


# ─── Config Endpoints ─────────────────────────────────────────

@app.get("/api/config")
async def get_config():
    cfg = load_config()
    # Mask API keys
    llm_cfg = cfg.get("llm", {})
    key = llm_cfg.get("gemini_api_key", "") or llm_cfg.get("gemini", {}).get("api_key", "")
    masked_key = f"...{key[-4:]}" if len(key) > 4 else ("set" if key else "")
    return {
        "app": cfg.get("app", {}),
        "browser": cfg.get("browser", {}),
        "llm": {"provider": llm_cfg.get("provider", "gemini"), "key_set": bool(key), "key_masked": masked_key},
        "rate_limits": cfg.get("rate_limits", {}),
        "search": cfg.get("search", {}),
        "scoring": cfg.get("scoring", {}),
        "candidate": cfg.get("candidate", cfg.get("user", {})),
        "has_resume": has_resume(),
        "has_gemini_key": bool(key or os.environ.get("GEMINI_API_KEY", "")),
        # Legacy
        "user": cfg.get("user", cfg.get("candidate", {})),
        "safety": cfg.get("safety", {"max_applies_per_hour": 5, "max_applies_per_day": 25}),
    }


@app.post("/api/config")
async def update_config(request: ConfigUpdateRequest):
    global CONFIG, LLM
    cfg = load_config()

    if request.user:
        cfg["user"] = {**cfg.get("user", {}), **request.user}
    if request.candidate:
        cfg["candidate"] = {**cfg.get("candidate", {}), **request.candidate}
        cfg["user"] = cfg["candidate"]  # keep in sync
    if request.search:
        cfg["search"] = {**cfg.get("search", {}), **request.search}
    if request.llm:
        cfg["llm"] = {**cfg.get("llm", {}), **request.llm}
    if request.app:
        cfg["app"] = {**cfg.get("app", {}), **request.app}

    save_config(cfg)
    CONFIG = cfg
    LLM = LLMAdapter(CONFIG, answer_cache=ANSWER_CACHE)
    return {"status": "success"}


# ─── Search Profiles ──────────────────────────────────────────

@app.get("/api/search-profiles")
async def get_search_profiles():
    cfg = load_config()
    return cfg.get("search_profiles", cfg.get("search", {}).get("profiles", []))


@app.post("/api/search-profiles")
async def save_search_profile(profile: SearchProfile):
    cfg = load_config()
    profiles = cfg.get("search_profiles", [])
    found = False
    for i, p in enumerate(profiles):
        if p.get("name") == profile.name:
            profiles[i] = profile.model_dump()
            found = True
            break
    if not found:
        profiles.append(profile.model_dump())
    cfg["search_profiles"] = profiles
    save_config(cfg)
    global CONFIG
    CONFIG = cfg
    return {"status": "success"}


@app.delete("/api/search-profiles/{name}")
async def delete_search_profile(name: str):
    cfg = load_config()
    profiles = [p for p in cfg.get("search_profiles", []) if p.get("name") != name]
    cfg["search_profiles"] = profiles
    save_config(cfg)
    global CONFIG
    CONFIG = cfg
    return {"status": "success"}


@app.post("/api/search-profiles/{name}/run")
async def run_search_profile(name: str, gemini_key: Optional[str] = None):
    global CONFIG, LLM
    if apply_status in ["applying", "paused_for_question", "paused_for_review"]:
        raise HTTPException(400, "Apply task in progress.")
    if apply_status == "searching":
        raise HTTPException(400, "Search already in progress.")

    cfg = load_config()
    profiles = cfg.get("search_profiles", [])
    profile = next((p for p in profiles if p.get("name") == name), None)
    if not profile:
        raise HTTPException(404, "Search profile not found")

    if gemini_key:
        LLM.override_api_key(gemini_key)

    req = SearchRequest(
        keywords=profile.get("keywords", ""),
        location=profile.get("location", ""),
        max_pages=profile.get("max_pages", 2),
        easy_apply=profile.get("easy_apply", True),
        gemini_key=gemini_key,
    )
    asyncio.create_task(run_search_background(req))
    return {"status": "searching"}


# ─── Session Endpoints ────────────────────────────────────────

@app.get("/api/session/health")
async def get_session_health():
    is_open, open_until = db.get_circuit_state()
    remaining = 0
    if is_open and open_until:
        remaining = max(0, int((open_until - datetime.utcnow()).total_seconds()))
    return {
        "session_valid": _session_valid,
        "circuit_open": is_open,
        "remaining_cooldown_seconds": remaining,
    }


@app.post("/api/session/verify")
async def verify_session():
    global _session_valid
    try:
        await close_shared_session()
        await get_shared_session(broadcast_log)
        return {"status": "success", "session_valid": _session_valid}
    except Exception as e:
        raise HTTPException(500, f"Session validation failed: {e}")


# ─── Cache Endpoints ──────────────────────────────────────────

@app.get("/api/cache/stats")
async def get_cache_stats():
    return {"stats": ANSWER_CACHE.stats(), "entries": ANSWER_CACHE.entries()}


@app.delete("/api/cache")
async def clear_cache():
    ANSWER_CACHE.clear()
    return {"status": "success"}


# ─── External Apply Endpoints ─────────────────────────────────

@app.post("/api/apply-external")
async def apply_external(request: ApplyExternalRequest):
    global current_apply_task

    if apply_status in ["applying", "paused_for_question", "paused_for_review"]:
        raise HTTPException(400, "Apply task already in progress.")

    if db.check_duplicate(request.job_id):
        raise HTTPException(409, "Already applied to this job.")

    is_blocked, reason = db.check_rate_limit()
    if is_blocked:
        raise HTTPException(429, reason)

    if request.gemini_key:
        LLM.override_api_key(request.gemini_key)

    question_event.clear()
    submit_event.clear()
    current_apply_task = asyncio.create_task(run_external_apply_background(request))
    return {"status": "started"}


# ─── HITL Question Helper ─────────────────────────────────────

async def solve_screening_question(field: dict, suggested: str = "") -> str:
    """Broadcast question to UI, wait for user answer."""
    global user_answer
    await broadcast_json({
        "type": "question",
        "question": {
            "id": field.get("index", 0),
            "text": field.get("labelText", ""),
            "type": field.get("type", "text"),
            "options": field.get("radios", []),
            "suggested": suggested,
        }
    })
    await broadcast_event("paused_for_question", {
        "question": field.get("labelText", ""),
        "suggested": suggested,
        "options": [r.get("text", "") for r in field.get("radios", [])],
    })
    await broadcast_status("paused_for_question")
    question_event.clear()
    await question_event.wait()
    return user_answer


# ─── Search Background Task ───────────────────────────────────

async def run_search_background(request: SearchRequest):
    global active_jobs
    await broadcast_status("searching")
    await broadcast_log(f"Searching '{request.keywords}' in '{request.location}'...", "info")

    # Normalize easy_apply flag
    easy_apply = request.easy_apply_only if request.easy_apply is None else request.easy_apply

    try:
        context, page = await get_shared_session(broadcast_log)

        # Build LinkedIn search URL
        from urllib.parse import quote_plus
        kw = quote_plus(request.keywords)
        loc = quote_plus(request.location)
        url = f"https://www.linkedin.com/jobs/search/?keywords={kw}&location={loc}"
        if easy_apply:
            url += "&f_AL=true"  # Easy Apply filter

        await broadcast_log(f"Navigating to LinkedIn Jobs...", "info")
        await page.goto(url, wait_until="domcontentloaded", timeout=30000)
        await asyncio.sleep(3.0)

        # Extract job IDs from first N pages
        job_ids = []
        max_pages = min(request.max_pages, 5)

        for pg_num in range(max_pages):
            # Extract job IDs from current page
            ids = await page.evaluate("""() => {
                const els = document.querySelectorAll('[data-job-id]');
                return Array.from(els).map(e => e.getAttribute('data-job-id')).filter(Boolean);
            }""")
            job_ids.extend([jid for jid in ids if jid not in job_ids])
            await broadcast_log(f"Page {pg_num + 1}: found {len(ids)} job IDs (total: {len(job_ids)})", "info")

            if pg_num < max_pages - 1:
                # Scroll to bottom and wait for next page
                await page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
                await asyncio.sleep(2.0)

                # Try to click next page
                next_btn = page.locator("button[aria-label='Next']")
                if await next_btn.count() > 0:
                    await next_btn.first.click()
                    await asyncio.sleep(3.0)
                else:
                    break

        await broadcast_log(f"Found {len(job_ids)} job IDs total.", "success")
        db.log_search(request.keywords, request.location, "", len(job_ids), 0)

        active_jobs = []
        await broadcast_json({"type": "search_results", "jobs": []})

        resume_text = RESUME_MANAGER.get_plain_text()
        max_jobs = min(len(job_ids), CONFIG.get("search", {}).get("max_jobs_per_search", 50))
        scored = 0

        for job_id in job_ids[:max_jobs]:
            try:
                # Skip already applied
                existing = db.get_job(job_id)
                if existing and existing["status"] in ("applied", "skipped", "applying"):
                    continue

                # Navigate to job page
                job_url = f"https://www.linkedin.com/jobs/view/{job_id}/"
                await page.goto(job_url, wait_until="domcontentloaded", timeout=20000)
                await asyncio.sleep(random.uniform(2.0, 4.0))

                # Extract job data
                job_data = await _extract_job_from_page(page, job_id, request.location)

                # Detect apply type
                apply_info = await _detect_apply_type(page)
                ats_type = "easy_apply"
                apply_url = None
                if apply_info["type"] == "external_apply":
                    raw_url = apply_info.get("href", "")
                    apply_url = clean_apply_url(raw_url)
                    ats_type = detect_ats_type(apply_url)
                elif apply_info["type"] == "unknown":
                    ats_type = "unknown"

                job_data["ats_type"] = ats_type
                job_data["apply_url"] = apply_url
                job_data["already_applied"] = bool(existing and existing["status"] in ("applied", "review"))

                # Upsert to DB
                db.upsert_job({
                    "job_id": job_id,
                    "title": job_data["title"],
                    "company": job_data["company"],
                    "location": job_data["location"],
                    "apply_url": apply_url or "",
                    "ats_type": ats_type,
                    "status": "discovered",
                    "raw_jd": job_data["description"],
                })

                active_jobs.append(job_data)
                await broadcast_json({"type": "search_results", "jobs": active_jobs})

                # Score vs resume
                await _score_and_broadcast(job_data, resume_text, job_id)
                scored += 1

            except Exception as e:
                await broadcast_log(f"Error on job {job_id}: {e}", "warning")
                logger.exception("Search job error: %s", job_id)

        db.log_search(request.keywords, request.location, "", len(job_ids), scored)
        await broadcast_log(f"✓ Search complete. {len(active_jobs)} jobs loaded, {scored} scored.", "success")

    except Exception as e:
        await broadcast_log(f"Search failed: {e}", "error")
        logger.exception("Search task error")
    finally:
        await broadcast_status("idle")


async def _extract_job_from_page(page, job_id: str, fallback_location: str) -> dict:
    """Extract job details from the LinkedIn job view page."""
    try:
        title = await page.locator(".job-details-jobs-unified-top-card__job-title, h1.t-24").first.inner_text()
    except Exception:
        title = f"Job {job_id}"

    try:
        company = await page.locator(".job-details-jobs-unified-top-card__company-name").first.inner_text()
    except Exception:
        company = "Unknown Company"

    try:
        location_el = page.locator(".job-details-jobs-unified-top-card__bullet, .job-details-jobs-unified-top-card__primary-description")
        location = await location_el.first.inner_text() if await location_el.count() > 0 else fallback_location
    except Exception:
        location = fallback_location

    try:
        desc = await page.locator(".jobs-description__content, .jobs-description-content").first.inner_text()
    except Exception:
        desc = ""

    return {
        "job_id": job_id,
        "title": title.strip(),
        "company": company.strip(),
        "location": location.strip(),
        "description": desc.strip()[:2000],
        "analysis": None,
    }


async def _detect_apply_type(page) -> dict:
    """Detect whether job has Easy Apply or external apply."""
    return await page.evaluate("""() => {
        const allLinks = Array.from(document.querySelectorAll('a'));
        const easyApplyLink = allLinks.find(a => a.href && a.href.includes('openSDUIApplyFlow=true'));
        if (easyApplyLink) return { type: 'easy_apply_link', href: easyApplyLink.href };
        const allButtons = Array.from(document.querySelectorAll('button'));
        const easyApplyBtn = allButtons.find(b => b.innerText.toLowerCase().includes('easy apply') && b.offsetParent !== null);
        if (easyApplyBtn) return { type: 'easy_apply', href: null };
        const applyLink = allLinks.find(a => {
            const text = a.innerText?.trim().toLowerCase() || '';
            return text === 'apply' && a.offsetParent !== null;
        });
        if (applyLink) return { type: 'external_apply', href: applyLink.href };
        return { type: 'unknown', href: null };
    }""")


async def _score_and_broadcast(job_data: dict, resume_text: str, job_id: str):
    """Score job vs resume and broadcast the result."""
    try:
        result = await LLM.score_job(resume_text, job_data.get("description", ""))
        job_data["analysis"] = result
        db.update_job_status(
            job_id, "scored",
            score=result.get("score", 0),
            matched_skills=result.get("matched_skills", []),
            skill_gaps=result.get("skill_gaps", []),
            outreach_note=result.get("outreach_note", ""),
        )
        await broadcast_json({"type": "score_result", "job_id": job_id, "analysis": result})
        await broadcast_event("job_scored", {"job_id": job_id, **job_data, "analysis": result})
    except Exception as e:
        await broadcast_log(f"Score error for {job_id}: {e}", "warning")


# ─── Apply Background Task ────────────────────────────────────

async def run_apply_background(request: ApplyRequest):
    """Run the LinkedIn Easy Apply flow."""
    global user_answer

    await broadcast_status("applying")
    await broadcast_event("apply_started", {"job_id": request.job_id})
    await broadcast_log(f"Starting apply for job {request.job_id}...", "info")

    user_data = {**CONFIG.get("user", {}), **CONFIG.get("candidate", {})}
    resume_pdf = get_resume_pdf_path()

    if not resume_pdf:
        await broadcast_log("No resume uploaded. Upload a PDF first.", "error")
        await broadcast_status("idle")
        return

    if not user_data.get("first_name") and not user_data.get("name"):
        await broadcast_log("User profile not configured. Fill in Settings first.", "error")
        await broadcast_status("idle")
        return

    # Upsert as "applying"
    job_info = next((j for j in active_jobs if j["job_id"] == request.job_id), {})
    db.upsert_job({
        "job_id": request.job_id,
        "title": job_info.get("title", ""),
        "company": job_info.get("company", ""),
        "location": job_info.get("location", ""),
        "status": "applying",
        "score": job_info.get("analysis", {}).get("score", 0) if job_info.get("analysis") else 0,
    })
    db.update_job_status(request.job_id, "applying")

    await close_shared_session()

    pw = None
    context = None
    handler = None

    try:
        pw = await async_playwright().start()
        headless_apply = CONFIG.get("browser", {}).get("headless_apply", False)

        await broadcast_log("Launching visible browser...", "info")
        context = await pw.chromium.launch_persistent_context(
            user_data_dir=str(SOURCE_PROFILE_DIR),
            headless=headless_apply,
            no_viewport=False,
            viewport={"width": 1280, "height": 720},
            locale="en-US",
            args=["--disable-blink-features=AutomationControlled", "--no-sandbox"],
        )
        page = context.pages[0] if context.pages else await context.new_page()
        await broadcast_log("✓ Browser opened!", "success")

        # Validate session
        await page.goto("https://www.linkedin.com/feed/", wait_until="domcontentloaded", timeout=20000)
        await asyncio.sleep(2.0)
        if any(x in page.url for x in ["/login", "/authwall"]):
            await broadcast_log("LinkedIn session expired. Re-login required.", "error")
            db.update_job_status(request.job_id, "failed")
            await broadcast_status("idle")
            return

        # Navigate to job
        job_url = f"https://www.linkedin.com/jobs/view/{request.job_id}/"
        await broadcast_log(f"Navigating to job page...", "info")
        await page.goto(job_url, wait_until="domcontentloaded", timeout=25000)
        await asyncio.sleep(3.0)

        if any(x in page.url for x in ["/login", "/authwall"]):
            await broadcast_log("Redirected to login page.", "error")
            db.update_job_status(request.job_id, "failed")
            return

        # Detect apply type
        apply_info = await _detect_apply_type(page)

        if not apply_info or apply_info["type"] == "unknown":
            await broadcast_log("No apply button found.", "warning")
            db.update_job_status(request.job_id, "skipped")
            await broadcast_status("idle")
            return

        if apply_info["type"] == "external_apply":
            # Route to external ATS handler
            raw_url = apply_info.get("href", "")
            ext_url = clean_apply_url(raw_url)
            ats_type = detect_ats_type(ext_url)
            await broadcast_log(f"External apply: {ats_type.upper()} — {ext_url[:80]}", "info")

            await page.goto(ext_url, wait_until="domcontentloaded", timeout=30000)
            await asyncio.sleep(3)

            handler_cls = get_handler_for_ats(ats_type)
            handler = handler_cls(
                ws_broadcaster=broadcast_log,
                llm_adapter=LLM,
                answer_cache=ANSWER_CACHE,
                resume_manager=RESUME_MANAGER,
            )
            jinfo = {**job_info, "user_data": user_data, "apply_url": ext_url}
            result = await handler.fill_form(page, jinfo)
        else:
            # Easy Apply
            from ats_handlers.easy_apply import EasyApplyHandler
            handler = EasyApplyHandler(
                ws_broadcaster=broadcast_log,
                llm_adapter=LLM,
                answer_cache=ANSWER_CACHE,
                resume_manager=RESUME_MANAGER,
            )
            handler._question_event = question_event
            handler._submit_event = submit_event

            jinfo = {**job_info, "user_data": user_data}
            result = await handler.fill_form(page, jinfo)

        # Handle result
        from ats_handlers.base import ApplyStatus
        if result.status == ApplyStatus.PENDING_REVIEW:
            db.update_job_status(request.job_id, "review")
            await broadcast_status("paused_for_review")
            await broadcast_event("paused_for_review", {"job_id": request.job_id, "message": result.message})
            await broadcast_log("✋ Application ready for review. Confirm in dashboard.", "warning")

            # Wait for confirm-submit or cancel
            submit_event.clear()
            await submit_event.wait()

            if user_answer == "__SUBMIT__":
                # Click the submit button
                try:
                    submit_btn = page.get_by_role("button", name="Submit application", exact=False)
                    if await submit_btn.count() > 0:
                        await page.mouse.move(random.randint(100, 500), random.randint(100, 400))
                        await asyncio.sleep(random.uniform(1.5, 3.0))
                        await submit_btn.first.click()
                        await asyncio.sleep(4.0)
                        await broadcast_log("✅ Application submitted successfully!", "success")
                    else:
                        await broadcast_log("Submit button not found — please submit manually.", "warning")
                except Exception as se:
                    await broadcast_log(f"Submit click failed: {se}. Please submit manually.", "warning")

                db.update_job_status(request.job_id, "applied", applied_at=datetime.utcnow().isoformat())
                db.record_success()
                await broadcast_event("apply_complete", {"job_id": request.job_id, "status": "applied"})
                await broadcast_status("idle")
            else:
                await broadcast_log("Application cancelled by user.", "info")
                db.update_job_status(request.job_id, "skipped")
                await broadcast_status("idle")

        elif result.status == ApplyStatus.SUCCESS:
            db.update_job_status(request.job_id, "applied", applied_at=datetime.utcnow().isoformat())
            db.record_success()
            await broadcast_event("apply_complete", {"job_id": request.job_id, "status": "applied"})
            await broadcast_status("idle")

        elif result.status == ApplyStatus.HITL_REQUIRED:
            db.update_job_status(request.job_id, "review")
            await broadcast_event("hitl_active", {
                "job_id": request.job_id,
                "message": result.message,
                "fields_filled": result.fields_filled,
                "fields_total": result.fields_total,
            })
            await broadcast_status("paused_for_review")

        else:  # FAILED
            db.update_job_status(request.job_id, "failed")
            db.record_failure()
            await broadcast_event("apply_failed", {"job_id": request.job_id, "message": result.message})
            await broadcast_status("idle")

    except asyncio.CancelledError:
        await broadcast_log("Apply task cancelled.", "warning")
        db.update_job_status(request.job_id, "skipped")
    except Exception as e:
        await broadcast_log(f"Apply error: {e}", "error")
        logger.exception("Apply task error")
        db.update_job_status(request.job_id, "failed")
        db.record_failure()
        await broadcast_event("apply_failed", {"job_id": request.job_id, "message": str(e)})
    finally:
        if handler and context:
            try:
                p = context.pages[0] if context.pages else None
                if p:
                    await handler.cleanup(p)
            except Exception:
                pass
        if context:
            try: await context.close()
            except Exception: pass
        if pw:
            try: await pw.stop()
            except Exception: pass
        if apply_status not in ["idle"]:
            await broadcast_status("idle")


# ─── External Apply Background Task ──────────────────────────

async def run_external_apply_background(request: ApplyExternalRequest):
    """External ATS application flow."""
    global user_answer

    await broadcast_status("applying")
    await broadcast_log(f"Starting external apply for job {request.job_id}...", "info")

    user_data = {**CONFIG.get("user", {}), **CONFIG.get("candidate", {})}
    cleaned_url = clean_apply_url(request.apply_url)
    ats_type = detect_ats_type(cleaned_url)

    job_info = next((j for j in active_jobs if j["job_id"] == request.job_id), {})
    db.upsert_job({
        "job_id": request.job_id,
        "title": job_info.get("title", ""),
        "company": job_info.get("company", ""),
        "location": job_info.get("location", ""),
        "apply_url": cleaned_url,
        "ats_type": ats_type,
        "status": "applying",
    })

    await close_shared_session()
    pw = None
    context = None

    try:
        pw = await async_playwright().start()
        context = await pw.chromium.launch_persistent_context(
            user_data_dir=str(SOURCE_PROFILE_DIR),
            headless=False,
            viewport={"width": 1280, "height": 720},
            locale="en-US",
            args=["--disable-blink-features=AutomationControlled", "--no-sandbox"],
        )
        page = context.pages[0] if context.pages else await context.new_page()
        await page.goto(cleaned_url, wait_until="domcontentloaded", timeout=30000)
        await asyncio.sleep(3)

        handler_cls = get_handler_for_ats(ats_type)
        handler = handler_cls(
            ws_broadcaster=broadcast_log,
            llm_adapter=LLM,
            answer_cache=ANSWER_CACHE,
            resume_manager=RESUME_MANAGER,
        )
        jinfo = {**job_info, "user_data": user_data, "apply_url": cleaned_url}
        result = await handler.fill_form(page, jinfo)

        from ats_handlers.base import ApplyStatus
        if result.status in (ApplyStatus.PENDING_REVIEW, ApplyStatus.HITL_REQUIRED):
            db.update_job_status(request.job_id, "review")
            await broadcast_event("paused_for_review", {"job_id": request.job_id})
            await broadcast_status("paused_for_review")
            submit_event.clear()
            await submit_event.wait()

            if user_answer == "__SUBMIT__":
                db.update_job_status(request.job_id, "applied", applied_at=datetime.utcnow().isoformat())
                db.record_success()
                await broadcast_event("apply_complete", {"job_id": request.job_id})
                await broadcast_log("✅ Manual submission confirmed.", "success")
            else:
                db.update_job_status(request.job_id, "skipped")
        elif result.status == ApplyStatus.SUCCESS:
            db.update_job_status(request.job_id, "applied", applied_at=datetime.utcnow().isoformat())
            db.record_success()
        else:
            db.update_job_status(request.job_id, "failed")
            db.record_failure()

    except asyncio.CancelledError:
        db.update_job_status(request.job_id, "skipped")
    except Exception as e:
        await broadcast_log(f"External apply error: {e}", "error")
        db.update_job_status(request.job_id, "failed")
        db.record_failure()
    finally:
        if context:
            try: await context.close()
            except Exception: pass
        if pw:
            try: await pw.stop()
            except Exception: pass
        await broadcast_status("idle")


# ─── Legacy scoring helper ────────────────────────────────────

async def score_single_job(job: dict, resume_text: str):
    """Score a single job (legacy function used by search loop)."""
    job_id = job["job_id"]
    await _score_and_broadcast(job, resume_text, job_id)


# ─── Startup / Shutdown ───────────────────────────────────────

@app.on_event("startup")
async def startup():
    db.init_db()
    logger.info("Database initialized.")

    if not has_resume():
        if migrate_legacy_resume(WORKSPACE_DIR):
            logger.info("Legacy resume migrated.")

    asyncio.create_task(_background_session_validate())


@app.on_event("shutdown")
async def shutdown():
    await close_shared_session()


async def _background_session_validate():
    """Validate session in background at startup."""
    global _session_valid
    try:
        if not SOURCE_PROFILE_DIR.exists():
            logger.info("LinkedIn profile directory not found. Session inactive.")
            _session_valid = False
            return
        logger.info("Validating LinkedIn session...")
        await get_shared_session()
        logger.info("Session validation complete.")
    except Exception as e:
        logger.info("Session validation failed (non-critical): %s", e)


# ─── Entry Point ──────────────────────────────────────────────

if __name__ == "__main__":
    host = CONFIG.get("app", {}).get("host", "127.0.0.1")
    port = CONFIG.get("app", {}).get("port", 8000)
    log_level = CONFIG.get("app", {}).get("log_level", "info")
    uvicorn.run("job_applier_dashboard:app", host=host, port=port, reload=False, log_level=log_level)
