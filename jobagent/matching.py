"""Transparent matching: deterministic hard constraints plus an explainable priority score.

Scores rank vacancies. They are not hiring probabilities. Every check returns
pass / fail / unknown so a confirmed incompatibility is never confused with a
missing piece of information.
"""
from __future__ import annotations

import re

from . import geo, policy as policy_mod, profile
from .db import Conn, j, new_id, now, unj

CATEGORIES = {
    "investment banking & corporate finance": ["investment bank", "m&a", "mergers", "corporate finance", "capital markets",
                                               "leveraged finance", "corporate development", "deal advisory", "transaction services",
                                               "valuation", "restructuring", "due diligence", "financial advisory"],
    "markets & trading": ["trading", "trader", "market making", "derivatives", "fixed income", "equities", "commodities",
                          "execution", "brokerage", "prime brokerage", "sales and trading"],
    "asset & wealth management, research": ["asset management", "wealth management", "portfolio", "investment analyst", "investment",
                                            "private equity", "venture capital", "hedge fund", "equity research", "investor relations",
                                            "fund", "economist"],
    "fp&a and controlling": ["fp&a", "financial planning", "financial analyst", "finance analyst", "controller", "controlling",
                             "budget", "forecasting", "finance business partner", "strategic finance", "finance associate",
                             "finance manager", "finance intern"],
    "accounting & audit": ["accounting", "accountant", "audit", "auditor", "tax", "financial reporting", "ifrs", "gaap",
                           "accounts payable", "accounts receivable", "bookkeeping", "revenue operations accounting"],
    "treasury, credit & risk": ["treasury", "credit", "risk", "liquidity", "underwriting", "lending", "collections", "capital management"],
    "compliance, aml & kyc": ["compliance", "aml", "anti-money laundering", "kyc", "kyb", "financial crime", "sanctions",
                              "fraud", "transaction monitoring", "regulatory", "onboarding analyst", "due diligence analyst"],
    "insurance & financial operations": ["insurance", "actuarial", "financial operations", "finance operations", "payment operations",
                                         "payments operations", "settlement", "reconciliation", "middle office", "back office",
                                         "fund operations", "trade support", "treasury operations", "banking operations"],
    "fintech & finance business analysis": ["business analyst", "strategy analyst", "banking", "financial services", "finance",
                                            "financial", "economics"],
}
OFF_TOPIC = re.compile(r"\b(software|engineer|engineering|developer|devops|sre|designer|design|recruit\w*|talent|marketing|"
                       r"legal counsel|lawyer|attorney|machine learning|data scientist|frontend|backend|full[- ]stack|security|"
                       r"it support|product manager|ux|copywriter|people partner|hr|office manager|executive assistant|"
                       r"account executive|sdr|bdr|hardware|fpga|quantitative researcher|quant researcher)\b", re.I)
LEVELS = [
    ("senior", re.compile(r"\b(senior|sr\.?|lead|principal|head|director|vp|vice president|svp|evp|avp|manager|staff|chief|partner|"
                          r"managing|expert|architect|experienced|ii|iii|iv|mba|ph\.?d|postdoc)\b", re.I)),
    ("intern", re.compile(r"\b(intern|interns|internship|summer analyst|spring week|insight|placement|working student|werkstudent|"
                          r"trainee|apprentice\w*|off[- ]cycle|stagiaire|praktik\w*|co-?op)\b", re.I)),
    ("graduate", re.compile(r"\b(graduate|grad|new grad\w*|campus|early careers?|entry[- ]level|junior|jr\.?|academy|"
                            r"analyst program\w*|rotational|university)\b", re.I)),
    ("entry", re.compile(r"\b(analyst|assistant|associate|coordinator|administrator|officer)\b", re.I)),
]
LANGS = {"en": "english", "no": "norwegian", "de": "german", "fr": "french", "es": "spanish", "pt": "portuguese", "it": "italian",
         "nl": "dutch", "sv": "swedish", "da": "danish", "fi": "finnish", "pl": "polish", "ja": "japanese", "zh": "mandarin",
         "ko": "korean", "ar": "arabic", "hi": "hindi", "tr": "turkish", "he": "hebrew", "cs": "czech", "ur": "urdu"}
_LANG_NAMES = "|".join(sorted(set(LANGS.values()) | {"chinese", "cantonese"}))
LANG_REQ = re.compile(rf"\b(fluen\w+|proficien\w+|native|business[- ]level|professional (?:working )?proficiency|excellent command|"
                      rf"must speak|required)\b[^.\n]{{0,60}}\b({_LANG_NAMES})\b|\b({_LANG_NAMES})\b[^.\n]{{0,40}}\b(fluen\w+|native|"
                      rf"is required|required|mandatory|proficien\w+)\b", re.I)
