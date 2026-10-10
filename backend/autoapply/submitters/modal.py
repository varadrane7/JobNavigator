"""In-site apply modals driven inside the user's real Chrome: LinkedIn Easy Apply and ZipRecruiter.

Both sites forbid automation in their terms, so this path is deliberately slow and small (its own
caps and jitter), stops at the first checkpoint or "unusual activity" page and pauses the whole
site for a day, and never tries to get past a challenge."""
import asyncio
import re

from backend.autoapply.submitters.base import BaseSubmitter, NeedsHuman, Outcome, _pause
from backend.autoapply.submitters.registry import classify

MAX_STEPS = 12


class SitePaused(Exception):
    pass


class ModalFlow(BaseSubmitter):
    site = "modal"
    entry = re.compile(r"$^")             # opens the in-site flow
    one_click = None                      # an entry button that submits on click
    external = re.compile(r"$^")          # leaves for the company's own form
    modal = '[role="dialog"]'
    next_btn = re.compile(r"^\s*(next|continue|continue to next step|review|review your application)\s*$", re.I)
    submit_btn = re.compile(r"^\s*submit( application)?\s*$", re.I)
    already = re.compile(r"$^")
    checkpoint_url = re.compile(r"$^")
    checkpoint_text = re.compile(r"unusual activity|security (check|verification)|verify (that )?you('| a)re (a )?human|are you a robot", re.I)
    error_sel = '[role="alert"], [aria-invalid="true"]'

    @property
    def form_root(self):
        return self.modal

    async def _btn(self, scope, pattern):
        for role in ("button", "link"):
            loc = scope.get_by_role(role, name=pattern)
            for i in range(await loc.count()):
                if await loc.nth(i).is_visible():
                    return loc.nth(i)
        return None

    async def guard(self, page):
        if self.checkpoint_url.search(page.url):
            raise SitePaused(f"{self.site} checkpoint/login page: {page.url}")
        st = await self.state(page)
        if self.checkpoint_text.search(st.get("text") or "") or st.get("challenge"):
            raise SitePaused(f"{self.site} is showing a security check")
        return st

    async def discard(self, page):
        """Close the modal without sending (LinkedIn asks 'Save this application?' -> Discard)."""
        try:
            await page.keyboard.press("Escape")
            await asyncio.sleep(0.8)
            d = await self._btn(page, re.compile(r"^\s*(discard|don.t save|leave)\s*$", re.I))
            if d:
                await d.click()
        except Exception:
            pass

    async def before_step(self, page, dialog):
        """Site-specific tidy-up on each step (LinkedIn: untick 'Follow company')."""

    async def run(self, page, packet, planner) -> Outcome:
        out = Outcome()
        try:
            out.steps.append(f"open {packet.url}")
            await page.goto(packet.url, wait_until="domcontentloaded", timeout=45000)
            await _pause(2.0, 4.0)
            st = await self.guard(page)
            if self.already.search(st.get("text") or ""):
                out.status, out.error = "already", "already applied on the site"
                return out

            entry = await self._btn(page, self.entry)
            if not entry:
                ext = await self._btn(page, self.external)
                if not ext:
                    raise RuntimeError(f"no {self.site} apply button")
                before = len(page.context.pages)
                await ext.click()
                await _pause(2.0, 3.5)
                target = page.context.pages[-1] if len(page.context.pages) > before else page
                url = target.url
                if target is not page:
                    await target.close()
                if classify(url) in (None, self.site):
                    raise RuntimeError(f"external apply went to an unsupported page: {url}")
                out.status, out.redirect_url = "redirect", url
                out.steps.append(f"external apply -> {url}")
                return out

            label = (await entry.inner_text() or "").strip()
            if self.one_click and self.one_click.search(label):
                await self.shot(page, packet, out, "one-click")
                if packet.dry_run:
                    out.status = "dry_run_ok"
                    out.steps.append("one-click apply: dry run stops before clicking")
                    return out
            await _pause(1.0, 2.5)
            await entry.click()
            out.steps.append(f"clicked {label!r}")
            await _pause(1.5, 3.0)

            for step in range(MAX_STEPS):
                await self.guard(page)
                conf = await self.confirmed(page)
                if conf:   # one-click flows land here straight away
                    out.confirmation_text, out.status = conf[:500], "submitted"
                    await self.shot(page, packet, out, "confirmation")
                    return out
                dialog = page.locator(self.modal).first
                if not await dialog.count():
                    raise RuntimeError("apply dialog did not open")
                await self.before_step(page, dialog)
                missing = await self.fill(page, packet, out, planner)
                await self.shot(page, packet, out, f"step{step + 1}")
                if missing:
                    out.questions = missing
                    raise NeedsHuman(f"{len(missing)} required field(s) unanswered")
                submit = await self._btn(dialog, self.submit_btn)
                if submit:
                    if packet.dry_run:
                        out.status = "dry_run_ok"
                        await self.discard(page)
                        return out
                    await _pause(1.5, 3.5)
                    await submit.click()
                    out.steps.append("clicked submit")
                    for _ in range(20):
                        await asyncio.sleep(1)
                        conf = await self.confirmed(page)
                        if conf:
                            out.confirmation_text, out.status = conf[:500], "submitted"
                            await self.shot(page, packet, out, "confirmation")
                            await page.keyboard.press("Escape")
                            return out
                    raise RuntimeError("no confirmation after submit")
                nxt = await self._btn(dialog, self.next_btn)
                if not nxt:
                    raise RuntimeError(f"no Next/Review/Submit on step {step + 1}")
                await _pause(1.0, 2.5)
                await nxt.click()
                out.steps.append(f"next (step {step + 1})")
                await _pause(1.5, 3.0)
                errs = await dialog.evaluate(
                    """(d, sel) => Array.from(d.querySelectorAll(sel))
                        .filter(e => e.getClientRects().length && getComputedStyle(e).visibility !== 'hidden')
                        .map(e => e.innerText.trim()).filter(Boolean)""", self.error_sel)
                if errs:
                    # The site refused the step: the fields it complains about need a person.
                    out.questions = [{"question": e.strip()[:300], "field_type": "text", "options": []} for e in errs[:5]]
                    raise NeedsHuman("the form rejected a step: " + "; ".join(e.strip() for e in errs[:3]))
            raise RuntimeError(f"gave up after {MAX_STEPS} steps")
        except SitePaused as e:
            out.status, out.error, out.pause_site = "needs_human", str(e), self.site
            await self.shot(page, packet, out, "checkpoint")
        except NeedsHuman as e:
            out.status, out.error = "needs_human", str(e)
            await self.shot(page, packet, out, "needs-human")
            await self.discard(page)
        except Exception as e:
            out.status, out.error = "failed", str(e)[:1000]
            await self.shot(page, packet, out, "failed")
            await self.discard(page)
        return out


