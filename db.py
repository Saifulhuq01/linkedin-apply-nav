"""
SQLite state persistence for Apply-Nav.

Tracks application history, prevents duplicate applications,
and stores search history for analytics.
"""

import sqlite3
import json
import logging
from pathlib import Path
from datetime import datetime, timedelta
from typing import Optional, List, Dict, Any

logger = logging.getLogger("apply_nav.db")

DATA_DIR = Path(__file__).parent / "data"
DB_PATH = DATA_DIR / "apply_nav.db"

_SCHEMA = """
CREATE TABLE IF NOT EXISTS applications (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    job_id TEXT UNIQUE NOT NULL,
    title TEXT,
    company TEXT,
    location TEXT,
    description TEXT,
    ats_type TEXT DEFAULT 'easy_apply',
    status TEXT DEFAULT 'pending',
    score INTEGER,
    matched_skills TEXT,
    missing_skills TEXT,
    outreach_note TEXT,
    error_message TEXT,
    apply_url TEXT,
    applied_at TIMESTAMP,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS searches (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    keywords TEXT,
    location TEXT,
    jobs_found INTEGER DEFAULT 0,
    jobs_applied INTEGER DEFAULT 0,
    searched_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX IF NOT EXISTS idx_applications_job_id ON applications(job_id);
CREATE INDEX IF NOT EXISTS idx_applications_status ON applications(status);
CREATE INDEX IF NOT EXISTS idx_applications_applied_at ON applications(applied_at);
"""


def _get_conn() -> sqlite3.Connection:
    """Get a SQLite connection, creating the database if needed."""
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(DB_PATH))
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


def init_db() -> None:
    """Initialize the database schema."""
    conn = _get_conn()
    try:
        conn.executescript(_SCHEMA)
        conn.commit()
        logger.info("Database initialized at %s", DB_PATH)
    finally:
        conn.close()


def is_already_applied(job_id: str) -> bool:
    """Check if a job has already been applied to (or is in progress)."""
    conn = _get_conn()
    try:
        row = conn.execute(
            "SELECT status FROM applications WHERE job_id = ? AND status IN ('applied', 'pending')",
            (job_id,)
        ).fetchone()
        return row is not None
    finally:
        conn.close()


def get_application_status(job_id: str) -> Optional[str]:
    """Get the current status of a job application."""
    conn = _get_conn()
    try:
        row = conn.execute(
            "SELECT status FROM applications WHERE job_id = ?",
            (job_id,)
        ).fetchone()
        return row["status"] if row else None
    finally:
        conn.close()


def record_application(
    job_id: str,
    *,
    title: str = "",
    company: str = "",
    location: str = "",
    description: str = "",
    ats_type: str = "easy_apply",
    status: str = "pending",
    score: Optional[int] = None,
    matched_skills: Optional[List[str]] = None,
    missing_skills: Optional[List[str]] = None,
    outreach_note: str = "",
    error_message: str = "",
    apply_url: str = "",
) -> int:
    """Record a new application attempt. Returns the row ID."""
    conn = _get_conn()
    try:
        applied_at = datetime.utcnow().isoformat() if status == "applied" else None
        cursor = conn.execute(
            """INSERT INTO applications 
               (job_id, title, company, location, description, ats_type, status, 
                score, matched_skills, missing_skills, outreach_note, 
                error_message, apply_url, applied_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
               ON CONFLICT(job_id) DO UPDATE SET
                   status = excluded.status,
                   score = COALESCE(excluded.score, applications.score),
                   matched_skills = COALESCE(excluded.matched_skills, applications.matched_skills),
                   missing_skills = COALESCE(excluded.missing_skills, applications.missing_skills),
                   error_message = COALESCE(excluded.error_message, applications.error_message),
                   applied_at = COALESCE(excluded.applied_at, applications.applied_at)
            """,
            (
                job_id, title, company, location, description, ats_type, status,
                score,
                json.dumps(matched_skills) if matched_skills else None,
                json.dumps(missing_skills) if missing_skills else None,
                outreach_note, error_message, apply_url, applied_at,
            )
        )
        conn.commit()
        logger.info("Recorded application: job_id=%s status=%s", job_id, status)
        return cursor.lastrowid
    finally:
        conn.close()


