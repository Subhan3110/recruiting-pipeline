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
    ("pronouns", r"pronoun"),
    ("demographic:lgbtq", r"lgbt"),
    ("condition_ack", r"^\s*as a condition of employment"),
    ("cv_check", r"on your (cv|resume)|in your (cv|resume)"),
    ("today_date", r"today'?s date|date of (this )?application"),
    ("visa_status", r"visa status|immigration status|right to work status"),
    ("onsite", r"in[- ]office|on[- ]site|onsite|hybrid|work from (our|the) office|from the office\(s\)|days (a|per) week|willing to work in our|available to work from our office"),
    ("sponsorship:", r"sponsor|work (permit|visa)|employment pass|visa (support|assistance)|require .* visa"),
    ("work_auth:", r"(authori[sz]ed|eligible|legally (able|permitted|entitled)|right|permission|entitled) to work|authori[sz]ed (to )?(lawfully |legally )?work|work authori[sz]ation|legal right to work"),
    ("non_compete_months", r"non\s*-?\s*compete period"),
    ("licenses", r"\bfinra\b|series \d|securities licen"),
    ("us_person", r"\bu\.?s\.? person\b"),
    ("nationality", r"nationality|citizenship"),
    ("residence_country", r"country (where )?you (currently )?(reside|live)|country are you (currently )?based|country of residence|based in which country"),
    ("country", r"^\s*country\s*$"),
    ("salary_currency", r"currency"),
    ("bonus", r"\bbonus\b"),
    ("current_industry", r"industry .{0,40}(current|field of work)"),
    ("english", r"fluent in english|english proficiency|english level|level of english"),
    ("is_student", r"(are you|currently) (a )?(current )?(student|enrolled)\b"),
    ("current_employer", r"(current|previous|most recent) (or previous )?employer"),
    ("current_title", r"(current|previous|most recent) (or previous )?(job )?title"),
    ("ready_fulltime", r"(ready|available) (for|to begin|to start) .{0,40}full[- ]time"),
    ("internship_dates", r"cohort|internship batch|batch would you|internship (dates|period|session)"),
    ("overall_grade", r"overall grade|degree classification|predicted grade"),
    ("exp_excel", r"experience .{0,80}\bexcel\b"),
    ("financial_statements", r"prepared financial statements"),
    ("events", r"been to any of our events|attended any of our"),
    ("team_choice", r"select the ones you feel|which team"),
    ("experience_years", r"years? of (work |professional |relevant |full[- ]time )?experience|how many years"),
    ("pep", r"government official|politically exposed|public (office|official|function)"),
    ("outstanding_offers", r"outstanding offer|other offers|competing offer|offers or deadlines|offers? from other|offer deadline"),
    ("ai_use", r"how you use ai|use ai tools today|your use of ai"),
    ("referred", r"were you referred|referred (by|to this)|employee referral\?"),
    ("current_region", r"region you are (currently )?(located|situated|based)|region .{0,30}(situated|studying)"),
    ("address", r"(street|postal|mailing|home|residential) address|address line"),
    ("first_name", r"^\s*(legal |preferred |given )?first name"),
    ("last_name", r"^\s*(legal )?(last|family) name|^\s*surname"),
    ("linkedin", r"linkedin"),
    ("website", r"website|portfolio|github"),
    ("heard_from", r"how did you (hear|learn|find)|where did you (hear|find|learn)|source of application|referred by"),
    ("previously_applied", r"(previously|ever|before) (applied|interviewed)|applied (to|at|with) .* before|interviewed with .{0,40}(before|in the past)|applied and been rejected|have you (ever )?(applied|interviewed)"),
    ("previously_employed", r"(previously|ever|currently) (worked|been employed|employed)|former employee|worked (for|at) .* before|have you (ever )?worked (for|at)|worked at or been a consultant|currently, or have you previously, worked|currently an? .{0,50}employee"),
    ("graduation_year", r"graduat\w+ year|year of graduation|year (that )?you will graduate|year of completion|expected graduation\??$|when (do|will) you graduate"),
    ("graduation_month", r"graduat\w+ month|month (that )?you will graduate"),
    ("graduation_date", r"graduat\w+ date|expected graduation"),
    ("degree_level", r"(what|which) degree|degree (are you|level|type)|level of (study|education)|highest (level of )?education"),
    ("gpa", r"\bgpa\b|grade point|cgpa|grade average|classification"),
    ("university", r"\b(school|university|college|institution)\b"),
    ("field_of_study", r"major|field of study|discipline|course of study|area of study|\bsubjects?\b(?!\s+to\b)"),
    ("salary_expectation", r"salary|compensation|remuneration|pay expectation"),
    ("notice_period", r"notice period"),
    ("start_date", r"start date|when (can|could) you start|available to start|earliest .* start|availability"),
    ("relocation", r"relocat"),
    ("current_location", r"(current|where are you) (location|based|located)|city of residence|where do you (currently )?live"),
    ("demographic:gender", r"\bgender\b|^\s*sex\s*$"),
    ("demographic:orientation", r"sexual orientation"),
    ("demographic:hispanic", r"hispanic|latino"),
    ("demographic:ethnicity", r"\brace\b|ethnic"),
    ("demographic:veteran", r"veteran|military status"),
    ("demographic:disability", r"disabilit"),
    ("languages_other", r"language"),
    ("military", r"military|armed forces"),
    ("consent:marketing", r"marketing|talent (community|network|pool)|future (opportunit|vacanc)|other opportunities|considered for (other|future)|keep (my|your) (data|details)|advertis|contact information"),
    ("consent:privacy", r"privacy|personal data|data transfer|notice at collection|pay range|arbitration|confirm receipt|be advised|may use ai|ai tools to assist|recorded|data protection|gdpr|consent|acknowledge|i (have read|agree|confirm|certify)|terms"),
    ("preferred_location", r"(which|preferred|what) (work )?(office|location)|preferred .{0,30}(office|location)|location (preference|of interest)|where would you (like|prefer)"),
    ("age_18", r"(over|at least|above) (the age of )?18|18 years"),
    ("non_compete", r"non\s*-?\s*compete|non-solicitation|restrictive covenant|garden leave"),
    ("relatives", r"relative|family member|personal relationship|familial|outside business activit|conflict of interest"),
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
def _by_number(options: list[dict], n: int) -> dict | None:
    """Pick the option whose range contains n: '1-3 years', '5+', 'less than 1', '2 years'."""
    for o in options:
        lab = o["label"].lower()
        m = re.search(r"(\d+)\s*(?:-|–|to)\s*(\d+)", lab)
        if m and int(m.group(1)) <= n <= int(m.group(2)):
            return o
        m = re.search(r"(?:less than|under|fewer than|<)\s*(\d+)", lab)
        if m and n < int(m.group(1)):
            return o
        m = re.search(r"(?:more than|over|>)\s*(\d+)", lab)
        if m and n > int(m.group(1)):
            return o
        m = re.search(r"(\d+)\s*(?:\+|or more|plus)", lab)
        if m and n >= int(m.group(1)):
            return o
        if re.fullmatch(rf"\s*{n}(\s*years?)?\s*", lab):
            return o
    return None


