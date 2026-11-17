"""Validate a drafted LinkedIn connection note instead of cutting it.

A note over LinkedIn's 300 characters used to be sliced, which cut sentences in half and could
send a note that ended mid-word. Now a draft is checked, the model gets one retry with what was
wrong stated back to it, and a draft that still fails is not saved: the lead is flagged as
needing a note written by hand (Contact.note_manual, Contact.note_issue).

The same checks cover the other texts the tool drafts, each with its own length limit (LIMITS): the
connection note, the message after a request is accepted, a follow-up, and the cold email.

The checks (`check_note`):
  * at most NOTE_MAX (300) characters for a note (the prompts ask for 250 to 280); 600 for the message
    after acceptance, 400 for a follow-up; an email is at most 150 words with a subject of at most 8;
  * no flattery phrases, no "I hope this finds you well", no emoji, at most one exclamation mark;
  * no word about visas, sponsorship or work authorisation: it is not for notes or first messages;
  * no em dash, no invisible characters, no letters from a script the inputs do not use (look-alikes);
  * it does not say the sender applied to the role unless the job has an application;
  * `fact_check`: nothing the inputs (the texts the prompt was given) do not contain:
      - numbers in any form: digits, 40%, 10x, 50k, and spelled-out ones (five, twice, dozen, first);
      - capitalised proper-noun phrases; a single capitalised word that only starts a sentence or
        follows a greeting is not one (it is capitalised by grammar), except a well-known company or
        school name; a multi-word run and any word capitalised mid-sentence are checked strictly;
      - names of technologies and products from a built-in vocabulary, even in lowercase;
      - attribution: a term only the sender's own facts contain must not be put in a clause about
        the recipient ("your team's work on X", "X at <their company>").

It is deliberately conservative: it would rather reject a fine note, which only means the lead gets a
manual note, than let an invention through. What it cannot catch: an invented single company or person
name that only starts a sentence ("Zephyr is great") and is not a well-known one; invented facts in
plain words ("led a team", "shipped last year"); a technology outside the built-in vocabulary written
in lowercase; wrong attribution that is not one of the simple patterns above (see the tests).
"""
import logging
import re
import unicodedata
from dataclasses import dataclass, field
from typing import Awaitable, Callable, Iterable, Optional

logger = logging.getLogger("jobnavigator.curator.note_check")

NOTE_MAX = 300          # LinkedIn Premium's limit for a connection note
NOTE_TARGET = "250 to 280"
EM_DASH = "\u2014"
MESSAGE_MAX = 600       # the LinkedIn message after a request is accepted has no hard limit; this is the tool's
FOLLOW_UP_MAX = 400
EMAIL_WORDS_MAX = 150
EMAIL_SUBJECT_WORDS_MAX = 8
KINDS = ("note", "message", "follow_up", "email")
# the character limit of each kind (an email is limited by words instead)
LIMITS = {"note": NOTE_MAX, "message": MESSAGE_MAX, "follow_up": FOLLOW_UP_MAX}
KIND_NAME = {"note": "connection note", "message": "message", "follow_up": "follow-up", "email": "email"}
KIND_TARGET = {"note": NOTE_TARGET + " characters (never more than 300)",
               "message": "300 to 450 characters (never more than 600)",
               "follow_up": "150 to 300 characters (never more than 400)",
               "email": "80 to 120 words, with a subject of at most 8 words"}

_GREETINGS = {"hi", "hello", "hey", "hiya", "dear", "greetings", "thanks", "thank", "cheers", "regards", "best",
              "sincerely", "warmly", "morning", "afternoon", "evening", "there", "team", "all"}
# capitalised without being a fact, anywhere in a note
_ALLOWED_ANYWHERE = {"i", "i'm", "i'd", "i've", "i'll", "linkedin", "ai", "ml"} | _GREETINGS
_MONTHS = ("January February March April May June July August September October November December "
           "Jan Feb Mar Apr Jun Jul Aug Sep Sept Oct Nov Dec").split()
