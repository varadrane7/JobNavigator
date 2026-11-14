"""A résumé as LaTeX, and that LaTeX as a PDF (pdflatex).

Why a second PDF path next to Chromium: Chromium's PDF writer (Skia) places every glyph with
its own text operator, about four thousand on a page, and a résumé parser has to rebuild
words, lines and paragraphs from positions. pdfTeX writes one operator per line of text in
Type 1 fonts, which is the PDF the parsers behind Workday and similar systems read cleanly.
A template whose meta.json says `"engine": "latex"` exports through this module; its
template.html.j2 is only the on-screen preview.

Everything printed comes from the résumé's JSON and is escaped: no user text reaches TeX as
code, and pdflatex runs with shell escape off in a throwaway directory.
"""
import asyncio
import re
import shutil
import tempfile
import unicodedata
from pathlib import Path

COMPILE_SECONDS = 45


class LatexError(RuntimeError):
    """pdflatex is missing or refused the document; the message is for the user."""


def available() -> bool:
    return shutil.which("pdflatex") is not None


# ── text ─────────────────────────────────────────────────────────────────────

_SPECIAL = {
    "\\": r"\textbackslash{}", "&": r"\&", "%": r"\%", "$": r"\$", "#": r"\#", "_": r"\_", "{": r"\{", "}": r"\}",
    "~": r"\textasciitilde{}", "^": r"\textasciicircum{}", "<": r"\textless{}", ">": r"\textgreater{}",
    "|": r"\textbar{}", '"': "''",
    "–": "--", "—": "---", "‘": "`", "’": "'", "“": "``", "”": "''", "•": r"\textbullet{}",
    "·": r"\textperiodcentered{}", "…": r"\ldots{}", "→": r"$\rightarrow$", "←": r"$\leftarrow$",
    "≥": r"$\geq$", "≤": r"$\leq$", "×": r"$\times$", "±": r"$\pm$", "≈": r"$\approx$", " ": "~",
    "€": r"\texteuro{}", "£": r"\pounds{}", "▸": r"\textbullet{}",
}


def esc(value) -> str:
    """`value` as LaTeX text. A character T1 cannot print is folded to ASCII or dropped."""
    out = []
    for ch in str(value or ""):
        if ch in _SPECIAL:
            out.append(_SPECIAL[ch])
        elif ch in "\n\r\t":
            out.append(" ")
        elif ord(ch) < 0x20 or ord(ch) == 0x7f:
            continue
        elif ord(ch) < 0x180:
            out.append(ch)
        else:
            out.append(unicodedata.normalize("NFKD", ch).encode("ascii", "ignore").decode())
    return "".join(out)


def rich(value) -> str:
    """Bullet text: `**x**` is bold, the rest escaped."""
    parts = re.split(r"\*\*(.+?)\*\*", str(value or ""))
    return "".join(r"\textbf{%s}" % esc(p) if i % 2 else esc(p) for i, p in enumerate(parts))


def _href(url, text) -> str:
    url = str(url or "").strip()
    if url and not re.match(r"^[a-zA-Z][a-zA-Z0-9+.\-]*:", url):
        url = "https://" + url
    if not re.match(r"^(https?://|mailto:)", url, re.I) or re.search(r"[\\{}\s]", url):
        return esc(text)
    return r"\href{%s}{%s}" % (re.sub(r"([%#&])", r"\\\1", url), esc(text))


def _joined(*parts) -> str:
    return ", ".join(str(p).strip() for p in parts if p and str(p).strip())


# ── document ─────────────────────────────────────────────────────────────────

