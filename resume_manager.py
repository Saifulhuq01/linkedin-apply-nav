"""
Resume Manager for Apply-Nav.

Handles PDF upload, text extraction, and storage.
Resumes are stored in data/resumes/ (gitignored).
"""

import logging
import shutil
from pathlib import Path
from typing import Optional, Tuple

logger = logging.getLogger("apply_nav.resume")

DATA_DIR = Path(__file__).parent / "data"
RESUMES_DIR = DATA_DIR / "resumes"
DEFAULT_PDF_PATH = RESUMES_DIR / "resume.pdf"
DEFAULT_TXT_PATH = RESUMES_DIR / "resume.txt"


def _ensure_dirs() -> None:
    """Create the resumes directory if it doesn't exist."""
    RESUMES_DIR.mkdir(parents=True, exist_ok=True)


def extract_text_from_pdf(pdf_path: Path) -> str:
    """Extract text from a PDF file using pdfplumber.
    
    Falls back to a basic PyMuPDF extraction if pdfplumber is unavailable,
    and finally to a raw bytes scan as a last resort.
    """
    text = ""

    # Try pdfplumber first (best quality)
    try:
        import pdfplumber
        with pdfplumber.open(str(pdf_path)) as pdf:
            pages = []
            for page in pdf.pages:
                page_text = page.extract_text()
                if page_text:
                    pages.append(page_text)
            text = "\n\n".join(pages)
            if text.strip():
                logger.info("Extracted %d chars via pdfplumber from %s", len(text), pdf_path.name)
                return text.strip()
    except ImportError:
        logger.warning("pdfplumber not installed — trying fallback extraction")
    except Exception as e:
        logger.warning("pdfplumber extraction failed: %s — trying fallback", e)

    # Try PyMuPDF (fitz) as fallback
    try:
        import fitz  # PyMuPDF
        doc = fitz.open(str(pdf_path))
        pages = []
        for page in doc:
            pages.append(page.get_text())
        doc.close()
        text = "\n\n".join(pages)
        if text.strip():
            logger.info("Extracted %d chars via PyMuPDF from %s", len(text), pdf_path.name)
            return text.strip()
    except ImportError:
        logger.warning("PyMuPDF not installed either")
    except Exception as e:
        logger.warning("PyMuPDF extraction failed: %s", e)

    # Last resort: raw text extraction (won't work well but better than nothing)
    logger.warning("All PDF extractors failed — install pdfplumber: pip install pdfplumber")
    return ""


def save_uploaded_resume(file_bytes: bytes, filename: str) -> Tuple[Path, Path, str]:
    """Save an uploaded resume PDF and extract its text.
    
    Args:
        file_bytes: Raw bytes of the uploaded PDF file
        filename: Original filename (used for logging only)
    
    Returns:
        Tuple of (pdf_path, txt_path, extracted_text)
    """
    _ensure_dirs()

    # Save PDF
    DEFAULT_PDF_PATH.write_bytes(file_bytes)
    logger.info("Saved resume PDF: %s (%d bytes)", DEFAULT_PDF_PATH, len(file_bytes))

    # Extract text
    extracted_text = extract_text_from_pdf(DEFAULT_PDF_PATH)

    # Save extracted text
    if extracted_text:
        DEFAULT_TXT_PATH.write_text(extracted_text, encoding="utf-8")
        logger.info("Saved extracted resume text: %s (%d chars)", DEFAULT_TXT_PATH, len(extracted_text))
    else:
        DEFAULT_TXT_PATH.write_text("", encoding="utf-8")
        logger.warning("No text extracted from PDF — resume.txt will be empty")

    return DEFAULT_PDF_PATH, DEFAULT_TXT_PATH, extracted_text


def get_resume_text() -> str:
    """Get the current resume text."""
    if DEFAULT_TXT_PATH.exists():
        return DEFAULT_TXT_PATH.read_text(encoding="utf-8")
    return ""


def get_resume_pdf_path() -> Optional[Path]:
    """Get the path to the current resume PDF, if it exists."""
    if DEFAULT_PDF_PATH.exists():
        return DEFAULT_PDF_PATH
    return None


def has_resume() -> bool:
    """Check if a resume has been uploaded."""
    return DEFAULT_PDF_PATH.exists() and DEFAULT_PDF_PATH.stat().st_size > 0


def update_resume_text(text: str) -> None:
    """Manually update the resume text (for user corrections)."""
    _ensure_dirs()
    DEFAULT_TXT_PATH.write_text(text, encoding="utf-8")
    logger.info("Resume text updated manually (%d chars)", len(text))


def migrate_legacy_resume(workspace_dir: Path) -> bool:
    """Migrate old hardcoded resume files to the new data/resumes/ location.
    
    Looks for any *.pdf and *.txt files in the workspace root that look like
    resume files and moves them to data/resumes/.
    
    Returns True if migration happened.
    """
    _ensure_dirs()
    migrated = False

    # Look for any PDF that might be a resume (not a project analysis)
    for pdf_file in workspace_dir.glob("*Resume*.pdf"):
        if not DEFAULT_PDF_PATH.exists():
            shutil.copy2(str(pdf_file), str(DEFAULT_PDF_PATH))
            logger.info("Migrated legacy resume PDF: %s → %s", pdf_file.name, DEFAULT_PDF_PATH)
            migrated = True

    for txt_file in workspace_dir.glob("*Resume*.txt"):
        if not DEFAULT_TXT_PATH.exists():
            shutil.copy2(str(txt_file), str(DEFAULT_TXT_PATH))
            logger.info("Migrated legacy resume TXT: %s → %s", txt_file.name, DEFAULT_TXT_PATH)
            migrated = True

    # If we migrated a PDF but not text, extract text
    if DEFAULT_PDF_PATH.exists() and not DEFAULT_TXT_PATH.exists():
        text = extract_text_from_pdf(DEFAULT_PDF_PATH)
        if text:
            DEFAULT_TXT_PATH.write_text(text, encoding="utf-8")
            migrated = True

    return migrated
