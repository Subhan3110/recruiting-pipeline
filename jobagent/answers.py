"""Reusable answer library and application-form resolution.

Answers are scoped (employer > country > global) and carry provenance. Only
confirmed answers and confirmed profile facts may be used for a live application;
anything missing becomes an explicit question for the user, never a guess.
"""
from __future__ import annotations

import re

from . import geo, profile
from .db import Conn, new_id, now

YES = re.compile(r"^\s*(yes|ja|true|y)\b", re.I)
NO = re.compile(r"^\s*(no|nei|false|n)\b", re.I)

# (question key, label pattern). First match wins. Keys ending in ':' take a country suffix.
PATTERNS = [
    ("sponsorship:", r"sponsor|work (permit|visa)|employment pass|visa (support|assistance)|require .* visa"),
    ("work_auth:", r"(authori[sz]ed|eligible|legally (able|permitted|entitled)|right|permission|entitled) to work|work authori[sz]ation|legal right to work"),
    ("linkedin", r"linkedin"),
    ("website", r"website|portfolio|github"),
    ("heard_from", r"how did you (hear|learn|find)|where did you (hear|find|learn)|source of application|referred by"),
    ("previously_applied", r"(previously|ever|before) (applied|interviewed)|applied (to|at|with) .* before"),
    ("previously_employed", r"(previously|ever|currently) (worked|been employed|employed)|former employee|worked (for|at) .* before"),
    ("graduation_year", r"graduat\w+ year|year of graduation|expected graduation\??$|when (do|will) you graduate"),
    ("graduation_month", r"graduat\w+ month"),
    ("graduation_date", r"graduat\w+ date|expected graduation"),
    ("degree_level", r"(what|which) degree|degree (are you|level|type)|level of (study|education)|highest (level of )?education"),
    ("gpa", r"\bgpa\b|grade point|cgpa|grade average|classification"),
    ("university", r"\b(school|university|college|institution)\b"),
    ("field_of_study", r"major|field of study|discipline|course of study|area of study"),
    ("salary_expectation", r"salary|compensation|remuneration|pay expectation"),
    ("notice_period", r"notice period"),
    ("start_date", r"start date|when (can|could) you start|available to start|earliest .* start|availability"),
    ("relocation", r"relocat"),
    ("current_location", r"(current|where are you) (location|based|located)|city of residence|where do you (currently )?live|country of residence"),
    ("languages_other", r"language"),
    ("military", r"military|armed forces|veteran"),
    ("consent:marketing", r"marketing|talent (community|network|pool)|future (opportunit|vacanc)|keep (my|your) (data|details)|advertis|contact information"),
    ("consent:privacy", r"privacy (policy|notice|statement)|data protection|gdpr|consent|acknowledge|i (have read|agree|confirm|certify)|terms"),
    ("preferred_location", r"(which|preferred|what) (office|location)|location (preference|of interest)|where would you (like|prefer)"),
    ("age_18", r"(over|at least|above) (the age of )?18|18 years"),
    ("non_compete", r"non-?compete|restrictive covenant|garden leave"),
    ("relatives", r"relative|family member|personal relationship"),
    ("pronouns", r"pronoun"),
]
_PATTERNS = [(k, re.compile(p, re.I)) for k, p in PATTERNS]
BUILTIN = {"first_name", "last_name", "email", "phone", "resume", "resume_text", "cover_letter", "cover_letter_text", "location"}


def norm_label(label: str) -> str:
    return re.sub(r"[^a-z0-9]+", " ", (label or "").lower()).strip()[:160]


def classify(label: str, vacancy_countries: list[str]) -> tuple[str, str | None]:
    """Map a form label to (question_key, country). Unknown labels get a label-derived key."""
    for key, pat in _PATTERNS:
        if pat.search(label):
            if key.endswith(":"):
                cc = _country_in(label) or (vacancy_countries[0] if len(vacancy_countries) == 1 else None)
                return key + (cc or "?"), cc
            return key, None
    return "q:" + norm_label(label), None


def _country_in(label: str) -> str | None:
    m = re.search(r"\b(?:in|within|for)\s+(?:the\s+)?([A-Z][\w.]*(?:\s+(?:of\s+)?[A-Z][\w.]*){0,3})", label)
    if m:
        cand = m.group(1).strip(" ?.")
        for n in range(len(cand.split()), 0, -1):
            cc = geo.country_of(" ".join(cand.split()[:n]))
            if cc:
                return cc
    for name in ("united states", "u.s.", "usa", "united kingdom", "uk", "eu", "european union", "eea"):
        if re.search(rf"\b{re.escape(name)}\b", label, re.I):
            return {"eu": "EEA", "european union": "EEA", "eea": "EEA"}.get(name) or geo.country_of(name)
    return None


