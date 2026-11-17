"""Drafting the texts the owner sends by hand: the LinkedIn connection note, the message after a
request is accepted, a follow-up, and a cold email.

One shared system prompt (SYSTEM) says who the sender is, who reads and what a good message does; an
audience block (AUDIENCE_BLOCKS, curator/audience.py) says what this reader needs. The model may draw on
exactly these inputs and nothing else, and `Brief.inputs()` hands the validator the same strings:

  * the sender facts block (`Sender.facts`), built once per run from the stored profile (the Persona)
  * the recipient's name and card text (headline, current and past position lines, location)
  * the evidence snippet (the hiring post, or the profile snippet the person was found from)
  * the job title and the company
  * for a follow-up, the earlier text it refers to

The model returns the text only. `curator/note_check.py` validates it (length per kind, the fact check, the
style rules), the model gets one retry with the problems named, and a text that still fails is not saved:
the lead is flagged for one written by hand. Nothing is ever cut to fit.

Nothing here runs when the profile's `outreach_drafts_enabled` is false (the callers check it).
"""
import logging
import re
from dataclasses import dataclass, field
from typing import Optional

from backend.curator import llm, note_check
from backend.curator.note_check import NoteResult

logger = logging.getLogger("jobnavigator.curator.drafting")

SYSTEM = """You draft short messages for a job seeker, called the sender here. The sender reads each message, changes it if they want, and sends it themselves under their own name, so it has to read exactly as they would say it, and every claim in it has to be one they could defend if the reader asked.

Who the sender is: everything you may say about the sender is in the block "About the sender" in the request, and nowhere else. If the block has a headline, that is how the sender describes themselves; if it has none, do not describe them beyond what the block lists. As far as you are concerned nothing else about the sender is true. Do not add a project, an employer, a title, a number, a technology or a result that is not written there, and do not stretch what is written: a project the sender built is not a system they ran at scale.

Who reads it: a busy person the sender has not met, or barely knows, reading on a phone between other things. They decide in a few seconds whether it deserves an answer. They answer people who sound like a capable colleague and ignore people who sound like they want something or are performing enthusiasm.

What a good message does: it says why the sender is writing to this person in particular, using one specific true detail you were given about them (a school, an employer, a line from their headline, something from a post they wrote), says who the sender is in a clause, and makes one small, easy ask. It sounds like an engineer writing to a peer: first person from the sender, plain words, short sentences, no sales voice.

Rules, each with its reason:
- Use only the facts in the request. The sender sends this under their own name, and anything invented is a claim they will be caught on. When there is no specific true detail about the person, write a shorter, plainer message that says who the sender is and why they are writing, instead of inventing a detail. A short honest message beats a warm made-up one.
- Name a shared school or employer only when both the sender's facts and the person's own card name it, and never say or suggest that they overlapped there or knew each other: nothing in the request says so.
- Do not say the sender applied for a role unless the request says they did. A false claim of an application is the quickest way to lose a reply.
- Never mention visas, sponsorship, work authorisation or work status. That conversation belongs to a much later stage, and early on it only makes people close the message.
- No flattery ("impressive", "inspiring", "love your work"), no "I hope this finds you well", no "I came across your profile", no buzzwords. Readers have seen thousands of them and they mark a template.
- No em dashes, no emojis, and at most one exclamation mark (none is better). They read as machine-written.
- One ask only, and a small one, such as a short question or a quick chat. Two asks give the reader a reason to do neither.
- Do not use any number, company name, project name or technology name that is not in the request.
- Keep to the length given for this kind of message in the request.

Return only the text of the message: no quotation marks, no label, no explanation, and no subject line unless the request asks for an email."""

