"""Read-only probe of a live application form: open it with the matching submitter, list what the
field engine discovers and what the planner would answer. Never types, uploads or submits.

    docker compose exec backend python -m backend.autoapply.tools.probe <job url> [<job url> ...]
"""
import asyncio
import json
import sys

from backend.autoapply.submitters.registry import classify


async def probe(url: str, ctx=None) -> dict:
    from backend.autoapply.runner import HeadlessBrowser, submitter_for
    from backend.autoapply.submitters.answers import Planner, load_context
    from backend.autoapply.submitters.base import Packet, Outcome

    sub_name = classify(url)
    cls = submitter_for(sub_name or "")
    if not cls:
        return {"url": url, "submitter": sub_name, "error": "no submitter"}
    ctx = ctx or load_context("", "")

    async def no_llm_choose(q, options, c):
        return None

    async def no_llm_write(q, n, c):
        return None
    planner = Planner(ctx, llm_choose=no_llm_choose, llm_write=no_llm_write)
    out = Outcome()
    sub = cls()
    packet = Packet(item_id="probe", company="", title="", url=url, resume_pdf="", folder="/tmp", ctx=ctx)
    async with HeadlessBrowser() as page:
        try:
            await sub.open_form(page, packet, out)
            state = await sub.state(page)
            fields = await page.evaluate("() => __aa.discover()")
        except Exception as e:
            return {"url": url, "submitter": sub_name, "error": str(e), "steps": out.steps}
        rows = []
        for f in fields:
            d = await planner.decide(f, fields)
            rows.append({"kind": f["kind"], "q": (f.get("question") or "")[:90], "req": f.get("required"),
                         "opts": (f.get("options") or [])[:6], "plan": d.source or ("FLAG" if d.flag else "-"),
                         "key": d.key, "value": d.shown})
        submit = await sub.submit_button(page)
        return {"url": page.url, "submitter": sub_name, "captcha": state.get("challenge") or state.get("captchaWidget"),
                "invisible_recaptcha": state.get("invisibleRecaptcha"), "password": state.get("password"),
                "submit_button": bool(submit), "fields": rows}


def main(urls):
    for u in urls:
        print(json.dumps(asyncio.run(probe(u)), indent=1, ensure_ascii=False))


if __name__ == "__main__":
    main(sys.argv[1:])