# -- library --------------------------------------------------------------------
def set_answer(conn: Conn, key: str, answer: str, scope_type: str = "global", scope_value: str = "", status: str = "confirmed",
               source: str = "user", actor: str = "user") -> str:
    with conn.tx():
        ts = now()
        conn.q("UPDATE answers SET superseded_at=? WHERE question_key=? AND scope_type=? AND scope_value=? AND superseded_at IS NULL",
               (ts, key, scope_type, scope_value))
        aid = new_id("ans")
        conn.q("INSERT INTO answers (id, question_key, scope_type, scope_value, answer, status, source, confirmed_at, created_at) "
               "VALUES (?,?,?,?,?,?,?,?,?)", (aid, key, scope_type, scope_value, answer, status, source,
                                             ts if status == "confirmed" else None, ts))
        conn.event("answer.set", "answer", key, {"scope": f"{scope_type}:{scope_value}", "status": status}, actor=actor, provenance=source)
        return aid


def lookup(conn: Conn, key: str, employer_id: str | None = None, country: str | None = None, allow_proposed: bool = False) -> dict | None:
    """Most specific current answer: employer, then country, then global. Scoped answers never leak across scopes."""
    for scope_type, scope_value in (("employer", employer_id), ("country", country), ("global", "")):
        if scope_value is None:
            continue
        row = conn.one("SELECT * FROM answers WHERE question_key=? AND scope_type=? AND scope_value=? AND superseded_at IS NULL",
                       (key, scope_type, scope_value))
        if row and (row["status"] == "confirmed" or allow_proposed):
            return row
    return None


def library(conn: Conn) -> list[dict]:
    return conn.q("SELECT * FROM answers WHERE superseded_at IS NULL ORDER BY question_key, scope_type, scope_value")


# -- form resolution --------------------------------------------------------------
def _pick_option(options: list[dict], text: str) -> dict | None:
    t = text.strip().lower()
    for o in options:
        if o["label"].strip().lower() == t:
            return o
    if YES.match(text) or NO.match(text):
        want = YES if YES.match(text) else NO
        hits = [o for o in options if want.match(o["label"])]
        if len(hits) == 1:
            return hits[0]
    hits = [o for o in options if t and (t in o["label"].lower() or o["label"].strip().lower() in t)]
    if len(hits) == 1:
        return hits[0]
    # Last resort: exactly one option shares a distinctive word ("Company website" -> "Flow Traders Website").
    words = [w for w in re.findall(r"[a-z]{5,}", t) if w not in ("company", "other", "please", "prefer")]
    hits = [o for o in options if any(re.search(rf"\b{w}\b", o["label"].lower()) for w in words)]
    return hits[0] if len(hits) == 1 else None


def _profile_value(conn: Conn, key: str, country: str | None, employer_id: str, strict: bool):
    """Answers derived from confirmed profile facts. Returns (text, source) or (None, None)."""
    get = lambda k: profile.get_fact(conn, k, confirmed_only=strict)  # noqa: E731
    name = get("identity/name") or {}
    edu = next((f["value"] for f in profile.facts(conn, "education/") if not strict or f["status"] == "confirmed"), {}) or {}
    simple = {
        "first_name": (name.get("first"), "identity/name"), "last_name": (name.get("last"), "identity/name"),
        "email": (get("contact/application_email") or get("contact/email"), "contact/email"),
        "phone": (get("contact/phone"), "contact/phone"), "linkedin": (get("links/linkedin"), "links/linkedin"),
        "website": (get("links/website"), "links/website"),
        "university": (edu.get("institution"), "education"), "field_of_study": (edu.get("field"), "education"),
        "degree_level": (edu.get("level"), "education"), "gpa": (edu.get("gpa_text"), "education"),
        "graduation_year": (str(edu["graduation"])[:4] if edu.get("graduation") else None, "education"),
        "graduation_month": (edu.get("graduation_month"), "education"),
        "graduation_date": (edu.get("graduation"), "education"),
    }
    loc = get("contact/location") or {}
    simple["current_location"] = (", ".join(x for x in (loc.get("city"), loc.get("country")) if x) or None, "contact/location")
    simple["location"] = simple["current_location"]
    if key in simple:
        val, src = simple[key]
        return (str(val), f"profile:{src}") if val else (None, None)
    if key.startswith(("work_auth:", "sponsorship:")) and country:
        fact = get(f"work_auth/{country}") or (get("work_auth/EEA") if country in geo.EEA else None)
        if fact is not None:
            if key.startswith("work_auth:"):
                return ("Yes" if fact.get("authorised") else "No"), f"profile:work_auth/{country}"
            return ("Yes" if fact.get("needs_sponsorship") else "No"), f"profile:work_auth/{country}"
    if key in ("previously_applied", "previously_employed"):
        complete = get("history/prior_applications_complete")
        if key == "previously_employed":
            worked = any(f["value"].get("employer_id") == employer_id for f in profile.facts(conn, "employment/"))
            names = {str(f["value"].get("employer", "")).lower() for f in profile.facts(conn, "employment/")}
            emp = conn.one("SELECT name FROM employers WHERE id=?", (employer_id,))
            from .employers import normalise
            if emp and any(normalise(n) == normalise(emp["name"]) for n in names):
                worked = True
            return ("Yes" if worked else "No"), "profile:employment"
        prior = conn.val("SELECT COUNT(*) FROM applications WHERE employer_id=? AND state IN ('submitted','uncertain')", (employer_id,))
        if prior:
            return "Yes", "application history"
        if complete:
            return "No", "application history (confirmed complete)"
    return None, None


