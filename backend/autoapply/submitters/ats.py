"""Greenhouse, Lever and Ashby: hosted forms with stable shapes. Each only overrides where the
form lives, how to get to it, and what its confirmation looks like."""
import re
from urllib.parse import urlparse, parse_qs

from backend.autoapply.submitters.base import BaseSubmitter


class Greenhouse(BaseSubmitter):
    name = "greenhouse"
    hosted = ("greenhouse.io",)
    success_url = r"/confirmation|/thank"
    submit_text = re.compile(r"^\s*submit( application)?\s*$", re.I)

    def apply_url(self, url: str) -> str:
        # A careers page with ?gh_jid=... embeds the board; the job id alone opens the hosted
        # form when the board slug is in the path of a greenhouse URL.
        return url

    async def open_form(self, page, packet, out):
        await super().open_form(page, packet, out)
        host = urlparse(page.url).hostname or ""
        if not any(host == h or host.endswith("." + h) for h in self.hosted):
            # Company site embedding the Greenhouse iframe: go to the iframe itself.
            src = await page.evaluate(
                "() => { const f = document.querySelector('iframe#grnhse_iframe, iframe[src*=\"greenhouse.io\"]'); return f ? f.src : null }")
            if not src:
                jid = parse_qs(urlparse(packet.url).query).get("gh_jid", [None])[0]
                raise RuntimeError(f"no Greenhouse form found on page (gh_jid={jid})")
            out.steps.append("following Greenhouse iframe")
            await page.goto(src, wait_until="domcontentloaded", timeout=45000)
        # Older boards keep the form below an "Apply" button.
        btn = page.get_by_role("button", name=re.compile(r"^\s*apply( for this job| now)?\s*$", re.I))
        if await btn.count() and await btn.first.is_visible():
            await btn.first.click()
        await page.wait_for_selector("form input, form textarea", timeout=20000)


class Lever(BaseSubmitter):
    name = "lever"
    success_url = r"/thanks"
    submit_text = re.compile(r"^\s*submit application\s*$", re.I)

    def apply_url(self, url: str) -> str:
        p = urlparse(url)
        path = p.path.rstrip("/")
        if not path.endswith("/apply"):
            path += "/apply"
        return p._replace(path=path, query="", fragment="").geturl()

    async def open_form(self, page, packet, out):
        await super().open_form(page, packet, out)
        await page.wait_for_selector("form input[name='name'], form input[name='email']", timeout=20000)


class Ashby(BaseSubmitter):
    name = "ashby"
    submit_text = re.compile(r"^\s*submit application\s*$", re.I)
    success_text = re.compile(r"thank(s| you) for (submitting|applying)|application (was )?(successfully )?submitted|we('ve| have) received", re.I)

    def apply_url(self, url: str) -> str:
        p = urlparse(url)
        path = p.path.rstrip("/")
        if not path.endswith("/application"):
            path += "/application"
        return p._replace(path=path, fragment="").geturl()

    async def open_form(self, page, packet, out):
        await super().open_form(page, packet, out)
        await page.wait_for_selector("input[type='file'], input[name*='_systemfield_']", state="attached", timeout=25000)