# What this reader needs, per audience. `generic` is used when nothing in the request says how the
# person relates to the sender (no audience, or a hiring manager or post author with no post to point to).
AUDIENCE_BLOCKS = {
    "alum": ("an alum of one of the sender's schools: a school in the sender's facts is also named on their card. Open "
             "with that shared school. Add one line about their path or their company, taken from their card text. Ask "
             "for a little advice, about their path or their team. Do not ask for a referral or a job: that comes much "
             "later, if at all."),
    "ex_colleague": ("someone whose card names an employer that the sender's facts also name as a past employer. Mention "
                     "that shared employer once, plainly, and do not say or imply that they worked together, overlapped "
                     "or knew each other. Then one line about what they do now, from their card, and a low ask, a "
                     "short chat about their team or company."),
    "engineer": ("an engineer on the team at the company the sender is interested in. Pick one specific detail from their "
                 "headline or from their post if one is given, add one line of overlap taken from the sender's facts (the "
                 "work or project closest to it), and make a low ask, such as a question about their team."),
    "recruiter": ("a recruiter. One line on who the sender is, the kind of role they are looking at (the job title when "
                  "one is given, otherwise the kind of work in the sender's facts), and an offer to send their résumé. "
                  "Nothing else: no flattery and no questions about their process."),
    "hiring_manager": ("a hiring manager or founder who wrote a post about a role, which is the post text in the request. "
                       "Refer to the post and the role in a few words of your own, say who the sender is with the one "
                       "project or piece of work from their facts that fits that role best, and offer to send their "
                       "résumé. Do not say the sender applied."),
    "post": ("someone who wrote a hiring post, which is the post text in the request. Refer to the post briefly, add one "
             "line on why the sender's background fits, from their facts, and either offer their résumé or ask whether "
             "the role is still open. Do not say the sender applied."),
    "generic": ("a person at a company the sender is interested in, and nothing in the request says how they relate to "
                "the sender. Write a short, plain note: who the sender is in a clause, why this company or their work, "
                "and a request to connect. Do not pretend there is a connection."),
}
# The text being written, per kind.
KIND_BLOCKS = {
    "note": ("Write the LinkedIn connection note. Length: {target}, counting spaces; LinkedIn cuts anything over "
             "300 characters, so stay under it. Start with a greeting by first name."),
    "message": ("They accepted the sender's connection request, and this is the sender's first message to them. Thank "
                "them in a word or two, without gushing, then ask one short question about their path, their team or "
                "their company, based on their card text (or the shared school or employer). Do not ask for a referral "
                "or a job in this message; that belongs to a later one. Length: {target}."),
    "follow_up": ("The sender sent them the earlier text below and has had no reply. Write a short nudge: refer to the "
                  "earlier text in a clause, add one new true thing only when the facts in the request contain one "
                  "(otherwise a light reminder of the same single ask), and make it easy to say no. Do not apologise for "
                  "writing again and do not guilt them. Length: {target}."),
    "email": ("Write a short cold email, in this form: a line \"Subject: \" with a subject of at most 8 words, a blank "
              "line, then the body in plain text. Length: {target}. The body names the one project or piece of work "
              "from the sender's facts that fits best, with its link if the facts give one, then asks for a short chat "
              "or, if there is a role, to be considered. End with the sender's first name."),
}


@dataclass
class Sender:
    name: str = ""
    first_name: str = ""
    last_name: str = ""
    facts: str = ""                       # the sender-facts block: the only facts about him a draft may use
    gaps: list = field(default_factory=list)   # what the stored profile lacks (shown in the log and the report)

    @property
    def names(self) -> list:
        return [x for x in (self.name, self.first_name, self.last_name) if x]


