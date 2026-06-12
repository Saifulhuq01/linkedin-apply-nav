"""
db.py — All SQLite operations for Apply-Nav.

Zero SQL outside this file. All state is persisted here.
Rate limits, circuit breaker state, dedup checks, history — all DB-backed.
"""

import sqlite3
import json
import logging
from pathlib import Path
from datetime import datetime, timedelta
from typing import Optional

logger = logging.getLogger("apply_nav.db")

DATA_DIR = Path(__file__).parent / "data"
DB_PATH = DATA_DIR / "database.sqlite"


# ─── Schema ───────────────────────────────────────────────────

_SCHEMA = """
CREATE TABLE IF NOT EXISTS applications (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    job_id        TEXT NOT NULL,
    title         TEXT NOT NULL,
    company       TEXT NOT NULL,
    location      TEXT,
    apply_url     TEXT,
    ats_type      TEXT DEFAULT 'unknown',
    score         INTEGER DEFAULT 0,
    matched_skills TEXT,
    skill_gaps    TEXT,
    outreach_note TEXT,
    status        TEXT DEFAULT 'discovered',
    applied_at    DATETIME,
    created_at    DATETIME DEFAULT CURRENT_TIMESTAMP,
    raw_jd        TEXT,
    resume_version TEXT
);

CREATE UNIQUE INDEX IF NOT EXISTS idx_job_dedup ON applications(job_id);

CREATE TABLE IF NOT EXISTS searches (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    keywords   TEXT,
    location   TEXT,
    filters    TEXT,
    jobs_found INTEGER DEFAULT 0,
    jobs_scored INTEGER DEFAULT 0,
    created_at DATETIME DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS session_health (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    consecutive_failures INTEGER DEFAULT 0,
    circuit_open_until   DATETIME,
    last_checked         DATETIME DEFAULT CURRENT_TIMESTAMP
);
"""


# ─── Connection ───────────────────────────────────────────────

def _get_conn() -> sqlite3.Connection:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(DB_PATH))
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


def _row_to_dict(row) -> dict:
    if row is None:
        return None
    return dict(row)


# ─── Init ─────────────────────────────────────────────────────

def init_db() -> None:
    """Creates tables; inserts one row into session_health if empty."""
    conn = _get_conn()
    try:
        conn.executescript(_SCHEMA)
        # Insert sentinel session_health row if empty
        row = conn.execute("SELECT COUNT(*) as c FROM session_health").fetchone()
        if row["c"] == 0:
            conn.execute(
                "INSERT INTO session_health (consecutive_failures, circuit_open_until) VALUES (0, NULL)"
            )
        conn.commit()
        logger.info("Database initialized at %s", DB_PATH)
    finally:
        conn.close()


# ─── Job Operations ───────────────────────────────────────────

