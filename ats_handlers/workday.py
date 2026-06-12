"""
ats_handlers/workday.py — Workday iframe + shadow DOM handler.
"""

import asyncio
import random
import logging
from typing import Any

from ats_handlers.base import BaseATSHandler, ApplyResult, ApplyStatus

logger = logging.getLogger("apply_nav.ats.workday")


class WorkdayHandler(BaseATSHandler):
    """Handles Workday ATS form automation."""

    ats_type = "workday"

    async def fill_form(self, page, job_info: dict) -> ApplyResult:
        """Navigate to Workday apply and fill form pages."""

        # ── 1. Find and click Apply button ────────────────────
        apply_locator = page.locator(
            "[data-automation-id='applyButton'], [data-automation-id='apply-now-button']"
        )
        try:
            await apply_locator.wait_for(state="visible", timeout=10000)
        except Exception:
            # Fallback: ARIA role
            apply_locator = page.get_by_role("button", name="Apply")
            if await apply_locator.count() == 0:
                return ApplyResult(ApplyStatus.FAILED, "No Workday apply button found")

        await self._emit("Found Workday apply button — clicking...", "info")
        await self._mouse_jitter(page)
        await self._random_delay(1.5, 3.0)
        await apply_locator.first.click()
        await asyncio.sleep(3.0)

        # ── 2. Check for login wall ────────────────────────────
        if "login" in page.url.lower() or "signin" in page.url.lower():
            is_login_wall = await page.locator("[data-automation-id='signIn']").count() > 0
            if is_login_wall:
                await self._emit("Workday login required — please sign in manually.", "error")
                return ApplyResult(ApplyStatus.FAILED, "Workday login required")

        # ── 3. Check for iframe (some portals wrap in iframe) ──
        iframe_locator = page.frame_locator("iframe[src*='workday']")
        try:
            iframe_count = await page.locator("iframe[src*='workday']").count()
            use_iframe = iframe_count > 0
        except Exception:
            use_iframe = False

        ctx = iframe_locator if use_iframe else page

        # ── 4. Multi-page form loop (up to 20 iterations) ─────
        for step in range(1, 21):
            await asyncio.sleep(2.0)
            await self._emit(f"Workday step {step}...", "info")

            # Detect current step label
            step_label = ""
            try:
                step_el = page.locator("[data-automation-id='currentStep']")
                if await step_el.count() > 0:
                    step_label = await step_el.first.inner_text()
                    await self._emit(f"  Step: {step_label}", "info")
            except Exception:
                pass

            # Fill text inputs
            n_filled = await self._fill_workday_inputs(page, ctx, job_info)
            await self._emit(f"  Filled {n_filled} text inputs", "info")

            # Upload resume
            resume_path = None
            if self.resume:
                try:
                    from resume_manager import get_resume_pdf_path
                    resume_path = get_resume_pdf_path()
                except Exception:
                    pass
            if resume_path:
                await self._upload_resume(page, resume_path, self.broadcast)

            # Handle select/dropdowns
            await self._fill_workday_selects(page, ctx, job_info)

            await asyncio.sleep(1.0)

            # Check for Next button
            next_btn = page.locator("[data-automation-id='bottom-navigation-next-btn']")
            if await next_btn.count() == 0:
                next_btn = page.get_by_role("button", name="Next")

            review_btn = page.locator("[data-automation-id='bottom-navigation-review-btn']")
            submit_btn = page.locator("[data-automation-id='bottom-navigation-finish-btn']")

            if await submit_btn.count() > 0 or await review_btn.count() > 0:
                await self._emit("✋ Workday review/submit step — awaiting user confirmation.", "warning")
                return ApplyResult(ApplyStatus.PENDING_REVIEW, "Workday form complete — awaiting user confirmation")

            if await next_btn.count() > 0 and await next_btn.first.is_visible():
                await self._mouse_jitter(page)
                await self._random_delay(1.5, 3.0)
                await next_btn.first.click()
            else:
                await self._emit("No Workday navigation button found — opening for manual review.", "warning")
                return ApplyResult(ApplyStatus.PENDING_REVIEW, "Workday: no next button found — manual review required")

        return ApplyResult(ApplyStatus.PENDING_REVIEW, "Workday: max steps reached — manual review")

    async def _fill_workday_inputs(self, page, ctx, job_info: dict) -> int:
        """Fill text inputs on the current Workday step."""
        filled = 0
        user_data = job_info.get("user_data", {})
        structured = {}
        if self.resume:
            try:
                structured = self.resume.get_structured()
            except Exception:
                pass

        # Find Workday text inputs by data-automation-id pattern
        try:
            inputs = page.locator("[data-automation-id$='-input'] input, [data-automation-id$='-formField'] input")
            count = await inputs.count()

            for i in range(count):
                el = inputs.nth(i)
                try:
                    if not await el.is_visible():
                        continue
                    # Find label
                    label = ""
                    try:
                        parent = page.locator("[data-automation-id$='-formField']").nth(i)
                        label_el = parent.locator("label")
                        if await label_el.count() > 0:
                            label = await label_el.first.inner_text()
                    except Exception:
                        pass

                    current = await el.input_value()
                    if current:
                        continue

                    val = self._match_pii_workday(label, user_data, structured)
                    if val:
                        await self._type_humanlike(el, val)
                        filled += 1
                        await self._emit(f"✓ {label or 'field'} → {val}", "success")
                    elif self.llm and label:
                        try:
                            structured_data = structured or {}
                            answer = await self.llm.answer_question(label, [], structured_data)
                            if answer:
                                await self._type_humanlike(el, answer)
                                filled += 1
                        except Exception:
                            pass
                except Exception:
                    pass
        except Exception as e:
            logger.debug("Workday input fill error: %s", e)

        return filled

    async def _fill_workday_selects(self, page, ctx, job_info: dict) -> None:
        """Fill select/dropdown fields on Workday."""
        structured = {}
        if self.resume:
            try:
                structured = self.resume.get_structured()
            except Exception:
                pass
        user_data = job_info.get("user_data", {})

        try:
            selects = page.locator("[data-automation-id$='-selectWidget']")
            count = await selects.count()
            for i in range(count):
                sel = selects.nth(i)
                try:
                    if not await sel.is_visible():
                        continue
                    # Find label text
                    label = ""
                    try:
                        parent = sel.locator("xpath=ancestor::*[@data-automation-id][1]")
                        label_el = parent.locator("label")
                        if await label_el.count() > 0:
                            label = await label_el.first.inner_text()
                    except Exception:
                        pass

                    # Get AI answer for the dropdown
                    if self.llm and label:
                        try:
                            answer = await self.llm.answer_question(label, [], structured or {})
                            if answer:
                                # Click dropdown and find matching option
                                await sel.click()
                                await asyncio.sleep(0.5)
                                opt = page.get_by_role("option", name=answer)
                                if await opt.count() > 0:
                                    await opt.first.click()
                                else:
                                    await page.keyboard.press("Escape")
                        except Exception:
                            pass
                except Exception:
                    pass
        except Exception as e:
            logger.debug("Workday select fill error: %s", e)

    def _match_pii_workday(self, label: str, user_data: dict, structured: dict) -> str:
        """Match label to user PII data."""
        ll = label.lower()
        name_parts = (structured.get("name", "") or "").split()
        pii = [
            (["first name", "given name"], name_parts[0] if name_parts else user_data.get("first_name", "")),
            (["last name", "surname"], name_parts[-1] if len(name_parts) > 1 else user_data.get("last_name", "")),
            (["email"], user_data.get("email", "") or structured.get("email", "")),
            (["phone", "mobile"], user_data.get("phone", "") or structured.get("phone", "")),
            (["city", "location"], user_data.get("city", "")),
        ]
        for keywords, value in pii:
            if value and any(kw in ll for kw in keywords):
                return value
        return ""

    async def can_handle(self, page) -> bool:
        return (
            "myworkdayjobs.com" in page.url
            or "workday.com" in page.url
            or await page.locator("[data-automation-id='applyButton']").count() > 0
        )
