"""Application documents: generated from profile facts, stored encrypted and immutable in the database."""
from __future__ import annotations

import hashlib
import io
import re

from . import profile, secretstore
from .db import Conn, j, new_id, now, unj


def store(conn: Conn, kind: str, filename: str, mime: str, content: bytes, meta: dict | None = None) -> str:
    sha = hashlib.sha256(content).hexdigest()
    src = (meta or {}).get("source_hash")
    if src:  # identical source text => reuse the existing immutable document
        row = conn.one("SELECT id FROM documents WHERE kind=? AND meta LIKE ? AND deleted_at IS NULL", (kind, f'%"source_hash": "{src}"%'))
        if row:
            return row["id"]
    did = new_id("doc")
    conn.q("INSERT INTO documents (id, kind, filename, mime, sha256, content_enc, meta, created_at) VALUES (?,?,?,?,?,?,?,?)",
           (did, kind, filename, mime, sha, secretstore.encrypt(content), j(meta or {}), now()))
    conn.event("document.stored", "document", did, {"kind": kind, "sha256": sha})
    return did


def load(conn: Conn, doc_id: str) -> tuple[dict, bytes]:
    row = conn.one("SELECT * FROM documents WHERE id=? AND deleted_at IS NULL", (doc_id,))
    if not row:
        raise KeyError(doc_id)
    content = secretstore.decrypt(row["content_enc"])
    if hashlib.sha256(content).hexdigest() != row["sha256"]:
        raise ValueError(f"document {doc_id} failed its integrity check")
    row["meta"] = unj(row["meta"], {})
    return row, content


def _facts(conn: Conn, prefix: str, strict: bool) -> list[dict]:
    return [f for f in profile.facts(conn, prefix) if not strict or f["status"] in ("confirmed", "preference")]


def cv_data(conn: Conn, strict: bool = True) -> dict:
    """Everything that goes on the CV, straight from stored facts. Nothing is paraphrased or added."""
    one = lambda k: profile.get_fact(conn, k, confirmed_only=strict)  # noqa: E731
    return {
        "name": (one("identity/name") or {}).get("full"), "email": one("contact/application_email") or one("contact/email"),
        "phone": one("contact/phone"), "location": one("contact/location"), "linkedin": one("links/linkedin"),
        "education": profile.education_entries(conn, strict),
        "employment": sorted((f["value"] for f in _facts(conn, "employment/", strict)), key=lambda e: str(e.get("start", "")), reverse=True),
        "achievements": [f["value"] for f in _facts(conn, "achievements/", strict)],
        "skills": [f["value"] for f in _facts(conn, "skills/", strict)],
        "languages": [f["value"] for f in _facts(conn, "languages/", strict)],
    }


def _month(v) -> str:
    """'2026-09' -> 'Sep 2026'; anything else is shown as given."""
    import datetime as dt
    try:
        return dt.datetime.strptime(str(v), "%Y-%m").strftime("%b %Y")
    except ValueError:
        return str(v)


