"""
Abstract base class for ATS-specific application handlers.
"""

import asyncio
import logging
from abc import ABC, abstractmethod
from pathlib import Path
from typing import Any, Callable, Coroutine, Dict, Optional

from ats_router import ApplyResult

logger = logging.getLogger("apply_nav.ats.base")

# Type alias for broadcast function
BroadcastFn = Callable[[str, str], Coroutine[Any, Any, None]]


class BaseATSHandler(ABC):
    """Base class for all ATS-specific application handlers.
    
    Each handler implements the form-filling logic for a specific ATS
    (LinkedIn Easy Apply, Workday, Greenhouse, etc.).
    
    The handler receives:
    - A Playwright page object (visible browser)
    - User data from config.local.yaml
    - Resume paths
    - An LLM adapter for AI-assisted field filling
    - A broadcast function for streaming logs to the UI
    """

    ats_type: str = "unknown"

    def __init__(self):
        self.logger = logging.getLogger(f"apply_nav.ats.{self.ats_type}")

    @abstractmethod
    async def can_handle(self, page: Any) -> bool:
        """Check if this handler can handle the current page.
        
        Called after navigation to the apply URL. The handler should
        inspect the page DOM to determine if it recognizes the form layout.
        
        Args:
            page: Playwright Page object
            
        Returns:
            True if this handler recognizes and can automate the page
        """
        ...

    @abstractmethod
    async def fill_form(
        self,
        page: Any,
        user_data: Dict[str, str],
        resume_pdf_path: Path,
        resume_text: str,
        llm: Any,
        broadcast: BroadcastFn,
        question_callback: Optional[Callable] = None,
    ) -> ApplyResult:
        """Fill the application form.
        
        This is the main automation method. Implementations should:
        1. Fill known fields (name, email, phone, etc.) from user_data
        2. Upload the resume
        3. Use the LLM for unknown fields
        4. Pause for HITL when needed
        5. Return the result
        
        Args:
            page: Playwright Page object (visible browser)
            user_data: Dict with keys: first_name, last_name, email, phone, city, etc.
            resume_pdf_path: Path to the resume PDF file
            resume_text: Extracted plaintext resume
            llm: LLMAdapter instance for AI-assisted field filling
            broadcast: Async function to send log messages to the UI
            question_callback: Optional async function for HITL question prompts
            
        Returns:
            ApplyResult with status and details
        """
        ...

    async def cleanup(self, page: Any, broadcast: BroadcastFn) -> None:
        """Clean up browser state after an apply attempt (success or failure).
        
        Override in subclasses for ATS-specific cleanup.
        Default implementation dismisses modals via Escape key.
        """
        try:
            await page.keyboard.press("Escape")
            await asyncio.sleep(0.5)
            await page.keyboard.press("Escape")
            await asyncio.sleep(0.5)
        except Exception as e:
            self.logger.debug("Cleanup error (non-critical): %s", e)

    async def _safe_fill(self, page: Any, selector: str, value: str) -> bool:
        """Safely fill a form field, returning True if successful."""
        try:
            locator = page.locator(selector)
            if await locator.count() > 0 and await locator.first.is_visible():
                await locator.first.fill(value)
                return True
        except Exception as e:
            self.logger.debug("Fill failed for %s: %s", selector, e)
        return False

    async def _safe_click(self, page: Any, selector: str) -> bool:
        """Safely click an element, returning True if successful."""
        try:
            locator = page.locator(selector)
            if await locator.count() > 0 and await locator.first.is_visible():
                await locator.first.click()
                return True
        except Exception as e:
            self.logger.debug("Click failed for %s: %s", selector, e)
        return False

    async def _upload_resume(self, page: Any, resume_path: Path, broadcast: BroadcastFn) -> bool:
        """Upload resume to file input fields on the page."""
        try:
            file_inputs = page.locator("input[type='file']")
            count = await file_inputs.count()
            if count > 0 and resume_path.exists():
                for i in range(count):
                    inp = file_inputs.nth(i)
                    try:
                        await inp.set_input_files(str(resume_path))
                        await broadcast("✓ Resume uploaded", "success")
                        await asyncio.sleep(2)
                        return True
                    except Exception:
                        continue
            return False
        except Exception as e:
            self.logger.debug("Resume upload failed: %s", e)
            return False
