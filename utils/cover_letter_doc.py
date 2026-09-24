"""Render a cover-letter text into downloadable .pdf / .docx bytes.

Shared by the dashboard (phase3_dashboard) and the Apply Review queue so both
produce byte-identical documents from the same letter. Forms that take the
cover letter as a file upload (not a paste-able textarea) need this.

reportlab's Paragraph parses its input as mini-XML, so bare "&"/"<"/">" —
common in German company names like "Merz Pharma & Co. KG" — must be escaped
or the build raises a paraparser syntax error. python-docx takes text
literally, so only the PDF path escapes.
"""
from __future__ import annotations

import io
import re
from xml.sax.saxutils import escape


def _candidate() -> dict:
    """Name + contact for the letterhead, straight from the profile. Empty when
    the profile is unreadable (tests, a half-set-up checkout) — the letter
    still builds, just without a sender block."""
    try:
        from utils.profile_loader import load_profile
        fields = load_profile(strict=False, check_cv_file=False).fields
    except Exception:
        return {}
    return {k: fields[k].value for k in ("full_name", "email", "phone", "city")
            if k in fields and fields[k].value}


def _sender_lines() -> list[str]:
    """[name, "email · phone · city"], or [] when there is no name to sign
    with. A letter sent as the BODY of an email is signed by the account it
    came from; one uploaded to a form is a loose file sitting next to a CV
    named after the candidate, and carried no name at all — neither in the
    document nor in its filename."""
    who = _candidate()
    if not who.get("full_name"):
        return []
    contact = " · ".join(v for k, v in who.items() if k != "full_name")
    return [who["full_name"], contact] if contact else [who["full_name"]]


def file_stem(company: str, title: str) -> str:
    """Filesystem-safe basename (no extension) for a downloaded letter, led by
    the candidate's name like the CV it is uploaded beside — a recruiter's
    inbox shows the filename, and "cover_letter_Acme_Backend.pdf" does not say
    whose it is."""
    who = _candidate().get("full_name", "")
    stem = f"{who}_cover_letter_{company}_{title}" if who \
        else f"cover_letter_{company}_{title}"
    return re.sub(r"[^\w.-]+", "_", stem).strip("_") or "cover_letter"


def build_pdf(text: str, title: str, company: str) -> bytes:
    """A4 PDF: sender block, "<title> @ <company>" heading, one paragraph per
    source line."""
    from reportlab.lib.pagesizes import A4
    from reportlab.lib.styles import getSampleStyleSheet
    from reportlab.lib.units import cm
    from reportlab.platypus import Paragraph, SimpleDocTemplate, Spacer

    buf = io.BytesIO()
    doc = SimpleDocTemplate(
        buf, pagesize=A4,
        leftMargin=2.5 * cm, rightMargin=2.5 * cm,
        topMargin=2.5 * cm, bottomMargin=2.5 * cm,
    )
    styles = getSampleStyleSheet()
    story = []
    for line, style in zip(_sender_lines(), ("Heading2", "Normal")):
        story.append(Paragraph(escape(line), styles[style]))
    if story:
        story.append(Spacer(1, 18))
    story += [
        Paragraph(escape(f"{title} @ {company}"), styles["Heading1"]),
        Spacer(1, 12),
    ]
    for para in text.strip().split("\n"):
        if para.strip():
            story.append(Paragraph(escape(para), styles["Normal"]))
            story.append(Spacer(1, 6))
        else:
            story.append(Spacer(1, 10))
    doc.build(story)
    return buf.getvalue()


def build_docx(text: str, title: str, company: str) -> bytes:
    """A .docx with the sender block, a "<title> @ <company>" heading and one
    paragraph per line."""
    from docx import Document as DocxDocument

    doc = DocxDocument()
    sender = _sender_lines()
    if sender:
        doc.add_heading(sender[0], level=2)
        for line in sender[1:]:
            doc.add_paragraph(line)
    doc.add_heading(f"{title} @ {company}", level=1)
    for para in text.strip().split("\n"):
        doc.add_paragraph(para)
    buf = io.BytesIO()
    doc.save(buf)
    return buf.getvalue()