_DAYS = "Monday Tuesday Wednesday Thursday Friday Saturday Sunday Mon Tue Tues Wed Thu Thur Thurs Fri Sat Sun".split()
_CALENDAR = {w.lower() for w in _MONTHS + _DAYS}

# A single capitalised word at the start of a sentence is waved through (grammar capitalises it), but
# not when it is one of these well-known names: "Stripe taught me" must still be caught.
_KNOWN_PROPER = {w.lower() for w in """
Google Amazon Meta Microsoft Apple Netflix Uber Lyft Stripe Airbnb Shopify Salesforce Oracle IBM Intel Nvidia OpenAI
Anthropic Databricks Snowflake Palantir Coinbase Robinhood Plaid Ramp Brex Datadog Cloudflare Atlassian Slack Zoom
Dropbox Adobe Tesla SpaceX Bloomberg Goldman JPMorgan Citi Twilio Figma Notion Canva Instacart DoorDash Pinterest
Snap Spotify Reddit LinkedIn GitHub GitLab Stanford Harvard MIT Berkeley Columbia Cornell Princeton Yale Caltech CMU
Purdue Rutgers NYU Waterloo Toronto UCLA USC Northeastern Duke Brown Penn
""".split()}

# technology and product names that give an invented detail away even in lowercase
_TECH = {w for w in """
kafka kubernetes k8s docker terraform ansible redis postgres postgresql mysql mariadb sqlite mongodb dynamodb cassandra
elasticsearch opensearch rabbitmq nginx jenkins graphql grpc protobuf django flask fastapi pydantic sqlalchemy celery
pandas numpy scipy sklearn scikit-learn pytorch tensorflow keras jax huggingface langchain llamaindex llm llms rag
airflow dbt snowflake bigquery redshift databricks hadoop pyspark flink beam presto trino aws gcp azure lambda ec2 s3
cloudformation cloudflare vercel heroku netlify react vue angular svelte nextjs nuxt webpack vite redux typescript
javascript nodejs node.js deno golang rust kotlin scala clojure elixir erlang haskell ruby rails php laravel dotnet .net
csharp c++ cpp java jvm graalvm spring-boot springboot hibernate maven gradle junit pytest selenium cypress playwright
prometheus grafana datadog splunk kibana logstash sentry opentelemetry istio envoy consul vault argocd helm github-actions
gitlab-ci circleci travis openai anthropic gpt chatgpt claude gemini llama mistral bert transformers cuda onnx mlflow
kubeflow ray dask polars arrow parquet avro iceberg delta-lake clickhouse timescaledb neo4j supabase firebase
stripe plaid twilio sendgrid salesforce hubspot shopify sap oracle snowpark tableau looker powerbi superset metabase
linux ubuntu debian bash zsh vim emacs vscode intellij websocket websockets oauth jwt saml ldap kerberos blockchain
solidity ethereum bitcoin
""".split()}

_NUMBER_WORDS = {w: d for w, d in {
    "one": "1", "two": "2", "three": "3", "four": "4", "five": "5", "six": "6", "seven": "7", "eight": "8",
    "nine": "9", "ten": "10", "eleven": "11", "twelve": "12", "thirteen": "13", "fourteen": "14", "fifteen": "15",
    "sixteen": "16", "seventeen": "17", "eighteen": "18", "nineteen": "19", "twenty": "20", "thirty": "30",
    "forty": "40", "fifty": "50", "sixty": "60", "seventy": "70", "eighty": "80", "ninety": "90",
    "hundred": "100", "hundreds": "100", "thousand": "1000", "thousands": "1000", "million": "1000000",
    "millions": "1000000", "billion": "1000000000", "dozen": "12", "dozens": "12", "half": "0.5",
    "first": "1", "third": "3", "fourth": "4", "fifth": "5", "twice": "2", "thrice": "3", "double": "2",
    "doubled": "2", "triple": "3", "tripled": "3"}.items()}
