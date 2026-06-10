"""
HITL Fallback handler for Apply-Nav.

Generic handler for unknown or unsupported ATS portals.
Opens the URL in the visible browser and provides AI-guided
field mapping suggestions in the UI terminal.
"""

import asyncio
import logging
from pathlib import Path
from typing import Any, Callable, Dict, Optional

from ats_handlers.base import BaseATSHandler, BroadcastFn
from ats_router import ApplyResult

logger = logging.getLogger("apply_nav.ats.hitl_fallback")


class HITLFallbackHandler(BaseATSHandler):
    """Generic HITL fallback for unrecognized ATS portals."""

    ats_type = "hitl_fallback"

    async def can_handle(self, page: Any) -> bool:
        """HITL fallback can always handle — it's the last resort."""
        return True

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
        """Provide AI-guided assistance for manual application.

        This handler doesn't try to automate the form. Instead it:
        1. Scans visible form fields
        2. Uses the LLM to suggest field mappings
        3. Displays suggestions in the terminal for the user
        4. Attempts resume upload if a file input is found
        """
        await broadcast(
            "⚠️ Unknown ATS detected. Switching to AI-guided manual mode.",
            "warning"
        )
        await broadcast(
            "The browser is open — you'll fill the form manually. "
            "AI suggestions will appear below to help.",
            "info"
        )

        await asyncio.sleep(2)

        # Scan for form fields
        fields = await page.evaluate("""() => {
            const fields = [];
            document.querySelectorAll(
                'input:not([type="hidden"]):not([type="submit"]):not([type="button"]), '
                + 'textarea, select'
            ).forEach(input => {
                if (!input.offsetParent) return;
                const label = document.querySelector(`label[for="${input.id}"]`)?.innerText?.trim()
                    || input.getAttribute('aria-label')
                    || input.getAttribute('placeholder')
                    || '';
                if (!label) return;
                fields.push({
                    id: input.id || '',
                    label: label,
                    type: input.tagName.toLowerCase() === 'select' ? 'select' : input.type || 'text',
                });
            });
            return fields;
        }""")

        if fields:
            await broadcast(f"Found {len(fields)} form fields. Generating suggestions...", "info")

            # Display field-by-field suggestions
            for field in fields:
                label = field.get("label", "Unknown")
                value = self._basic_match(label.lower(), user_data)
                if value:
                    await broadcast(f"💡 {label} → Suggested: \"{value}\"", "system")
                else:
                    await broadcast(f"📝 {label} → Please fill manually", "system")
                await asyncio.sleep(0.2)

            # Try LLM-powered field mapping for remaining fields
            if llm and llm.has_api_key():
                try:
                    unmapped = [f for f in fields if not self._basic_match(f.get("label", "").lower(), user_data)]
                    if unmapped:
                        mappings = await llm.map_form_fields(unmapped, user_data)
                        for field_id, value in mappings.items():
                            matching = [f for f in unmapped if f.get("id") == field_id]
                            if matching:
                                await broadcast(
                                    f"🤖 AI suggests for \"{matching[0]['label']}\": \"{value}\"",
                                    "system"
                                )
                except Exception as e:
                    logger.debug("LLM field mapping failed: %s", e)
        else:
            await broadcast("No standard form fields detected. Fill the form manually.", "info")

        # Try resume upload
        resume_uploaded = await self._upload_resume(page, resume_pdf_path, broadcast)

        await broadcast(
            "✋ Complete the form in the browser and submit manually. "
            "Close this modal when done.",
            "warning"
        )

        return ApplyResult(
            status="manual_needed",
            ats_type="hitl_fallback",
            message=(
                f"AI-guided manual mode. {len(fields)} fields found with suggestions. "
                f"Resume {'uploaded' if resume_uploaded else 'upload needed'}. "
                f"Complete and submit manually."
            ),
        )

    def _basic_match(self, label: str, user_data: Dict[str, str]) -> Optional[str]:
        """Basic label-to-data matching."""
        mappings = [
            (["first name", "given name"], user_data.get("first_name", "")),
            (["last name", "family name", "surname"], user_data.get("last_name", "")),
            (["email", "e-mail"], user_data.get("email", "")),
            (["phone", "mobile", "telephone", "cell"], user_data.get("phone", "")),
            (["city", "location", "address"], user_data.get("city", "")),
        ]
        for keywords, value in mappings:
            if value and any(kw in label for kw in keywords):
                return value
        return None