def render_cv(data: dict) -> bytes:
    from reportlab.lib.pagesizes import A4
    from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
    from reportlab.lib.units import mm
    from reportlab.platypus import Paragraph, SimpleDocTemplate, Spacer
    from xml.sax.saxutils import escape as e

    ss = getSampleStyleSheet()
    h1 = ParagraphStyle("h1", parent=ss["Title"], fontSize=18, spaceAfter=2, alignment=0)
    h2 = ParagraphStyle("h2", parent=ss["Heading2"], fontSize=11, spaceBefore=9, spaceAfter=3, textColor="#1a3a5c")
    body = ParagraphStyle("body", parent=ss["BodyText"], fontSize=9.5, leading=12.5)
    bullet = ParagraphStyle("bullet", parent=body, leftIndent=10, bulletIndent=0)
    out = io.BytesIO()
    doc = SimpleDocTemplate(out, pagesize=A4, leftMargin=18 * mm, rightMargin=18 * mm, topMargin=15 * mm, bottomMargin=15 * mm,
                            title=f"CV {data['name']}", author=data["name"] or "")
    loc = data.get("location") or {}
    contact = " | ".join(x for x in (data.get("email"), data.get("phone"), ", ".join(v for v in (loc.get("city"), loc.get("country")) if v),
                                     data.get("linkedin")) if x)
    story = [Paragraph(e(data["name"] or ""), h1), Paragraph(e(contact), body)]
    if data["education"]:
        story.append(Paragraph("Education", h2))
        for ed in data["education"]:
            head = f"<b>{e(ed.get('institution', ''))}</b>, {e(ed.get('degree', ''))}"
            when = " to ".join(_month(x) for x in (ed.get("start"), ed.get("graduation") or ed.get("end")) if x)
            story.append(Paragraph(head + (f" ({e(when)})" if when else ""), body))
            for line in ([f"GPA {ed['gpa_text']}"] if ed.get("gpa_text") else []) + list(ed.get("highlights", [])):
                story.append(Paragraph(e(line), bullet, bulletText="•"))
    if data["employment"]:
        story.append(Paragraph("Experience", h2))
        for job in data["employment"]:
            when = f"{_month(job.get('start', ''))} to {_month(job['end']) if job.get('end') else 'present'}"
            story.append(Paragraph(f"<b>{e(job.get('employer', ''))}</b>, {e(job.get('title', ''))} ({e(when)})", body))
            for line in job.get("bullets", []):
                story.append(Paragraph(e(line), bullet, bulletText="•"))
            story.append(Spacer(1, 3))
    if data["achievements"]:
        story.append(Paragraph("Projects and leadership", h2))
        for a in data["achievements"]:
            story.append(Paragraph(e(a.get("text", "") if isinstance(a, dict) else str(a)), bullet, bulletText="•"))
    if data["skills"]:
        story.append(Paragraph("Skills", h2))
        story.append(Paragraph(e("; ".join((s.get("name", "") + (f" ({s['level']})" if s.get("level") else "")) if isinstance(s, dict) else str(s)
                                           for s in data["skills"])), body))
    if data["languages"]:
        story.append(Paragraph("Languages", h2))
        story.append(Paragraph(e("; ".join(f"{lang.get('language')} ({lang.get('level')})" for lang in data["languages"])), body))
    doc.build(story)
    return out.getvalue()


def compose_cover_letter(conn: Conn, vacancy: dict, categories: list[str], strict: bool = True) -> str | None:
    """Deterministic letter assembled only from stored facts and the candidate's own approved story wording."""
    one = lambda k: profile.get_fact(conn, k, confirmed_only=strict)  # noqa: E731
    name = (one("identity/name") or {}).get("full")
    ed = profile.current_education(conn, strict)
    if not name or not ed:
        return None
    from datetime import date
    in_progress = str(ed.get("graduation") or "0") > date.today().strftime("%Y-%m")
    stories = [f["value"] for f in _facts(conn, "stories/", strict)]
    cat_words = set(re.findall(r"[a-z&]+", " ".join(categories).lower()))
    ranked = sorted(stories, key=lambda s: -len(cat_words & set(t.lower() for t in s.get("tags", []))))
    chosen = [s for s in ranked if cat_words & set(t.lower() for t in s.get("tags", []))][:2] or ranked[:1]
    paras = [f"I am applying for the {vacancy['title']} position at {vacancy['employer_name']}.",
             (f"I study {ed.get('degree', '')} at {ed.get('institution', '')}" if in_progress
              else f"I hold a {ed.get('degree', '')} from {ed.get('institution', '')}")
             + (f" and expect to graduate in {_month_long(ed['graduation'])}" if in_progress and ed.get("graduation") else "") + "."
             + (f" My current GPA is {ed['gpa_text']}." if ed.get("gpa_text") else "")]
    done = profile.current_education(conn, strict, completed=True)
    if done and done is not ed and done != ed:
        paras[-1] += f" I hold a {done.get('degree', '')} from {done.get('institution', '')}" + \
            (f" with a GPA of {done['gpa_text']}" if done.get("gpa_text") else "") + "."
    paras += [s["text"] for s in chosen]
    avail = one("preferences/availability")
    paras.append((f"{avail} " if isinstance(avail, str) and avail else "") + "I would welcome the chance to discuss the role.")
    text = "\n\n".join(paras) + f"\n\nKind regards,\n{name}"
    return re.sub(r"\s+[—–]\s+", ", ", text).replace("—", ",")  # writing preference: no dashes