_PREAMBLE = r"""\documentclass[%(size)s,%(paper)s]{article}
\pdfminorversion=4
\pdfobjcompresslevel=0
\usepackage[T1]{fontenc}
\usepackage[utf8]{inputenc}
\usepackage{lmodern}
\usepackage{textcomp}
\usepackage[hidelinks]{hyperref}
\input{glyphtounicode}
\pdfgentounicode=1
\hypersetup{pdftitle={%(title)s},pdfauthor={%(author)s}}
\linespread{%(spread)s}
\setlength{\textwidth}{\dimexpr\paperwidth-%(side)sin-%(side)sin}
\setlength{\oddsidemargin}{\dimexpr%(side)sin-1in}
\setlength{\topmargin}{\dimexpr%(top)sin-1in}
\setlength{\headheight}{0pt}\setlength{\headsep}{0pt}\setlength{\footskip}{0pt}
\setlength{\textheight}{\dimexpr\paperheight-%(top)sin-%(top)sin}
\pagestyle{empty}
\setlength{\parindent}{0pt}
\setlength{\parskip}{0pt}
\setlength{\tabcolsep}{0in}
\raggedright
\raggedbottom
\sloppy
\hyphenpenalty=10000
\exhyphenpenalty=10000
\newcommand{\sect}[1]{\vspace{4pt}{\large\scshape #1}\par\vspace{1pt}\hrule\vspace{3pt}}
\newcommand{\subheading}[4]{\begin{tabular*}{\textwidth}{l@{\extracolsep{\fill}}r}
\textbf{#1} & #2 \\
\textit{\small #3} & \textit{\small #4} \\
\end{tabular*}\par}
\newenvironment{bullets}{\begin{list}{\textbullet}{\setlength{\leftmargin}{1.3em}\setlength{\labelwidth}{0.6em}%%
\setlength{\labelsep}{0.5em}\setlength{\itemsep}{0pt}\setlength{\parsep}{0pt}\setlength{\topsep}{1pt}%%
\setlength{\partopsep}{0pt}\small}}{\end{list}}
\begin{document}
"""


# Leading and margins (inches), loosest first. `render_pdf` moves down the list only when a
# tighter page saves a whole page.
FITS = ({"spread": "1", "side": "0.5", "top": "0.4"},
        {"spread": "0.96", "side": "0.45", "top": "0.35"},
        {"spread": "0.92", "side": "0.4", "top": "0.3"})


def to_tex(json_data: dict, page_format: str = "letter", section_order=None, summary: bool = False,
           font_size: str = "10pt", fit: int = 0) -> str:
    """The résumé as a complete LaTeX document, in the layout of the common one-column
    LaTeX résumé: a role's title and place over its company and dates, bullets under it,
    projects and publications one line each. `section_order` is the list of {key, title}
    the HTML templates get. The summary is left out unless `summary` is true: résumé
    parsers often read a paragraph under the name as a first job."""
    d = {k: v for k, v in (json_data or {}).items() if not str(k).startswith("_")}
    header = d.get("header") if isinstance(d.get("header"), dict) else {}
    name = str(header.get("name") or "").strip()
    out = [_PREAMBLE % {"paper": "a4paper" if str(page_format or "").lower() == "a4" else "letterpaper",
                        "size": font_size if font_size in ("10pt", "11pt", "12pt") else "10pt",
                        **FITS[max(0, min(len(FITS) - 1, fit))],
                        "title": esc(name + " Resume" if name else "Resume"), "author": esc(name)}]
    if name:
        out.append(r"\textbf{\LARGE %s}\par" % esc(name))
    if header.get("title"):
        out.append(r"\vspace{2pt}%s\par" % esc(header["title"]))
    contact, detail = [], []
    for item in header.get("contact_items") or []:
        if not isinstance(item, dict) or not str(item.get("text") or "").strip():
            continue
        if item.get("url"):
            contact.append(_href(item["url"], item["text"]))
        elif ": " in item["text"]:
            detail.append(esc(item["text"]))      # "Nationality: Indian" goes on its own line
        else:
            contact.append(esc(item["text"]))
    for line in (contact, detail):
        if line:
            out.append(r"\vspace{2pt}{\small " + r" \textbar{} ".join(line) + r"}\par")

    def bullets(items):
        items = [b for b in items or [] if isinstance(b, str) and b.strip()]
        if items:
            out.append(r"\begin{bullets}")
            out.extend(r"\item " + rich(b) for b in items)
            out.append(r"\end{bullets}")

    def one_liners(entries):
        """Projects and publications: "Name: what it is", the name linked when it has a URL."""
        rows = []
        for e in entries:
            if not isinstance(e, dict):
                continue
            title = str(e.get("name") or e.get("title") or "").strip()
            head = (r"\textbf{%s}" % (_href(e["url"], title) if e.get("url") else esc(title))) if title else ""
            said = [b for b in e.get("bullets") or [] if isinstance(b, str) and b.strip()]
            body = " ".join(rich(b) for b in said) if said else esc(e.get("description"))
            tech = esc(str(e.get("description") or "").strip()) if said else ""
            if not head:                              # an untitled entry is just its text
                rows.append(r"\item " + " ".join(x for x in (body, f"({tech})" if tech else "") if x))
                continue
            rows.append(r"\item %s%s%s" % (head, f" ({tech})" if tech else "", f": {body}" if body else ""))
        if rows:
            out.append(r"\begin{bullets}")
            out.extend(rows)
            out.append(r"\end{bullets}")

    for sec in section_order or [{"key": k, "title": k.title()} for k in
                                 ("summary", "experience", "skills", "education", "projects", "publications")]:
        key, title, value = sec["key"], sec["title"], d.get(sec["key"])
        if not value or (key == "summary" and not summary):
            continue
        out.append(r"\sect{%s}" % esc(title))
        if key == "summary":
            out.append(r"{\small %s}\par" % esc(value))
        elif key == "experience":
            for job in value:
                if not isinstance(job, dict):
                    continue
                out.append(r"\subheading{%s}{%s}{%s}{%s}" % (esc(job.get("title")), esc(job.get("location")),
                                                             esc(job.get("company")), esc(job.get("date"))))
                if job.get("description"):
                    out.append(r"{\small %s}\par" % esc(job["description"]))
                bullets(job.get("bullets"))
                out.append(r"\vspace{2pt}")
        elif key == "skills":
            pairs = value.items() if isinstance(value, dict) else [
                (s.get("category"), s.get("items")) for s in value if isinstance(s, dict)]
            for label, items in pairs:
                out.append(r"{\small \textbf{%s:} %s}\par\vspace{1pt}" % (esc(label), esc(items)))
        elif key == "education":
            for edu in value:
                if not isinstance(edu, dict):
                    continue
                degree, years = str(edu.get("degree") or ""), str(edu.get("years") or edu.get("date") or "")
                if years and years in degree:
                    years = ""                     # a degree line that already carries its dates
                out.append(r"\subheading{%s}{%s}{%s}{%s}" % (esc(edu.get("school")), esc(edu.get("location")),
                                                             esc(degree), esc(years)))
                bullets(edu.get("bullets"))
        else:
            one_liners(value)
    out.append(r"\end{document}")
    return "\n".join(out) + "\n"


