"""
ats_router.py — URL pattern matching + DOM probe to select correct handler.
"""

import re
import logging
import urllib.parse
from dataclasses import dataclass
from typing import Optional

logger = logging.getLogger("apply_nav.ats_router")


# ─── Platform URL Patterns ────────────────────────────────────

PLATFORM_PATTERNS = {
    "easy_apply":      [],  # detected by DOM probe, not URL
    "workday":         ["myworkdayjobs.com", "workday.com", "wd1.myworkdayjobs", "wd3.myworkdayjobs", "wd5.myworkdayjobs"],
    "greenhouse":      ["greenhouse.io", "boards.greenhouse.io"],
    "lever":           ["lever.co", "jobs.lever.co"],
    "icims":           ["icims.com"],
    "taleo":           ["taleo.net"],
    "smartrecruiters": ["smartrecruiters.com"],
    "bamboohr":        ["bamboohr.com"],
}


# ─── ApplyResult (legacy shape) ───────────────────────────────

@dataclass
class ApplyResult:
    """Legacy result shape (used by job_applier_dashboard.py)."""
    status: str          # "applied" | "failed" | "review" | "manual_needed" | "cancelled" | "interrupted"
    ats_type: str = "unknown"
    message: str = ""
    error: str = ""


# ─── URL Utilities ────────────────────────────────────────────

def clean_apply_url(url: str) -> str:
    """Decode LinkedIn safety redirect URLs to the real target."""
    if not url:
        return ""
    if "linkedin.com/safety/go" in url:
        try:
            parsed = urllib.parse.urlparse(url)
            params = urllib.parse.parse_qs(parsed.query)
            if "url" in params:
                decoded = params["url"][0]
                logger.info("Decoded safety URL: %s → %s", url[:50], decoded[:80])
                return decoded
        except Exception as e:
            logger.warning("Failed to decode safety URL %s: %s", url, e)
    return url


# ─── ATSRouter class ─────────────────────────────────────────

class ATSRouter:
    """Detects the ATS platform and returns the correct handler."""

    def detect_platform(self, apply_url: str, page=None) -> str:
        """
        URL matching first.
        If apply_url is empty or is a LinkedIn internal apply URL, return "easy_apply".
        """
        if not apply_url:
            return "easy_apply"

        url_lower = apply_url.lower()

        # LinkedIn internal = Easy Apply
        if any(x in url_lower for x in ["/apply/", "linkedin.com/jobs", "opensdui"]):
            return "easy_apply"

        # Match against platform patterns
        for platform, patterns in PLATFORM_PATTERNS.items():
            for pattern in patterns:
                if pattern in url_lower:
                    logger.info("Detected platform '%s' from URL: %s", platform, apply_url[:80])
                    return platform

        return "hitl_fallback"

    def get_handler(self, platform: str):
        """Returns the correct handler instance for the given platform."""
        from ats_handlers.easy_apply import EasyApplyHandler
        from ats_handlers.workday import WorkdayHandler
        from ats_handlers.greenhouse import GreenhouseHandler
        from ats_handlers.hitl_fallback import HITLFallbackHandler

        handler_map = {
            "easy_apply": EasyApplyHandler,
            "workday": WorkdayHandler,
            "greenhouse": GreenhouseHandler,
            "lever": HITLFallbackHandler,
            "icims": HITLFallbackHandler,
            "taleo": HITLFallbackHandler,
            "smartrecruiters": HITLFallbackHandler,
            "bamboohr": HITLFallbackHandler,
            "hitl_fallback": HITLFallbackHandler,
            "unknown": HITLFallbackHandler,
        }

        cls = handler_map.get(platform, HITLFallbackHandler)
        logger.info("Using handler: %s for platform: %s", cls.__name__, platform)
        return cls()


# ─── Module-level helpers (legacy API) ────────────────────────

_router = ATSRouter()


def detect_ats_type(url: str) -> str:
    """Legacy function: detect ATS type from URL."""
    return _router.detect_platform(url)


def get_handler_for_ats(ats_type: str):
    """Legacy function: returns handler class for the given ATS type."""
    from ats_handlers.easy_apply import EasyApplyHandler
    from ats_handlers.workday import WorkdayHandler
    from ats_handlers.greenhouse import GreenhouseHandler
    from ats_handlers.hitl_fallback import HITLFallbackHandler

    handler_map = {
        "easy_apply": EasyApplyHandler,
        "workday": WorkdayHandler,
        "greenhouse": GreenhouseHandler,
        "lever": HITLFallbackHandler,
        "icims": HITLFallbackHandler,
        "taleo": HITLFallbackHandler,
        "smartrecruiters": HITLFallbackHandler,
        "bamboohr": HITLFallbackHandler,
        "unknown": HITLFallbackHandler,
    }
    return handler_map.get(ats_type, HITLFallbackHandler)
