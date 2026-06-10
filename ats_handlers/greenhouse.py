"""
Greenhouse ATS handler for Apply-Nav.

Greenhouse uses standard HTML forms with predictable structure,
making it the easiest external ATS to automate.
"""

import asyncio
import logging
from pathlib import Path
from typing import Any, Callable, Dict, Optional

from ats_handlers.base import BaseATSHandler, BroadcastFn
from ats_router import ApplyResult

logger = logging.getLogger("apply_nav.ats.greenhouse")


class GreenhouseHandler(BaseATSHandler):
    """Semi-automated Greenhouse application handler."""

    ats_type = "greenhouse"

    async def can_handle(self, page: Any) -> bool:
        """Check if the page is a Greenhouse application form."""
        url = page.url.lower()
        if "greenhouse.io" in url:
            return True
        has_gh = await page.evaluate("""() => {
            return !!(
                document.querySelector('#application_form') ||
                document.querySelector('[data-source="greenhouse"]') ||
                document.querySelector('.application--header')
            );
        }""")
        return has_gh

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
        """Fill Greenhouse application form.

        Greenhouse uses standard HTML forms with well-known IDs:
        - #first_name, #last_name, #email, #phone
        - Resume upload via standard file input
        - Custom questions in labeled fieldsets
        """
        await broadcast("Greenhouse application detected. Auto-filling form...", "info")
        await asyncio.sleep(2)

        filled_count = 0

        # Standard Greenhouse field IDs
        gh_fields = [
            ("#first_name", user_data.get("first_name", "")),
            ("#last_name", user_data.get("last_name", "")),
            ("#email", user_data.get("email", "")),
            ("#phone", user_data.get("phone", "")),
            ("#location", user_data.get("city", "")),
            ("#job_application_location", user_data.get("city", "")),
        ]

        for selector, value in gh_fields:
            if value and await self._safe_fill(page, selector, value):
                label = selector.replace("#", "").replace("_", " ").title()
                await broadcast(f"✓ {label} → {value}", "success")
                filled_count += 1
                await asyncio.sleep(0.3)

        # Fallback: scan for labeled inputs
        fields = await page.evaluate("""() => {
            const fields = [];
            document.querySelectorAll('.field, .application-field').forEach(el => {
                const label = el.querySelector('label');
                const input = el.querySelector('input:not([type="hidden"]):not([type="file"]), textarea, select');
                if (label && input && !input.value) {
                    fields.push({
                        id: input.id,
                        label: label.innerText.trim(),
                        type: input.tagName.toLowerCase() === 'select' ? 'select' : input.type || 'text',
                        required: input.hasAttribute('required')
                    });
                }
            });
            return fields;
        }""")

        for field in fields:
            label = field.get("label", "").lower()
            value = self._match_field(label, user_data)
            if value and field.get("id"):
                if await self._safe_fill(page, f"#{field['id']}", value):
                    await broadcast(f"✓ {field['label']} → {value}", "success")
                    filled_count += 1
                    await asyncio.sleep(0.3)

        # Resume upload
        resume_uploaded = await self._upload_resume(page, resume_pdf_path, broadcast)

        await broadcast(
            f"Auto-filled {filled_count} fields. "
            f"Please review remaining fields and submit in the browser.",
            "warning"
        )

        return ApplyResult(
            status="manual_needed",
            ats_type="greenhouse",
            message=(
                f"Greenhouse form partially automated: {filled_count} fields filled, "
                f"resume {'uploaded' if resume_uploaded else 'not uploaded'}. "
                f"Review and submit manually."
            ),
        )

    def _match_field(self, label: str, user_data: Dict[str, str]) -> Optional[str]:
        """Match field label to user data."""
        mappings = [
            (["first name"], user_data.get("first_name", "")),
            (["last name", "surname"], user_data.get("last_name", "")),
            (["email"], user_data.get("email", "")),
            (["phone", "mobile"], user_data.get("phone", "")),
            (["city", "location"], user_data.get("city", "")),
        ]
        for keywords, value in mappings:
            if value and any(kw in label for kw in keywords):
                return value
        return None
