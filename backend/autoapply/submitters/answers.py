"""Decide what goes into each discovered form field.

Order, cheapest first: the persona's structured answers (same patterns and synonyms the
extension uses), the learned Q&A bank, then the LLM. A required field nothing can answer
becomes a question for the human, and the item parks as needs_human."""
import json
import logging
import re
from dataclasses import dataclass, field

logger = logging.getLogger("jobnavigator.autoapply.answers")

DECLINE = ("prefer not to answer", "prefer not to say", "decline to self", "decline to answer",
           "i decline", "do not wish", "don t wish", "choose not to", "not wish to")
DEMOGRAPHIC = re.compile(r"gender|identity|transgender|sexual|orientation|\brace\b|ethnic|ancestry|\bage\b|"
                         r"disab|veteran|pronoun|hispanic|latin|lgbt|religio|marital|neurodiver", re.I)
_STOP = {"the", "a", "an", "to", "of", "you", "your", "are", "is", "do", "in", "for", "and", "or", "with", "this",
         "what", "please", "have", "will", "be", "on", "at", "if", "we", "our", "any"}


def norm(s) -> str:
    return re.sub(r"[^a-z0-9]+", " ", str(s or "").lower().replace("’", "'")).strip()


# Whole-question labels too short or ambiguous for the shared pattern list ("Name" alone would
# otherwise match every "... name" question). Matched only against the full normalised question.
EXACT_QUESTIONS = {
    "name": "full_name", "your name": "full_name", "legal name": "full_name",
    "location": "city", "current location": "city", "city": "city",
    "country": "country", "github": "github", "website": "portfolio",
}


def match_field_key(signature: str, patterns: dict) -> str | None:
    """Python port of extension/lib/autofill_match.js matchFieldKey: longest synonym wins."""
    sig = f" {norm(signature)} "
    best, best_len = None, 0
    for key, syns in (patterns or {}).items():
        for syn in syns or []:
            n = norm(syn)
            if n and f" {n} " in sig and len(n) > best_len:
                best, best_len = key, len(n)
    return best


def match_option(value, options: list, synonyms_for_key: dict) -> int | None:
    syns = sorted((norm(s) for s in (synonyms_for_key or {}).get(str(value), [])), key=len, reverse=True)
    best, best_len = None, 0
    for i, txt in enumerate(options):
        n = norm(txt)
        for s in syns:
            if s and s in n and len(s) > best_len:
                best, best_len = i, len(s)
    return best


def bool_option(value: bool, options: list, bool_syn: dict) -> int | None:
    i = match_option("true" if value else "false", options, bool_syn)
    if i is not None:
        return i
    want = "yes" if value else "no"
    for i, txt in enumerate(options):
        if norm(txt).split()[:1] == [want]:
            return i
    return None


def text_option(value: str, options: list) -> int | None:
    """Free-text answer to an option list: exact, then containment either way."""
    nv = norm(value)
    if not nv:
        return None
    for i, t in enumerate(options):
        if norm(t) == nv:
            return i
    for i, t in enumerate(options):
        nt = norm(t)
        if nt and (nv in nt or nt in nv):
            return i
    return None


def decline_option(options: list) -> int | None:
    for i, t in enumerate(options):
        n = norm(t)
        if any(d in n for d in DECLINE):
            return i
    return None


def _tokens(s) -> set:
    return {t for t in norm(s).split() if t not in _STOP and len(t) > 1}


def qa_lookup(question: str, bank: list, threshold: float = 0.7) -> str | None:
    """Best stored answer for a question whose wording overlaps enough (Jaccard on content words)."""
    q = _tokens(question)
    if not q:
        return None
    best, best_score = None, 0.0
    for entry in bank or []:
        if not isinstance(entry, dict):
            continue
        if "question" in entry or "answer" in entry:
            bq, ba = entry.get("question"), entry.get("answer")
        else:
            bq, ba = next(iter(entry.items()), (None, None))
        t = _tokens(bq)
        if not t or not ba:
            continue
        score = len(q & t) / len(q | t)
        if score > best_score:
            best, best_score = str(ba), score
    return best if best_score >= threshold else None


MAX_LLM_OPTIONS = 60


def narrow_options(options: list, ctx) -> list:
    """A long list (universities, countries, referral sources) is cut to the entries the résumé or
    persona actually mention before it goes to the model; nothing mentioned -> nothing to choose."""
    if len(options) <= MAX_LLM_OPTIONS:
        return options
    evidence = norm(f"{ctx.resume_text} {ctx.persona_text}")
    hits = [o for o in options if norm(o) and f" {norm(o)} " in f" {evidence} "]
    return hits[:MAX_LLM_OPTIONS]