NICE = re.compile(r"\b(plus|bonus|advantage|nice to have|preferred|desirable|beneficial|asset|optional)\b", re.I)
YEARS = re.compile(r"(\d{1,2})\s*(?:\+|plus|-\s*\d{1,2}|to\s*\d{1,2})?\s*(?:\+\s*)?years?(?:['’]|\s+of)?\s+(?:of\s+)?(?:\w+\s+){0,4}?experience", re.I)
NO_SPONSOR = re.compile(r"(not|unable to|cannot|can't|won't|will not|do not|does not|doesn't)\s+(?:\w+\s+){0,4}?(sponsor|provide (?:visa|work))|"
                        r"without (?:the need for )?(?:visa )?sponsorship|must (?:already )?(?:have|possess|hold)[^.\n]{0,40}(right|authori[sz]ation|eligib\w+) to work|"
                        r"no (?:visa )?sponsorship", re.I)
ADV_DEGREE = re.compile(r"\b(master'?s?|msc|m\.sc|mba|ph\.?d|doctorate|postgraduate)\b[^.\n]{0,50}\b(required|must|essential|minimum)\b|"
                        r"\b(required|must have|minimum)\b[^.\n]{0,50}\b(master'?s?|msc|mba|ph\.?d|doctorate)\b|"
                        r"\b(pursuing|enrolled in|studying)\b[^.\n]{0,30}\b(master'?s?|msc|mba|ph\.?d)\b(?![^.\n]{0,25}\b(or|/)\b[^.\n]{0,15}bachelor)", re.I)


def _kw(pattern: str) -> re.Pattern:
    return re.compile(rf"(?<![a-z]){re.escape(pattern)}(?![a-z])", re.I)


def seniority(title: str, description: str, cfg: dict) -> dict:
    level = "unknown"
    for name, pat in LEVELS:
        if pat.search(title):
            level = name
            break
    years = []
    for m in YEARS.finditer(description or ""):
        ctx = (description[max(0, m.start() - 70):m.end() + 30]).lower()
        # Company boilerplate ("our firm has 30 years of experience investing") is not a candidate requirement.
        if int(m.group(1)) <= 15 and not re.search(r"\b(our|we|firm|company|team has|history|combined|collective|founded|track record)\b", ctx):
            years.append(int(m.group(1)))
    min_years = min(years) if years else None
    if level == "senior":
        return {"result": "fail", "level": level, "min_years": min_years, "reason": "title indicates an experienced-hire role"}
    if min_years is not None and min_years > cfg["max_years_experience"] and level not in ("intern", "graduate"):
        return {"result": "fail", "level": level, "min_years": min_years,
                "reason": f"asks for {min_years}+ years of experience (limit {cfg['max_years_experience']})"}
    if level in cfg["allowed"]:
        return {"result": "pass", "level": level, "min_years": min_years, "reason": f"{level}-level role"}
    return {"result": "unknown", "level": level, "min_years": min_years, "reason": "seniority is not stated in the title"}


def relevance(title: str, description: str) -> dict:
    t_hits, d_hits, cats = [], set(), []
    for cat, kws in CATEGORIES.items():
        th = [k for k in kws if _kw(k).search(title)]
        if th:
            t_hits += th
            cats.append(cat)
        d_hits |= {k for k in kws if _kw(k).search(description or "")}
    off = bool(OFF_TOPIC.search(title))
    generic_only = t_hits and all(h in ("finance", "financial", "banking", "investment", "risk", "credit", "fraud", "fund", "execution",
                                         "regulatory", "tax", "trading", "equities") for h in t_hits)
    if t_hits and not (off and generic_only):
        pts = 35 + min(20, 2 * len(d_hits))
    elif off:
        pts = min(12, len(d_hits))
    else:
        pts = min(28, 3 * len(d_hits))
    return {"points": pts, "title_hits": sorted(set(t_hits)), "description_hits": len(d_hits), "categories": cats, "off_topic_title": off}


