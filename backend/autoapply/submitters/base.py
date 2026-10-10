"""Submitter base: open the form, fill every field the planner can answer, attach files, prove it
with screenshots, and (unless dry run) submit and confirm. ATS subclasses only say where the form
is, which button submits, and what success looks like."""
import asyncio
import logging
import os
import random
import re
from dataclasses import dataclass, field
from typing import Awaitable, Callable, Optional

from backend.autoapply.submitters.answers import (
    Planner, Context, match_option, bool_option, text_option, norm,
)

logger = logging.getLogger("jobnavigator.autoapply.submit")

_JS_DIR = os.path.join(os.path.dirname(__file__), "js")
_JS = None


def page_script() -> str:
    global _JS
    if _JS is None:
        parts = []
        for name in ("autofill_match.js", "ats_combobox.js", "aa_page.js"):
            with open(os.path.join(_JS_DIR, name), encoding="utf-8") as f:
                parts.append(f.read())
        _JS = "\n;\n".join(parts)
    return _JS


@dataclass
class Packet:
    item_id: str
    company: str
    title: str
    url: str
    resume_pdf: str
    folder: str
    ctx: Context
    dry_run: bool = True
    cover_letter_pdf: Optional[str] = None
    cover_letter_text: Optional[str] = None
    # Called the first time a form asks for a letter, when none was made up front.
    make_cover_letter: Optional[Callable[[], Awaitable[tuple]]] = None
    # Per-tenant ATS logins (Workday): host -> (email, password) | None, and store a new one.
    get_login: Optional[Callable[[str], Optional[tuple]]] = None
    save_login: Optional[Callable[[str, str, str], None]] = None


@dataclass
class Outcome:
    status: str = "failed"          # submitted | dry_run_ok | needs_human | failed
    error: Optional[str] = None
    receipt: dict = field(default_factory=dict)
    screenshots: list = field(default_factory=list)
    questions: list = field(default_factory=list)
    steps: list = field(default_factory=list)
    confirmation_text: Optional[str] = None
    llm_calls: int = 0
    redirect_url: Optional[str] = None   # generic submitter landed on another ATS
    pause_site: Optional[str] = None     # a site showed a checkpoint: stop using it for a while


# A step button, never a submit, even when the form marks it type=submit.
NEXT_TEXT = re.compile(r"^\s*(next|continue|save (and|&) continue|proceed|review( application)?)\s*$", re.I)


class NeedsHuman(Exception):
    pass


async def _pause(lo=0.25, hi=0.9):
    """Human-ish gap between field actions; also lets React state settle."""
    await asyncio.sleep(random.uniform(lo, hi))