# "one" is ordinary English in these frames and is not a claim
_ONE_OK_AFTER = {"of", "question", "thing", "more", "quick", "short", "small", "last", "line", "ask", "idea", "day",
                 "minute", "moment", "chat", "call", "note", "request", "suggestion", "another", "other"}
_ONE_OK_BEFORE = {"any", "every", "no", "each", "some", "the", "this", "that", "which", "at", "as"}

_WORD = re.compile(r"[^\W_][\w'\u2019+#&.\-]*")
_NUMBER = re.compile(r"(?<![^\W_])(\d[\d,]*(?:\.\d+)?)(%|[xkmb](?![^\W\d_]))?")
_SENTENCE_END = re.compile(r"[.!?]\s+|\n+")
_CLAUSE_SPLIT = re.compile(r"[,;:()\u2014\u2013]|\s-\s")
_SENDER_MARKERS = {"i", "i'm", "i've", "i'd", "i'll", "my", "me", "mine", "we", "our", "we're"}
_RECIPIENT_MARKERS = {"your", "you", "you're", "yours", "you've", "their", "they"}

_APPLIED = re.compile(
    r"\b(?:i|we|just|already|recently|have|i've|ive)\s+(?:\w+\s+)?applied\b"
    r"|\bmy\s+(?:recent\s+|latest\s+)?application\b"
    r"|\b(?:application|applications)\s+(?:is\s+|are\s+|has\s+been\s+|was\s+)?(?:in|submitted|sent)\b"
    r"|\bsubmitted\s+(?:an|my|the)\s+application\b", re.I)


_FLATTERY = re.compile(
    r"\bi hope (?:this|you)\b[^.!?\n]{0,40}\bwell\b|\bhope you(?:'|\u2019)?re (?:doing )?well\b"
    r"|\b(?:impressive|inspiring|inspired by|amazing|incredible|awesome|fantastic|outstanding|remarkable|brilliant|"
    r"passionate|thrilled|excited to connect)\b|\blove (?:your|what you)\b|\bhuge fan\b|\bbig fan\b"
    r"|\bi came across your (?:profile|post)\b|\bi stumbled (?:upon|across)\b|\bpicking your brain\b", re.I)
