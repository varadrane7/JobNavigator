"""Documents for an item: the tailored résumé copy, its PDF, and an optional cover letter.

PDFs come from upstream's own export endpoints, called as plain coroutines with a session, so
the user's templates (Cambridge, section order, tracer links, filenames) apply unchanged."""
import logging
import os
import re

from backend.models.db import SessionLocal, CoverLetter

logger = logging.getLogger("jobnavigator.autoapply.docs")

_FILENAME = re.compile(r'filename="([^"]+)"')


def item_dir(data_dir: str, item_id) -> str:
    path = os.path.join(data_dir, str(item_id))
    os.makedirs(path, exist_ok=True)
    return path


def _write(resp, folder: str, fallback: str) -> str:
    m = _FILENAME.search(resp.headers.get("content-disposition", ""))
    name = os.path.basename(m.group(1)) if m else fallback
    path = os.path.join(folder, name)
    with open(path, "wb") as f:
        f.write(resp.body)
    return path


async def resume_pdf(resume_id: str, folder: str) -> str:
    from backend.api.routes_resumes import export_pdf
    db = SessionLocal()
    try:
        resp = await export_pdf(str(resume_id), template=None, format=None, db=db)
    finally:
        db.close()
    return _write(resp, folder, "Resume.pdf")


async def cover_letter(resume_id: str, job_id: str, folder: str, length: str = "concise") -> tuple[str, str]:
    """Generate a letter with upstream's generator (voice presets, persona preferences) and
    render it; returns (cover_letter_id, pdf_path)."""
    from backend.api.routes_cover_letters import _generate_impl, export_pdf
    await _generate_impl(resume_id=str(resume_id), job_id=str(job_id), voice=None, length=length,
                         template=None, page_format=None)
    db = SessionLocal()
    try:
        cl = (db.query(CoverLetter)
              .filter(CoverLetter.job_id == job_id, CoverLetter.resume_id == resume_id)
              .order_by(CoverLetter.created_at.desc()).first())
        if not cl:
            raise RuntimeError("cover letter was not saved")
        resp = await export_pdf(str(cl.id), db=db)
        return str(cl.id), _write(resp, folder, "CoverLetter.pdf")
    finally:
        db.close()
