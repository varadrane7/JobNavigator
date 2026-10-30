"""The European CV template: a contact line, then a details line for "Label: value" items
(nationality, work permit) that European recruiters screen on; A4 unless letter is asked."""
import re
from pathlib import Path

import pytest

jinja2 = pytest.importorskip("jinja2")

TEMPLATE_DIR = Path(__file__).resolve().parents[1] / "resume_templates" / "europe"


def _render(header, page_format="a4", **sections):
    from jinja2 import Environment, FileSystemLoader
    from markupsafe import Markup
    env = Environment(loader=FileSystemLoader(str(TEMPLATE_DIR)))
    env.filters["bold"] = lambda text: Markup(re.sub(r"\*\*(.+?)\*\*", r"<b>\1</b>", text or ""))
    content = {"summary": "", "experience": [], "skills": {}, "education": [], "projects": [],
               "publications": [], **sections}
    order = [{"key": k, "title": k.title()} for k in
             ("summary", "experience", "skills", "education", "projects", "publications")]
    return env.get_template("template.html.j2").render(
        header=header, page_format=page_format, fonts_base="", fonts={}, section_order=order, **content)


def _div(html, cls):
    m = re.search(rf'<div class="{cls}">(.*?)</div>', html, re.S)
    return m.group(1) if m else None


HEADER = {"name": "Dana Okonkwo", "contact_items": [
    {"text": "Lagos, Nigeria", "url": ""},
    {"text": "dana@example.com", "url": "mailto:dana@example.com"},
    {"text": "Nationality: Nigerian", "url": ""},
    {"text": "Work permit: EU Blue Card eligible", "url": ""},
]}


def test_label_value_items_go_on_the_details_line():
    html = _render(HEADER)
    contact, details = _div(html, "contact"), _div(html, "details")
    assert "Lagos, Nigeria" in contact and 'href="mailto:dana@example.com"' in contact
    assert "Nationality" not in contact and "Work permit" not in contact
    assert "<b>Nationality:</b> Nigerian" in details
    assert "<b>Work permit:</b> EU Blue Card eligible" in details


def test_no_details_line_without_label_value_items():
    html = _render({"name": "Dana", "contact_items": [{"text": "dana@example.com", "url": "mailto:dana@example.com"}]})
    assert _div(html, "details") is None


def test_a_linked_item_with_a_colon_stays_a_link():
    html = _render({"name": "Dana", "contact_items": [{"text": "Site: dana.dev", "url": "https://dana.dev"}]})
    assert 'href="https://dana.dev"' in _div(html, "contact")
    assert _div(html, "details") is None


def test_a4_by_default_and_letter_when_asked():
    assert "size: A4;" in _render(HEADER)
    assert "size: letter;" in _render(HEADER, page_format="letter")


def test_experience_shows_role_dates_then_company_and_place():
    html = _render(HEADER, experience=[{"title": "Backend Engineer", "company": "Acme", "location": "Berlin",
                                        "date": "03/2022 – present", "bullets": ["Shipped **X**."]}])
    assert re.search(r"Backend Engineer</span>\s*<span class=\"entry-date\">03/2022 – present", html)
    assert 'Acme<span class="place">, Berlin</span>' in html
    assert "<li>Shipped <b>X</b>.</li>" in html


def test_education_and_publication_render():
    html = _render(HEADER,
                   education=[{"degree": "MSc Informatics", "school": "TU Berlin", "location": "Berlin",
                               "years": "10/2020 – 09/2022"}],
                   publications=[{"title": "A Paper", "description": "Okonkwo, D. Springer, 2022."}])
    assert "MSc Informatics" in html and "TU Berlin" in html and "10/2020 – 09/2022" in html
    assert '<span class="pub-title">A Paper</span>. Okonkwo, D. Springer, 2022.' in html