# Work authorisation belongs to a later stage of the conversation, never to a note or a first message. Matched by
# phrase, not by bare word, so a company called Visa, "sponsored an event", "opt-in" and "F1 score" pass.
_H1B = r"h[\s\-‐-―]?1[\s\-‐-―]?b"
_STATUS_VISA = r"(?:h[\s\-‐-―]?1[\s\-‐-―]?b|[lojh][\s\-‐-―]1|f[\s\-‐-―]?1|tn|work|student|employment|tourist|immigrant|sponsored|immigration)"
_WORK_AUTH_CI = re.compile(
    rf"\b{_H1B}\b"                                                       # H-1B, H1B, H 1 B
    r"|\bh[\s\-‐-―]?1[\s\-‐-―]?b1\b"                           # H-1B1
    r"|\be[\-‐-―]3\b"                                            # E-3 (a separator is required: "E3" alone is not)
    r"|\b[lojh][\s\-‐-―]1[ab]?\b"                               # L-1, O-1, J-1 (with a separator)
    r"|\b(?:visa|visas)\s+(?:status|sponsorship|sponsor\w*|support|transfer|requirements?|process|holder|issues?|application)\b"
    rf"|\b{_STATUS_VISA}\s+visas?\b"
    r"|\b(?:need|needs|needing|require|requires|requiring|obtain|get|getting|hold|holds|holding|have|having|on)\s+(?:an?\s+|my\s+|a\s+valid\s+)?(?:\w+\s+)?visa\b(?!['’]s|-)"
    r"|\b(?:immigration|visa|work|employment|employer|h-?1b)\s+sponsorship\b"
    r"|\b(?:need|needs|needed|needing|require|requires|required|requiring)\s+(?:\w+\s+){0,2}sponsored\b"   # need to be sponsored
    r"|\bsponsorship\s+(?:is\s+|will\s+be\s+|would\s+be\s+)?(?:required|needed|necessary)\b"
    r"|\b(?:need|needs|needing|require|requires|requiring|provide|provides|offer|offers|without|no)\s+(?:\w+\s+)?sponsorship\b"
    r"|\bsponsor(?:s|ed|ing)?\s+(?:my\s+|his\s+|her\s+|a\s+|the\s+)?(?:\w+\s+)?(?:visa|h-?1b|green\s+card|work\s+(?:permit|authori[sz]ation))\b"
    r"|\b(?:work|employment)\s+(?:authori[sz]ation|authori[sz]ed|permit|status|eligibility)\b"
    r"|\bauthori[sz]ed\s+to\s+work\b|\beligible\s+to\s+work\b|\bright\s+to\s+work\b|\bworking\s+rights\b"
    r"|\bpermanent\s+resident\w*\b|\bgreen\s*card\b|\bcitizenship\b|\bcitizen\b"
    r"|\bstem\s+opt\b|\bopt\s+status\b|\b(?:on|my|in|during|post[- ]completion)\s+opt\b(?![\-‐-―])|\bcurricular\s+practical\s+training\b"
    r"|\bf[\s\-‐-―]?1\s+(?:visa|status|student|opt)\b|\b(?:visa|status|student)\s+f[\s\-‐-―]?1\b(?!\s+score)|\bon\s+(?:an?\s+)?f[\-‐-―]1\b"
    r"|\btn\s+(?:visa|status)\b|\bimmigration\b", re.I)
# upper-case tokens only: OPT, EAD, CPT, TN-1 (opt-in, opt out, "ead" inside a word, "tn" for Tennessee are not matched)
_WORK_AUTH_CS = re.compile(r"(?<![A-Za-z0-9])(?:OPT|EAD|CPT)(?![A-Za-z0-9\-])|(?<![A-Za-z0-9])STEM[\s\-]OPT(?![A-Za-z0-9])")


def work_auth_hit(text: str, company_terms: Iterable[str] = ()) -> Optional[str]:
    """The immigration or work-authorisation wording in `text`, or None. A recipient or job company named Visa
    is not wording about visas: its capitalised name is taken out before the phrases are looked for."""
    t = text
    if any(re.fullmatch(r"\s*visa\s*", _norm(c or "")) for c in company_terms):
        t = re.sub(r"\bVisa\b(?!\s+(?:status|sponsorship|support|transfer|requirements?|holder|issues?))", " ", t)
    m = _WORK_AUTH_CI.search(t) or _WORK_AUTH_CS.search(t)
    return m.group(0) if m else None


def _has_emoji(text: str) -> bool:
    """Pictographs, symbols and dingbats, emoji modifiers and joiners, arrows, and the watch, hourglass and media
    symbols: any symbol-other character, minus a few ordinary signs (copyright, registered, trademark, degree)."""
    for ch in text:
        cp = ord(ch)
        if cp in (0x00A9, 0x00AE, 0x2122, 0x00B0):
            continue
        cat = unicodedata.category(ch)
        if cat in ("So", "Sk") or cp in (0xFE0F, 0x200D, 0x20E3) or 0x2190 <= cp <= 0x21FF or 0x2B00 <= cp <= 0x2BFF \
                or 0x231A <= cp <= 0x23FF or 0x2600 <= cp <= 0x27BF or 0x1F000 <= cp <= 0x1FAFF or 0xE0020 <= cp <= 0xE007F:
            return True
    return False


