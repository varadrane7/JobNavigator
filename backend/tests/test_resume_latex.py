"""The LaTeX résumé path: JSON to .tex, and the PDF route's switch to pdflatex."""
import uuid

import pytest

from backend.api import resume_latex, routes_resumes as rr
from backend.models.db import Resume
from backend.tests.r4_support import client  # noqa: F401

DATA = {
    "header": {"name": "Dana O'Konkwo & Co", "title": "Engineer", "contact_items": [
        {"text": "dana_o@x.io", "url": "mailto:dana_o@x.io"}, {"text": "x.io/a", "url": "x.io/a?b=1&c=50%#d"},
        {"text": "bad", "url": "javascript:alert(1)"}, {"text": "Nationality: Indian", "url": ""}]},
    "summary": "Summary text",
    "experience": [{"title": "SWE II", "company": "Acme_Corp", "location": "Pune", "date": "2020 – 2022",
                    "bullets": ["Cut cost **30%** with C# & \\input{/etc/passwd} $x^2$ {y} ~z", "", "100% → done 🚀"]}],
    "skills": {"Languages": "C#, C++"},
    "education": [{"school": "NJIT", "degree": "MS, CS · Jan 2025 - May 2026", "location": "Newark", "years": "Jan 2025 - May 2026"}],
    "projects": [{"name": "Tool ", "description": "Go", "url": "https://x.io/tool", "bullets": ["Does **things**."]}],
    "publications": [{"title": "Paper", "description": "Springer, 2022."}],
}


def test_user_text_never_reaches_tex_as_code():
    tex = resume_latex.to_tex(DATA)
    body = tex[tex.index(r"\begin{document}"):]
    assert r"\textbackslash{}input\{/etc/passwd\}" in body and r"\input{/etc" not in body
    assert r"\$x\textasciicircum{}2\$" in body and r"\{y\}" in body and r"\textasciitilde{}z" in body
    assert r"\textbf{30\%}" in body and r"C\# \&" in body and r"Acme\_Corp" in body
    assert r"100\% $\rightarrow$ done " in body and "🚀" not in tex
    assert r"\item " + "\n" not in body                       # the empty bullet is dropped


def test_links_and_layout():
    tex = resume_latex.to_tex(DATA, "a4")
    assert "a4paper" in tex and "letterpaper" in resume_latex.to_tex(DATA)
    assert r"\href{mailto:dana_o@x.io}{dana\_o@x.io}" in tex
    assert r"\href{https://x.io/a?b=1\&c=50\%\#d}{x.io/a}" in tex
    assert "javascript" not in tex and " bad" in tex           # an unsafe link prints as text
    assert r"\subheading{SWE II}{Pune}{Acme\_Corp}{2020 -- 2022}" in tex
    assert r"\subheading{NJIT}{Newark}{MS, CS \textperiodcentered{} Jan 2025 - May 2026}{}" in tex   # dates not printed twice
    assert r"\item \textbf{\href{https://x.io/tool}{Tool}} (Go): Does \textbf{things}." in tex
    assert r"\item \textbf{Paper}: Springer, 2022." in tex
    assert "Summary text" not in tex and "Summary text" in resume_latex.to_tex(DATA, summary=True)
    assert tex.index("Nationality: Indian") > tex.index("x.io/a")


def test_section_order_and_titles_are_the_callers():
    order = [{"key": "skills", "title": "Tech"}, {"key": "experience", "title": "Work"}]
    tex = resume_latex.to_tex(DATA, section_order=order)
    assert tex.index(r"\sect{Tech}") < tex.index(r"\sect{Work}") and r"\sect{Education}" not in tex


def test_latex_template_is_the_only_latex_engine():
    assert rr.template_engine("latex") == "latex"
    assert rr.template_engine("ats") == "html" and rr.template_engine("no_such") == "html"


def _resume(test_db, template):
    r = Resume(id=uuid.uuid4(), name="General", is_base=True, template=template, page_format="letter", json_data=DATA)
    test_db.add(r)
    test_db.commit()
    return r


def test_pdf_route_compiles_a_latex_template_with_pdflatex(client, test_db, monkeypatch):
    seen = {}

    async def fake(tex):
        seen.setdefault("tex", tex)
        seen["tries"] = seen.get("tries", 0) + 1
        return b"%PDF-1.4 /Type /Pages /Type /Page /Type /Page"
    monkeypatch.setattr(resume_latex, "compile_pdf", fake)
    r = client.get(f"/api/resumes/{_resume(test_db, 'latex').id}/pdf")
    assert r.status_code == 200 and r.content.startswith(b"%PDF")
    assert r.headers["x-page-count"] == "2" and "2 pages" in r.headers["x-warning"]
    assert 'filename="DanaOKonkwoCo_Resume.pdf"' in r.headers["content-disposition"].replace("&", "").replace("'", "")
    assert r"\subheading{SWE II}" in seen["tex"] and seen["tries"] == len(resume_latex.FITS)   # tighter fits tried, none helped


def test_pdf_route_says_so_when_pdflatex_is_missing(client, test_db, monkeypatch):
    monkeypatch.setattr(resume_latex, "available", lambda: False)
    r = client.get(f"/api/resumes/{_resume(test_db, 'latex').id}/pdf")
    assert r.status_code == 503 and "pdflatex is not installed" in r.json()["detail"]


def test_tex_download_for_any_template(client, test_db):
    r = client.get(f"/api/resumes/{_resume(test_db, 'ats').id}/tex")
    assert r.status_code == 200 and r.text.startswith(r"\documentclass") and ".tex" in r.headers["content-disposition"]


@pytest.mark.skipif(not resume_latex.available(), reason="pdflatex not installed")
async def test_compile_makes_a_pdf_and_reports_a_tex_error():
    pdf = await resume_latex.compile_pdf(resume_latex.to_tex(DATA))
    assert pdf.startswith(b"%PDF") and pdf.count(b"/Type /Page") - pdf.count(b"/Type /Pages") == 1
    with pytest.raises(resume_latex.LatexError, match="!"):
        await resume_latex.compile_pdf(r"\documentclass{article}\begin{document}\nosuchmacro\end{document}")


async def test_a_tighter_fit_is_used_only_when_it_saves_a_page(monkeypatch):
    async def fake(tex):
        return b"/Type /Pages /Type /Page" + (b"" if r"\linespread{0.92}" in tex else b" /Type /Page")
    monkeypatch.setattr(resume_latex, "compile_pdf", fake)
    pdf, pages = await resume_latex.render_pdf(DATA)
    assert pages == 1 and r"\linespread{0.96}" in resume_latex.to_tex(DATA, fit=1)