def languages(conn: Conn, vacancy: dict) -> dict:
    known = {f["key"].split("/")[1]: f for f in profile.facts(conn, "languages/")}
    known_names = {LANGS.get(c, c) for c in known}
    if vacancy.get("language") and vacancy["language"][:2] not in known:
        return {"result": "fail", "reason": f"vacancy is written in '{vacancy['language']}', not a confirmed language", "evidence": []}
    missing = set()
    for m in LANG_REQ.finditer(vacancy.get("description") or ""):
        lang = (m.group(2) or m.group(3)).lower()
        sentence = (vacancy["description"][max(0, m.start() - 80):m.end() + 80])
        if lang not in known_names and not NICE.search(sentence):
            missing.add(lang)
    if not known:
        return {"result": "unknown", "reason": "no languages recorded in the profile", "evidence": []}
    if missing:
        return {"result": "fail", "reason": "requires " + ", ".join(sorted(missing)), "evidence": [f"languages/{c}" for c in known]}
    return {"result": "pass", "reason": "no language requirement beyond " + ", ".join(sorted(known_names)),
            "evidence": [f"languages/{c}" for c in known]}


def work_authorisation(conn: Conn, countries: list[str], description: str, cfg: dict) -> dict:
    """Per-country check from confirmed facts only. Worldwide interest is not worldwide authorisation."""
    no_sponsor = bool(NO_SPONSOR.search(description or ""))
    per, ok, unknown = {}, [], []
    for cc in countries:
        fact = profile.get_fact(conn, f"work_auth/{cc}", confirmed_only=True)
        key = f"work_auth/{cc}"
        if fact is None and cc in geo.EEA:
            fact = profile.get_fact(conn, "work_auth/EEA", confirmed_only=True)
            key = "work_auth/EEA"
        if fact is None:
            per[cc] = {"result": "unknown", "reason": "work authorisation not confirmed"}
            unknown.append(cc)
        elif fact.get("authorised") and not fact.get("needs_sponsorship"):
            per[cc] = {"result": "pass", "reason": "authorised to work without sponsorship", "evidence": key}
            ok.append(cc)
        elif no_sponsor:
            per[cc] = {"result": "fail", "reason": "sponsorship needed and the vacancy rules it out", "evidence": key}
        elif cfg["apply_when_sponsorship_needed"]:
            per[cc] = {"result": "pass", "reason": "sponsorship needed; policy allows applying where it is not ruled out", "evidence": key}
            ok.append(cc)
        else:
            per[cc] = {"result": "fail", "reason": "sponsorship needed; policy does not apply in that case", "evidence": key}
    if not countries:
        return {"result": "unknown", "reason": "country of work could not be determined", "per_country": per, "ok": [], "unknown": []}
    if ok:
        return {"result": "pass", "reason": "eligible in " + ", ".join(ok), "per_country": per, "ok": ok, "unknown": unknown}
    if unknown:
        return {"result": "unknown", "reason": "work authorisation not confirmed for " + ", ".join(unknown), "per_country": per,
                "ok": [], "unknown": unknown}
    return {"result": "fail", "reason": "; ".join(f"{c}: {p['reason']}" for c, p in per.items()), "per_country": per, "ok": [], "unknown": []}