REGION_ALIASES = {"GB": ["UK", "United Kingdom", "Great Britain", "Europe", "EMEA"], "US": ["United States", "USA", "North America"],
                  "NO": ["Norway", "Europe", "EMEA"]}


def _plain(s: str) -> str:
    return re.sub(r"\s+", " ", (s or "").replace("\u2019", "'").replace("\u2018", "'").replace("\xa0", " ")).strip().lower()


DECLINE = re.compile(r"decline|prefer not|rather not|don.t wish|do not wish|not to (say|answer|disclose|share)", re.I)
OTHER_OK = {"university", "field_of_study", "current_industry", "residence_country", "country", "heard_from", "nationality", "languages_other"}


def _pick_option(options: list[dict], text: str) -> dict | None:
    t = _plain(text)
    options = [o | {"_l": _plain(o["label"])} for o in options]
    if t.isdigit() and (hit := _by_number(options, int(t))):
        return hit
    for o in options:
        if o["_l"] == t:
            return o
    if YES.match(text) or NO.match(text):
        want = YES if YES.match(text) else NO
        hits = [o for o in options if want.match(o["_l"])]
        if len(hits) == 1:
            return hits[0]
        if want is YES:  # consent boxes offer "Acknowledge/Confirm" rather than "Yes"
            hits = [o for o in options if re.match(r"\s*(i\s+)?(acknowledge|confirm|confirmed|agree|accept|consent|understand|opt in|have read|'ve read)\b|\s*i've read", o["_l"])]
            if len(hits) == 1:
                return hits[0]
        if want is NO:  # "I have never worked at X", "No, I don't have a disability"
            hits = [o for o in options if re.match(r"\s*(no|never|i have never|i am not|i do not|i don't|not )", o["_l"])]
            if len(hits) == 1:
                return hits[0]
    hits = [o for o in options if t and (t in o["label"].lower() or o["label"].strip().lower() in t)]
    if len(hits) == 1:
        return hits[0]
    if "website" in t or "career" in t:  # "Company website" -> "Career Page", "Careers Site", "Flow Traders Website"
        hits = [o for o in options if re.search(r"careers? (site|page)|company (web)?site|\bwebsite\b", o["_l"])]
        if len(hits) == 1:
            return hits[0]
    # Last resort: exactly one option shares a distinctive word ("Company website" -> "Flow Traders Website").
    words = [w for w in re.findall(r"[a-z]{5,}", t) if w not in ("company", "other", "please", "prefer")]
    hits = [o for o in options if any(re.search(rf"\b{w}\b", o["_l"]) for w in words)]
    return hits[0] if len(hits) == 1 else None


