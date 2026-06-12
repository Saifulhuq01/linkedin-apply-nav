"""
ats_handlers/easy_apply.py — LinkedIn Easy Apply multi-page handler.
"""

import asyncio
import random
import logging
from pathlib import Path
from typing import Any, Callable, Dict, Optional

from ats_handlers.base import BaseATSHandler, ApplyResult, ApplyStatus

logger = logging.getLogger("apply_nav.ats.easy_apply")


class EasyApplyHandler(BaseATSHandler):
    """Handles LinkedIn Easy Apply multi-page form automation."""

    ats_type = "easy_apply"

    def __init__(self, ws_broadcaster=None, llm_adapter=None, answer_cache=None, resume_manager=None):
        super().__init__(ws_broadcaster, llm_adapter, answer_cache, resume_manager)
        # Per-apply HITL event (set by /api/submit-answer)
        self._question_event: Optional[asyncio.Event] = None
        self._question_answer: str = ""
        self._submit_event: Optional[asyncio.Event] = None

    def set_events(self, question_event: asyncio.Event, submit_event: asyncio.Event):
        """Inject shared events from the server."""
        self._question_event = question_event
        self._submit_event = submit_event

    async def fill_form(self, page, job_info: dict) -> ApplyResult:
        """Execute the LinkedIn Easy Apply multi-step flow."""
        # ── 1. Find Easy Apply button ──────────────────────────
        apply_btn = await self._find_easy_apply_button(page)
        if not apply_btn:
            return ApplyResult(ApplyStatus.FAILED, "No Easy Apply button found")

        await self._emit("Found Easy Apply button — clicking...", "info")
        await self._mouse_jitter(page)
        await self._random_delay(1.5, 3.0)
        await apply_btn.click()
        await asyncio.sleep(3.0)

        # ── 2. Wait for modal ──────────────────────────────────
        modal = page.locator("[role='dialog'], .jobs-easy-apply-modal, .artdeco-modal")
        try:
            await modal.wait_for(state="visible", timeout=10000)
            await self._emit("Easy Apply form opened!", "success")
        except Exception:
            if "apply" in page.url:
                modal = page.locator("main, [role='main'], form")
            else:
                return ApplyResult(ApplyStatus.FAILED, "Apply form didn't appear")

        # ── 3. Multi-page form loop (up to 15 pages) ──────────
        for step in range(1, 16):
            if await modal.count() == 0:
                await self._emit("Form closed unexpectedly.", "warning")
                break

            try:
                # Fill the current page
                n_filled = await self._fill_page(page, job_info)
                await self._emit(f"Step {step}: filled {n_filled} fields", "info")

                await asyncio.sleep(1.5)

                # Detect navigation buttons
                submit_btn = page.get_by_role("button", name="Submit application", exact=False)
                review_btn = page.get_by_role("button", name="Review", exact=False)
                next_btn = page.get_by_role("button", name="Next", exact=False)

                if await submit_btn.count() > 0 and await submit_btn.first.is_visible():
                    # HITL: pause before final submit
                    await self._emit("✋ Ready to submit! Awaiting user confirmation in dashboard.", "warning")
                    if self._submit_event:
                        await self._emit_broadcast_event("paused_for_review", {
                            "job_id": job_info.get("job_id", ""),
                            "message": "Application ready. Please confirm submission.",
                        })
                    return ApplyResult(ApplyStatus.PENDING_REVIEW, "Awaiting user confirmation")

                elif await review_btn.count() > 0 and await review_btn.first.is_visible():
                    await self._emit("Clicking Review...", "info")
                    await self._mouse_jitter(page)
                    await self._random_delay(1.5, 3.0)
                    await review_btn.first.click()
                    await asyncio.sleep(2.0)

                elif await next_btn.count() > 0 and await next_btn.first.is_visible():
                    await self._emit(f"Advancing to step {step + 1}...", "info")
                    await self._mouse_jitter(page)
                    await self._random_delay(1.5, 3.0)
                    await next_btn.first.click()
                    await asyncio.sleep(2.0)

                else:
                    await self._emit("No navigation button found. Check browser.", "warning")
                    return ApplyResult(ApplyStatus.PENDING_REVIEW, "Form navigation unclear — browser opened for manual review")

            except Exception as ex:
                await self._emit(f"Step {step} error: {ex}", "error")
                logger.exception("EasyApply step %d error", step)
                return ApplyResult(ApplyStatus.FAILED, f"Error at step {step}: {ex}")

            await asyncio.sleep(random.uniform(1.5, 3.0))

        return ApplyResult(ApplyStatus.PENDING_REVIEW, "Form loop completed — manual review recommended")

    async def _fill_page(self, page, job_info: dict) -> int:
        """Fill all fields on the current Easy Apply page. Returns number filled."""
        filled = 0

        # Get user/resume data
        resume_text = ""
        structured = {}
        if self.resume:
            resume_text = self.resume.get_plain_text()
            structured = self.resume.get_structured()

        user_config = job_info.get("user_data", {})

        # Upload resume to file inputs
        if self.resume:
            pdf_path = None
            try:
                from resume_manager import get_resume_pdf_path
                pdf_path = get_resume_pdf_path()
            except Exception:
                pass
            if pdf_path:
                await self._upload_resume(page, pdf_path, self.broadcast)

        # Fill known PII fields
        pii_map = {
            "input[id*='name']": self._get_name(user_config, structured),
            "input[id*='phone']": user_config.get("phone", ""),
            "input[id*='email']": user_config.get("email", ""),
            "input[id*='city']": user_config.get("city", ""),
        }
        for selector, value in pii_map.items():
            if value:
                locator = page.locator(selector)
                cnt = await locator.count()
                for i in range(min(cnt, 3)):
                    try:
                        el = locator.nth(i)
                        if await el.is_visible():
                            current = await el.input_value()
                            if not current:
                                await self._type_humanlike(el, value)
                                filled += 1
                    except Exception:
                        pass

        # Extract and fill unknown form fields
        fields = await self._extract_form_fields(page)
        for field in fields:
            label = field.get("labelText", "")
            field_type = field.get("type", "text")
            element_id = field.get("elementId")
            current_val = field.get("currentValue")

            # Skip pre-filled
            if current_val and field_type in ("text", "textarea"):
                continue

            # Try PII match
            val = self._match_pii(label.lower(), user_config, structured)
            if val:
                if element_id:
                    try:
                        locator = page.locator(f"#{element_id}")
                        if await locator.count() > 0:
                            await self._type_humanlike(locator.first, val)
                            filled += 1
                    except Exception:
                        pass
                await self._emit(f"✓ {label} → {val}", "success")
                continue

            # Unknown field — get AI answer
            if self.llm:
                options = [r.get("text", "") for r in field.get("radios", [])]
                try:
                    ai_answer = await self.llm.answer_question(label, options, structured)
                except Exception as e:
                    logger.debug("AI answer failed: %s", e)
                    ai_answer = ""

                if ai_answer:
                    # Broadcast HITL question with AI suggestion
                    await self._emit_broadcast_event("paused_for_question", {
                        "question": label,
                        "suggested": ai_answer,
                        "options": options,
                        "field_type": field_type,
                        "question_hash": self._hash_question(label),
                    })
                    await self._emit(f"⏸ Waiting for answer to: '{label}'", "system")

                    # Wait for user to confirm via asyncio Event
                    if self._question_event:
                        self._question_event.clear()
                        await self._question_event.wait()
                        confirmed_answer = self._question_answer or ai_answer
                    else:
                        confirmed_answer = ai_answer

                    # Fill the field
                    await self._fill_field(page, field, confirmed_answer)
                    filled += 1

        return filled

    async def _fill_field(self, page, field: dict, answer: str) -> None:
        """Fill a specific form field with the given answer."""
        field_type = field.get("type", "text")
        element_id = field.get("elementId")

        if field_type in ("text", "textarea") and element_id:
            locator = page.locator(f"#{element_id}")
            if await locator.count() > 0:
                await self._type_humanlike(locator.first, answer)

        elif field_type == "select" and element_id:
            locator = page.locator(f"#{element_id}")
            if await locator.count() > 0:
                try:
                    await locator.first.select_option(label=answer)
                except Exception:
                    try:
                        await locator.first.select_option(value=answer)
                    except Exception:
                        pass

        elif field_type == "radio":
            radios = field.get("radios", [])
            target_id = None
            for r in radios:
                if r.get("text", "").lower() == answer.lower() or answer.lower() in r.get("text", "").lower():
                    target_id = r.get("id")
                    break
            if not target_id and radios:
                target_id = radios[0].get("id")
            if target_id:
                try:
                    await page.locator(f"label[for='{target_id}']").click()
                except Exception:
                    pass

        elif field_type == "checkbox" and element_id:
            if any(k in answer.lower() for k in ["yes", "true", "agree", "check"]):
                try:
                    await page.locator(f"#{element_id}").check()
                except Exception:
                    pass

    async def _find_easy_apply_button(self, page):
        """Find the Easy Apply button or link. Returns a locator or None."""
        # Try aria-label containing "Easy Apply"
        locators = [
            page.locator("button[aria-label*='Easy Apply']"),
            page.locator("button:has-text('Easy Apply')"),
            page.locator("a[href*='openSDUIApplyFlow=true']"),
            page.locator("[data-job-id] button"),
        ]
        for loc in locators:
            try:
                cnt = await loc.count()
                for i in range(cnt):
                    el = loc.nth(i)
                    if await el.is_visible():
                        return el
            except Exception:
                pass
        return None

    async def _extract_form_fields(self, page) -> list:
        """Extract visible form fields from the current Easy Apply step."""
        return await page.evaluate("""() => {
            const fields = [];
            document.querySelectorAll('.fb-dash-form-element, fieldset, .jobs-easy-apply-form-section').forEach((el, i) => {
                const lbl = el.querySelector('label, legend, .fb-dash-form-element__label, .fb-form-element-label');
                if (!lbl) return;
                const t = el.querySelector('input[type="text"], input[type="tel"], input[type="email"], input[type="number"]');
                const ta = el.querySelector('textarea');
                const sel = el.querySelector('select');
                const radios = Array.from(el.querySelectorAll('input[type="radio"]')).map(r => ({
                    value: r.value,
                    text: (el.querySelector(`label[for="${r.id}"]`) || r.parentElement)?.innerText?.trim() || r.value,
                    id: r.id,
                }));
                const cb = el.querySelector('input[type="checkbox"]');
                fields.push({
                    index: i,
                    labelText: lbl.innerText.trim(),
                    type: t ? 'text' : (ta ? 'textarea' : (sel ? 'select' : (radios.length ? 'radio' : (cb ? 'checkbox' : 'unknown')))),
                    elementId: t?.id || ta?.id || sel?.id || cb?.id || null,
                    radios,
                    currentValue: t?.value || ta?.value || sel?.value || (cb ? cb.checked : null),
                });
            });
            return fields;
        }""")

    def _match_pii(self, label_lower: str, user_config: dict, structured: dict) -> str:
        """Match label to PII value from user config or structured resume."""
        name_parts = (structured.get("name", "") or user_config.get("name", "")).split()
        first = name_parts[0] if name_parts else user_config.get("first_name", "")
        last = name_parts[-1] if len(name_parts) > 1 else user_config.get("last_name", "")

        pii_map = [
            (["first name", "given name"], first),
            (["last name", "surname", "family name"], last),
            (["email"], user_config.get("email", "") or structured.get("email", "")),
            (["phone", "mobile", "telephone"], user_config.get("phone", "") or structured.get("phone", "")),
            (["city", "location"], user_config.get("city", "")),
        ]
        for keywords, value in pii_map:
            if value and any(kw in label_lower for kw in keywords):
                return value
        return ""

    def _get_name(self, user_config: dict, structured: dict) -> str:
        name = structured.get("name", "") or ""
        if not name:
            first = user_config.get("first_name", "")
            last = user_config.get("last_name", "")
            name = f"{first} {last}".strip()
        return name

    @staticmethod
    def _hash_question(question: str) -> str:
        import hashlib, re
        normalized = re.sub(r"[^\w\s]", "", question.lower().strip())
        return hashlib.md5(normalized.encode()).hexdigest()

    async def _emit_broadcast_event(self, event_type: str, data: dict) -> None:
        """Broadcast a structured WebSocket event."""
        if self.broadcast:
            try:
                # The server's broadcast_json is passed as broadcast fn
                # We encode the event as a log message with JSON prefix for now
                import json as _json
                msg = _json.dumps({"__event": event_type, **data})
                await self.broadcast(f"__WS_EVENT__{msg}", "event")
            except Exception:
                pass

    async def cleanup(self, page, broadcast_fn=None) -> None:
        """Close Easy Apply modal if open."""
        try:
            close_btn = page.locator(
                "button[aria-label='Dismiss'], button[aria-label='Close'], .artdeco-modal__dismiss"
            )
            if await close_btn.count() > 0:
                await close_btn.first.click()
                await asyncio.sleep(1)
            discard_btn = page.get_by_role("button", name="Discard", exact=False)
            if await discard_btn.count() > 0:
                await discard_btn.first.click()
                await asyncio.sleep(1)
        except Exception:
            await super().cleanup(page, broadcast_fn)

    # ─── Legacy fill_form signature (used by existing dashboard) ──────────────

    async def fill_form_legacy(
        self, page, user_data: dict, resume_pdf_path: Path,
        resume_text: str, llm, broadcast, question_callback=None,
    ):
        """Legacy signature for backward compatibility with job_applier_dashboard.py."""
        # Build a minimal job_info dict
        job_info = {"user_data": user_data}
        self.broadcast = broadcast
        self.llm = llm

        # Set a simple question callback bridge
        result = await self._fill_form_with_callback(
            page, job_info, resume_pdf_path, resume_text, question_callback
        )
        return result

    async def _fill_form_with_callback(
        self, page, job_info: dict, resume_pdf_path: Path,
        resume_text: str, question_callback=None,
    ):
        """Fill form using legacy question_callback interface."""
        # Delegate to fill_form but wire up a legacy callback
        from ats_router import ApplyResult as LegacyResult

        result = await self.fill_form(page, job_info)

        # Map to legacy ApplyResult shape
        status_map = {
            ApplyStatus.SUCCESS: "applied",
            ApplyStatus.PENDING_REVIEW: "review",
            ApplyStatus.FAILED: "failed",
            ApplyStatus.HITL_REQUIRED: "manual_needed",
        }
        return LegacyResult(
            status=status_map.get(result.status, "failed"),
            ats_type=self.ats_type,
            message=result.message,
        )