def split_email(text: str) -> tuple:
    """("subject", "body") of a drafted email written as "Subject: ...", a blank line, the body. The subject
    is empty when there is no Subject line."""
    m = re.match(r"\s*subject\s*:\s*(.*?)\s*\n\s*\n(.*)\Z", text or "", re.I | re.S)
    return (m.group(1).strip(), m.group(2).strip()) if m else ("", (text or "").strip())


def _strip_invisible(text: str) -> str:
    return "".join(ch for ch in text if unicodedata.category(ch) != "Cf")


def _norm(text: str) -> str:
    """NFKC (fullwidth and compatibility forms become plain ones), invisible characters removed,
    whitespace collapsed, lower-cased."""
    t = _strip_invisible(unicodedata.normalize("NFKC", text or "")).replace("\u2019", "'")
    return re.sub(r"\s+", " ", t).lower()


def _clean(text: str) -> str:
    return _strip_invisible(unicodedata.normalize("NFKC", text or ""))


def _clean_word(w: str) -> str:
    w = re.sub(r"'s$", "", w.replace("\u2019", "'"))
    w = re.sub(r"^[a-z]+-(?=[A-Z])", "", w)        # "ex-Kotak" is the name Kotak behind a lowercase prefix
    w = re.sub(r"^([A-Z][A-Za-z0-9.]*)-[a-z]+$", r"\1", w)      # "Kafka-based" is the name Kafka
    return w.strip(".,;:!?-'&")


def _in_inputs(phrase: str, hay: str) -> bool:
    p = _norm(phrase)
    return bool(p) and re.search(r"(?<![^\W_])" + re.escape(p) + r"(?![^\W_])", hay) is not None


def _is_cap(word: str) -> bool:
    return bool(word) and (word[0].isupper() or any(ch.isupper() for ch in word[1:]))


def _numbers(text_lower: str) -> list:
    return [(m.group(1).rstrip(",.").replace(",", ""), m.group(2) or "") for m in _NUMBER.finditer(text_lower)]


def _scripts(text: str) -> set:
    out = set()
    for ch in text:
        if ch.isalpha():
            try:
                out.add(unicodedata.name(ch).split()[0])
            except ValueError:
                out.add("UNKNOWN")
    return out


def _runs(sentence: str, exempt: set) -> list:
    """Capitalised runs of a sentence as [(words, initial)]: adjacent capitalised words (an "of" may
    join two: University of Mumbai). `initial` marks a run that starts a sentence or follows the
    greeting ("Hi Maya, Saw your post"), where the capital is grammar. Names in `exempt`, greetings,
    month and day names are not part of a run."""
    toks = [(_clean_word(m.group(0)), m.start(), m.end()) for m in _WORD.finditer(sentence)]
    toks = [t for t in toks if t[0]]
    runs, run, run_initial = [], [], False
    clause_start, greeting = True, False
    prev_end = None

    def flush():
        nonlocal run
        if run and run[-1].lower() == "of":
            run.pop()
        if run:
            runs.append((list(run), run_initial))
        run = []

    for w, start, end in toks:
        lw = w.lower()
        follow = sentence[end:end + 1]
        adjacent = prev_end is not None and sentence[prev_end:start].strip() == ""
        prev_end = end
        if clause_start and not run and lw in _GREETINGS and not greeting:
            greeting = True
            if follow in ",:;!-\u2014\u2013":
                greeting, clause_start = False, True
            continue
        if greeting:
            if lw in exempt or lw in _GREETINGS:
                if follow in ",:;!-\u2014\u2013":
                    greeting, clause_start = False, True
                continue
            greeting, clause_start = False, True          # the greeting had no comma: this word starts the clause
        if run and lw == "of" and adjacent:
            run.append(w)
            continue
        capital = _is_cap(w) and lw not in _ALLOWED_ANYWHERE and lw not in _CALENDAR and lw not in exempt
        if capital:
            if run and not adjacent:
                flush()
            if not run:
                run_initial = clause_start
            run.append(w)
        else:
            flush()
        clause_start = False
    flush()
    return runs