def _strip_internal(o):
    return {k: v for k, v in o.items() if k != "_l"} if o else o


def pick(options: list[dict], text: str, key: str = "") -> dict | None:
    """Pick an option for an answer. Answers may list fallbacks as 'A || B || C': the first that matches an option wins."""
    for alt in [a.strip() for a in text.split("||") if a.strip()]:
        hit = _pick_option(options, alt)
        if hit:
            return _strip_internal(hit)
    if key in OTHER_OK:
        hit = [o for o in options if re.fullmatch(r"\s*others?\s*(\.\.\.|…|\(please specify\)|:)?\s*", o["label"], re.I)]
        if len(hit) == 1:
            return hit[0]
    return None


def free_text(text: str) -> str:
    """Value for a text field: the first fallback that is not a decline phrase, without dashes used as punctuation."""
    alts = [a.strip() for a in text.split("||") if a.strip()]
    value = next((a for a in alts if not DECLINE.search(a)), alts[0] if alts else text)
    return re.sub(r"\s*[\u2014\u2013]\s*", ", ", value)  # never an em or en dash


CURRENCY = {"US": "USD", "GB": "GBP", "IN": "INR", "CH": "CHF", "SE": "SEK", "NO": "NOK", "DK": "DKK", "PL": "PLN", "CZ": "CZK", "HU": "HUF",
            "AE": "AED", "SA": "SAR", "BR": "BRL", "CA": "CAD", "SG": "SGD", "HK": "HKD", "JP": "JPY", "AU": "AUD", "ZA": "ZAR", "MX": "MXN",
            "CN": "CNY", "IL": "ILS", "TR": "TRY", "RO": "RON", "BG": "BGN"}
EUROZONE = {"AT", "BE", "HR", "CY", "EE", "FI", "FR", "DE", "GR", "IE", "IT", "LV", "LT", "LU", "MT", "NL", "PT", "SK", "SI", "ES"}
COMPLETED = re.compile(r"\bcompleted\b|obtain|earned|attained|highest|achieved|graduated from", re.I)