class LinkedInEasyApply(ModalFlow):
    name = site = "linkedin"
    entry = re.compile(r"easy apply", re.I)
    external = re.compile(r"^\s*apply(\s+on\s+company\s+(website|site))?\b(?!.*easy)", re.I)
    modal = '.jobs-easy-apply-modal, div[role="dialog"]'
    submit_btn = re.compile(r"^\s*submit application\s*$", re.I)
    success_text = re.compile(r"(your )?application (was )?sent|you applied", re.I)
    already = re.compile(r"\bapplied (\d+|a|an) \w+ ago\b|application submitted\b", re.I)
    checkpoint_url = re.compile(r"linkedin\.com/(checkpoint|authwall|login|uas/login)", re.I)
    error_sel = '.artdeco-inline-feedback--error, [role="alert"]'

    async def before_step(self, page, dialog):
        # Applying should not also follow the company.
        try:
            await page.evaluate("""() => { const c = document.querySelector('#follow-company-checkbox');
                if (c && c.checked) { const l = document.querySelector('label[for="follow-company-checkbox"]'); (l || c).click(); } }""")
        except Exception:
            pass


class ZipRecruiter(ModalFlow):
    name = site = "ziprecruiter"
    entry = re.compile(r"^\s*(1-click apply|quick apply|apply now|apply)\s*$", re.I)
    one_click = re.compile(r"1-click|quick apply", re.I)
    external = re.compile(r"apply on company site|continue to (company|employer) site", re.I)
    success_text = re.compile(r"application (submitted|sent|was sent)|you applied|applied!|successfully applied", re.I)
    already = re.compile(r"\byou applied\b|\balready applied\b", re.I)
    checkpoint_url = re.compile(r"ziprecruiter\.com/(login|authn|captcha)", re.I)