@dataclass
class Decision:
    idx: int
    action: dict = field(default_factory=dict)   # what aa_page.apply / the combobox driver gets
    shown: str = ""                              # the value as the form shows it (for the receipt)
    source: str = ""                             # persona | qa | llm | decline | file
    flag: str | None = None                      # set when the field could not be answered
    key: str | None = None


@dataclass
class Context:
    """Everything the planner needs, loaded once per submission."""
    answers: dict
    patterns: dict
    synonyms: dict
    schema: dict
    decline_self_id: bool
    qa_bank: list
    persona_text: str
    company: str
    title: str
    resume_text: str = ""


def load_context(job_company: str, job_title: str, resume_json: dict | None = None) -> Context:
    from backend.api.routes_autofill import autofill_config, _flatten_persona
    from backend.analyzer.cv_scorer import _flatten_resume
    from backend.models.db import SessionLocal, Persona
    cfg = autofill_config()
    db = SessionLocal()
    try:
        p = db.query(Persona).filter(Persona.id == 1).first()
        bank = list(p.qa_bank or []) if p else []
        persona_text = _flatten_persona(p) if p else ""
    finally:
        db.close()
    return Context(answers=cfg["answers"], patterns=cfg["field_patterns"], synonyms=cfg["option_synonyms"],
                   schema=cfg["schema"], decline_self_id=cfg["decline_self_id"], qa_bank=bank,
                   persona_text=persona_text, company=job_company or "", title=job_title or "",
                   resume_text=_flatten_resume(resume_json or {}))


def _value_to_action(f: dict, val, key: str, ctx: Context) -> tuple[dict | None, str]:
    """Turn a known answer into an action for this field kind; (None, reason) if it does not fit."""
    kind = f["kind"]
    spec = (ctx.schema or {}).get(key, {}) if key else {}
    syn = (ctx.synonyms or {}).get(key, {}) if key else {}
    bool_syn = (ctx.synonyms or {}).get("_bool", {})
    opts = f.get("options") or []
    if kind in ("text", "textarea"):
        if isinstance(val, bool):
            val = "Yes" if val else "No"
        elif spec.get("kind") == "enum" and syn.get(val):
            val = syn[val][0].title()
        return {"text": str(val)}, str(val)
    if kind == "checkbox":
        if isinstance(val, bool):
            return {"checked": val}, "checked" if val else "unchecked"
        nv = norm(val)
        if nv in ("yes", "true", "agree", "i agree"):
            return {"checked": True}, "checked"
        return None, "type-mismatch"
    if kind in ("select", "radioGroup", "checkboxGroup"):
        if isinstance(val, bool) or spec.get("kind") == "bool":
            i = bool_option(bool(val) if not isinstance(val, str) else norm(val) in ("yes", "true"), opts, bool_syn)
        else:
            i = match_option(val, opts, syn)
            if i is None and isinstance(val, str):
                i = text_option(val, opts)
        if i is None:
            return None, "no-option"
        if kind == "select":
            return {"selectIndex": i}, opts[i]
        if kind == "radioGroup":
            return {"radioIndex": i}, opts[i]
        return {"checkIndices": [i]}, opts[i]
    if kind == "combobox":
        return {"combo": val, "search": val if spec.get("kind") == "text" and isinstance(val, str) else None}, str(val)
    return None, "unsupported"


