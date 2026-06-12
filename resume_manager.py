"""
resume_manager.py — PDF upload storage, structured extraction, version management.

Storage: data/resumes/
Index: data/resumes/index.json
"""

import json
import logging
import time
from pathlib import Path
from typing import Optional

logger = logging.getLogger("apply_nav.resume")

DATA_DIR = Path(__file__).parent / "data"
RESUMES_DIR = DATA_DIR / "resumes"
INDEX_FILE = RESUMES_DIR / "index.json"

# Legacy paths (for backward compat with existing uploads)
DEFAULT_PDF_PATH = RESUMES_DIR / "resume.pdf"
DEFAULT_TXT_PATH = RESUMES_DIR / "resume.txt"


def _ensure_dirs() -> None:
    RESUMES_DIR.mkdir(parents=True, exist_ok=True)


def _load_index() -> list:
    _ensure_dirs()
    if INDEX_FILE.exists():
        try:
            with open(INDEX_FILE, "r", encoding="utf-8") as f:
                return json.load(f)
        except (json.JSONDecodeError, IOError):
            pass
    return []


def _save_index(entries: list) -> None:
    _ensure_dirs()
    with open(INDEX_FILE, "w", encoding="utf-8") as f:
        json.dump(entries, f, indent=2, ensure_ascii=False)


class ResumeManager:
    """Manages resume PDFs with versioning, structured extraction, and index persistence."""

    def save_resume(self, file_bytes: bytes, filename: str) -> dict:
        """Save PDF to data/resumes/, extract structured data, persist index entry."""
        _ensure_dirs()
        ts = int(time.time())
        stored_filename = f"{ts}_{filename}"
        pdf_path = RESUMES_DIR / stored_filename
        pdf_path.write_bytes(file_bytes)
        logger.info("Saved resume PDF: %s (%d bytes)", stored_filename, len(file_bytes))

        # Extract structured data lazily (no LLM at save time — use heuristics)
        raw_text = self.extract_text(str(pdf_path))
        structured = self._heuristic_extract(raw_text)

        from datetime import datetime
        entry = {
            "filename": stored_filename,
            "original_name": filename,
            "uploaded_at": datetime.utcnow().isoformat(),
            "is_active": True,
            "structured": structured,
            "raw_text": raw_text,
        }

        # Deactivate all others
        index = _load_index()
        for e in index:
            e["is_active"] = False
        index.append(entry)
        _save_index(index)

        # Also write legacy paths for backward compat
        DEFAULT_PDF_PATH.write_bytes(file_bytes)
        if raw_text:
            DEFAULT_TXT_PATH.write_text(raw_text, encoding="utf-8")

        logger.info("Resume indexed as active: %s", stored_filename)
        return {k: v for k, v in entry.items() if k != "raw_text"}

    def get_active_resume(self) -> Optional[dict]:
        """Returns the active resume record from the index."""
        index = _load_index()
        for entry in reversed(index):
            if entry.get("is_active"):
                return {k: v for k, v in entry.items() if k != "raw_text"}
        # Fallback: last entry
        if index:
            return {k: v for k, v in index[-1].items() if k != "raw_text"}
        return None

    def set_active_resume(self, filename: str) -> None:
        """Mark a resume as active in the index."""
        index = _load_index()
        found = False
        for entry in index:
            entry["is_active"] = entry["filename"] == filename
            if entry["is_active"]:
                found = True
        if not found:
            raise FileNotFoundError(f"Resume not found in index: {filename}")
        _save_index(index)
        # Update legacy paths
        pdf_path = RESUMES_DIR / filename
        if pdf_path.exists():
            DEFAULT_PDF_PATH.write_bytes(pdf_path.read_bytes())
        logger.info("Activated resume: %s", filename)

    def list_resumes(self) -> list:
        """Returns all resume records (without raw text)."""
        index = _load_index()
        return [{k: v for k, v in e.items() if k != "raw_text"} for e in index]

    def extract_text(self, filepath: str) -> str:
        """Extract raw text from PDF using PyMuPDF (fitz)."""
        try:
            import fitz
            doc = fitz.open(filepath)
            pages = []
            for page in doc:
                pages.append(page.get_text())
            doc.close()
            text = "\n\n".join(pages).strip()
            if text:
                logger.info("Extracted %d chars via PyMuPDF from %s", len(text), filepath)
                return text
        except ImportError:
            logger.warning("PyMuPDF (fitz) not installed — trying pdfplumber")
        except Exception as e:
            logger.warning("PyMuPDF extraction failed: %s", e)

        # Fallback: pdfplumber
        try:
            import pdfplumber
            with pdfplumber.open(filepath) as pdf:
                pages = [p.extract_text() or "" for p in pdf.pages]
            text = "\n\n".join(pages).strip()
            if text:
                return text
        except Exception as e:
            logger.warning("pdfplumber extraction failed: %s", e)

        logger.error("All PDF extractors failed for %s", filepath)
        return ""

    def extract_structured(self, filepath: str) -> dict:
        """Extract structured data from resume using LLM (or heuristics as fallback)."""
        raw = self.extract_text(filepath)
        # Try LLM extraction
        try:
            from llm_adapter import LLMAdapter
            import asyncio
            import yaml

            config_path = Path(__file__).parent / "config.local.yaml"
            if not config_path.exists():
                config_path = Path(__file__).parent / "config.yaml"
            with open(config_path, "r") as f:
                cfg = yaml.safe_load(f) or {}

            llm = LLMAdapter(cfg)
            loop = asyncio.new_event_loop()
            structured = loop.run_until_complete(llm.extract_resume_structure(raw))
            loop.close()
            return structured
        except Exception as e:
            logger.debug("LLM extraction unavailable: %s — using heuristic", e)
            return self._heuristic_extract(raw)

    def _heuristic_extract(self, raw_text: str) -> dict:
        """Heuristic extraction of structured resume data."""
        import re

        lines = [l.strip() for l in raw_text.split("\n") if l.strip()]

        # Email
        email = ""
        email_match = re.search(r"[\w.+-]+@[\w-]+\.\w+", raw_text)
        if email_match:
            email = email_match.group(0)

        # Phone
        phone = ""
        phone_match = re.search(r"(\+?\d[\d\s\-().]{8,}\d)", raw_text)
        if phone_match:
            phone = phone_match.group(0).strip()

        # Name: first non-empty, non-email, non-phone line
        name = ""
        for line in lines[:5]:
            if "@" not in line and not re.match(r"^[\d\s\-+()]+$", line) and len(line) < 60:
                name = line
                break

        # Skills
        skills = []
        skill_keywords = [
            "Java", "Python", "Spring", "Kafka", "PostgreSQL", "MySQL", "Angular",
            "React", "Docker", "Kubernetes", "AWS", "GCP", "Azure", "Node",
            "TypeScript", "JavaScript", "REST", "GraphQL", "microservices", "Git",
            "CI/CD", "Jenkins", "Terraform", "Redis", "MongoDB", "Spark",
            "FastAPI", "Flask", "Django", "Golang", "Rust", "C++", "Swift", "Kotlin",
        ]
        raw_lower = raw_text.lower()
        for skill in skill_keywords:
            if skill.lower() in raw_lower:
                skills.append(skill)

        # Years experience
        exp_years = ""
        exp_match = re.search(r"(\d+)\+?\s+years?\s+(?:of\s+)?experience", raw_text, re.IGNORECASE)
        if exp_match:
            exp_years = exp_match.group(1)

        return {
            "name": name,
            "email": email,
            "phone": phone,
            "skills": skills,
            "experience_years": exp_years,
            "current_title": lines[1] if len(lines) > 1 else "",
            "education": "",
            "summary": " ".join(lines[:5]) if lines else "",
        }

    def get_plain_text(self) -> str:
        """Returns raw text of the active resume."""
        index = _load_index()
        for entry in reversed(index):
            if entry.get("is_active"):
                if entry.get("raw_text"):
                    return entry["raw_text"]
                pdf_path = RESUMES_DIR / entry["filename"]
                if pdf_path.exists():
                    return self.extract_text(str(pdf_path))

        # Fallback to legacy path
        if DEFAULT_TXT_PATH.exists():
            return DEFAULT_TXT_PATH.read_text(encoding="utf-8")
        if DEFAULT_PDF_PATH.exists():
            return self.extract_text(str(DEFAULT_PDF_PATH))
        return ""

    def get_structured(self) -> dict:
        """Returns structured extraction of the active resume."""
        index = _load_index()
        for entry in reversed(index):
            if entry.get("is_active") and entry.get("structured"):
                return entry["structured"]
        # Fallback: heuristic from legacy file
        if DEFAULT_PDF_PATH.exists():
            return self._heuristic_extract(self.extract_text(str(DEFAULT_PDF_PATH)))
        return {}