def _name_words(names: Iterable[str]) -> set:
    words = set()
    for n in names or []:
        for part in re.split(r"[\s,]+", _clean(str(n or ""))):
            part = _clean_word(part)
            if part:
                words.add(part.lower())
    return words


def _tech_terms(text_lower: str) -> list:
    out = []
    for w in re.findall(r"[^\W_][\w.+#\-]*", text_lower):
        w = w.strip(".-")
        for piece in {w, *re.split(r"[-/]", w)}:
            if piece in _TECH and piece not in out:
                out.append(piece)
    return out


def _attributed(note: str, terms: list, recipient_terms: set) -> list:
    """Of `terms` (only the sender's own facts contain them), those the note puts in a clause about
    the recipient: the last person word before the term in its clause is "your"/"you" or the
    recipient's company or name, or the term is followed by "at <recipient company>"."""
    bad = []
    for sentence in filter(None, re.split(_SENTENCE_END, note)):
        parts, last = [], 0
        for m in _CLAUSE_SPLIT.finditer(sentence):
            parts.append(sentence[last:m.start()])
            last = m.end()
        parts.append(sentence[last:])
        for clause in parts:
            low = clause.lower().replace("\u2019", "'")
            for term in terms:
                for m in re.finditer(r"(?<![^\W_])" + re.escape(term.lower()) + r"(?![^\W_])", low):
                    before = re.findall(r"[^\W_][\w']*", low[:m.start()])
                    who = None
                    for w in reversed(before):
                        w = re.sub(r"'s$", "", w) if w not in _SENDER_MARKERS else w
                        if w in _SENDER_MARKERS:
                            who = "sender"
                            break
                        if w in _RECIPIENT_MARKERS or any(w == t for t in recipient_terms):
                            who = "recipient"
                            break
                    after = low[m.end():]
                    at_them = any(re.match(r"\s+(?:at|for|from|in)\s+" + re.escape(t), after) for t in recipient_terms if len(t) > 2)
                    if (who == "recipient" or at_them) and term not in bad:
                        bad.append(term)
    return bad


def fact_check(draft: str, inputs: Iterable[str], names: Iterable[str] = (), sender: Iterable[str] = (),
               recipient_terms: Iterable[str] = ()) -> dict:
    """What the draft states that none of `inputs` (the exact texts the prompt was given) contains:
    {"numbers", "names", "tech", "attributed", "scripts"}. `names` are the sender's and recipient's names;
    `sender` the sender-facts texts among the inputs and `recipient_terms` the recipient's company and
    name, which together drive the attribution check."""
    inputs = [str(x) for x in inputs if x]
    sender = [str(x) for x in sender if x]
    text = _clean(draft)
    hay = _norm("\n".join(inputs))
    exempt = _name_words(names)
    out = {"numbers": [], "names": [], "tech": [], "attributed": [], "scripts": []}

    allowed_scripts = _scripts(hay) | {"LATIN"}
    out["scripts"] = sorted(_scripts(text) - allowed_scripts)

    in_nums = _numbers(hay)
    in_plain = {n for n, suf in in_nums}
    in_set = set(in_nums)
    low = text.lower()
    for n, suf in _numbers(low):
        shown = n + suf
        ok = (n, suf) in in_set if suf else n in in_plain
        if not ok and shown not in out["numbers"]:
            out["numbers"].append(shown)
    words = re.findall(r"[^\W\d_]+", low)
    for i, w in enumerate(words):
        digit = _NUMBER_WORDS.get(w)
        if digit is None:
            continue
        if w == "one" and ((i + 1 < len(words) and words[i + 1] in _ONE_OK_AFTER) or (i and words[i - 1] in _ONE_OK_BEFORE)):
            continue
        if w == "half" and i and words[i - 1] in {"a", "an"}:
            pass
        if not _in_inputs(w, hay) and digit not in in_plain and w not in out["numbers"]:
            out["numbers"].append(w)

    exempt_names = exempt
    for sentence in filter(None, re.split(_SENTENCE_END, text)):
        for run, initial in _runs(sentence, exempt_names):
            phrase = " ".join(run)
            if initial and len(run) == 1:
                if run[0].lower() in _KNOWN_PROPER and not _in_inputs(phrase, hay) and phrase not in out["names"]:
                    out["names"].append(phrase)
                continue
            if initial and len(run) > 1 and _in_inputs(" ".join(run[1:]), hay):
                continue
            if not _in_inputs(phrase, hay) and phrase not in out["names"]:
                out["names"].append(phrase)

    tech = _tech_terms(low)
    out["tech"] = [t for t in tech if not _in_inputs(t, hay)
                   and not any(t.lower() == n.lower() for n in out["names"])]

    recip_inputs = [x for x in inputs if x not in sender]
    recip_hay = _norm("\n".join(recip_inputs))
    sender_hay = _norm("\n".join(sender))
    if sender:
        rterms = {w.lower() for t in recipient_terms for w in re.findall(r"[^\W_][\w']*", _clean(str(t))) if len(w) > 2}
        rterms |= {_norm(str(t)) for t in recipient_terms if t}
        candidates = list(dict.fromkeys(tech + [" ".join(r).lower() for r, _ in
                                               [x for sent in filter(None, re.split(_SENTENCE_END, text))
                                                for x in _runs(sent, exempt_names)]]))
        sender_only = [t for t in candidates if _in_inputs(t, sender_hay) and not _in_inputs(t, recip_hay)]
        out["attributed"] = _attributed(text, sender_only, rterms)
    return out


