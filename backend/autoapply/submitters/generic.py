"""Any other careers site: find the Apply entry point, then walk a multi-step form (fill, Next,
fill, ..., Submit) with the same field planner. Lands on a known ATS -> hands the URL back so the
runner re-routes the item. Account walls and captchas park as needs_human."""
import re

from backend.autoapply.submitters.base import BaseSubmitter, NeedsHuman, Outcome, _pause, NEXT_TEXT
from backend.autoapply.submitters.registry import classify

APPLY = re.compile(r"^\s*(apply( now| for this (job|position|role))?|start (your )?application|i'?m interested)\s*$", re.I)
NEXT = NEXT_TEXT
MAX_STEPS = 8


class Generic(BaseSubmitter):
    name = "generic"

    async def _visible(self, page, pattern):
        for role in ("button", "link"):
            loc = page.get_by_role(role, name=pattern)
            for i in range(await loc.count()):
                if await loc.nth(i).is_visible():
                    return loc.nth(i)
        return None

    async def _field_count(self, page) -> int:
        await self.inject(page)
        fields = await page.evaluate("() => __aa.discover()")
        return sum(1 for f in fields if f["kind"] not in ("file",))

    async def run(self, page, packet, planner) -> Outcome:
        out = Outcome()
        try:
            await self.open_form(page, packet, out)
            if await self._field_count(page) < 3:
                btn = await self._visible(page, APPLY)
                if not btn:
                    raise RuntimeError("no application form or Apply button found")
                out.steps.append("clicked Apply")
                pages_before = len(page.context.pages)
                await btn.click()
                await _pause(2.0, 3.5)
                if len(page.context.pages) > pages_before:
                    page = page.context.pages[-1]
                    await page.wait_for_load_state("domcontentloaded")
            sub = classify(page.url)
            if sub in ("greenhouse", "lever", "ashby", "workday", "linkedin", "ziprecruiter"):
                out.status, out.redirect_url = "redirect", page.url
                out.steps.append(f"apply flow is on {sub}: {page.url}")
                return out

            for step in range(MAX_STEPS):
                await self.check_walls(page, out)
                missing = await self.fill(page, packet, out, planner)
                await self.shot(page, packet, out, f"step{step + 1}")
                if missing:
                    out.questions = missing
                    raise NeedsHuman(f"{len(missing)} required field(s) unanswered")
                submit = await self.submit_button(page)
                if submit:
                    if packet.dry_run:
                        out.status = "dry_run_ok"
                        return out
                    await self.do_submit(page, packet, out)
                    await self.shot(page, packet, out, "confirmation")
                    out.status = "submitted"
                    return out
                nxt = await self._visible(page, NEXT)
                if not nxt:
                    raise RuntimeError("no Next or Submit button on step %d" % (step + 1))
                if packet.dry_run and step >= 1:
                    # Past the first page a dry run would start creating server-side drafts.
                    out.status = "dry_run_ok"
                    out.steps.append("dry run stops before advancing further")
                    return out
                await _pause(1.0, 2.5)
                await nxt.click()
                out.steps.append(f"next (step {step + 1})")
                await page.wait_for_load_state("domcontentloaded")
                await _pause(1.5, 3.0)
            raise RuntimeError(f"gave up after {MAX_STEPS} steps")
        except NeedsHuman as e:
            out.status, out.error = "needs_human", str(e)
            await self.shot(page, packet, out, "needs-human")
        except Exception as e:
            out.status, out.error = "failed", str(e)[:1000]
            await self.shot(page, packet, out, "failed")
        return out
