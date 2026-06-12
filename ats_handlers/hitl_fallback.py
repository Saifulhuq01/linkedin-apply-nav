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
        """Provide automated generic field mapping and manual fallback instructions."""
        await broadcast(
            "⚠️ Unknown ATS detected. Switching to automated generic mode.",
            "warning"
        )
        await broadcast(
            "Scanning form fields and attempting auto-fill...",
            "info"
        )

        await asyncio.sleep(2)

        # Scan for form fields with extended properties
        fields = await page.evaluate("""() => {
            const fields = [];
            document.querySelectorAll(
                'input:not([type="hidden"]):not([type="submit"]):not([type="button"]):not([type="file"]), '
                + 'textarea, select'
            ).forEach(input => {
                if (!input.offsetParent) return;
                const label = document.querySelector(`label[for="${input.id}"]`)?.innerText?.trim()
                    || input.getAttribute('aria-label')
                    || input.getAttribute('placeholder')
                    || input.name
                    || '';
                if (!label) return;
                fields.push({
                    id: input.id || '',
                    name: input.name || '',
                    placeholder: input.getAttribute('placeholder') || '',
                    label: label,
                    type: input.tagName.toLowerCase() === 'select' ? 'select' : input.type || 'text',
                });
            });
            return fields;
        }""")

        filled_field_ids = set()
        filled_field_names = set()
        filled_count = 0

        if fields:
            await broadcast(f"Found {len(fields)} form fields. Attempting basic heuristics auto-fill...", "info")

            # Display field-by-field suggestions and fill what we can
            for field in fields:
                label = field.get("label", "Unknown")
                value = self._basic_match(label.lower(), user_data)
                if value:
                    success = await self._fill_field(page, field, value)
                    if success:
                        await broadcast(f"✓ Auto-filled {label} → \"{value}\"", "success")
                        if field.get("id"): filled_field_ids.add(field["id"])
                        if field.get("name"): filled_field_names.add(field["name"])
                        filled_count += 1
                    else:
                        await broadcast(f"💡 {label} → Suggestion: \"{value}\" (Please fill manually)", "system")
                await asyncio.sleep(0.1)

            # Try LLM-powered field mapping for remaining unmapped fields
            if llm and llm.has_api_key():
                try:
                    unmapped = [
                        f for f in fields 
                        if (not f.get("id") or f["id"] not in filled_field_ids) 
                        and (not f.get("name") or f["name"] not in filled_field_names)
                    ]
                    if unmapped:
                        await broadcast("🤖 Consulting AI to map remaining custom fields...", "info")
                        mappings = await llm.map_form_fields(unmapped, user_data)
                        for field_identifier, value in mappings.items():
                            matching = [f for f in unmapped if f.get("id") == field_identifier or f.get("name") == field_identifier]
                            if matching:
                                field = matching[0]
                                success = await self._fill_field(page, field, value)
                                if success:
                                    await broadcast(f"🤖 AI Auto-filled \"{field['label']}\" → \"{value}\"", "success")
                                    if field.get("id"): filled_field_ids.add(field["id"])
                                    if field.get("name"): filled_field_names.add(field["name"])
                                    filled_count += 1
                                else:
                                    await broadcast(
                                        f"🤖 AI suggests for \"{field['label']}\": \"{value}\" (Please fill manually)",
                                        "system"
                                    )
                                await asyncio.sleep(0.1)
                except Exception as e:
                    logger.debug("LLM field mapping failed: %s", e)
        else:
            await broadcast("No standard form fields detected. Fill the form manually.", "info")

        # Try resume upload
        resume_uploaded = await self._upload_resume(page, resume_pdf_path, broadcast)

        await broadcast(
            "✋ Complete any remaining fields in the browser and submit manually. "
            "Close this modal when done.",
            "warning"
        )

        return ApplyResult(
            status="manual_needed",
            ats_type="hitl_fallback",
            message=(
                f"Generic ATS auto-filled: {filled_count} fields auto-filled, "
                f"resume {'uploaded' if resume_uploaded else 'upload needed'}. "
                f"Review and submit manually."
            ),
        )

    async def _fill_field(self, page: Any, field: Dict[str, str], value: str) -> bool:
        """Fill a form field using various locator strategies."""
        field_id = field.get("id")
        field_name = field.get("name")
        placeholder = field.get("placeholder")
        label = field.get("label")
        field_type = field.get("type", "text")

        locators = []
        if field_id:
            locators.append(page.locator(f"#{field_id}"))
        if field_name:
            locators.append(page.locator(f"input[name='{field_name}'], textarea[name='{field_name}'], select[name='{field_name}']"))
        if placeholder:
            try: locators.append(page.get_by_placeholder(placeholder, exact=False))
            except Exception: pass
        if label:
            try: locators.append(page.get_by_label(label, exact=False))
            except Exception: pass

        for locator in locators:
            try:
                if await locator.count() > 0 and await locator.first.is_visible():
                    if field_type == "select":
                        await locator.first.select_option(label=value)
                    elif field_type in ["checkbox", "radio"]:
                        if value.lower() in ["true", "yes", "1", "checked", "selected"]:
                            await locator.first.check()
                    else:
                        await locator.first.fill(value)
                    return True
            except Exception:
                continue
        return False

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