def check_note(note: str, inputs: Iterable[str], names: Iterable[str] = (), job_applied: bool = False,
               sender: Iterable[str] = (), recipient_terms: Iterable[str] = (), kind: str = "note") -> list:
    """What is wrong with `note` (a text of `kind`: note, message, follow_up or email), one sentence each, for
    the retry and for the flag; [] when it is fine. An email is the text "Subject: ...", a blank line, the body."""
    problems = []
    recipient_terms = list(recipient_terms)
    text = _clean(note)
    n = len(note)
    if kind == "email":
        subject, body = split_email(text)
        if not subject:
            problems.append('it has no "Subject:" line followed by a blank line')
        elif len(subject.split()) > EMAIL_SUBJECT_WORDS_MAX:
            problems.append(f"the subject is {len(subject.split())} words; the limit is {EMAIL_SUBJECT_WORDS_MAX}")
        text = f"{subject}.\n\n{body}" if subject else body       # the subject is a sentence of its own, not a "Subject:" label
        words = len(body.split())
        if words > EMAIL_WORDS_MAX:
            problems.append(f"the body is {words} words; the limit is {EMAIL_WORDS_MAX} and it should be 80 to 120")
    else:
        limit = LIMITS.get(kind, NOTE_MAX)
        if n > limit:
            problems.append(f"it is {n} characters; the limit is {limit} and it should be {KIND_TARGET.get(kind, NOTE_TARGET)}")
    if EM_DASH in unicodedata.normalize("NFKC", note):
        problems.append("it contains an em dash; use a comma, a period or a colon")
    if any(unicodedata.category(ch) == "Cf" for ch in note):
        problems.append("it contains invisible characters")
    if _has_emoji(text):
        problems.append("it contains an emoji or symbol")
    if text.count("!") > 1 or text.count("\u203c") + text.count("\u2757") > 1:
        problems.append("it has more than one exclamation mark; use none, or one")
    flattery = _FLATTERY.search(text)
    if flattery:
        problems.append(f'it uses flattery or a stock phrase ("{flattery.group(0)}")')
    auth = work_auth_hit(text, recipient_terms)
    if auth:
        problems.append(f'it mentions work authorisation or visas ("{auth}"); that does not belong in this text')
    if not job_applied and _APPLIED.search(text):
        problems.append("it says the sender applied, but the sender has not applied to this role")
    bad = fact_check(text, inputs, names, sender, recipient_terms)
    if bad["scripts"]:
        problems.append("it uses letters from another script than the facts given: " + ", ".join(bad["scripts"]))
    if bad["numbers"]:
        problems.append("it states numbers that are not in the facts given: " + ", ".join(bad["numbers"]))
    if bad["names"]:
        problems.append("it names things that are not in the facts given: " + ", ".join(f'"{x}"' for x in bad["names"]))
    if bad["tech"]:
        problems.append("it mentions technologies that are not in the facts given: " + ", ".join(bad["tech"]))
    if bad["attributed"]:
        problems.append("it credits the recipient or their company with the sender's own work or skills: "
                        + ", ".join(bad["attributed"]))
    return problems


