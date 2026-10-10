"""The instructions the agent gets for one application. Everything about the applicant comes from the
request; nothing personal lives in this file."""
import json

STATUSES = ("submitted", "dry_run_ok", "needs_human", "captcha", "login_required", "closed", "already", "failed")

TEMPLATE = """You are filling in ONE job application in a real Chromium browser, using the Playwright browser tools.
Work carefully and finish it. You have no other tools: no shell, no files beyond the uploads listed below.

== THE JOB ==
Company: {company}
Title: {title}
Application URL: {url}

== MODE ==
{mode}

== FILES YOU CAN UPLOAD (paths for browser_file_upload) ==
{files}

== APPLICANT ==
{applicant}

== STANDARD ANSWERS (use these exactly when a question matches) ==
{answers}

== QUESTION BANK (answers the applicant gave to past questions; reuse when the meaning matches) ==
{qa_bank}

== RESUME (the source of truth for experience, dates, employers, skills) ==
{resume}
{cover_letter}
{extra}
== HOW TO WORK ==
1. browser_navigate to the application URL. Take a browser_snapshot and read it before acting.
2. If the page is a job description, find and click the Apply / Apply for this job / I'm interested button.
   If it offers "Apply manually", "Apply with resume" or "Continue as guest", prefer those over "Apply with LinkedIn/Indeed" and over creating an account.
3. If the posting is closed, removed or 404 -> report status "closed".
4. If you are told you already applied -> report status "already".
5. If the form needs a login or account you do not have, try a guest path first. If none -> report "login_required" (do NOT sign up for anything unless the page offers no other way AND an account email+password is given in EXTRA INSTRUCTIONS).
6. If there is a captcha you cannot pass by simply clicking a checkbox -> report "captcha".
7. Upload the resume to the resume/CV field first (browser_file_upload), then wait a moment: many forms autofill from it. Then check and correct every field.
8. Fill EVERY required field and every optional field you have a truthful answer for:
   - Name, email, phone, location, links: from APPLICANT.
   - Work authorization / sponsorship / relocation / salary / start date / notice period: from STANDARD ANSWERS or APPLICANT. Never guess these.
   - Voluntary self-identification (gender, race, veteran, disability): {self_id}
   - "How did you hear about us": "Company website" unless a better listed option exists, then the closest one.
   - Free-text questions ("Why do you want to work here?" etc.): 2-5 sentences, first person, specific to this company and role, grounded ONLY in the resume. Plain text, no em-dashes.
   - Cover letter field: upload the cover letter file if there is one, else paste the cover letter text, else leave optional ones empty.
   - Dropdowns/comboboxes: click, type to filter, pick the matching option, then confirm in the next snapshot that it stuck.
   - Dates: use the format the field shows.
9. A required question you cannot answer truthfully from the data above (e.g. a specific certification, security clearance, a yes/no fact not given): do NOT invent an answer. Collect it and report "needs_human" with the exact question text, the field type and the options, after filling everything else.
10. Multi-page forms: after each page, check for validation errors in the snapshot, fix them, then continue.
11. Before the final submit, take a browser_snapshot and verify every required field is filled correctly.
12. {submit_rule}
13. After submitting, wait for the confirmation (a thank-you message, a "submitted" page, or a confirmation URL). If an email verification code is asked for, report "needs_human" with that as the question.
14. Do not open unrelated pages, do not click ads, do not log out of anything, do not change account settings.
15. Never grant camera/microphone/location permissions; never do video interviews, assessments or ID checks -> report "needs_human" with what was asked.

== WHEN YOU ARE DONE ==
Your very last line must be exactly one line starting with RESULT: followed by compact JSON, for example
RESULT: {{"status": "submitted", "submit_clicked": true, "reason": "", "confirmation": "Thanks for applying to Acme", "questions": []}}
status is one of: {statuses}
- submit_clicked: true if you clicked the final submit button at any point (even if the result is unclear).
- reason: one short sentence for anything but submitted/dry_run_ok.
- confirmation: the confirmation text you saw, if any.
- questions: for needs_human, a list of {{"question": "...", "field_type": "text|textarea|select|radio|checkbox", "options": ["..."]}}.
"""


def _block(v) -> str:
    if not v:
        return "(none)"
    if isinstance(v, str):
        return v.strip()
    return json.dumps(v, indent=1, ensure_ascii=False)


def build(req, files: list[tuple[str, str]]) -> str:
    """`req` is the RunRequest; `files` is [(kind, filename in the working dir)]."""
    if req.dry_run:
        mode = "DRY RUN. Fill the whole form but do NOT click the final submit button."
        submit_rule = ("DRY RUN: do NOT click the final Submit / Send / Apply button. Stop on the last page with "
                       "everything filled and report status \"dry_run_ok\" (submit_clicked false).")
    else:
        mode = "LIVE. Submit the application when the form is complete and correct."
        submit_rule = "Click the final Submit / Send application button once. Never submit twice."
    files_txt = "\n".join(f"- {kind}: {name}" for kind, name in files) or "(none)"
    cl = f"\n== COVER LETTER TEXT ==\n{req.cover_letter_text.strip()}\n" if req.cover_letter_text else ""
    extra = f"\n== EXTRA INSTRUCTIONS ==\n{req.extra_instructions.strip()}\n" if req.extra_instructions else ""
    self_id = ("choose \"Decline to self-identify\" / \"I don't wish to answer\" (or the closest option)."
               if req.decline_self_id else "use the STANDARD ANSWERS; if none, choose the decline option.")
    return TEMPLATE.format(
        company=req.company or "(unknown)", title=req.title or "(unknown)", url=req.url, mode=mode,
        files=files_txt, applicant=_block(req.applicant), answers=_block(req.answers),
        qa_bank=_block([{"q": q.get("question"), "a": q.get("answer")} for q in (req.qa_bank or [])
                        if isinstance(q, dict) and q.get("answer")]),
        resume=_block(req.resume_text), cover_letter=cl, extra=extra, self_id=self_id,
        submit_rule=submit_rule, statuses=", ".join(STATUSES),
    )