class Planner:
    """Per-form planner. `llm_choose(question, options, ctx) -> int|None` and
    `llm_write(question, max_chars, ctx) -> str|None` are injectable for tests."""

    def __init__(self, ctx: Context, llm_choose=None, llm_write=None):
        self.ctx = ctx
        self.llm_choose = llm_choose or default_llm_choose
        self.llm_write = llm_write or default_llm_write
        self.llm_calls = 0

    def split_phone(self, fields) -> bool:
        return any(("phone" in norm(f.get("signature")) and ("country" in norm(f.get("signature")) or "code" in norm(f.get("signature"))))
                   for f in fields if f["kind"] in ("select", "combobox"))

    async def decide(self, f: dict, fields: list) -> Decision:
        d = Decision(idx=f["idx"])
        kind = f["kind"]
        if kind in ("file", "password"):
            return d   # files are handled by the submitter; passwords mean a login wall
        question = f.get("question") or ""
        # The question text decides first; the signature (name/id/path) only when it says nothing.
        key = (EXACT_QUESTIONS.get(norm(question))
               or match_field_key(question, self.ctx.patterns)
               or match_field_key(f.get("signature", ""), self.ctx.patterns))
        # A long question that merely mentions "company"/"location" is not asking for that value.
        if key and (self.ctx.schema or {}).get(key, {}).get("kind") == "text" and len(question.split()) > 8:
            key = None
        d.key = key
        if key:
            val = self.ctx.answers.get(key)
            if key == "phone" and self.split_phone(fields) and self.ctx.answers.get("phone_national"):
                val = self.ctx.answers["phone_national"]
            if val not in (None, "") and not (isinstance(val, str) and not val.strip()):
                action, shown = _value_to_action(f, val, key, self.ctx)
                if action:
                    d.action, d.shown, d.source = action, shown, "persona"
                    return d

        opts = f.get("options") or []

        # Self-ID questions the persona does not cover: decline when the persona says so.
        if kind in ("select", "radioGroup", "checkboxGroup") and self.ctx.decline_self_id and \
                (DEMOGRAPHIC.search(f.get("signature", "")) or any(DEMOGRAPHIC.search(o) for o in opts)):
            i = decline_option(opts)
            if i is not None:
                d.action = {"selectIndex": i} if kind == "select" else {"radioIndex": i} if kind == "radioGroup" else {"checkIndices": [i]}
                d.shown, d.source = opts[i], "decline"
                return d

        banked = qa_lookup(question, self.ctx.qa_bank)
        if banked:
            action, shown = _value_to_action(f, banked, None, self.ctx)
            if action:
                d.action, d.shown, d.source = action, shown, "qa"
                return d

        # Only spend an LLM call on what the form actually requires.
        if not f.get("required"):
            return d
        if kind in ("select", "radioGroup", "checkboxGroup"):
            real = [o for o in opts if norm(o) not in ("", "select", "select an option", "please select", "select one")]
            real = narrow_options(real, self.ctx)
            if real:
                self.llm_calls += 1
                choice = await self.llm_choose(question, real, self.ctx)
                if choice is not None and 0 <= choice < len(real):
                    i = opts.index(real[choice])
                    d.action = {"selectIndex": i} if kind == "select" else {"radioIndex": i} if kind == "radioGroup" else {"checkIndices": [i]}
                    d.shown, d.source = opts[i], "llm"
                    return d
        elif kind in ("text", "textarea"):
            limit = f.get("maxlength") or (700 if kind == "textarea" else 150)
            self.llm_calls += 1
            text = await self.llm_write(question, min(limit, 1500), self.ctx)
            if text:
                d.action, d.shown, d.source = {"text": text[:limit]}, text[:limit], "llm"
                return d
        elif kind == "checkbox":
            n = norm(question)
            # Consent / acknowledgement boxes ("I agree", "I acknowledge", privacy policy).
            if re.search(r"\b(agree|acknowledge|consent|certify|confirm|accept|privacy|terms)\b", n):
                d.action, d.shown, d.source = {"checked": True}, "checked", "consent"
                return d
        elif kind == "combobox":
            d.action, d.source = {"combo_llm": True}, "llm"
            return d
        d.flag = "required, no answer"
        return d


async def default_llm_choose(question: str, options: list, ctx: Context) -> int | None:
    from backend.analyzer.llm_client import call_autofill_llm
    from backend.analyzer.model_json import parse_model_json
    from backend.analyzer.prompt_fence import fence
    numbered = "\n".join(f"{i + 1}. {o}" for i, o in enumerate(options))
    bank = "\n".join(f"Q: {e.get('question')}\nA: {e.get('answer')}" for e in ctx.qa_bank if isinstance(e, dict) and e.get("question"))[:3000]
    prompt = (f"Candidate profile:\n{ctx.persona_text[:4000]}\n\nPrevious answers:\n{bank or '(none)'}\n\n"
              f"Applying to: {ctx.title} at {ctx.company}\n\n"
              f"{fence(question, 'APPLICATION QUESTION', note=False)}\n\nOptions:\n{numbered}\n\n"
              'Pick the option that truthfully fits the candidate. Reply ONLY with JSON: {"choice": <option number>, "sure": true|false}. '
              'If the profile does not support any option, reply {"choice": null, "sure": false}.')
    try:
        resp = await call_autofill_llm(prompt, "You answer job-application questions truthfully from the candidate's profile. Output only JSON.",
                                       max_tokens=60)
        data = parse_model_json(resp.get("text") or "")
        n = data.get("choice")
        if not data.get("sure") or not isinstance(n, int):
            return None
        return n - 1
    except Exception as e:
        logger.warning(f"llm_choose failed: {e}")
        return None


async def default_llm_write(question: str, max_chars: int, ctx: Context) -> str | None:
    from backend.api.routes_autofill import autofill_answer
    try:
        out = await autofill_answer({"question": question, "company": ctx.company, "position": ctx.title,
                                     "max_chars": max_chars})
        return (out or {}).get("answer") or None
    except Exception as e:
        logger.warning(f"llm_write failed: {e}")
        return None


def to_json(decisions: list) -> str:
    return json.dumps([d.__dict__ for d in decisions], default=str)