def retry_instruction(note: str, problems: list, kind: str = "note") -> str:
    """Appended to the original prompt for the one retry."""
    name = KIND_NAME.get(kind, "text")
    limit = (f"never more than {LIMITS[kind]} characters" if kind in LIMITS else f"never more than {EMAIL_WORDS_MAX} words")
    form = ("Write the email again as \"Subject: ...\", a blank line, then the body" if kind == "email"
            else f"Write the {name} again, the text only, nothing else")
    return (f"\n\nYour previous {name} was rejected:\n"
            f'"{note}"\n'
            "Problems: " + "; ".join(problems) + ".\n"
            f"{form}: {KIND_TARGET.get(kind, NOTE_TARGET)} ({limit}), using only the facts given above, with no em "
            "dashes, no mention of visas or work authorisation, and without saying the sender applied unless told so.")


@dataclass
class NoteResult:
    note: Optional[str]                  # the note to save; None when there is none to save
    manual: bool = False                 # True: the lead needs a note written by hand
    issue: Optional[str] = None          # why, in words (shown on the lead)
    problems: list = field(default_factory=list)


async def settle_note(first: Optional[str], retry: Callable[[list, str], Awaitable[Optional[str]]],
                      inputs: Iterable[str], names: Iterable[str] = (), job_applied: bool = False,
                      sender: Iterable[str] = (), recipient_terms: Iterable[str] = (), kind: str = "note") -> NoteResult:
    """Validate `first`; on a problem call `retry(problems, first)` once and validate its answer.
    Never truncates. A text that fails twice, or that the model never produced, is not saved."""
    inputs, sender, recipient_terms = list(inputs), list(sender), list(recipient_terms)
    note = (first or "").strip()
    name = KIND_NAME.get(kind, "text")
    if not note:
        return NoteResult(None, True, f"the model returned no {name}; write one by hand")
    problems = check_note(note, inputs, names, job_applied, sender, recipient_terms, kind)
    if not problems:
        return NoteResult(note)
    logger.info(f"{name} rejected ({'; '.join(problems)}); retrying once")
    second = ""
    try:
        second = ((await retry(problems, note)) or "").strip()
    except Exception as e:      # a failed retry is a flagged lead, not a failed run
        logger.warning(f"{name} retry failed: {e}")
    if second:
        again = check_note(second, inputs, names, job_applied, sender, recipient_terms, kind)
        if not again:
            return NoteResult(second)
        problems = again
    return NoteResult(None, True, "the draft was not usable twice: " + "; ".join(problems), problems)


def job_applied(job_id) -> bool:
    """True when the job has an application (applied, interview, offer or rejected)."""
    if not job_id:
        return False
    from backend.models.db import Application, SessionLocal
    db = SessionLocal()
    try:
        return db.query(Application.id).filter(Application.job_id == job_id).first() is not None
    except Exception as e:
        logger.warning(f"could not read the job's applications ({e}); treating it as not applied")
        return False
    finally:
        db.close()