def _month_long(v) -> str:
    import datetime as dt
    try:
        return dt.datetime.strptime(str(v), "%Y-%m").strftime("%B %Y")
    except ValueError:
        return str(v)


# -- writer agent: letters written by a Claude session, checked here before use ------------------------------
INVENTED = re.compile(r"\b(podcast|webinar|coffee chat|spoke (with|to) (one of )?your|met (with )?(one of )?your|attended your|conversation with)\b", re.I)
BANNED = re.compile(r"passionat|thrilled|dream (job|company)|esteemed|synerg|\bleverag", re.I)


def letter_facts_text(conn: Conn) -> str:
    """All confirmed fact text the writer may draw on, used to check that every number in a letter is backed by a fact."""
    import json as _json
    return _json.dumps([f["value"] for f in profile.facts(conn) if f["status"] in ("confirmed", "preference")], ensure_ascii=False)


def check_letter(conn: Conn, text: str, vacancy: dict) -> list[str]:
    """Rules a written letter must pass before it can be attached. Returns problems (empty = accepted)."""
    problems = []
    if re.search(r"[\u2014\u2013]", text):
        problems.append("contains an em or en dash")
    text = letter_body(text)
    words = len(re.findall(r"\b\w+\b", text))
    if not 150 <= words <= 350:
        problems.append(f"length {words} words (expected 150 to 350)")
    if vacancy["employer_name"].split()[0].lower() not in text.lower():
        problems.append("does not name the employer")
    if BANNED.search(text):
        problems.append(f"generic phrasing: {BANNED.search(text).group(0)!r}")
    facts = letter_facts_text(conn)
    vac_text = (vacancy.get("title", "") + " " + (vacancy.get("description") or "")).lower()
    for num in set(re.findall(r"\d[\d,.]*%?", text)):
        n = num.rstrip(".,")
        if n and n not in facts and n.lower() not in vac_text:
            problems.append(f"number {n!r} is not in your confirmed facts or the vacancy")
    for m in INVENTED.finditer(text):
        if m.group(0).lower() not in facts.lower():
            problems.append(f"mentions {m.group(0)!r}, which is not in your facts (no invented personal experiences)")
    return problems


def add_written_letter(conn: Conn, vacancy_id: str, text: str, author: str = "writer-agent") -> dict:
    from . import vacancies as vac_mod

    v = vac_mod.get(conn, vacancy_id)
    if not v:
        return {"ok": False, "problems": ["unknown vacancy"]}
    text = text.strip()
    problems = check_letter(conn, text, v)
    if problems:
        conn.event("letter.rejected", "vacancy", vacancy_id, {"problems": problems}, actor=author)
        return {"ok": False, "problems": problems}
    with conn.tx():  # a newer letter for the same vacancy replaces the old one; the old text stays in history
        conn.q("UPDATE answers SET superseded_at=? WHERE question_key='letter' AND scope_type='employer' AND scope_value=? AND superseded_at IS NULL",
               (now(), vacancy_id))
        conn.q("INSERT INTO answers (id, question_key, scope_type, scope_value, answer, status, source, confirmed_at, created_at) "
               "VALUES (?,?,?,?,?,?,?,?,?)", (new_id("ans"), "letter", "employer", vacancy_id, text, "confirmed", author, now(), now()))
    conn.event("letter.accepted", "vacancy", vacancy_id, {"words": len(text.split())}, actor=author)
    return {"ok": True, "problems": []}


def written_letter(conn: Conn, vacancy_id: str) -> str | None:
    row = conn.one("SELECT answer FROM answers WHERE question_key='letter' AND scope_type='employer' AND scope_value=? AND superseded_at IS NULL "
                   "ORDER BY created_at DESC LIMIT 1", (vacancy_id,))
    return row["answer"] if row else None


_FONTS: tuple[str, str] | None = None