def upsert_job(job_dict: dict) -> bool:
    """Inserts or ignores on conflict. Returns True if inserted (new), False if already existed."""
    conn = _get_conn()
    try:
        cursor = conn.execute(
            """INSERT OR IGNORE INTO applications
               (job_id, title, company, location, apply_url, ats_type, score,
                matched_skills, skill_gaps, outreach_note, status, raw_jd, resume_version)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                job_dict.get("job_id", ""),
                job_dict.get("title", ""),
                job_dict.get("company", ""),
                job_dict.get("location", ""),
                job_dict.get("apply_url", ""),
                job_dict.get("ats_type", "unknown"),
                job_dict.get("score", 0),
                json.dumps(job_dict.get("matched_skills", [])),
                json.dumps(job_dict.get("skill_gaps", [])),
                job_dict.get("outreach_note", ""),
                job_dict.get("status", "discovered"),
                job_dict.get("raw_jd", ""),
                job_dict.get("resume_version", ""),
            ),
        )
        conn.commit()
        return cursor.rowcount > 0
    finally:
        conn.close()


def update_job_status(job_id: str, status: str, **kwargs) -> None:
    """Updates status and any extra keyword fields on the row."""
    conn = _get_conn()
    try:
        # Build dynamic SET clause from kwargs
        set_parts = ["status = ?"]
        params = [status]

        allowed = {
            "applied_at", "ats_type", "score", "matched_skills",
            "skill_gaps", "outreach_note", "apply_url", "raw_jd", "resume_version"
        }
        for key, val in kwargs.items():
            if key in allowed:
                if key in ("matched_skills", "skill_gaps") and isinstance(val, list):
                    val = json.dumps(val)
                set_parts.append(f"{key} = ?")
                params.append(val)

        if status == "applied" and "applied_at" not in kwargs:
            set_parts.append("applied_at = ?")
            params.append(datetime.utcnow().isoformat())

        params.append(job_id)
        conn.execute(
            f"UPDATE applications SET {', '.join(set_parts)} WHERE job_id = ?",
            params,
        )
        conn.commit()
    finally:
        conn.close()


def get_job(job_id: str) -> Optional[dict]:
    """Returns the job record or None."""
    conn = _get_conn()
    try:
        row = conn.execute(
            "SELECT * FROM applications WHERE job_id = ?", (job_id,)
        ).fetchone()
        if row is None:
            return None
        d = dict(row)
        for field in ("matched_skills", "skill_gaps"):
            if d.get(field):
                try:
                    d[field] = json.loads(d[field])
                except (json.JSONDecodeError, TypeError):
                    d[field] = []
            else:
                d[field] = []
        return d
    finally:
        conn.close()


def get_history(limit: int = 100) -> list:
    """Returns last N application records, newest first."""
    conn = _get_conn()
    try:
        rows = conn.execute(
            "SELECT * FROM applications ORDER BY created_at DESC LIMIT ?", (limit,)
        ).fetchall()
        results = []
        for row in rows:
            d = dict(row)
            for field in ("matched_skills", "skill_gaps"):
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


def check_duplicate(job_id: str) -> bool:
    """Returns True if job is already applied, applying, review, or queued."""
    conn = _get_conn()
    try:
        row = conn.execute(
            """SELECT status FROM applications
               WHERE job_id = ?
               AND status IN ('applied', 'applying', 'review', 'queued')""",
            (job_id,),
        ).fetchone()
        return row is not None
    finally:
        conn.close()


# ─── Rate Limiting ────────────────────────────────────────────

def check_rate_limit() -> tuple:
    """Returns (is_blocked: bool, reason: str). Hard limits: 5/hr, 25/day."""
    conn = _get_conn()
    try:
        now = datetime.utcnow()
        hour_ago = (now - timedelta(hours=1)).isoformat()
        day_start = now.replace(hour=0, minute=0, second=0, microsecond=0).isoformat()

        hour_count = conn.execute(
            "SELECT COUNT(*) as c FROM applications WHERE applied_at >= ? AND status = 'applied'",
            (hour_ago,),
        ).fetchone()["c"]

        if hour_count >= 5:
            return True, f"Hourly rate limit: {hour_count}/5 applications this hour. Wait before applying."

        day_count = conn.execute(
            "SELECT COUNT(*) as c FROM applications WHERE applied_at >= ? AND status = 'applied'",
            (day_start,),
        ).fetchone()["c"]

        if day_count >= 25:
            return True, f"Daily rate limit: {day_count}/25 applications today. Resume tomorrow."

        return False, ""
    finally:
        conn.close()


# ─── Search Log ───────────────────────────────────────────────

def log_search(keywords: str, location: str, filters: str, found: int, scored: int) -> None:
    """Record a search operation."""
    conn = _get_conn()
    try:
        conn.execute(
            "INSERT INTO searches (keywords, location, filters, jobs_found, jobs_scored) VALUES (?, ?, ?, ?, ?)",
            (keywords, location, filters, found, scored),
        )
        conn.commit()
    finally:
        conn.close()


# ─── Circuit Breaker ──────────────────────────────────────────

def get_circuit_state() -> tuple:
    """Returns (is_open: bool, open_until: datetime | None)."""
    conn = _get_conn()
    try:
        row = conn.execute(
            "SELECT consecutive_failures, circuit_open_until FROM session_health ORDER BY id LIMIT 1"
        ).fetchone()
        if row is None:
            return False, None
        open_until_str = row["circuit_open_until"]
        if open_until_str:
            try:
                open_until = datetime.fromisoformat(open_until_str)
                if datetime.utcnow() < open_until:
                    return True, open_until
            except ValueError:
                pass
        return False, None
    finally:
        conn.close()


def record_failure() -> None:
    """Increments consecutive_failures; trips circuit if >= 3."""
    conn = _get_conn()
    try:
        row = conn.execute(
            "SELECT id, consecutive_failures FROM session_health ORDER BY id LIMIT 1"
        ).fetchone()
        if row is None:
            conn.execute(
                "INSERT INTO session_health (consecutive_failures) VALUES (1)"
            )
            conn.commit()
            return

        new_count = row["consecutive_failures"] + 1
        open_until = None
        if new_count >= 3:
            open_until = (datetime.utcnow() + timedelta(minutes=5)).isoformat()
            logger.warning("Circuit breaker tripped! open_until=%s", open_until)

        conn.execute(
            "UPDATE session_health SET consecutive_failures = ?, circuit_open_until = ?, last_checked = ? WHERE id = ?",
            (new_count, open_until, datetime.utcnow().isoformat(), row["id"]),
        )
        conn.commit()
    finally:
        conn.close()


def record_success() -> None:
    """Resets consecutive_failures to 0, clears circuit_open_until."""
    conn = _get_conn()
    try:
        conn.execute(
            "UPDATE session_health SET consecutive_failures = 0, circuit_open_until = NULL, last_checked = ?",
            (datetime.utcnow().isoformat(),),
        )
        conn.commit()
    finally:
        conn.close()


# ─── Statistics ───────────────────────────────────────────────

def get_statistics() -> dict:
    """Returns counts by status, total applied today, this week, this month."""
    conn = _get_conn()
    try:
        now = datetime.utcnow()
        today_start = now.replace(hour=0, minute=0, second=0, microsecond=0).isoformat()
        week_start = (now - timedelta(days=now.weekday())).replace(hour=0, minute=0, second=0, microsecond=0).isoformat()
        month_start = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0).isoformat()

        # Counts by status
        rows = conn.execute(
            "SELECT status, COUNT(*) as c FROM applications GROUP BY status"
        ).fetchall()
        by_status = {r["status"]: r["c"] for r in rows}

        applied_today = conn.execute(
            "SELECT COUNT(*) as c FROM applications WHERE applied_at >= ? AND status = 'applied'",
            (today_start,),
        ).fetchone()["c"]

        applied_week = conn.execute(
            "SELECT COUNT(*) as c FROM applications WHERE applied_at >= ? AND status = 'applied'",
            (week_start,),
        ).fetchone()["c"]

        applied_month = conn.execute(
            "SELECT COUNT(*) as c FROM applications WHERE applied_at >= ? AND status = 'applied'",
            (month_start,),
        ).fetchone()["c"]

        total = sum(by_status.values())
        applied_total = by_status.get("applied", 0)

        return {
            "by_status": by_status,
            "total": total,
            "applied_today": applied_today,
            "applied_this_week": applied_week,
            "applied_this_month": applied_month,
            "applied_total": applied_total,
            "success_rate": round((applied_total / total * 100), 1) if total > 0 else 0,
        }
    finally:
        conn.close()


# ─── Legacy compatibility shims (used by job_applier_dashboard.py) ────────────

def is_already_applied(job_id: str) -> bool:
    return check_duplicate(job_id)


def get_application_status(job_id: str) -> Optional[str]:
    job = get_job(job_id)
    return job["status"] if job else None


def record_application(
    job_id: str,
    *,
    title: str = "",
    company: str = "",
    location: str = "",
    description: str = "",
    ats_type: str = "easy_apply",
    status: str = "discovered",
    score: Optional[int] = None,
    matched_skills=None,
    missing_skills=None,
    outreach_note: str = "",
    error_message: str = "",
    apply_url: str = "",
) -> int:
    """Legacy: upsert an application record."""
    upsert_job({
        "job_id": job_id,
        "title": title,
        "company": company,
        "location": location,
        "apply_url": apply_url,
        "ats_type": ats_type,
        "score": score or 0,
        "matched_skills": matched_skills or [],
        "skill_gaps": missing_skills or [],
        "outreach_note": outreach_note,
        "status": status,
        "raw_jd": description,
    })
    job = get_job(job_id)
    return job["id"] if job else 0


def update_application_status(job_id: str, status: str, error_message: str = "") -> None:
    """Legacy: thin wrapper around update_job_status."""
    update_job_status(job_id, status)


def get_application_history(
    limit: int = 50,
    offset: int = 0,
    status_filter: Optional[str] = None,
    ats_filter: Optional[str] = None,
) -> list:
    """Legacy: filtered history query."""
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
            for field in ("matched_skills", "skill_gaps"):
                if d.get(field):
                    try:
                        d[field] = json.loads(d[field])
                    except (json.JSONDecodeError, TypeError):
                        d[field] = []
                else:
                    d[field] = []
            # Map new field names to legacy names for compatibility
            d["missing_skills"] = d.get("skill_gaps", [])
            d["description"] = d.get("raw_jd", "")
            results.append(d)
        return results
    finally:
        conn.close()


def get_stats() -> dict:
    """Legacy alias for get_statistics()."""
    stats = get_statistics()
    # Add legacy keys
    stats["total_tracked"] = stats["total"]
    stats["total_applied"] = stats["applied_total"]
    stats["total_failed"] = stats["by_status"].get("failed", 0)
    stats["today_applied"] = stats["applied_today"]
    return stats


def check_rate_limit_legacy(max_per_hour: int = 5, max_per_day: int = 25) -> Optional[str]:
    """Legacy: returns error message string or None."""
    is_blocked, reason = check_rate_limit()
    return reason if is_blocked else None


def record_search(keywords: str, location: str, jobs_found: int) -> int:
    """Legacy: log a search with minimal params."""
    log_search(keywords, location, "", jobs_found, 0)
    return 0
