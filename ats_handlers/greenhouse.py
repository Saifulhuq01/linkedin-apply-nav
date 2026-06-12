"""
ats_handlers/greenhouse.py — Greenhouse standard form handler.
"""

import asyncio
import logging
from pathlib import Path

from ats_handlers.base import BaseATSHandler, ApplyResult, ApplyStatus

logger = logging.getLogger("apply_nav.ats.greenhouse")


class GreenhouseHandler(BaseATSHandler):
    """Handles Greenhouse ATS form automation (single-page)."""

    ats_type = "greenhouse"

    async def fill_form(self, page, job_info: dict) -> ApplyResult:
        """Fill Greenhouse application form."""
        user_data = job_info.get("user_data", {})
        structured = {}
        if self.resume:
            try:
                structured = self.resume.get_structured()
            except Exception:
                pass

        await self._emit("Filling Greenhouse application form...", "info")
        filled = 0

        # ── 1. Fill known Greenhouse field IDs ────────────────
        name_parts = (structured.get("name", "") or "").split()
        first = name_parts[0] if name_parts else user_data.get("first_name", "")
        last = name_parts[-1] if len(name_parts) > 1 else user_data.get("last_name", "")

        known_fields = {
            "first_name": first,
            "last_name": last,
            "email": user_data.get("email", "") or structured.get("email", ""),
            "phone": user_data.get("phone", "") or structured.get("phone", ""),
        }

        for field_id, value in known_fields.items():
            if value:
                locator = page.locator(f"#{field_id}")
                if await locator.count() > 0 and await locator.first.is_visible():
                    try:
                        current = await locator.first.input_value()
                        if not current:
                            await self._type_humanlike(locator.first, value)
                            filled += 1
                            await self._emit(f"✓ {field_id} → {value}", "success")
                    except Exception as e:
                        logger.debug("Greenhouse field %s fill error: %s", field_id, e)

        # ── 2. Upload resume ──────────────────────────────────
        resume_path = None
        if self.resume:
            try:
                from resume_manager import get_resume_pdf_path
                resume_path = get_resume_pdf_path()
            except Exception:
                pass

        if resume_path:
            # Greenhouse uses #resume input
            resume_input = page.locator("#resume, input[type='file'][name*='resume']")
            if await resume_input.count() > 0:
                try:
                    await resume_input.first.set_input_files(str(resume_path))
                    filled += 1
                    await self._emit("✓ Resume uploaded", "success")
                    await asyncio.sleep(2)
                except Exception as e:
                    logger.debug("Resume upload failed: %s", e)
            else:
                # Generic file input fallback
                await self._upload_resume(page, resume_path, self.broadcast)

        # ── 3. Cover letter (optional) ────────────────────────
        cover_letter_input = page.locator("#cover_letter, input[type='file'][name*='cover']")
        if await cover_letter_input.count() > 0:
            # Skip cover letter upload — leave for human
            pass

        # ── 4. Scan for remaining unfilled visible inputs ─────
        remaining = await self._scan_remaining_fields(page)
        for field_info in remaining:
            label = field_info.get("label", "")
            field_id = field_info.get("id", "")
            field_type = field_info.get("type", "text")
            current_val = field_info.get("currentValue", "")

            if current_val:
                continue

            # Try PII match first
            val = self._match_pii(label.lower(), user_data, structured)
            if val:
                if field_id:
                    try:
                        locator = page.locator(f"#{field_id}")
                        if await locator.count() > 0:
                            await self._type_humanlike(locator.first, val)
                            filled += 1
                    except Exception:
                        pass
                continue

            # Use LLM for unknown fields
            if self.llm and label:
                try:
                    answer = await self.llm.answer_question(label, [], structured)
                    if answer and field_id:
                        locator = page.locator(f"#{field_id}")
                        if await locator.count() > 0 and field_type in ("text", "textarea"):
                            await self._type_humanlike(locator.first, answer)
                            filled += 1
                except Exception as e:
                    logger.debug("LLM answer failed for %s: %s", label, e)

        await self._emit(f"Greenhouse form filled ({filled} fields). Ready for review.", "success")

        # ── 5. Do NOT submit — broadcast paused_for_review ───
        return ApplyResult(ApplyStatus.PENDING_REVIEW, f"Greenhouse form ready — {filled} fields filled. Awaiting user confirmation.")

    async def _scan_remaining_fields(self, page) -> list:
        """Scan for unfilled visible inputs on Greenhouse form."""
        return await page.evaluate("""() => {
            const fields = [];
            document.querySelectorAll('input:not([type="hidden"]):not([type="file"]), textarea, select').forEach(el => {
                if (el.offsetParent === null) return; // not visible
                const id = el.id || el.name || '';
                // Skip already handled known IDs
                if (['first_name','last_name','email','phone','resume','cover_letter'].includes(id)) return;
                // Find label
                let label = '';
                if (id) {
                    const lblEl = document.querySelector(`label[for="${id}"]`);
                    if (lblEl) label = lblEl.innerText.trim();
                }
                if (!label) {
                    const parent = el.closest('.field, .form-group, .application-field');
                    if (parent) {
                        const lbl = parent.querySelector('label');
                        if (lbl) label = lbl.innerText.trim();
                    }
                }
                fields.push({
                    id: id,
                    type: el.type || el.tagName.toLowerCase(),
                    label: label,
                    placeholder: el.placeholder || '',
                    currentValue: el.value || '',
                });
            });
            return fields;
        }""")

    def _match_pii(self, label_lower: str, user_data: dict, structured: dict) -> str:
        name_parts = (structured.get("name", "") or "").split()
        pii = [
            (["first name", "given name"], name_parts[0] if name_parts else user_data.get("first_name", "")),
            (["last name", "surname"], name_parts[-1] if len(name_parts) > 1 else user_data.get("last_name", "")),
            (["email"], user_data.get("email", "") or structured.get("email", "")),
            (["phone", "mobile", "tel"], user_data.get("phone", "") or structured.get("phone", "")),
            (["city", "location"], user_data.get("city", "")),
            (["linkedin", "linkedin url"], user_data.get("linkedin_url", "")),
            (["github", "portfolio"], user_data.get("github_url", "")),
        ]
        for keywords, value in pii:
            if value and any(kw in label_lower for kw in keywords):
                return value
        return ""

    async def can_handle(self, page) -> bool:
        return (
            "greenhouse.io" in page.url
            or await page.locator("#first_name, #last_name").count() > 0
        )