def _letter_fonts() -> tuple[str, str]:
    """Calibri where installed (the owner's template uses it), else the metric-compatible Carlito, else Helvetica."""
    global _FONTS
    if _FONTS:
        return _FONTS
    import os
    from reportlab.pdfbase import pdfmetrics
    from reportlab.pdfbase.ttfonts import TTFont

    pairs = [
        (os.environ.get("JOBAGENT_LETTER_FONT", ""), os.environ.get("JOBAGENT_LETTER_FONT_BOLD", "")),
        ("/Applications/Microsoft Word.app/Contents/Resources/DFonts/Calibri.ttf", "/Applications/Microsoft Word.app/Contents/Resources/DFonts/Calibrib.ttf"),
        ("/Library/Fonts/Calibri.ttf", "/Library/Fonts/Calibri Bold.ttf"),
        (os.path.expanduser("~/Library/Fonts/Calibri.ttf"), os.path.expanduser("~/Library/Fonts/Calibri Bold.ttf")),
        ("C:/Windows/Fonts/calibri.ttf", "C:/Windows/Fonts/calibrib.ttf"),
        ("/usr/share/fonts/truetype/crosextra/Carlito-Regular.ttf", "/usr/share/fonts/truetype/crosextra/Carlito-Bold.ttf"),
    ]
    for reg, bold in pairs:
        if reg and bold and os.path.exists(reg) and os.path.exists(bold):
            try:
                pdfmetrics.registerFont(TTFont("LetterBody", reg))
                pdfmetrics.registerFont(TTFont("LetterBold", bold))
                _FONTS = ("LetterBody", "LetterBold")
                return _FONTS
            except Exception:  # noqa: BLE001
                continue
    _FONTS = ("Helvetica", "Helvetica-Bold")
    return _FONTS


SALUTATION = re.compile(r"^\s*dear\b[^\n]*\n+", re.I)
SIGNOFF = re.compile(r"\n+\s*(kind|best|warm)?\s*regards,?[\s\S]*$|\n+\s*(sincerely|yours (sincerely|faithfully)),?[\s\S]*$", re.I)


def letter_body(text: str) -> str:
    """The writer supplies body paragraphs; any salutation or sign-off it included is removed (the template adds its own)."""
    return SIGNOFF.sub("", SALUTATION.sub("", text.strip())).strip()


def render_letter(text: str, name: str, employer: str = "", contact: dict | None = None, logo_png: bytes | None = None,
                  date_text: str | None = None) -> bytes:
    """Cover letter in the owner's layout: date top right, employer logo, contact block, employer name centred,
    'Dear <employer> team,', body, 'Kind Regards,' and name. Calibri 11 pt."""
    import datetime as dt
    from reportlab.lib.enums import TA_CENTER, TA_RIGHT
    from reportlab.lib.pagesizes import A4
    from reportlab.lib.styles import ParagraphStyle
    from reportlab.lib.units import mm
    from reportlab.platypus import Image as RLImage, Paragraph, SimpleDocTemplate, Spacer
    from xml.sax.saxutils import escape as e

    reg, bold = _letter_fonts()
    body = ParagraphStyle("body", fontName=reg, fontSize=11, leading=13.4, spaceAfter=11)
    tight = ParagraphStyle("tight", parent=body, spaceAfter=0)
    right = ParagraphStyle("right", parent=tight, alignment=TA_RIGHT)
    title = ParagraphStyle("title", fontName=bold, fontSize=14, leading=17, alignment=TA_CENTER)
    today = date_text or (lambda d: f"{d.day} {d.strftime('%B')}, {d.year}")(dt.date.today())
    contact = contact or {}
    story = [Paragraph(e(today), right), Spacer(1, 16 * mm if logo_png else 8 * mm)]
    if logo_png:
        from PIL import Image as PILImage
        w, h = PILImage.open(io.BytesIO(logo_png)).size
        scale = min(120 / w, 38 / h)  # at most 120 x 38 pt, like the template's logo
        img = RLImage(io.BytesIO(logo_png), width=w * scale, height=h * scale)
        img.hAlign = "LEFT"
        story += [img, Spacer(1, 18 * mm)]
    lines = []
    if contact.get("email"):
        lines.append(f'Email: <a href="mailto:{e(contact["email"])}" color="#0563C1"><u>{e(contact["email"])}</u></a>')
    if contact.get("phone"):
        lines.append(f"Phone: {e(contact['phone'])}")
    if contact.get("address"):
        lines.append(f"Address: {e(contact['address'])}")
    story += [Paragraph(x, tight) for x in lines]
    story += [Spacer(1, 14 * mm)]
    if employer:
        story += [Paragraph(e(employer), title), Spacer(1, 6 * mm), Paragraph(e(f"Dear {employer} team,"), body), Spacer(1, 6 * mm)]
    for para in [p.strip() for p in re.split(r"\n\s*\n", letter_body(text)) if p.strip()]:
        story.append(Paragraph(e(" ".join(para.split())), body))
    story += [Spacer(1, 8 * mm), Paragraph("Kind Regards,", body), Paragraph(e(name), body)]
    out = io.BytesIO()
    doc = SimpleDocTemplate(out, pagesize=A4, leftMargin=25 * mm, rightMargin=25 * mm, topMargin=20 * mm, bottomMargin=20 * mm,
                            title=f"Cover letter {name}", author=name)
    doc.build(story)
    return out.getvalue()