def resolve_form(conn: Conn, form: dict, vacancy: dict, geo_eval: dict, strict: bool = True) -> dict:
    """Turn a form schema into concrete answers.

    Returns {'answers': [...], 'missing': [...], 'skipped': [...], 'selected_location': str|None, 'needs': {'resume','cover_letter'}}.
    strict=True uses only confirmed facts and answers (required for live submission).
    """
    # Approval-only (Oslo/London) vacancies have no auto-eligible country; their questions still refer to the job's country.
    countries = geo_eval.get("countries") or geo_eval.get("all_countries") or []
    employer_id = vacancy["employer_id"]
    out = {"answers": [], "missing": [], "skipped": [], "selected_location": None,
           "needs": {"resume": False, "cover_letter": False}, "location_selectable": False}
    from . import policy as policy_mod
    _, cfg = policy_mod.current(conn)
    for q in form["questions"]:
        fields = q["fields"]
        names = [f["name"] for f in fields]
        if "resume" in names or "cover_letter" in names:
            kind = "resume" if "resume" in names else "cover_letter"
            out["needs"][kind] = True
            out["answers"].append({"field": kind, "type": "input_file", "label": q["label"], "key": kind, "value": f"document:{kind}",
                                   "source": "generated document", "required": q["required"]})
            continue
        field = fields[0]
        fname, ftype, options = field["name"], field["type"], field.get("options") or []
        if ftype == "input_hidden":
            continue  # set by the page itself (e.g. coordinates from the location autocomplete)
        base = fname if fname in BUILTIN else None
        key, country = (base, None) if base else classify(q["label"], countries)
        entry = {"field": fname, "type": ftype, "label": q["label"], "key": key, "required": q["required"]}
        # Location choice: only eligible places may be selected.
        if key == "preferred_location" and options:
            elig = [o for o in options if geo.classify_location(o["label"], cfg)["kind"] == "eligible"
                    and (not countries or geo.classify_location(o["label"], cfg)["country"] in countries)]
            if elig:
                out["selected_location"], out["location_selectable"] = elig[0]["label"], True
                out["answers"].append(entry | {"value": elig[0]["label"], "option_value": elig[0]["value"],
                                               "source": "policy: first eligible location"})
            else:
                out["missing"].append(entry | {"options": [o["label"] for o in options], "why": "no eligible location among the options"})
            continue
        text, source = _profile_value(conn, key, country, employer_id, strict)
        if text is None:
            lib = lookup(conn, key, employer_id, country or (countries[0] if len(countries) == 1 else None), allow_proposed=not strict)
            if lib:
                text, source = lib["answer"], f"answer library ({lib['scope_type']}{':' + lib['scope_value'] if lib['scope_value'] else ''}; {lib['source']})"
        if text is None:
            if q["required"]:
                out["missing"].append(entry | {"options": [o["label"] for o in options],
                                               "why": "no confirmed fact or answer on file"})
            else:
                out["skipped"].append(entry)
            continue
        if options:
            wanted = [t.strip() for t in text.split(";")] if ftype == "multi_value_multi_select" else [text]
            picked = [_pick_option(options, w) for w in wanted]
            if any(p is None for p in picked):
                (out["missing"] if q["required"] else out["skipped"]).append(
                    entry | {"options": [o["label"] for o in options], "why": f"stored answer '{text}' does not match any option"})
                continue
            out["answers"].append(entry | {"value": "; ".join(p["label"] for p in picked),
                                           "option_value": [p["value"] for p in picked] if len(picked) > 1 or ftype == "multi_value_multi_select" else picked[0]["value"],
                                           "source": source})
        else:
            out["answers"].append(entry | {"value": text, "source": source})
    # Voluntary demographic questions: only an explicit stored preference is ever used.
    for dq in form.get("demographic") or []:
        key = "demographic:" + norm_label(dq["label"])
        lib = lookup(conn, key, employer_id) or lookup(conn, "demographic:default", employer_id)
        entry = {"field": str(dq["id"]), "type": "demographic", "label": dq["label"], "key": key, "required": dq["required"]}
        opt = _pick_option(dq["options"], lib["answer"]) if lib else None
        if lib and not opt and re.search(r"decline|prefer not|not (wish|want)", lib["answer"], re.I):
            opt = next((o for o in dq["options"] if re.search(r"decline|prefer not|don.t wish|do not wish|not to (say|answer|disclose)", o["label"], re.I)), None)
        if opt:
            out["answers"].append(entry | {"value": opt["label"], "option_value": opt["value"], "source": f"answer library ({lib['source']})"})
        elif dq["required"]:
            out["missing"].append(entry | {"options": [o["label"] for o in dq["options"]], "why": "mandatory demographic question without a stored preference"})
        else:
            out["skipped"].append(entry)
    return out