def evaluate(conn: Conn, vacancy_id: str, llm=None) -> dict:
    """Evaluate one vacancy against the profile and current policy; persist and return the evaluation."""
    from . import vacancies

    v = vacancies.get(conn, vacancy_id)
    pv, cfg = policy_mod.current(conn)
    title, desc = v["title"], v["description"] or ""
    home = profile.get_fact(conn, "contact/location")
    structured = (unj(conn.val("SELECT raw FROM vacancy_sources WHERE vacancy_id=? LIMIT 1", (vacancy_id,)), {}) or {}).get("offices")
    # Offices are a board-level grouping and can be broader than the job's own location; trust the job location first.
    g = geo.evaluate(v["location_raw"] or "", None, desc, cfg, home)
    if g["decision"] == "hold" and structured:
        g2 = geo.evaluate("", structured, desc, cfg, home)
        if g2["decision"] != "hold":
            g = g2
    rel = relevance(title, desc)
    sen = seniority(title, desc, cfg["seniority"])
    lang = languages(conn, v)
    countries = g["countries"] if g["decision"] in ("auto", "auto_if_selectable") else sorted(
        {loc["country"] for loc in g["locations"] if loc["country"]})
    auth = work_authorisation(conn, countries, desc, cfg["eligibility"])
    edu = {"result": "fail", "reason": "requires or targets a postgraduate degree"} if ADV_DEGREE.search(title + ". " + desc) and \
        not any("master" in str(f["value"]).lower() or "phd" in str(f["value"]).lower() for f in profile.facts(conn, "education/")) \
        else {"result": "pass", "reason": "no postgraduate requirement found"}
    skills = [f["value"].get("name", "") if isinstance(f["value"], dict) else str(f["value"]) for f in profile.facts(conn, "skills/")]
    skill_hits = sorted({s for s in skills if s and _kw(s.split(" (")[0]).search(desc)})
    score = rel["points"] + {"pass": 25, "unknown": 12, "fail": 0}[sen["result"]] + min(10, 3 * len(skill_hits)) + \
        (10 if re.search(r"\b(bachelor|undergraduate|bsc|b\.sc|degree in (finance|economics|business|accounting))", desc, re.I) else 5)
    score = float(min(100, score))
    checks = {"employer_excluded": {"result": "fail" if v["employer_excluded"] else "pass",
                                    "reason": "employer is on the exclusion list" if v["employer_excluded"] else "not excluded"},
              "dismissed": {"result": "fail" if v["dismissed_at"] else "pass", "reason": "dismissed by user" if v["dismissed_at"] else ""},
              "seniority": sen, "language": lang, "education": edu, "work_authorisation": auth,
              "location": {"result": "unknown" if g["decision"] == "hold" else "pass", "reason": "; ".join(g["reasons"])}}
    if v["needs_reconciliation"]:
        checks["repost"] = {"result": "unknown", "reason": "looks like a repost of an earlier vacancy at this employer; needs reconciliation"}
    fails = [f"{k}: {c['reason']}" for k, c in checks.items() if c["result"] == "fail"]
    unknowns = [f"{k}: {c['reason']}" for k, c in checks.items() if c["result"] == "unknown" and k != "seniority"]
    if fails:
        decision = "reject"
    elif score < cfg["roles"]["min_score_recommend"]:
        decision = "reject"
        fails.append(f"relevance: score {score:.0f} is below the recommendation threshold")
    elif unknowns:
        decision = "hold"
    elif g["decision"] == "approval":
        decision = "approval"
    elif score >= cfg["roles"]["min_score_auto"] and cfg["auto_submit_outside_excluded"]:
        decision = g["decision"]  # auto | auto_if_selectable
    else:
        decision = "review"
    why = {"reject": "Not pursued", "hold": "Held for clarification", "approval": "Needs your approval (Oslo/London)",
           "auto": "Eligible for automatic application", "auto_if_selectable": "Automatic only if an eligible location can be selected",
           "review": "Relevant, below the automatic threshold"}[decision]
    rationale = f"{why}. Score {score:.0f}: " + (", ".join(rel["categories"][:2]) or "weak finance signal") + f"; {sen['reason']}. " + \
        ("Blocking: " + "; ".join(fails) + ". " if fails else "") + ("Unknown: " + "; ".join(unknowns) + "." if unknowns else "")
    detail = {"relevance": rel, "checks": checks, "geo": g, "skill_hits": skill_hits, "score_parts": {
        "relevance": rel["points"], "seniority": {"pass": 25, "unknown": 12, "fail": 0}[sen["result"]],
        "skills": min(10, 3 * len(skill_hits))}, "fails": fails, "unknowns": unknowns}
    with conn.tx():
        conn.q("UPDATE evaluations SET is_current=0 WHERE vacancy_id=? AND is_current=1", (vacancy_id,))
        conn.q("INSERT INTO evaluations (id, vacancy_id, created_at, policy_version, score, decision, geo, rationale, detail) VALUES (?,?,?,?,?,?,?,?,?)",
               (new_id("eval"), vacancy_id, now(), pv, score, decision, g["decision"], rationale.strip(), j(detail)))
        conn.q("UPDATE vacancies SET locations=? WHERE id=?", (j(g["locations"]), vacancy_id))
        if decision == "hold":
            if auth.get("unknown"):
                profile.open_task(conn, "question", "Work authorisation is not confirmed for some countries",
                                  "Roles are held until you record, per country, whether you may work there and whether you need sponsorship. "
                                  "The Profile page lists the countries that currently have held roles.", dedup_key="q:work_auth")
    return {"vacancy_id": vacancy_id, "score": score, "decision": decision, "geo": g, "rationale": rationale.strip(), "detail": detail,
            "policy_version": pv}


def current(conn: Conn, vacancy_id: str) -> dict | None:
    row = conn.one("SELECT * FROM evaluations WHERE vacancy_id=? AND is_current=1", (vacancy_id,))
    if row:
        row["detail"] = unj(row["detail"])
    return row