# ── compile ──────────────────────────────────────────────────────────────────

def _log_errors(log: str) -> str:
    lines = [ln for ln in log.splitlines() if ln.startswith("!")]
    return "; ".join(lines[:3]) or "pdflatex failed without an error line"


async def compile_pdf(tex: str) -> bytes:
    """`tex` as PDF bytes. Raises LatexError when pdflatex is not installed, times out or
    stops on an error."""
    if not available():
        raise LatexError("pdflatex is not installed in this image: rebuild it "
                         "(`docker compose up --build -d backend`), or pick another template")
    with tempfile.TemporaryDirectory(prefix="jn-tex-") as tmp:
        (Path(tmp) / "resume.tex").write_text(tex, encoding="utf-8")
        proc = await asyncio.create_subprocess_exec(
            "pdflatex", "-interaction=nonstopmode", "-halt-on-error", "-no-shell-escape", "resume.tex",
            cwd=tmp, stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL)
        try:
            await asyncio.wait_for(proc.wait(), COMPILE_SECONDS)
        except asyncio.TimeoutError:
            proc.kill()
            raise LatexError("pdflatex took too long")
        pdf, log = Path(tmp) / "resume.pdf", Path(tmp) / "resume.log"
        if proc.returncode != 0 or not pdf.exists():
            raise LatexError(_log_errors(log.read_text(errors="replace") if log.exists() else ""))
        return pdf.read_bytes()


def page_count(pdf: bytes) -> int:
    return max(1, pdf.count(b"/Type /Page") - pdf.count(b"/Type /Pages"))


async def render_pdf(json_data: dict, page_format: str = "letter", section_order=None) -> tuple[bytes, int]:
    """(PDF bytes, pages). A résumé that runs a few lines onto another page is set tighter
    (FITS) when that brings it back; one that does not fit any tighter keeps the normal page."""
    first = await compile_pdf(to_tex(json_data, page_format, section_order))
    pages = page_count(first)
    if pages > 1:
        for fit in range(1, len(FITS)):
            pdf = await compile_pdf(to_tex(json_data, page_format, section_order, fit=fit))
            if page_count(pdf) < pages:
                return pdf, page_count(pdf)
    return first, pages
