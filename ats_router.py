"""
ATS Router for Apply-Nav.

Detects ATS type from external apply URLs and dispatches
to the appropriate handler for semi-automated form filling.
"""

import re
import logging
from typing import Optional, Dict, Any
from dataclasses import dataclass

logger = logging.getLogger("apply_nav.ats_router")


@dataclass
class ApplyResult:
    """Result of an application attempt."""
    status: str          # "applied" | "failed" | "manual_needed" | "captcha" | "cancelled"
    ats_type: str        # "easy_apply" | "workday" | "greenhouse" | "lever" | "unknown"
    message: str         # Human-readable status message
    error: str = ""      # Error details if failed


# URL patterns for ATS detection
ATS_PATTERNS: Dict[str, list] = {
    "workday": [
        r"myworkdayjobs\.com",
        r"\.wd\d+\.myworkdayjobs",
        r"workday\.com/.*job",
    ],
    "greenhouse": [
        r"boards\.greenhouse\.io",
        r"greenhouse\.io/.*job",
        r"job_app\?.*token=",
    ],
    "lever": [
        r"jobs\.lever\.co",
        r"lever\.co/.*apply",
    ],
    "icims": [
        r"\.icims\.com",
        r"icims\.com/jobs",
    ],
    "taleo": [
        r"taleo\.net",
        r"oracle\.com/.*careers",
    ],
    "smartrecruiters": [
        r"jobs\.smartrecruiters\.com",
    ],
    "bamboohr": [
        r".*\.bamboohr\.com/careers",
    ],
}


def detect_ats_type(url: str) -> str:
    """Detect the ATS type from an external apply URL.
    
    Args:
        url: The external application URL
        
    Returns:
        ATS type string: workday | greenhouse | lever | icims | taleo | 
                         smartrecruiters | bamboohr | unknown
    """
    if not url:
        return "unknown"

    url_lower = url.lower()

    for ats_type, patterns in ATS_PATTERNS.items():
        for pattern in patterns:
            if re.search(pattern, url_lower):
                logger.info("Detected ATS type '%s' from URL: %s", ats_type, url[:100])
                return ats_type

    logger.info("Unknown ATS type for URL: %s", url[:100])
    return "unknown"


def get_handler_for_ats(ats_type: str):
    """Get the appropriate handler class for an ATS type.
    
    Returns the handler class (not instantiated) or None for unsupported types.
    """
    from ats_handlers.easy_apply import EasyApplyHandler
    from ats_handlers.workday import WorkdayHandler
    from ats_handlers.greenhouse import GreenhouseHandler
    from ats_handlers.hitl_fallback import HITLFallbackHandler

    handler_map = {
        "easy_apply": EasyApplyHandler,
        "workday": WorkdayHandler,
        "greenhouse": GreenhouseHandler,
        # lever, icims, taleo, smartrecruiters, bamboohr all use HITL for now
        "lever": HITLFallbackHandler,
        "icims": HITLFallbackHandler,
        "taleo": HITLFallbackHandler,
        "smartrecruiters": HITLFallbackHandler,
        "bamboohr": HITLFallbackHandler,
        "unknown": HITLFallbackHandler,
    }

    handler_class = handler_map.get(ats_type, HITLFallbackHandler)
    logger.info("Using handler: %s for ATS type: %s", handler_class.__name__, ats_type)
    return handler_class