def verify_pdf(content: bytes, must_contain: list[str], max_pages: int = 3) -> list[str]:
    """Rendering check before a document may be attached. Returns a list of problems (empty = fine)."""
    from pypdf import PdfReader

    problems = []
    try:
        reader = PdfReader(io.BytesIO(content))
        text = " ".join((p.extract_text() or "") for p in reader.pages)
    except Exception as exc:  # noqa: BLE001
        return [f"PDF cannot be read: {exc}"]
    if not 1 <= len(reader.pages) <= max_pages:
        problems.append(f"unexpected page count {len(reader.pages)}")
    flat = re.sub(r"\s+", " ", text)
    for needle in must_contain:
        if needle and re.sub(r"\s+", " ", needle) not in flat:
            problems.append(f"expected text missing from rendering: {needle[:40]!r}")
    if "None" in flat.split() or "{" in flat:
        problems.append("rendering contains placeholder artefacts")
    return problems


def build_for(conn: Conn, vacancy: dict, categories: list[str], needs: dict, strict: bool = True) -> tuple[dict, list[str]]:
    """Generate, verify and store the documents an application needs. Returns ({kind: doc_id}, problems)."""
    docs, problems = {}, []
    data = cv_data(conn, strict)
    if not data["name"] or not data["email"]:
        return {}, ["profile is missing a confirmed name or e-mail address"]
    slug = re.sub(r"[^A-Za-z]+", "_", data["name"]).strip("_")
    if needs.get("resume"):
        if not data["education"] and not data["employment"]:
            problems.append("no confirmed education or employment facts to build a CV from")
        else:
            src = hashlib.sha256(j(data).encode()).hexdigest()
            pdf = render_cv(data)
            problems += verify_pdf(pdf, [data["name"], data["email"]] + [x.get("institution", "") for x in data["education"]]
                                   + [x.get("employer", "") for x in data["employment"]])
            docs["resume"] = store(conn, "resume", f"{slug}_CV.pdf", "application/pdf", pdf, {"source_hash": src, "strict": strict})
    if needs.get("cover_letter"):
        text = written_letter(conn, vacancy["id"]) or compose_cover_letter(conn, vacancy, categories, strict)
        if text:
            import datetime as dt
            from . import logos
            d = dt.date.today()
            date_text = f"{d.day} {d.strftime('%B')}, {d.year}"
            try:
                logo = logos.get(conn, vacancy["employer_id"])
            except Exception:  # noqa: BLE001 - a logo is optional; never block an application on it
                logo = None
            contact = {"email": data["email"], "phone": data["phone"],
                       "address": profile.get_fact(conn, "contact/address", confirmed_only=strict)}
            employer = vacancy["employer_name"]
            pdf = render_letter(text, data["name"], employer, contact, logo, date_text)
            problems += verify_pdf(pdf, [employer, data["name"], "Kind Regards"], max_pages=1)
            body = letter_body(text)
            # The date changes daily; approvals are bound to content_key (text, employer, logo), not to the PDF bytes.
            content_key = hashlib.sha256(j({"text": body, "employer": employer, "logo": hashlib.sha256(logo).hexdigest() if logo else None,
                                             "contact": contact}).encode()).hexdigest()
            docs["cover_letter"] = store(conn, "cover_letter", f"{slug}_Cover_Letter_{re.sub(r'[^A-Za-z0-9]+', '_', employer)}.pdf",
                                         "application/pdf", pdf, {"source_hash": hashlib.sha256((content_key + date_text).encode()).hexdigest(),
                                                                 "content_key": content_key, "text": body, "date": date_text,
                                                                 "vacancy_id": vacancy["id"], "strict": strict, "logo": bool(logo)})
    return docs, problems