# ─── Module-level singleton ───────────────────────────────────

_manager: Optional[ResumeManager] = None


def get_manager() -> ResumeManager:
    global _manager
    if _manager is None:
        _manager = ResumeManager()
    return _manager


# ─── Legacy module-level functions (used by job_applier_dashboard.py) ────────

def save_uploaded_resume(file_bytes: bytes, filename: str):
    """Legacy: save and return (pdf_path, txt_path, text)."""
    mgr = get_manager()
    record = mgr.save_resume(file_bytes, filename)
    stored = RESUMES_DIR / record["filename"]
    text = mgr.get_plain_text()
    return DEFAULT_PDF_PATH, DEFAULT_TXT_PATH, text


def get_resume_text() -> str:
    return get_manager().get_plain_text()


def get_resume_pdf_path() -> Optional[Path]:
    """Returns path to active resume PDF."""
    index = _load_index()
    for entry in reversed(index):
        if entry.get("is_active"):
            p = RESUMES_DIR / entry["filename"]
            if p.exists():
                return p
    if DEFAULT_PDF_PATH.exists():
        return DEFAULT_PDF_PATH
    return None


def has_resume() -> bool:
    index = _load_index()
    for entry in index:
        p = RESUMES_DIR / entry.get("filename", "")
        if p.exists() and p.stat().st_size > 0:
            return True
    return DEFAULT_PDF_PATH.exists() and DEFAULT_PDF_PATH.stat().st_size > 0


def update_resume_text(text: str) -> None:
    _ensure_dirs()
    DEFAULT_TXT_PATH.write_text(text, encoding="utf-8")
    logger.info("Resume text updated manually (%d chars)", len(text))


def migrate_legacy_resume(workspace_dir: Path) -> bool:
    """Migrate old root-level resume files to data/resumes/."""
    _ensure_dirs()
    migrated = False
    for pdf_file in workspace_dir.glob("*Resume*.pdf"):
        if not DEFAULT_PDF_PATH.exists():
            import shutil
            shutil.copy2(str(pdf_file), str(DEFAULT_PDF_PATH))
            logger.info("Migrated legacy resume PDF: %s", pdf_file.name)
            migrated = True

    if DEFAULT_PDF_PATH.exists() and not DEFAULT_TXT_PATH.exists():
        mgr = get_manager()
        text = mgr.extract_text(str(DEFAULT_PDF_PATH))
        if text:
            DEFAULT_TXT_PATH.write_text(text, encoding="utf-8")
            migrated = True

    return migrated
