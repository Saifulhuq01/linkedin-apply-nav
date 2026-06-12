"""
ats_handlers/base.py — Abstract base class for ATS-specific application handlers.
"""

import asyncio
import logging
import random
from abc import ABC, abstractmethod
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Any, Callable, Coroutine, Dict, Optional


class ApplyStatus(Enum):
    SUCCESS = "applied"
    PENDING_REVIEW = "review"
    FAILED = "failed"
    HITL_REQUIRED = "hitl_required"


@dataclass
class ApplyResult:
    status: ApplyStatus
    message: str = ""
    fields_filled: int = 0
    fields_total: int = 0


# Type alias for broadcast function
BroadcastFn = Callable[[str, str], Coroutine[Any, Any, None]]


class BaseATSHandler(ABC):
    """Base class for all ATS-specific application handlers."""

    ats_type: str = "unknown"

    def __init__(self, ws_broadcaster=None, llm_adapter=None, answer_cache=None, resume_manager=None):
        self.broadcast = ws_broadcaster   # async callable: (message, level)
        self.llm = llm_adapter
        self.cache = answer_cache
        self.resume = resume_manager
        self.logger = logging.getLogger(f"apply_nav.ats.{self.ats_type}")

    @abstractmethod
    async def fill_form(self, page, job_info: dict) -> ApplyResult:
        pass

    # ─── Human-like Interaction Helpers ──────────────────────

    async def _type_humanlike(self, locator, text: str) -> None:
        """Type text character by character with random delays."""
        await locator.click()
        for char in str(text):
            await locator.type(char)
            await asyncio.sleep(random.uniform(0.03, 0.09))

    async def _random_delay(self, min_s: float = 1.5, max_s: float = 3.0) -> None:
        """Random sleep between actions to appear human."""
        await asyncio.sleep(random.uniform(min_s, max_s))

    async def _mouse_jitter(self, page) -> None:
        """Move mouse to a random position before key interactions."""
        x = random.randint(100, 500)
        y = random.randint(100, 400)
        await page.mouse.move(x, y)

    # ─── Safe Interaction Helpers ─────────────────────────────

    async def _safe_fill(self, page, selector: str, value: str) -> bool:
        """Safely fill a form field, returning True if successful."""
        try:
            locator = page.locator(selector)
            if await locator.count() > 0 and await locator.first.is_visible():
                await locator.first.fill(value)
                return True
        except Exception as e:
            self.logger.debug("Fill failed for %s: %s", selector, e)
        return False

    async def _safe_click(self, page, selector: str) -> bool:
        """Safely click an element, returning True if successful."""
        try:
            locator = page.locator(selector)
            if await locator.count() > 0 and await locator.first.is_visible():
                await self._mouse_jitter(page)
                await self._random_delay(1.5, 3.0)
                await locator.first.click()
                return True
        except Exception as e:
            self.logger.debug("Click failed for %s: %s", selector, e)
        return False

    async def _upload_resume(self, page, resume_path: Path, broadcast_fn=None) -> bool:
        """Upload resume to any file input on the page."""
        try:
            file_inputs = page.locator("input[type='file']")
            count = await file_inputs.count()
            if count > 0 and resume_path and resume_path.exists():
                for i in range(count):
                    inp = file_inputs.nth(i)
                    try:
                        await inp.set_input_files(str(resume_path))
                        if broadcast_fn:
                            await broadcast_fn("✓ Resume uploaded", "success")
                        await asyncio.sleep(2)
                        return True
                    except Exception:
                        continue
            return False
        except Exception as e:
            self.logger.debug("Resume upload failed: %s", e)
            return False

    async def _emit(self, message: str, level: str = "info") -> None:
        """Emit log to broadcast fn if set."""
        if self.broadcast:
            try:
                await self.broadcast(message, level)
            except Exception:
                pass
        self.logger.info("[%s] %s", level, message)

    async def cleanup(self, page, broadcast_fn=None) -> None:
        """Clean up browser state after apply attempt."""
        try:
            await page.keyboard.press("Escape")
            await asyncio.sleep(0.5)
            await page.keyboard.press("Escape")
            await asyncio.sleep(0.5)
        except Exception as e:
            self.logger.debug("Cleanup error (non-critical): %s", e)

    # ─── Legacy compatibility ─────────────────────────────────

    async def can_handle(self, page) -> bool:
        return False
