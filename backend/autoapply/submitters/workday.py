"""Workday (myworkdayjobs.com): every employer is its own tenant with its own candidate account, then
a wizard (My Information -> My Experience -> Application Questions -> Self Identify -> Review).

Account creation is a real side effect on the employer's system, so a dry run never creates one:
with no stored login it stops and says so. A tenant that wants the email verified parks the item;
after you click the link, the saved login is used on the retry.

Selectors lean on Workday's stable data-automation-id attributes; the field filling itself is the
shared planner."""
import asyncio
import re
from urllib.parse import urlparse

from backend.autoapply.submitters.base import BaseSubmitter, NeedsHuman, Outcome, _pause

MAX_STEPS = 10


def aid(name: str) -> str:
    return f'[data-automation-id="{name}"]'


class Workday(BaseSubmitter):
    name = "workday"
    success_text = re.compile(r"application submitted|thank(s| you) for applying|your application has been (submitted|received)|congratulations", re.I)
    submit_text = re.compile(r"^\s*submit\s*$", re.I)
    verify_text = re.compile(r"verify your (email|account)|verification email|check your (email|inbox)|confirm your email", re.I)

    async def _first_visible(self, page, selectors: str):
        loc = page.locator(selectors)
        for i in range(await loc.count()):
            if await loc.nth(i).is_visible():
                return loc.nth(i)
        return None

    async def _click(self, loc):
        # Workday overlays a click-filter on its main buttons; a forced click goes to the real one.
        try:
            await loc.click(timeout=5000)
        except Exception:
            await loc.click(force=True, timeout=5000)

    async def _start(self, page, out):
        btn = await self._first_visible(page, f'{aid("adventureButton")}, {aid("applyButton")}')
        if not btn:
            by_name = page.get_by_role("link", name=re.compile(r"^\s*apply\s*$", re.I))
            if not await by_name.count():
                by_name = page.get_by_role("button", name=re.compile(r"^\s*apply\s*$", re.I))
            if await by_name.count():
                btn = by_name.first
        if btn:
            await self._click(btn)
            out.steps.append("clicked Apply")
            await _pause(1.5, 3.0)
        manual = await self._first_visible(page, aid("applyManually"))
        if manual:
            await self._click(manual)
            out.steps.append("chose Apply Manually")
            await _pause(1.5, 3.0)

    async def _on_auth(self, page) -> bool:
        return bool(await self._first_visible(page, f'{aid("signInContent")}, {aid("createAccountSubmitButton")}, '
                                                     f'{aid("signInSubmitButton")}'))

    async def _wait_wizard_or_auth(self, page):
        sel = (f'{aid("signInContent")}, {aid("createAccountSubmitButton")}, {aid("signInSubmitButton")}, '
               f'{aid("bottom-navigation-next-button")}')
        await page.wait_for_selector(sel, timeout=30000)

    async def _auth(self, page, packet, out):
        host = urlparse(page.url).hostname or ""
        email = (packet.ctx.answers or {}).get("email") if packet.ctx else None
        cred = packet.get_login(host) if packet.get_login else None
        creating = bool(await self._first_visible(page, aid("createAccountSubmitButton")))
        if cred:
            if creating:
                link = await self._first_visible(page, aid("signInLink"))
                if link:
                    await self._click(link)
                    await _pause(1.0, 2.0)
            await page.locator(aid("email")).first.fill(cred[0])
            await page.locator(aid("password")).first.fill(cred[1])
            btn = await self._first_visible(page, aid("signInSubmitButton"))
            if not btn:
                raise RuntimeError("no Sign In button")
            out.steps.append(f"signing in to {host}")
            await self._click(btn)
        else:
            if packet.dry_run:
                out.status = "dry_run_ok"
                out.steps.append(f"dry run: no login stored for {host}; would create an account here")
                return False
            if not email:
                raise NeedsHuman("no email in the persona to create a Workday account with")
            if not creating:
                link = await self._first_visible(page, aid("createAccountLink"))
                if link:
                    await self._click(link)
                    await _pause(1.0, 2.0)
            from backend.autoapply.credentials import new_password
            pw = new_password()
            await page.locator(aid("email")).first.fill(email)
            await page.locator(aid("password")).first.fill(pw)
            verify = page.locator(aid("verifyPassword"))
            if await verify.count():
                await verify.first.fill(pw)
            box = await self._first_visible(page, aid("createAccountCheckbox"))
            if box and not await box.is_checked():
                await box.check(force=True)
            btn = await self._first_visible(page, aid("createAccountSubmitButton"))
            if not btn:
                raise RuntimeError("no Create Account button")
            # Stored before the click: if the account gets created and the run dies, the password is not lost.
            if packet.save_login:
                packet.save_login(host, email, pw)
            out.steps.append(f"creating account on {host}")
            await self._click(btn)
        await _pause(2.0, 4.0)
        text = await page.evaluate("() => document.body.innerText")
        if self.verify_text.search(text or ""):
            raise NeedsHuman(f"{host} sent a verification email to {email or cred[0]}; confirm it, then retry")
        err = await self._first_visible(page, f'{aid("errorMessage")}, {aid("errorBanner")}')
        if err:
            raise NeedsHuman(f"Workday login failed on {host}: {(await err.inner_text()).strip()[:200]}")
        await self._wait_wizard_or_auth(page)
        if await self._first_visible(page, aid("signInSubmitButton")) and not await self._first_visible(page, aid("bottom-navigation-next-button")):
            raise NeedsHuman(f"still on the sign-in page of {host}")
        return True

    async def _step_errors(self, page) -> list:
        return await page.evaluate("""(sel) => Array.from(document.querySelectorAll(sel))
            .filter(e => e.getClientRects().length).map(e => e.innerText.trim()).filter(Boolean)""",
                                   f'{aid("errorBanner")}, {aid("errorMessage")}, [role="alert"]')

    async def run(self, page, packet, planner) -> Outcome:
        out = Outcome()
        try:
            await self.open_form(page, packet, out)
            from backend.scraper._shared.bot_wall import is_bot_wall
            if is_bot_wall(await page.content()):
                raise NeedsHuman("bot wall")
            await self._start(page, out)
            await self._wait_wizard_or_auth(page)
            if await self._on_auth(page) and not await self._first_visible(page, aid("bottom-navigation-next-button")):
                if not await self._auth(page, packet, out):
                    await self.shot(page, packet, out, "auth")
                    return out
            for step in range(MAX_STEPS):
                st = await self.state(page)
                if st.get("challenge") or st.get("captchaWidget"):
                    raise NeedsHuman("captcha in the application")
                conf = await self.confirmed(page)
                if conf:
                    out.confirmation_text, out.status = conf[:500], "submitted"
                    await self.shot(page, packet, out, "confirmation")
                    return out
                missing = await self.fill(page, packet, out, planner)
                await self.shot(page, packet, out, f"step{step + 1}")
                if missing:
                    out.questions = missing
                    raise NeedsHuman(f"{len(missing)} required field(s) unanswered")
                nav = await self._first_visible(page, aid("bottom-navigation-next-button"))
                if not nav:
                    raise RuntimeError(f"no Continue button on step {step + 1}")
                label = (await nav.inner_text() or "").strip()
                if self.submit_text.search(label):
                    if packet.dry_run:
                        out.status = "dry_run_ok"
                        return out
                    await _pause(1.5, 3.5)
                    await self._click(nav)
                    out.steps.append("clicked submit")
                    for _ in range(30):
                        await asyncio.sleep(1)
                        conf = await self.confirmed(page)
                        if conf:
                            out.confirmation_text, out.status = conf[:500], "submitted"
                            await self.shot(page, packet, out, "confirmation")
                            return out
                    errs = await self._step_errors(page)
                    raise RuntimeError("no confirmation after submit" + (f": {'; '.join(errs[:3])}" if errs else ""))
                await _pause(1.0, 2.5)
                await self._click(nav)
                out.steps.append(f"continue (step {step + 1}: {label!r})")
                await _pause(1.5, 3.0)
                errs = await self._step_errors(page)
                if errs:
                    out.questions = [{"question": e[:300], "field_type": "text", "options": []} for e in errs[:5]]
                    raise NeedsHuman("Workday rejected a step: " + "; ".join(errs[:3]))
            raise RuntimeError(f"gave up after {MAX_STEPS} steps")
        except NeedsHuman as e:
            out.status, out.error = "needs_human", str(e)
            await self.shot(page, packet, out, "needs-human")
        except Exception as e:
            out.status, out.error = "failed", str(e)[:1000]
            await self.shot(page, packet, out, "failed")
        return out