def _profile_value(conn: Conn, key: str, country: str | None, employer_id: str, strict: bool, label: str = ""):
    """Answers derived from confirmed profile facts. Returns (text, source) or (None, None)."""
    get = lambda k: profile.get_fact(conn, k, confirmed_only=strict)  # noqa: E731
    name = get("identity/name") or {}
    edu = profile.current_education(conn, strict, completed=bool(COMPLETED.search(label))) or {}
    gpa = next((e["gpa_text"] for e in profile.education_entries(conn, strict) if e.get("gpa_text")), None)
    simple = {
        "first_name": (name.get("first"), "identity/name"), "last_name": (name.get("last"), "identity/name"),
        "email": (get("contact/application_email") or get("contact/email"), "contact/email"),
        "phone": (get("contact/phone"), "contact/phone"), "linkedin": (get("links/linkedin"), "links/linkedin"),
        "website": (get("links/website"), "links/website"),
        "university": (edu.get("institution"), "education"), "field_of_study": (edu.get("field"), "education"),
        "degree_level": (edu.get("level"), "education"), "gpa": (gpa, "education"),
        "graduation_year": (str(edu["graduation"])[:4] if edu.get("graduation") else None, "education"),
        "graduation_month": (edu.get("graduation_month"), "education"),
        "graduation_date": (edu.get("graduation"), "education"),
    }
    loc = get("contact/location") or {}
    simple["current_location"] = (", ".join(x for x in (loc.get("city"), loc.get("country")) if x) or None, "contact/location")
    simple["location"] = simple["current_location"]
    import datetime as dt
    today = dt.date.today()
    if key == "today_date":
        fmt = "%m/%d/%y" if re.search(r"mm/dd/yy\b", label, re.I) else "%m/%d/%Y" if re.search(r"mm/dd/yyyy", label, re.I) else \
            "%d/%m/%Y" if re.search(r"dd/mm/yyyy", label, re.I) else "%d/%m/%y" if re.search(r"dd/mm/yy\b", label, re.I) else "%Y-%m-%d"
        return today.strftime(fmt), "today's date"
    if key in ("current_employer", "current_title"):
        jobs = [f["value"] for f in profile.facts(conn, "employment/") if not strict or f["status"] == "confirmed"]
        if jobs:
            job = max(jobs, key=lambda j: (str(j.get("end") or "9999"), str(j.get("start") or "")))
            return (job.get("employer") if key == "current_employer" else job.get("title")), "profile:employment (most recent)"
    if key == "is_student":
        cur = profile.current_education(conn, strict) or {}
        if cur.get("graduation"):
            return ("Yes" if str(cur["graduation"]) >= today.strftime("%Y-%m") else "No"), "profile:education"
    if key == "residence_country":
        c_name = (get("contact/location") or {}).get("country")
        if c_name:
            alias = {"United Kingdom": "United Kingdom || UK || Great Britain"}.get(c_name, c_name)
            return f"{alias} || Other", "profile:contact/location"
    if key == "salary_currency" and country is None:
        return None, None
    if key == "salary_currency":
        cur = CURRENCY.get(country) or ("EUR" if country in EUROZONE else None)
        return (cur, "vacancy country") if cur else (None, None)
    if key == "visa_status" and country:
        fact, _ = profile.work_auth_fact(conn, country, strict)
        if fact is not None:
            if fact.get("authorised") and not fact.get("needs_sponsorship"):
                return "I'm a citizen or have permanent residency || Citizen || EU/EEA citizen || N/A", f"profile:work_auth/{country}"
            return "Would require visa sponsorship || sponsor || Foreigner || N/A", f"profile:work_auth/{country}"
    if key == "graduation_month":
        g = (edu or {}).get("graduation")
        if g and re.fullmatch(r"\d{4}-\d{2}", str(g)):
            return dt.datetime.strptime(str(g), "%Y-%m").strftime("%B"), "profile:education"
    if key == "experience_years":
        years = profile.experience_years(conn, strict)
        return (str(years), "profile:employment (computed from confirmed employment dates)") if years is not None else (None, None)
    if key == "current_region":
        for alias in REGION_ALIASES.get(geo.country_of((get("contact/location") or {}).get("country", "")) or "", []):
            return alias, "profile:contact/location"
    if key == "address":
        addr = get("contact/address")
        return (str(addr), "profile:contact/address") if addr else (None, None)
    if key == "languages_other":
        langs = [f["value"]["language"] for f in profile.facts(conn, "languages/") if (not strict or f["status"] == "confirmed")]
        if re.search(r"other than english|besides english|apart from english", label, re.I):
            langs = [x for x in langs if x.lower() != "english"]
        if langs:
            return "; ".join(langs), "profile:languages"
    if key in simple:
        val, src = simple[key]
        return (str(val), f"profile:{src}") if val else (None, None)
    if key.startswith(("work_auth:", "sponsorship:")) and country:
        fact, _ = profile.work_auth_fact(conn, country, strict)
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
        if re.search(r"interview", label, re.I):  # interviewed, not merely applied
            prior = conn.val("SELECT COUNT(*) FROM applications WHERE employer_id=? AND (LOWER(state_reason) LIKE '%(interview)%' "
                             "OR LOWER(state_reason) LIKE '%(hirevue)%' OR LOWER(state_reason) LIKE '%(offer)%')", (employer_id,))
        else:
            prior = conn.val("SELECT COUNT(*) FROM applications WHERE employer_id=? AND (submitted_at IS NOT NULL OR state IN ('submitted','uncertain'))",
                             (employer_id,))
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

    def sponsor_rank(o):  # prefer locations where no visa is needed
        fact, _ = profile.work_auth_fact(conn, geo.classify_location(o["label"], cfg)["country"] or "", strict)
        return 0 if fact and fact.get("authorised") and not fact.get("needs_sponsorship") else 1

    # A location choice decides which country the visa questions refer to, so settle it first.
    for q in form["questions"]:
        f0 = q["fields"][0]
        if f0.get("options") and classify(q["label"], countries)[0] == "preferred_location":
            elig = sorted([o for o in f0["options"] if geo.classify_location(o["label"], cfg)["kind"] == "eligible"
                           and (not countries or geo.classify_location(o["label"], cfg)["country"] in countries)], key=sponsor_rank)
            if elig:
                out["selected_location"], out["location_selectable"] = elig[0]["label"], True
                break
    sel_country = geo.classify_location(out["selected_location"], cfg)["country"] if out["selected_location"] else None
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
        if key.endswith(":?") and sel_country:
            key, country = key[:-1] + sel_country, sel_country
            entry["key"] = key
        if key in ("salary_currency", "visa_status") and country is None:
            country = sel_country or (countries[0] if len(countries) == 1 else None)
        if key == "preferred_location" and options:
            hit = next((o for o in options if out["selected_location"] and o["label"] == out["selected_location"]), None)
            if hit:
                out["answers"].append(entry | {"value": hit["label"], "option_value": [hit["value"]] if ftype == "multi_value_multi_select" else hit["value"],
                                               "source": "policy: eligible location, preferring countries without a visa requirement"})
            else:
                out["missing"].append(entry | {"options": [o["label"] for o in options], "why": "no eligible location among the options"})
            continue
        text, source = _profile_value(conn, key, country, employer_id, strict, q["label"])
        if text is not None and key.startswith("sponsorship:") and not options and re.match(r"\s*(if .{0,80},\s*)?(what|which)\b", q["label"], re.I):
            text = "Work visa sponsorship" if YES.match(text) else "None"   # "What sponsorship would you need?" wants a description, not Yes/No
        if text is None and key.endswith(":?") and countries:
            found = {_profile_value(conn, key[:-1] + cc, cc, employer_id, strict, q["label"]) for cc in countries}
            if len(found) == 1 and next(iter(found))[0] is not None:
                text, source = next(iter(found))
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
        if options and key == "graduation_date" and {_plain(o["label"]) for o in options} <= {"yes", "no"}:
            text = _in_date_range(q["label"], text) or text  # "Is your graduation between Aug 2027 and December 2028?"
        if options:
            if ftype == "multi_value_multi_select":
                alts = [a.strip() for a in text.split("||") if a.strip()]
                parts = [x.strip() for x in alts[0].split(";")] if alts else []
                got = [pick(options, x, key) for x in parts]
                if len(alts) > 1 and not any(got):
                    got = [pick(options, text, key)]
                got = [g for g in got if g]
                if got and (key in ("languages_other",) or len(got) == len(parts) or len(alts) > 1):
                    out["answers"].append(entry | {"value": "; ".join(g["label"] for g in got), "option_value": [g["value"] for g in got], "source": source})
                    continue
                (out["missing"] if q["required"] else out["skipped"]).append(
                    entry | {"options": [o["label"] for o in options], "why": f"stored answer '{text}' does not match any option"})
                continue
            wanted = [text]
            if key in ("university", "graduation_date") and ftype != "multi_value_multi_select":
                cands = [text]
                if key == "university":
                    cands += (profile.current_education(conn, strict) or {}).get("aliases", [])
                elif re.fullmatch(r"\d{4}-\d{2}", text):
                    import datetime as dt
                    d = dt.datetime.strptime(text, "%Y-%m")
                    season = "Fall" if d.month in (9, 10, 11) else "Spring/Summer" if 3 <= d.month <= 8 else "Winter"
                    cands += [d.strftime("%B %Y"), d.strftime("%b %Y"), d.strftime("%m/%Y"), f"{season} {d.year}",
                              f"Autumn {d.year}" if season == "Fall" else f"{season} {d.year}", d.strftime("%Y")]
                wanted = [next((c_ for c_ in cands if pick(options, c_, key)), text)]
            if key == "current_region":
                loc_c = geo.country_of((profile.get_fact(conn, "contact/location", confirmed_only=strict) or {}).get("country", ""))
                wanted = [next((a for a in REGION_ALIASES.get(loc_c, []) if pick(options, a)), text)]
            picked = [pick(options, w, key) for w in wanted]
            if any(p is None for p in picked):
                (out["missing"] if q["required"] else out["skipped"]).append(
                    entry | {"options": [o["label"] for o in options], "why": f"stored answer '{text}' does not match any option"})
                continue
            out["answers"].append(entry | {"value": "; ".join(p["label"] for p in picked),
                                           "option_value": [p["value"] for p in picked] if len(picked) > 1 or ftype == "multi_value_multi_select" else picked[0]["value"],
                                           "source": source})
        else:
            out["answers"].append(entry | {"value": free_text(text), "source": source})
    # Voluntary demographic questions: only an explicit stored preference is ever used.
    for dq in form.get("demographic") or []:
        key = demographic_key(dq["label"], dq["options"])
        lib = (lookup(conn, key, employer_id, allow_proposed=not strict) or lookup(conn, "demographic:" + norm_label(dq["label"]), employer_id, allow_proposed=not strict)
               or lookup(conn, "demographic:default", employer_id, allow_proposed=not strict))
        entry = {"field": str(dq["id"]), "type": "demographic", "label": dq["label"], "key": key, "required": dq["required"]}
        opt = pick(dq["options"], lib["answer"]) if lib else None
        if lib and not opt and re.search(r"decline|prefer not|not (wish|want)", lib["answer"], re.I):
            opt = next((o for o in dq["options"] if re.search(r"decline|prefer not|don.t wish|do not wish|not to (say|answer|disclose)", o["label"], re.I)), None)
        if opt:
            out["answers"].append(entry | {"value": opt["label"], "option_value": opt["value"], "source": f"answer library ({lib['source']})"})
        elif dq["required"]:
            out["missing"].append(entry | {"options": [o["label"] for o in dq["options"]], "why": "mandatory demographic question without a stored preference"})
        else:
            out["skipped"].append(entry)
    return out


