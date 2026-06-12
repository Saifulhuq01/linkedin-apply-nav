"""
ats_handlers/hitl_fallback.py — Generic fallback for unknown ATS.

Opens the apply URL in the visible browser, scans all form fields,
pre-fills what LLM can map confidently, then hands off to human.
"""

import asyncio
import logging
from pathlib import Path

from ats_handlers.base import BaseATSHandler, ApplyResult, ApplyStatus

logger = logging.getLogger("apply_nav.ats.hitl_fallback")


class HITLFallbackHandler(BaseATSHandler):
    """Generic fallback handler — opens browser, pre-fills what it can, requires human for rest."""

    ats_type = "hitl_fallback"

    async def fill_form(self, page, job_info: dict) -> ApplyResult:
        """Scan form, map fields via LLM, pre-fill, leave rest for human."""
        apply_url = job_info.get("apply_url", "")
        user_data = job_info.get("user_data", {})

        structured = {}
        if self.resume:
            try:
                structured = self.resume.get_structured()
            except Exception:
                pass

        await self._emit(f"Opening external application: {apply_url[:80]}", "info")

        # Navigate to the apply URL if not already there
        if apply_url and page.url != apply_url:
            try:
                await page.goto(apply_url, wait_until="domcontentloaded", timeout=30000)
                await asyncio.sleep(3)
            except Exception as e:
                await self._emit(f"Navigation failed: {e}", "error")
                return ApplyResult(ApplyStatus.FAILED, f"Navigation failed: {e}")

        # ── 1. Scan all visible form fields ───────────────────
        all_fields = await self._scan_all_fields(page)
        await self._emit(f"Detected {len(all_fields)} form fields.", "info")

        # ── 2. Upload resume to any file input ────────────────
        resume_path = None
        if self.resume:
            try:
                from resume_manager import get_resume_pdf_path
                resume_path = get_resume_pdf_path()
            except Exception:
                pass

        if resume_path:
            await self._upload_resume(page, resume_path, self.broadcast)

        # ── 3. Map fields via LLM ─────────────────────────────
        field_labels = [f.get("label", f.get("placeholder", f.get("name", ""))) for f in all_fields]
        mapping = {}
        if self.llm and field_labels:
            try:
                mapping = await self.llm.map_form_fields(field_labels, structured)
            except Exception as e:
                logger.debug("LLM field mapping failed: %s", e)

        # ── 4. Pre-fill mapped fields ─────────────────────────
        fields_prefilled = 0
        fields_requiring_human = 0

        for field_info in all_fields:
            field_id = field_info.get("id", "")
            field_name = field_info.get("name", "")
            field_type = field_info.get("type", "text")
            label = field_info.get("label", field_info.get("placeholder", ""))
            current = field_info.get("currentValue", "")

            if current or field_type in ("file", "submit", "button", "hidden"):
                continue

            # Check LLM mapping
            value = mapping.get(label, "")
            if not value:
                # Try direct PII heuristic
                value = self._match_pii_direct(label, field_id, field_name, user_data, structured)

            if value:
                filled = await self._fill_by_id_or_name(page, field_id, field_name, field_type, value)
                if filled:
                    fields_prefilled += 1
                    await self._emit(f"✓ Pre-filled '{label}' → '{value}'", "success")
                else:
                    fields_requiring_human += 1
            else:
                fields_requiring_human += 1

        # ── 5. Broadcast HITL active event ────────────────────
        summary = (
            f"Browser opened. Pre-filled {fields_prefilled} fields. "
            f"{fields_requiring_human} fields require manual input."
        )
        await self._emit(f"🤖 HITL Active: {summary}", "warning")
        await self._emit("Please complete the remaining fields in the browser window.", "warning")

        return ApplyResult(
            ApplyStatus.HITL_REQUIRED,
            f"Browser opened. Pre-filled {fields_prefilled} fields. Manual completion required.",
            fields_filled=fields_prefilled,
            fields_total=len(all_fields),
        )

    async def _scan_all_fields(self, page) -> list:
        """Scan all visible input/textarea/select elements on the page."""
        return await page.evaluate("""() => {
            const fields = [];
            document.querySelectorAll('input:not([type="hidden"]):not([type="submit"]):not([type="button"]), textarea, select').forEach(el => {
                if (el.offsetParent === null) return;
                const id = el.id || '';
                const name = el.name || '';
                let label = '';
                if (id) {
                    const lbl = document.querySelector(`label[for="${id}"]`);
                    if (lbl) label = lbl.innerText.trim();
                }
                if (!label) {
                    const parent = el.closest('div, fieldset, .form-group, .field');
                    if (parent) {
                        const lbl = parent.querySelector('label');
                        if (lbl && lbl !== el) label = lbl.innerText.trim();
                    }
                }
                fields.push({
                    id: id,
                    name: name,
                    type: el.type || el.tagName.toLowerCase(),
                    label: label,
                    placeholder: el.placeholder || '',
                    currentValue: el.value || '',
                });
            });
            return fields;
        }""")

    async def _fill_by_id_or_name(
        self, page, field_id: str, field_name: str, field_type: str, value: str
    ) -> bool:
        """Fill a field by ID or name attribute."""
        selector = ""
        if field_id:
            selector = f"#{field_id}"
        elif field_name:
            selector = f"[name='{field_name}']"
        else:
            return False

        try:
            locator = page.locator(selector)
            if await locator.count() == 0:
                return False
            el = locator.first
            if not await el.is_visible():
                return False

            if field_type in ("text", "email", "tel", "number", "search", "url"):
                await self._type_humanlike(el, value)
                return True
            elif field_type == "textarea":
                await self._type_humanlike(el, value)
                return True
            elif field_type == "select":
                try:
                    await el.select_option(label=value)
                    return True
                except Exception:
                    try:
                        await el.select_option(value=value)
                        return True
                    except Exception:
                        return False
            return False
        except Exception as e:
            logger.debug("Fill by id/name failed (%s): %s", selector, e)
            return False

    def _match_pii_direct(
        self, label: str, field_id: str, field_name: str, user_data: dict, structured: dict
    ) -> str:
        """Direct PII matching from label, id, or name attributes."""
        text = f"{label} {field_id} {field_name}".lower()
        name_parts = (structured.get("name", "") or "").split()

        pii = [
            (["first_name", "first name", "firstname", "given"], name_parts[0] if name_parts else user_data.get("first_name", "")),
            (["last_name", "last name", "lastname", "surname", "family"], name_parts[-1] if len(name_parts) > 1 else user_data.get("last_name", "")),
            (["email"], user_data.get("email", "") or structured.get("email", "")),
            (["phone", "mobile", "tel"], user_data.get("phone", "") or structured.get("phone", "")),
            (["city", "location", "address"], user_data.get("city", "")),
            (["linkedin"], user_data.get("linkedin_url", "")),
            (["github", "portfolio"], user_data.get("github_url", "")),
        ]
        for keywords, value in pii:
            if value and any(kw in text for kw in keywords):
                return value
        return ""

    async def can_handle(self, page) -> bool:
        return True  # Always handles as fallback