def _one_line(text, limit: int = 0) -> str:
    t = " ".join(str(text or "").replace("**", "").split())
    if limit and len(t) > limit:
        cut = t[:limit]
        stop = max(cut.rfind(". "), cut.rfind("; "))
        t = (cut[:stop] if stop > limit // 2 else cut.rsplit(" ", 1)[0]).rstrip(" .,;")
    return t


def _degree(text: str) -> str:
    """"MS, Computer Science - GPA 3.90 / 4.00" -> "MS, Computer Science": a grade is a number to be tempted by."""
    return _one_line(re.split(r"\s+[-–]\s+(?=GPA|CGPA|Grade)|\s*\(?\bGPA\b", str(text or ""), maxsplit=1)[0])


def _projects(persona, limit: int = 2) -> list:
    """At most `limit` named projects: the résumé's own project list in its order, else the networking
    profile's visible projects with a one-liner, newest first."""
    rc = persona.resume_content if isinstance(persona.resume_content, dict) else {}
    out = []
    for p in rc.get("projects") or []:
        if isinstance(p, dict) and _one_line(p.get("name")):
            bullets = [b for b in p.get("bullets") or [] if _one_line(b)]
            what = _one_line(bullets[0], 170) if bullets else ""
            out.append({"name": _one_line(p["name"]), "what": what, "stack": _one_line(p.get("description"), 120),
                        "url": _one_line(p.get("url"))})
    if not out:
        net = persona.networking if isinstance(persona.networking, dict) else {}
        rows = [p for p in net.get("projects") or [] if isinstance(p, dict) and not p.get("hidden")
                and _one_line(p.get("name")) and _one_line(p.get("one_liner")) and p.get("name") != "blog"]
        rows.sort(key=lambda p: str(p.get("pushed_at") or ""), reverse=True)
        for p in rows:
            out.append({"name": _one_line(p["name"]), "what": _one_line(p.get("one_liner"), 170),
                        "stack": ", ".join(str(x) for x in (p.get("stack") or [])[:6]), "url": _one_line(p.get("url"))})
    return out[:limit]


def build_sender(persona) -> Sender:
    """The sender-facts block, from the stored profile (a Persona row, or None) and nothing else: name,
    headline, schools with degrees, employers with titles, and at most two named projects. What the profile
    lacks is listed in `gaps`, never filled in from anywhere else."""
    if persona is None:
        return Sender(name="the candidate", facts="", gaps=["no profile is stored"])
    contact = persona.contact if isinstance(persona.contact, dict) else {}
    rc = persona.resume_content if isinstance(persona.resume_content, dict) else {}
    net = persona.networking if isinstance(persona.networking, dict) else {}
    first, last = _one_line(contact.get("first_name")), _one_line(contact.get("last_name"))
    name = " ".join(x for x in (first, last) if x) or "the candidate"
    gaps, lines = [], [f"Name: {name}"]
    headline = _one_line(contact.get("headline") or rc.get("headline") or net.get("headline"))
    if headline:
        lines.append(f"Headline: {headline}")
    else:
        gaps.append("headline: no headline is stored (LinkedIn has one)")
    from backend.curator import audience as aud
    school_rows, job_rows = aud.entries(persona)
    schools = [f"{_degree(e['degree'])}, {e['school']}" if _degree(e["degree"]) else e["school"] for e in school_rows]
    if schools:
        lines.append("Education: " + "; ".join(schools))
    else:
        gaps.append("education: none stored")
    jobs = [f"{j['title']} at {j['company']} ({'current' if j['current'] else 'past'})" for j in job_rows]
    if jobs:
        lines.append("Experience: " + "; ".join(jobs))
    else:
        gaps.append("experience: none stored")
    projects = _projects(persona)
    if projects:
        lines.append("Projects:")
        for p in projects:
            bits = [p["name"]] + [x for x in (p["what"], p["stack"] and f"built with {p['stack']}", p["url"]) if x]
            lines.append("- " + ": ".join(bits[:2]) + "".join(f" ({x})" for x in bits[2:]))
    else:
        gaps.append("projects: none stored")
    return Sender(name=name, first_name=first, last_name=last, facts="\n".join(lines), gaps=gaps)


def load_sender(db=None) -> Sender:
    """The current account's sender facts; once per run, not once per person."""
    from backend.accounts.owned import get_persona
    from backend.models.db import SessionLocal
    own = db is None
    db = db or SessionLocal()
    try:
        return build_sender(get_persona(db, create=False))
    finally:
        if own:
            db.close()


def card_text(c: dict) -> str:
    """What a person's own card or profile says, as one block: headline, the current and past position
    lines LinkedIn adds, and the location. Never the text of a post."""
    parts = []
    if c.get("headline"):
        parts.append(str(c["headline"]))
    if c.get("current_text"):
        parts.append(f"Current: {c['current_text']}")
    if c.get("past_text"):
        parts.append(f"Past: {c['past_text']}")
    if c.get("location"):
        parts.append(f"Location: {c['location']}")
    return "\n".join(parts)


@dataclass
class Brief:
    """Everything a draft may draw on. The prompt is built from these fields and the validator is handed
    `inputs()`, so the fact check sees exactly what the model saw."""
    sender: Sender
    name: str                       # the recipient
    card: str = ""                  # card_text()
    evidence: str = ""              # the hiring post or the snippet the person was found from
    company: str = ""
    job_title: str = ""
    previous: str = ""              # the earlier text a follow-up refers to
    audience: Optional[str] = None
    applied: bool = False
    from_post: bool = False         # the lead came from a hiring post (post search), so `evidence` is that post

    def inputs(self) -> list:
        return [x for x in (self.sender.facts, self.name, self.card, self.evidence, self.company, self.job_title,
                            self.previous) if x]

    def block(self) -> str:
        a = self.audience
        if a in ("hiring_manager", "post") and not (self.from_post and self.evidence):
            a = None            # no post to point to: the plain generic note
        return AUDIENCE_BLOCKS.get(a or "", AUDIENCE_BLOCKS["generic"])

    def prompt(self, kind: str) -> str:
        parts = ["About the sender (the only facts about the sender you may use):\n" + self.sender.facts,
                 f"The person the sender is writing to: {self.name}"]
        if self.card:
            parts.append("What their own card or profile says:\n" + self.card)
        if self.evidence:
            parts.append(("The hiring post they wrote:\n" if self.from_post else "The profile snippet they were found from:\n")
                         + self.evidence)
        if self.company:
            parts.append(f"Company: {self.company}")
        if self.job_title:
            parts.append(f"Role the sender is interested in: {self.job_title}. "
                         + ("The sender has applied to it." if self.applied else "The sender has not applied to it."))
        if kind == "follow_up" and self.previous:
            parts.append("The earlier text the sender sent:\n" + self.previous)
        parts.append("The person is " + self.block())
        parts.append(KIND_BLOCKS[kind].format(target=note_check.KIND_TARGET[kind]))
        return "\n\n".join(parts)


_LABEL = re.compile(r"^\s*(?:connection note|note|message|follow[- ]?up|draft)\s*:\s*", re.I)


def plain(text) -> Optional[str]:
    """The model's reply as the text itself: a JSON reply with a text field, a label and wrapping quotes are
    taken off. The text is never edited beyond that."""
    t = str(text or "").strip()
    if t.startswith("{"):
        from backend.analyzer.model_json import parse_model_json
        out = parse_model_json(t)
        if isinstance(out, dict):
            t = str(next((out[k] for k in ("note", "message", "text", "email") if out.get(k)), "")).strip()
    t = _LABEL.sub("", t)
    if len(t) > 1 and ((t[0] == t[-1] and t[0] in "\"'") or (t[0] == "“" and t[-1] == "”")):
        t = t[1:-1].strip()
    return t or None


async def draft_text(kind: str, brief: Brief, purpose: str, job_id=None, max_tokens: int = 700) -> NoteResult:
    """Ask for the text, validate it, retry once with the problems named, and flag it when it still fails."""
    prompt = brief.prompt(kind)
    names = [*brief.sender.names, brief.name]

    async def ask(p: str) -> Optional[str]:
        return plain(await llm.ask(p, SYSTEM, purpose=purpose, max_tokens=max_tokens, job_id=job_id))

    try:
        first = await ask(prompt)
    except Exception as e:
        logger.info(f"{purpose}: no draft ({e})")
        first = None

    async def retry(problems, text):
        return await ask(prompt + note_check.retry_instruction(text, problems, kind))

    return await note_check.settle_note(first, retry, brief.inputs(), names, brief.applied,
                                        sender=[brief.sender.facts], recipient_terms=[brief.company, brief.name],
                                        kind=kind)
