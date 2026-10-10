"""Code-level guarantee that tailoring only touched SUMMARY and SKILLS.

The prompt asks for that too, but a prompt is a request; this is the check. Every top-level
section other than summary/skills must come back byte-identical (canonical JSON), skill tokens
must already be evidenced somewhere in the base résumé, and the summary may not introduce a
number (years, team sizes, percentages) the base résumé never states."""
import json
import re

MUTABLE = ("summary", "skills")
_SPLIT = re.compile(r"[,;|/()\n•·]+")
_NUM = re.compile(r"\d+(?:[.,]\d+)?")
MAX_SUMMARY_CHARS = 1500


def _canon(v) -> str:
    return json.dumps(v, sort_keys=True, ensure_ascii=False, separators=(",", ":"))


def _base_text(base: dict) -> str:
    """Everything the base résumé says, lowercased, as the evidence pool for skills and numbers."""
    from backend.analyzer.cv_scorer import _flatten_resume
    return (_flatten_resume(base) + "\n" + _canon(base)).lower()


def _has(evidence: str, token: str) -> bool:
    """Token present as a whole word/number: "go" must not match inside "google", "8" not inside "2018"."""
    t = re.escape(token.lower())
    return re.search(rf"(?<![a-z0-9]){t}(?![a-z0-9])", evidence) is not None


def skill_tokens(skills) -> list[str]:
    """Individual skill names out of {label: "a, b"} / {label: [a, b]} / [a, b] shapes."""
    values = []
    if isinstance(skills, dict):
        for v in skills.values():
            values.extend(v if isinstance(v, list) else [v])
    elif isinstance(skills, list):
        values = skills
    elif isinstance(skills, str):
        values = [skills]
    out = []
    for v in values:
        for tok in _SPLIT.split(str(v)):
            tok = tok.strip().strip(".").strip()
            if tok:
                out.append(tok)
    return out


def _section_diff(key: str, a, b) -> str:
    """Name the first place a protected section changed, for the retry feedback and the flag."""
    if isinstance(a, list) and isinstance(b, list):
        if len(a) != len(b):
            return f"{key}: {len(a)} entries became {len(b)}"
        for i, (x, y) in enumerate(zip(a, b)):
            if _canon(x) != _canon(y):
                if isinstance(x, dict) and isinstance(y, dict):
                    for f in sorted(set(x) | set(y)):
                        if _canon(x.get(f)) != _canon(y.get(f)):
                            return f"{key}[{i}].{f} changed"
                return f"{key}[{i}] changed"
    return f"{key} changed"


def check(base: dict, tailored: dict, allow_new_skills: bool = False) -> list[str]:
    """Violations (empty list = safe to use)."""
    base = base or {}
    tailored = tailored or {}
    problems = []

    for key in sorted(set(base) | set(tailored)):
        if key in MUTABLE:
            continue
        if key not in tailored:
            problems.append(f"{key}: section removed")
        elif key not in base:
            problems.append(f"{key}: section added")
        elif _canon(base[key]) != _canon(tailored[key]):
            problems.append(_section_diff(key, base[key], tailored[key]))

    evidence = _base_text(base)

    summary = tailored.get("summary")
    if not isinstance(summary, str) or not summary.strip():
        problems.append("summary: empty or not text")
    else:
        if len(summary) > MAX_SUMMARY_CHARS:
            problems.append(f"summary: {len(summary)} chars (max {MAX_SUMMARY_CHARS})")
        for n in _NUM.findall(summary):
            if not _has(evidence, n):
                problems.append(f"summary: number '{n}' is not in the base résumé")

    skills = tailored.get("skills")
    if base.get("skills") and not skills:
        problems.append("skills: emptied")
    elif skills is not None and not isinstance(skills, (dict, list)):
        problems.append("skills: wrong shape")
    elif not allow_new_skills:
        unknown = sorted({t for t in skill_tokens(skills) if not _has(evidence, t)})
        if unknown:
            problems.append("skills: not in base résumé: " + ", ".join(unknown[:12]))

    return problems