class BaseSubmitter:
    name = "base"
    form_root = None          # CSS selector to scope discovery (a modal), None = whole page
    submit_text = re.compile(r"^\s*(submit( your)?( application)?|send application|apply( now)?)\s*$", re.I)
    success_text = re.compile(r"thank(s| you) for (applying|your (application|interest))|application (has been |was )?(successfully )?(submitted|received)|we('ve| have) received your application", re.I)
    success_url = None

    def apply_url(self, url: str) -> str:
        return url

    # ── page plumbing ────────────────────────────────────────────────────────
    async def inject(self, page):
        """The runner adds the script with context.add_init_script, so it is normally there; a
        page the init script missed (CDP into an already-open tab) gets it by eval."""
        if not await page.evaluate("() => !!window.__aa"):
            await page.evaluate("src => (0, eval)(src)", page_script())

    async def state(self, page) -> dict:
        await self.inject(page)
        return await page.evaluate("() => __aa.pageState()")

    async def shot(self, page, packet: Packet, out: Outcome, label: str):
        path = os.path.join(packet.folder, f"{len(out.screenshots):02d}-{label}.png")
        try:
            await page.screenshot(path=path, full_page=True)
            out.screenshots.append(path)
        except Exception as e:
            out.steps.append(f"screenshot {label} failed: {e}")

    async def open_form(self, page, packet: Packet, out: Outcome):
        url = self.apply_url(packet.url)
        out.steps.append(f"open {url}")
        await page.goto(url, wait_until="domcontentloaded", timeout=45000)
        try:
            await page.wait_for_load_state("networkidle", timeout=15000)
        except Exception:
            pass

    async def check_walls(self, page, out: Outcome):
        from backend.scraper._shared.bot_wall import is_bot_wall
        st = await self.state(page)
        if st.get("challenge") or st.get("captchaWidget"):
            raise NeedsHuman("captcha on the form")
        if is_bot_wall(await page.content()):
            raise NeedsHuman("bot wall")
        if st.get("password"):
            raise NeedsHuman("login / account required")
        return st

    # ── filling ──────────────────────────────────────────────────────────────
    async def fill(self, page, packet: Packet, out: Outcome, planner: Planner) -> list:
        """Fill the current step; returns the required fields left unanswered."""
        await self.inject(page)
        fields = await page.evaluate("sel => __aa.discover(sel)", self.form_root)
        out.steps.append(f"discovered {len(fields)} fields")
        missing = []
        files = [f for f in fields if f["kind"] == "file"]
        for f in fields:
            if f["kind"] == "file":
                continue
            label = f.get("question") or f.get("name") or f"field {f['idx']}"
            if f["kind"] == "textarea" and "cover letter" in norm(label):
                text = await self.cover_letter_text(packet, out)
                if text and await self.apply(page, f, {"text": text}, packet, planner):
                    out.receipt[label] = "(cover letter text)"
                elif f.get("required"):
                    missing.append({"question": label, "field_type": f["kind"], "options": []})
                continue
            d = await planner.decide(f, fields)
            if d.flag:
                missing.append({"question": label, "field_type": f["kind"], "options": f.get("options") or []})
                continue
            if not d.action:
                continue
            ok = await self.apply(page, f, d.action, packet, planner)
            if ok:
                is_combo = "combo" in d.action or "combo_llm" in d.action
                out.receipt[label] = ok if is_combo and isinstance(ok, str) else (d.shown or ok)
            elif f.get("required"):
                missing.append({"question": label, "field_type": f["kind"], "options": f.get("options") or []})
            await _pause()
        await self.attach(page, packet, files, out, missing)
        out.llm_calls = planner.llm_calls
        return missing

    async def apply(self, page, f: dict, action: dict, packet: Packet, planner: Planner):
        sel = f'[data-aa-idx="{f["idx"]}"]'
        try:
            if "text" in action:
                loc = page.locator(sel)
                try:
                    await loc.fill(action["text"], timeout=5000)
                except Exception:
                    await page.evaluate("([i, t]) => __aa.apply(i, {text: t})", [f["idx"], action["text"]])
                if f["kind"] == "text" and re.search(r"\b(location|city)\b", norm(f.get("question"))):
                    await self.pick_suggestion(page)
                return action["text"]
            if "combo" in action or "combo_llm" in action:
                return await self.combobox(page, f, action, planner)
            res = await page.evaluate("([i, a]) => __aa.apply(i, a)", [f["idx"], action])
            return res == "ok"
        except Exception as e:
            logger.info(f"fill {f.get('question')!r} failed: {e}")
            return False

    async def pick_suggestion(self, page):
        """Location typeaheads (Lever, Google Places) want a suggestion chosen, not just text."""
        await asyncio.sleep(1.2)
        sugg = page.locator('[role="option"]:visible, .dropdown-location:visible, .pac-item:visible, '
                            '[class*="suggestion" i] li:visible, [class*="dropdown-results" i] > div:visible')
        try:
            if await sugg.count():
                await sugg.first.click(timeout=3000)
        except Exception:
            pass

    async def combobox(self, page, f: dict, action: dict, planner: Planner):
        search = action.get("search")
        options = await page.evaluate("([i, s]) => __aa.comboOptions(i, s)", [f["idx"], search])
        options = [o for o in options if o]
        if not options:
            await page.keyboard.press("Escape")
            return False
        idx = None
        if "combo" in action:
            val = action["combo"]
            key = None
            ctx = planner.ctx
            if isinstance(val, bool):
                idx = bool_option(val, options, (ctx.synonyms or {}).get("_bool", {}))
            else:
                for k, v in (ctx.answers or {}).items():
                    if v == val:
                        key = k
                        break
                idx = match_option(val, options, (ctx.synonyms or {}).get(key, {})) if key else None
                if idx is None:
                    idx = text_option(str(val), options)
        else:
            planner.llm_calls += 1
            idx = await planner.llm_choose(f.get("question") or "", options, planner.ctx)
        if idx is None or not (0 <= idx < len(options)):
            await page.keyboard.press("Escape")
            return False
        res = await page.evaluate("([i, w, s]) => __aa.comboPick(i, w, s)", [f["idx"], options[idx], search])
        return options[idx] if res == "filled" else False

    async def cover_letter_text(self, packet: Packet, out: Outcome) -> Optional[str]:
        if not packet.cover_letter_text and packet.make_cover_letter:
            try:
                packet.cover_letter_pdf, packet.cover_letter_text = await packet.make_cover_letter()
            except Exception as e:
                out.steps.append(f"cover letter failed: {e}")
        return packet.cover_letter_text

    async def attach(self, page, packet: Packet, files: list, out: Outcome, missing: list):
        if not files:
            return
        resume_done = False
        # The résumé goes to the input that says résumé/CV; only a form with no such input gets it
        # in its first file input. (Ashby's first input is "autofill from résumé", which rewrites
        # fields already filled.)
        named = [f for f in files if re.search(r"\b(resume|cv|curriculum)\b", norm(f.get("signature"))) and "cover" not in norm(f.get("signature"))]
        resume_target = named[-1] if named else files[0]
        for f in files:
            sig = norm(f.get("signature"))
            sel = f'[data-aa-idx="{f["idx"]}"]'
            want = None
            if "cover" in sig:
                if not packet.cover_letter_pdf and packet.make_cover_letter:
                    try:
                        packet.cover_letter_pdf, packet.cover_letter_text = await packet.make_cover_letter()
                    except Exception as e:
                        out.steps.append(f"cover letter failed: {e}")
                want = packet.cover_letter_pdf
                label = "Cover letter"
            elif f is resume_target and not resume_done:
                want, label = packet.resume_pdf, "Resume"
            else:
                continue
            if not want:
                if f.get("required"):
                    missing.append({"question": label, "field_type": "file", "options": []})
                continue
            try:
                await page.locator(sel).set_input_files(want, timeout=10000)
                out.receipt[label] = os.path.basename(want)
                resume_done = resume_done or label == "Resume"
                await _pause(1.0, 2.0)   # parsers (Lever, Ashby) read the file and prefill
            except Exception as e:
                out.steps.append(f"attach {label} failed: {e}")
                if f.get("required"):
                    missing.append({"question": label, "field_type": "file", "options": []})

    # ── submit ───────────────────────────────────────────────────────────────
    async def submit_button(self, page):
        for role in ("button", "link"):
            loc = page.get_by_role(role, name=self.submit_text)
            n = await loc.count()
            for i in range(n):
                b = loc.nth(i)
                if await b.is_visible():
                    return b
        loc = page.locator('button[type="submit"], input[type="submit"]')
        for i in range(await loc.count()):
            b = loc.nth(i)
            if not await b.is_visible():
                continue
            label = (await b.inner_text() if await b.evaluate("e => e.tagName") == "BUTTON"
                     else await b.get_attribute("value")) or ""
            if NEXT_TEXT.search(label):
                continue
            return b
        return None

    async def confirmed(self, page) -> Optional[str]:
        """The confirmation text if the page shows one, else the URL if it is a known thank-you URL."""
        st = await self.state(page)
        m = self.success_text.search(st.get("text") or "")
        if m:
            return m.group(0)
        if self.success_url and re.search(self.success_url, page.url):
            return page.url
        return None

    async def do_submit(self, page, packet: Packet, out: Outcome):
        btn = await self.submit_button(page)
        if not btn:
            raise RuntimeError("no submit button found")
        await _pause(1.5, 3.5)
        await btn.click()
        out.steps.append("clicked submit")
        for _ in range(30):
            await asyncio.sleep(1)
            conf = await self.confirmed(page)
            if conf:
                out.confirmation_text = conf[:500]
                return
            st = await self.state(page)
            if st.get("challenge"):
                raise NeedsHuman("captcha after submit")
        errs = await page.evaluate("() => __aa.errorsShown()")
        raise RuntimeError("no confirmation after submit" + (f": {'; '.join(errs[:5])}" if errs else ""))

    async def run(self, page, packet: Packet, planner: Planner) -> Outcome:
        out = Outcome()
        try:
            await self.open_form(page, packet, out)
            await self.check_walls(page, out)
            missing = await self.fill(page, packet, out, planner)
            await self.shot(page, packet, out, "filled")
            if missing:
                out.questions = missing
                raise NeedsHuman(f"{len(missing)} required field(s) unanswered")
            if packet.dry_run:
                out.status = "dry_run_ok"
                return out
            await self.do_submit(page, packet, out)
            await self.shot(page, packet, out, "confirmation")
            out.status = "submitted"
        except NeedsHuman as e:
            out.status, out.error = "needs_human", str(e)
            await self.shot(page, packet, out, "needs-human")
        except Exception as e:
            out.status, out.error = "failed", str(e)[:1000]
            await self.shot(page, packet, out, "failed")
        return out
