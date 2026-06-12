"""
LinkedIn Easy Apply handler for Apply-Nav.

Refactored from the monolithic job_applier_dashboard.py into a
standalone handler class. Handles the 15-step Easy Apply form flow.
"""

import asyncio
import random
import logging
from pathlib import Path
from typing import Any, Callable, Dict, Optional

from ats_handlers.base import BaseATSHandler, BroadcastFn
from ats_router import ApplyResult

logger = logging.getLogger("apply_nav.ats.easy_apply")


class EasyApplyHandler(BaseATSHandler):
    """Handles LinkedIn Easy Apply form automation."""

    ats_type = "easy_apply"

    async def can_handle(self, page: Any) -> bool:
        """Check if the page has an Easy Apply button."""
        apply_info = await self._detect_apply_button(page)
        return apply_info is not None and apply_info["type"] in (
            "easy_apply_button",
            "easy_apply_link",
        )

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
        """Execute the LinkedIn Easy Apply multi-step form flow."""

        # Step 1: Detect and click the Easy Apply button
        apply_info = await self._detect_apply_button(page)
        if not apply_info:
            return ApplyResult(
                status="failed",
                ats_type="easy_apply",
                message="No Easy Apply button found. You may have already applied.",
            )

        if apply_info["type"] == "external_apply":
            return ApplyResult(
                status="failed",
                ats_type="easy_apply",
                message=f"External apply (not Easy Apply). URL: {apply_info.get('href', 'N/A')}",
                error="external_apply",
            )

        # Click Easy Apply
        if apply_info["type"] == "easy_apply_link":
            await broadcast("Found Easy Apply link — clicking...", "success")
            easy_link = page.locator("a[href*='openSDUIApplyFlow=true']")
            await easy_link.first.click()
        else:
            await broadcast("Found Easy Apply button — clicking...", "success")
            easy_btn = page.locator("button:has-text('Easy Apply')")
            await easy_btn.first.click()

        await asyncio.sleep(3.0)

        # Wait for modal/dialog
        modal = page.locator("[role='dialog'], .jobs-easy-apply-modal, .artdeco-modal")
        try:
            await modal.wait_for(state="visible", timeout=10000)
            await broadcast("Easy Apply form opened!", "success")
        except Exception:
            if "apply" in page.url:
                await broadcast("Apply form opened in new view.", "success")
                modal = page.locator("main, [role='main'], form")
            else:
                return ApplyResult(
                    status="failed",
                    ats_type="easy_apply",
                    message="Apply form didn't appear. Check browser.",
                )

        # Step 2: Multi-page form loop (15 steps max)
        for step in range(1, 16):
            if await modal.count() == 0:
                await broadcast("Form closed.", "info")
                break

            max_retries = 2
            step_success = False

            for attempt in range(1, max_retries + 1):
                try:
                    submit_btn = page.get_by_role("button", name="Submit application", exact=False)
                    review_btn = page.get_by_role("button", name="Review", exact=False)
                    next_btn = page.get_by_role("button", name="Next", exact=False)

                    # Extract form fields
                    fields = await self._extract_form_fields(page)

                    # Handle resume upload
                    await self._upload_resume(page, resume_pdf_path, broadcast)

                    if fields:
                        await broadcast(f"Step {step} (Attempt {attempt}): {len(fields)} fields", "info")

                        for field in fields:
                            lbl = field["labelText"].lower()

                            # Skip pre-filled fields
                            if field["currentValue"] and field["type"] in ["text", "textarea"]:
                                continue

                            # Auto-fill known PII fields from config
                            val = self._match_pii_field(lbl, user_data)
                            if val:
                                if field["elementId"]:
                                    await page.locator(f"#{field['elementId']}").fill(val)
                                await broadcast(f"✓ {field['labelText']} → {val}", "success")
                                continue

                            # Unknown field — use HITL with AI suggestion
                            if question_callback:
                                suggested = ""
                                if llm:
                                    opts = [r["text"] for r in field.get("radios", [])]
                                    try:
                                        suggested = await llm.answer_screening_question(
                                            field["labelText"], field["type"], opts, resume_text
                                        )
                                        if suggested:
                                            await broadcast(f"AI suggested: '{suggested}'", "system")
                                    except Exception as e:
                                        logger.debug("AI suggestion failed: %s", e)

                                ans = await question_callback(field, suggested)
                                if ans == "__CANCEL__":
                                    return ApplyResult(
                                        status="cancelled",
                                        ats_type="easy_apply",
                                        message="Cancelled by user.",
                                    )
                                if ans == "__SKIP__":
                                    continue

                                # Fill the answer
                                await self._fill_field(page, field, ans)
                                await broadcast(f"✓ Filled: {ans}", "success")
                                await asyncio.sleep(random.uniform(0.5, 1.2))

                    await asyncio.sleep(1.5)

                    # Navigate to next step
                    if await submit_btn.count() > 0 and await submit_btn.first.is_visible():
                        # Final submit — pause for HITL review
                        await broadcast("Ready to submit! Confirm in dashboard.", "warning")
                        return ApplyResult(
                            status="review",
                            ats_type="easy_apply",
                            message="Application ready for final submission. Awaiting user confirmation.",
                        )
                    elif await review_btn.count() > 0 and await review_btn.first.is_visible():
                        await review_btn.first.click()
                    elif await next_btn.count() > 0 and await next_btn.first.is_visible():
                        await next_btn.first.click()
                    else:
                        await broadcast("No navigation button found. Check browser.", "warning")
                        return ApplyResult(
                            status="manual_needed",
                            ats_type="easy_apply",
                            message="Form navigation unclear. Please check the browser.",
                        )

                    step_success = True
                    break  # Success, exit retry loop and go to next step

                except Exception as ex:
                    await broadcast(f"Step {step} error (Attempt {attempt}/{max_retries}): {ex}", "warning")
                    if attempt < max_retries:
                        await asyncio.sleep(random.uniform(3.0, 5.0))
                        # Refresh/re-locate modal if lost
                        if await modal.count() == 0:
                            await broadcast("Modal lost during error recovery.", "warning")
                            break
                    else:
                        # Unrecoverable error on this step
                        await broadcast(f"Step {step} failed after {max_retries} attempts. Saving state.", "error")
                        return ApplyResult(
                            status="interrupted",
                            ats_type="easy_apply",
                            message=f"Interrupted at step {step}: {ex}",
                            error=str(ex)
                        )

            if not step_success:
                return ApplyResult(
                    status="failed",
                    ats_type="easy_apply",
                    message=f"Failed to complete step {step}.",
                )

            await asyncio.sleep(random.uniform(1.5, 3.0))

        return ApplyResult(
            status="manual_needed",
            ats_type="easy_apply",
            message="Form loop completed without clear submit. Check browser.",
        )

    async def cleanup(self, page: Any, broadcast: BroadcastFn) -> None:
        """Close any open Easy Apply modal."""
        try:
            # Try clicking dismiss/close button
            close_btn = page.locator(
                "button[aria-label='Dismiss'], "
                "button[aria-label='Close'], "
                ".artdeco-modal__dismiss"
            )
            if await close_btn.count() > 0:
                await close_btn.first.click()
                await asyncio.sleep(1)

            # Dismiss any "discard" confirmation
            discard_btn = page.get_by_role("button", name="Discard", exact=False)
            if await discard_btn.count() > 0:
                await discard_btn.first.click()
                await asyncio.sleep(1)
        except Exception:
            # Last resort: Escape key
            await super().cleanup(page, broadcast)

    # ─── Private Helpers ───

    async def _detect_apply_button(self, page: Any) -> Optional[Dict]:
        """Detect the type of apply button on the job page."""
        return await page.evaluate("""() => {
            const allLinks = Array.from(document.querySelectorAll('a'));
            const easyApplyLink = allLinks.find(a =>
                a.href && a.href.includes('openSDUIApplyFlow=true')
            );
            if (easyApplyLink) {
                return { type: 'easy_apply_link', text: easyApplyLink.innerText.trim(), href: easyApplyLink.href };
            }
            const allButtons = Array.from(document.querySelectorAll('button'));
            const easyApplyBtn = allButtons.find(b =>
                b.innerText.toLowerCase().includes('easy apply') && b.offsetParent !== null
            );
            if (easyApplyBtn) {
                return { type: 'easy_apply_button', text: easyApplyBtn.innerText.trim() };
            }
            const applyLink = allLinks.find(a => {
                const text = a.innerText?.trim().toLowerCase() || '';
                return text === 'apply' && a.offsetParent !== null;
            });
            if (applyLink) {
                return { type: 'external_apply', text: applyLink.innerText.trim(), href: applyLink.href };
            }
            return null;
        }""")

    async def _extract_form_fields(self, page: Any) -> list:
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
                    id: r.id
                }));
                const cb = el.querySelector('input[type="checkbox"]');
                fields.push({
                    index: i,
                    labelText: lbl.innerText.trim(),
                    type: t ? 'text' : (ta ? 'textarea' : (sel ? 'select' : (radios.length ? 'radio' : (cb ? 'checkbox' : 'unknown')))),
                    elementId: t?.id || ta?.id || sel?.id || cb?.id || null,
                    radios,
                    currentValue: t?.value || ta?.value || sel?.value || (cb ? cb.checked : null)
                });
            });
            return fields;
        }""")

    def _match_pii_field(self, label: str, user_data: Dict[str, str]) -> Optional[str]:
        """Match a form field label to a PII value from user config."""
        label_lower = label.lower()

        pii_map = [
            (["first name"], user_data.get("first_name", "")),
            (["last name", "surname", "family name"], user_data.get("last_name", "")),
            (["email"], user_data.get("email", "")),
            (["phone", "mobile", "telephone"], user_data.get("phone", "")),
            (["city", "location"], user_data.get("city", "")),
        ]

        for keywords, value in pii_map:
            if value and any(kw in label_lower for kw in keywords):
                return value

        return None

    async def _fill_field(self, page: Any, field: Dict, answer: str) -> None:
        """Fill a form field with the given answer."""
        if field["type"] in ["text", "textarea"] and field["elementId"]:
            await page.locator(f"#{field['elementId']}").fill(answer)
        elif field["type"] == "select" and field["elementId"]:
            await page.locator(f"#{field['elementId']}").select_option(label=answer)
        elif field["type"] == "radio":
            rid = None
            for r in field.get("radios", []):
                if r["text"].lower() == answer.lower() or answer.lower() in r["text"].lower():
                    rid = r["id"]
                    break
            if not rid and field.get("radios"):
                rid = field["radios"][0]["id"]
            if rid:
                await page.locator(f"label[for='{rid}']").click()
        elif field["type"] == "checkbox" and field["elementId"]:
            if any(k in answer.lower() for k in ["yes", "true"]):
                await page.locator(f"#{field['elementId']}").check()
