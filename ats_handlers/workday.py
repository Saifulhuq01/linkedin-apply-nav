"""
Workday ATS handler for Apply-Nav.

Semi-automated form filling for Workday career portals.
Workday uses shadow DOM components, so this handler uses a
combination of standard DOM traversal and JavaScript evaluation
to interact with form elements.

Strategy:
1. Navigate to the Workday job page
2. Detect common form patterns (shadow DOM traversal)
3. Auto-fill: name, email, phone, resume upload
4. Pause at: account creation, CAPTCHAs, complex multi-page flows
5. Use LLM to map unusual field labels to resume data
6. Fall back to HITL if DOM is unrecognized
"""

import asyncio
import logging
from pathlib import Path
from typing import Any, Callable, Dict, Optional

from ats_handlers.base import BaseATSHandler, BroadcastFn
from ats_router import ApplyResult

logger = logging.getLogger("apply_nav.ats.workday")


class WorkdayHandler(BaseATSHandler):
    """Semi-automated Workday application handler."""

    ats_type = "workday"

    async def can_handle(self, page: Any) -> bool:
        """Check if the page is a Workday application form."""
        url = page.url.lower()
        if "myworkdayjobs.com" in url or "workday.com" in url:
            return True

        # Check for Workday-specific DOM markers
        has_workday = await page.evaluate("""() => {
            return !!(
                document.querySelector('[data-automation-id]') ||
                document.querySelector('[data-uxi-widget-type]') ||
                document.querySelector('.css-1q2dra3') ||
                document.title.toLowerCase().includes('workday')
            );
        }""")
        return has_workday

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
        """Semi-automated Workday form filling.
        
        Workday forms are highly variable per employer. This handler:
        1. Detects the "Apply" button and clicks it
        2. Handles "Sign In" / "Create Account" gates
        3. Fills identifiable fields (name, email, phone)
        4. Uploads resume
        5. Pauses for HITL on unknown fields
        """
        await broadcast("Workday application detected. Starting semi-automated flow...", "info")
        await asyncio.sleep(2)

        # Step 1: Find and click the Apply button
        apply_clicked = await self._click_apply_button(page)
        if not apply_clicked:
            await broadcast("Could not find Workday Apply button. Manual navigation may be needed.", "warning")

        await asyncio.sleep(3)

        # Step 2: Check for Sign In / Create Account gates
        needs_auth = await self._check_auth_gate(page)
        if needs_auth:
            await broadcast(
                "⚠️ Workday requires account sign-in or creation. "
                "Please sign in manually in the browser, then the automation will continue.",
                "warning"
            )
            # Wait for user to handle auth (up to 5 minutes)
            for _ in range(60):
                await asyncio.sleep(5)
                still_auth = await self._check_auth_gate(page)
                if not still_auth:
                    await broadcast("Auth gate cleared! Continuing automation...", "success")
                    break
            else:
                return ApplyResult(
                    status="manual_needed",
                    ats_type="workday",
                    message="Workday authentication gate was not resolved within 5 minutes.",
                )

        await asyncio.sleep(2)

        # Step 3: Extract and fill form fields
        await broadcast("Scanning Workday form fields...", "info")

        try:
            fields = await self._extract_workday_fields(page)
        except Exception as e:
            await broadcast(f"⚠️ Failed to scan Workday fields: {e}. Falling back to HITL mode...", "warning")
            return ApplyResult(
                status="manual_needed",
                ats_type="workday",
                message=f"Workday scan failed: {e}. Fallback to manual fill.",
            )

        filled_count = 0

        for field in fields:
            label = field.get("label", "").lower()
            field_id = field.get("id", "")
            field_type = field.get("type", "text")

            # Auto-fill known fields
            value = self._match_workday_field(label, user_data)
            if value and field_id:
                success = await self._fill_workday_field(page, field_id, field_type, value)
                if success:
                    await broadcast(f"✓ {field.get('label', 'Field')} → {value}", "success")
                    filled_count += 1
                    await asyncio.sleep(0.5)
                continue

        # Step 4: Resume upload with retry
        resume_uploaded = False
        for attempt in range(1, 3):
            try:
                resume_uploaded = await self._upload_workday_resume(page, resume_pdf_path)
                if resume_uploaded:
                    await broadcast("✓ Resume uploaded to Workday", "success")
                    break
            except Exception as re:
                await broadcast(f"Resume upload attempt {attempt} failed: {re}", "warning")
                await asyncio.sleep(2)

        await broadcast(
            f"Auto-filled {filled_count} fields. "
            f"Please review and complete remaining fields in the browser.",
            "warning"
        )

        return ApplyResult(
            status="manual_needed",
            ats_type="workday",
            message=(
                f"Workday form partially automated: {filled_count} fields auto-filled, "
                f"resume {'uploaded' if resume_uploaded else 'not uploaded'}. "
                f"Please complete remaining fields and submit manually."
            ),
        )

    # ─── Private Helpers ───

    async def _click_apply_button(self, page: Any) -> bool:
        """Find and click the Workday Apply button."""
        selectors = [
            "a[data-automation-id='jobPostingApplyButton']",
            "button[data-automation-id='jobPostingApplyButton']",
            "a:has-text('Apply')",
            "button:has-text('Apply')",
        ]
        for sel in selectors:
            if await self._safe_click(page, sel):
                return True
        return False

    async def _check_auth_gate(self, page: Any) -> bool:
        """Check if Workday is showing a sign-in or create-account page."""
        return await page.evaluate("""() => {
            const text = document.body?.innerText?.toLowerCase() || '';
            return (
                text.includes('sign in') ||
                text.includes('create account') ||
                text.includes('log in to apply') ||
                !!document.querySelector('[data-automation-id="signInLink"]') ||
                !!document.querySelector('[data-automation-id="createAccountLink"]')
            );
        }""")

    async def _extract_workday_fields(self, page: Any) -> list:
        """Extract form fields from a Workday application form."""
        return await page.evaluate("""() => {
            const fields = [];
            
            // Standard Workday data-automation-id fields
            document.querySelectorAll('[data-automation-id]').forEach(el => {
                const automationId = el.getAttribute('data-automation-id');
                if (!automationId) return;
                
                const input = el.querySelector('input, textarea, select');
                if (!input) return;
                
                const label = el.querySelector('label')?.innerText?.trim() ||
                              el.getAttribute('aria-label') ||
                              automationId;
                
                fields.push({
                    id: input.id || automationId,
                    label: label,
                    type: input.tagName.toLowerCase() === 'select' ? 'select' :
                          input.type || 'text',
                    automationId: automationId,
                    currentValue: input.value || ''
                });
            });
            
            // Fallback: scan all visible inputs
            if (fields.length === 0) {
                document.querySelectorAll('input:not([type="hidden"]), textarea, select').forEach(input => {
                    if (!input.offsetParent) return;
                    const label = document.querySelector(`label[for="${input.id}"]`)?.innerText?.trim() ||
                                  input.getAttribute('aria-label') ||
                                  input.getAttribute('placeholder') || '';
                    if (!label) return;
                    
                    fields.push({
                        id: input.id,
                        label: label,
                        type: input.tagName.toLowerCase() === 'select' ? 'select' :
                              input.type || 'text',
                        automationId: '',
                        currentValue: input.value || ''
                    });
                });
            }
            
            return fields;
        }""")

    def _match_workday_field(self, label: str, user_data: Dict[str, str]) -> Optional[str]:
        """Match Workday field labels to user data."""
        label_lower = label.lower()

        mappings = [
            (["first name", "given name", "legalnamesection_firstname"], user_data.get("first_name", "")),
            (["last name", "family name", "surname", "legalnamesection_lastname"], user_data.get("last_name", "")),
            (["email", "e-mail"], user_data.get("email", "")),
            (["phone", "mobile", "telephone"], user_data.get("phone", "")),
            (["city", "location", "address"], user_data.get("city", "")),
        ]

        for keywords, value in mappings:
            if value and any(kw in label_lower for kw in keywords):
                return value
        return None

    async def _fill_workday_field(self, page: Any, field_id: str, field_type: str, value: str) -> bool:
        """Fill a Workday form field with retry logic."""
        for attempt in range(1, 3):
            try:
                if field_type == "select":
                    await page.locator(f"#{field_id}").select_option(label=value)
                    return True
                else:
                    el = page.locator(f"#{field_id}")
                    if await el.count() > 0:
                        await el.fill(value)
                        return True

                    # Try data-automation-id
                    el = page.locator(f"[data-automation-id='{field_id}'] input")
                    if await el.count() > 0:
                        await el.first.fill(value)
                        return True
            except Exception as e:
                if attempt == 2:
                    logger.debug("Workday field fill failed for %s: %s", field_id, e)
                await asyncio.sleep(1)
        return False

    async def _upload_workday_resume(self, page: Any, resume_path: Path) -> bool:
        """Upload resume to Workday's file input."""
        if not resume_path.exists():
            return False

        try:
            # Workday uses data-automation-id for file inputs
            file_input = page.locator(
                "input[type='file'][data-automation-id*='resume'], "
                "input[type='file'][data-automation-id*='Resume'], "
                "input[type='file']"
            )
            if await file_input.count() > 0:
                await file_input.first.set_input_files(str(resume_path))
                await asyncio.sleep(3)
                return True
        except Exception as e:
            logger.debug("Workday resume upload failed: %s", e)
        return False