def demographic_key(label: str, options: list[dict]) -> str:
    """Normalise voluntary self-identification questions to a few stable keys, using the options when the label is vague."""
    labs = " ".join(o["label"].lower() for o in options)
    if re.search(r"cisgender|transgender", labs + " " + label.lower()):
        return "demographic:transgender"
    key, _ = classify(label, [])
    if key.startswith("demographic:"):
        return key
    if key == "military":
        return "demographic:veteran"
    if re.search(r"\b(man|woman|male|female)\b", labs):
        return "demographic:gender"
    return "demographic:" + norm_label(label)


_MONTHS = {m: i for i, m in enumerate(["jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec"], 1)}


def _in_date_range(label: str, value: str) -> str | None:
    """'Is your graduation between Aug 2027 and December 2028?' with value '2027-09' -> 'Yes'."""
    found = re.findall(r"\b([A-Za-z]{3,9})\.?\s+(\d{4})\b", label)
    dates = [(int(y), _MONTHS[m[:3].lower()]) for m, y in found if m[:3].lower() in _MONTHS]
    if len(dates) != 2 or not re.fullmatch(r"\d{4}-\d{2}", value or ""):
        return None
    v = tuple(map(int, value.split("-")))
    return "Yes" if dates[0] <= v <= dates[1] else "No"
