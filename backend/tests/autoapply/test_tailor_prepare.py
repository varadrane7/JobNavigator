import copy
import json
from datetime import date

import pytest

from backend.autoapply import guard
from backend.autoapply.prepare import tailor, prepare_item, release_stale
from backend.autoapply.models import AutoApplyItem

from backend.tests.autoapply.resume_data import BASE


def _with(**kw):
    d = copy.deepcopy(BASE)
    d.update(kw)
    return d


# ── guard ────────────────────────────────────────────────────────────────────

def test_guard_accepts_summary_and_skill_reorder():
    t = _with(summary="Cloud engineer with 6 years of AWS, Terraform and C# delivery.",
              skills={"Cloud & IaC": "Terraform, AWS, Docker", "Languages": "Python, C#"})
    assert guard.check(BASE, t) == []


@pytest.mark.parametrize("mutate,expect", [
    (lambda d: d["experience"][0]["bullets"].__setitem__(0, "Built Rust APIs"), "experience[0].bullets changed"),
    (lambda d: d["experience"].reverse(), "experience[0]"),
    (lambda d: d["experience"][1]["bullets"].append("Invented AI"), "experience[1].bullets changed"),
    (lambda d: d["education"][0].__setitem__("school", "MIT"), "education[0].school changed"),
    (lambda d: d.pop("education"), "education: section removed"),
    (lambda d: d.__setitem__("projects", [{"name": "x"}]), "projects: section added"),
    (lambda d: d["header"].__setitem__("name", "Bob"), "header changed"),
])
def test_guard_rejects_protected_edits(mutate, expect):
    t = copy.deepcopy(BASE)
    mutate(t)
    problems = guard.check(BASE, t)
    assert any(expect in p for p in problems), problems


def test_guard_rejects_invented_skill_and_number():
    t = _with(summary="Engineer with 10 years of experience.",
              skills={"Languages": "C#, Rust, Python", "Cloud": "AWS, Kubernetes"})
    problems = guard.check(BASE, t)
    assert any("'10'" in p for p in problems)
    assert any("Kubernetes" in p and "Rust" in p for p in problems)


def test_guard_whole_word_matching():
    # "Go" is evidenced; "Goo" is not, and must not pass as a substring of "Google"-like text.
    t = _with(skills={"Languages": "Go, Goo"})
    assert any("Goo" in p for p in guard.check(BASE, t))


def test_guard_allow_new_skills_and_empty():
    t = _with(skills={"Languages": "Rust"})
    assert guard.check(BASE, t, allow_new_skills=True) == []
    assert any("emptied" in p for p in guard.check(BASE, _with(skills={})))
    assert any("summary" in p for p in guard.check(BASE, _with(summary="")))


# ── tailor ───────────────────────────────────────────────────────────────────

def _llm(*replies):
    calls = []

    async def fake(prompt, system, job_id=None):
        calls.append(prompt)
        r = replies[min(len(calls) - 1, len(replies) - 1)]
        return r if isinstance(r, str) else json.dumps(r)
    fake.calls = calls
    return fake


async def test_tailor_ignores_experience_in_reply():
    good = {"summary": "AWS and C# engineer, 6 years.", "skills": {"Cloud": "AWS, Terraform", "Languages": "C#"},
            "experience": [{"bullets": ["hacked"]}]}
    llm = _llm(good)
    out, flags = await tailor(BASE, "We need AWS", "{resume_text}|{sections_json}|{job_description}", False, llm=llm)
    assert flags == []
    assert out["experience"] == BASE["experience"]
    assert out["summary"] == good["summary"]
    assert len(llm.calls) == 1
    # the prompt carries only summary/skills as editable sections
    assert '"experience"' not in llm.calls[0].split("|")[1]


async def test_tailor_retries_with_feedback_then_succeeds():
    bad = {"summary": "12 years of Kubernetes.", "skills": BASE["skills"]}
    good = {"summary": "C# and AWS engineer.", "skills": BASE["skills"]}
    llm = _llm(bad, good)
    out, flags = await tailor(BASE, "jd", "{resume_text}{sections_json}{job_description}", False, llm=llm)
    assert flags == [] and out["summary"] == good["summary"]
    assert "rejected" in llm.calls[1]


async def test_tailor_falls_back_to_base_after_two_violations():
    bad = {"summary": "12 years.", "skills": {"X": "Kubernetes"}}
    out, flags = await tailor(BASE, "jd", "{resume_text}{sections_json}{job_description}", False, llm=_llm(bad, bad))
    assert out == BASE
    assert flags and all(f.startswith("tailoring not used") for f in flags)