def update_application_status(job_id: str, status: str, error_message: str = "") -> None:
    """Update the status of an existing application."""
    conn = _get_conn()
    try:
        applied_at = datetime.utcnow().isoformat() if status == "applied" else None
        conn.execute(
            """UPDATE applications 
               SET status = ?, error_message = ?, 
                   applied_at = COALESCE(?, applied_at)
               WHERE job_id = ?""",
            (status, error_message, applied_at, job_id)
        )
        conn.commit()
    finally:
        conn.close()


def get_application_history(
    limit: int = 50,
    offset: int = 0,
    status_filter: Optional[str] = None,
    ats_filter: Optional[str] = None,
) -> List[Dict[str, Any]]:
    """Get application history with optional filters."""
    conn = _get_conn()
    try:
        query = "SELECT * FROM applications WHERE 1=1"
        params: list = []

        if status_filter:
            query += " AND status = ?"
            params.append(status_filter)
        if ats_filter:
            query += " AND ats_type = ?"
            params.append(ats_filter)

        query += " ORDER BY created_at DESC LIMIT ? OFFSET ?"
        params.extend([limit, offset])

        rows = conn.execute(query, params).fetchall()
        results = []
        for row in rows:
            d = dict(row)
            # Parse JSON fields
            for field in ("matched_skills", "missing_skills"):
                if d.get(field):
                    try:
                        d[field] = json.loads(d[field])
                    except (json.JSONDecodeError, TypeError):
                        d[field] = []
                else:
                    d[field] = []
            results.append(d)
        return results
    finally:
        conn.close()


def get_stats() -> Dict[str, Any]:
    """Get aggregate statistics."""
    conn = _get_conn()
    try:
        total = conn.execute("SELECT COUNT(*) as c FROM applications").fetchone()["c"]
        applied = conn.execute(
            "SELECT COUNT(*) as c FROM applications WHERE status = 'applied'"
        ).fetchone()["c"]
        failed = conn.execute(
            "SELECT COUNT(*) as c FROM applications WHERE status = 'failed'"
        ).fetchone()["c"]
        avg_score = conn.execute(
            "SELECT AVG(score) as a FROM applications WHERE score IS NOT NULL"
        ).fetchone()["a"]

        # Today's count
        today_start = datetime.utcnow().replace(hour=0, minute=0, second=0).isoformat()
        today_count = conn.execute(
            "SELECT COUNT(*) as c FROM applications WHERE applied_at >= ? AND status = 'applied'",
            (today_start,)
        ).fetchone()["c"]

        # This hour's count
        hour_ago = (datetime.utcnow() - timedelta(hours=1)).isoformat()
        hour_count = conn.execute(
            "SELECT COUNT(*) as c FROM applications WHERE applied_at >= ? AND status = 'applied'",
            (hour_ago,)
        ).fetchone()["c"]

        return {
            "total_tracked": total,
            "total_applied": applied,
            "total_failed": failed,
            "success_rate": round((applied / total * 100), 1) if total > 0 else 0,
            "avg_score": round(avg_score, 1) if avg_score else 0,
            "today_applied": today_count,
            "this_hour_applied": hour_count,
        }
    finally:
        conn.close()


def check_rate_limit(max_per_hour: int, max_per_day: int) -> Optional[str]:
    """Check if rate limits are exceeded. Returns error message or None."""
    conn = _get_conn()
    try:
        hour_ago = (datetime.utcnow() - timedelta(hours=1)).isoformat()
        hour_count = conn.execute(
            "SELECT COUNT(*) as c FROM applications WHERE applied_at >= ? AND status = 'applied'",
            (hour_ago,)
        ).fetchone()["c"]
        if hour_count >= max_per_hour:
            return f"Rate limit: {hour_count}/{max_per_hour} applications this hour. Wait before applying."

        today_start = datetime.utcnow().replace(hour=0, minute=0, second=0).isoformat()
        day_count = conn.execute(
            "SELECT COUNT(*) as c FROM applications WHERE applied_at >= ? AND status = 'applied'",
            (today_start,)
        ).fetchone()["c"]
        if day_count >= max_per_day:
            return f"Daily limit: {day_count}/{max_per_day} applications today. Resume tomorrow."

        return None
    finally:
        conn.close()


def record_search(keywords: str, location: str, jobs_found: int) -> int:
    """Record a search operation."""
    conn = _get_conn()
    try:
        cursor = conn.execute(
            "INSERT INTO searches (keywords, location, jobs_found) VALUES (?, ?, ?)",
            (keywords, location, jobs_found)
        )
        conn.commit()
        return cursor.lastrowid
    finally:
        conn.close()