async def test_tailor_raises_when_no_attempt_answered():
    with pytest.raises(RuntimeError):
        await tailor(BASE, "jd", "{resume_text}{sections_json}{job_description}", False,
                     llm=_llm("sorry, I can't", "still prose"))


async def test_tailor_one_garbage_then_good():
    good = {"summary": "C# engineer.", "skills": BASE["skills"]}
    out, flags = await tailor(BASE, "jd", "{resume_text}{sections_json}{job_description}", False,
                              llm=_llm("prose", good))
    assert flags == [] and out["summary"] == "C# engineer."


# ── prepare_item ─────────────────────────────────────────────────────────────

class FakeRender:
    def __init__(self):
        self.cl_calls = 0

    async def resume_pdf(self, resume_id, folder):
        return f"{folder}/r-{resume_id}.pdf"

    async def cover_letter(self, resume_id, job_id, folder):
        self.cl_calls += 1
        return None, f"{folder}/cl.pdf"


@pytest.fixture
def approved_item(test_db, make_job, make_resume, aa_settings, tmp_path):
    aa_settings(autoapply_data_dir=str(tmp_path))
    base = make_resume("Cloud", json_data=copy.deepcopy(BASE))
    job = make_job("Gamma")
    job.description = "AWS Terraform role"
    it = AutoApplyItem(job_id=job.id, run_date=date(2026, 10, 9), rank=1, submitter="greenhouse",
                       status="approved", base_resume_id=base.id, base_resume_name="Cloud")
    test_db.add(it)
    test_db.commit()
    return it


async def test_prepare_item_builds_tailored_copy(test_db, approved_item):
    from backend.models.db import Resume
    render = FakeRender()
    good = {"summary": "AWS engineer, 6 years, C#.", "skills": BASE["skills"]}
    r = await prepare_item(approved_item.id, llm=_llm(good), render=render)
    assert r == "ready"
    test_db.expire_all()
    it = test_db.query(AutoApplyItem).get(approved_item.id)
    assert it.status == "ready"
    copy_row = test_db.query(Resume).get(it.tailored_resume_id)
    assert copy_row.is_base is False and copy_row.parent_id == it.base_resume_id
    assert copy_row.json_data["experience"] == BASE["experience"]
    assert copy_row.json_data["summary"] == good["summary"]
    assert copy_row.name == "Cloud → Gamma — Engineer"
    assert it.resume_pdf_path.endswith(f"r-{copy_row.id}.pdf")
    assert render.cl_calls == 0     # default mode if_field: letters are written at submit time


async def test_prepare_item_always_cover_letter(test_db, approved_item, aa_settings):
    aa_settings(autoapply_cover_letter="always")
    render = FakeRender()
    await prepare_item(approved_item.id, llm=_llm({"summary": "AWS.", "skills": BASE["skills"]}), render=render)
    test_db.expire_all()
    assert render.cl_calls == 1
    assert test_db.query(AutoApplyItem).get(approved_item.id).cl_pdf_path.endswith("cl.pdf")


async def test_prepare_item_requeues_then_fails(test_db, approved_item):
    class BadRender(FakeRender):
        async def resume_pdf(self, resume_id, folder):
            raise RuntimeError("chromium died")
    for n in range(1, 4):
        await prepare_item(approved_item.id, llm=_llm({"summary": "C#.", "skills": BASE["skills"]}), render=BadRender())
        test_db.expire_all()
        it = test_db.query(AutoApplyItem).get(approved_item.id)
        assert it.attempts == n
        assert it.status == ("failed" if n == 3 else "approved")
        assert "chromium died" in it.error


async def test_prepare_llm_down_retries_later(test_db, approved_item):
    async def boom(prompt, system, job_id=None):
        raise RuntimeError("usage limit reached")
    await prepare_item(approved_item.id, llm=boom, render=FakeRender())
    test_db.expire_all()
    it = test_db.query(AutoApplyItem).get(approved_item.id)
    assert it.status == "approved" and "usage limit" in it.error and it.tailored_resume_id is None


async def test_prepare_skips_non_approved(test_db, approved_item):
    approved_item.status = "queued"
    test_db.commit()
    assert await prepare_item(approved_item.id, llm=_llm({}), render=FakeRender()) == "not approved"


def test_release_stale(test_db, approved_item):
    from datetime import datetime, timedelta, timezone
    approved_item.status = "preparing"
    test_db.commit()
    assert release_stale(test_db, now=datetime.now(timezone.utc)) == 0
    assert release_stale(test_db, now=datetime.now(timezone.utc) + timedelta(hours=1)) == 1
