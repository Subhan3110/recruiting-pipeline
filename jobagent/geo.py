"""Location interpretation and the Oslo/London boundary.

Every vacancy location is classified against the policy's excluded areas as
'excluded', 'eligible' or 'unclear'. Unclear is a first-class outcome: a bare
"Norway" or "United Kingdom" could mean Oslo or London, so it is held.
"""
from __future__ import annotations

import re
import unicodedata

COUNTRIES = {
    "NO": ["norway", "norge"], "GB": ["united kingdom", "uk", "u.k.", "great britain", "england", "scotland", "wales", "northern ireland"],
    "US": ["united states", "usa", "us", "u.s.", "u.s.a.", "united states of america"], "CA": ["canada"],
    "SE": ["sweden", "sverige"], "DK": ["denmark", "danmark"], "FI": ["finland"], "IS": ["iceland"],
    "DE": ["germany", "deutschland"], "FR": ["france"], "NL": ["netherlands", "the netherlands"], "BE": ["belgium"],
    "LU": ["luxembourg"], "CH": ["switzerland"], "AT": ["austria"], "IE": ["ireland"], "ES": ["spain"], "PT": ["portugal"],
    "IT": ["italy"], "PL": ["poland"], "CZ": ["czech republic", "czechia"], "HU": ["hungary"], "RO": ["romania"],
    "BG": ["bulgaria"], "GR": ["greece"], "EE": ["estonia"], "LV": ["latvia"], "LT": ["lithuania"], "MT": ["malta"],
    "CY": ["cyprus"], "HR": ["croatia"], "SK": ["slovakia"], "SI": ["slovenia"], "LI": ["liechtenstein"],
    "AE": ["united arab emirates", "uae", "u.a.e."], "SA": ["saudi arabia", "ksa"], "QA": ["qatar"], "BH": ["bahrain"],
    "IL": ["israel"], "TR": ["turkey", "türkiye"], "EG": ["egypt"], "ZA": ["south africa"], "NG": ["nigeria"], "KE": ["kenya"],
    "IN": ["india"], "SG": ["singapore"], "HK": ["hong kong", "hong kong sar"], "JP": ["japan"], "CN": ["china"],
    "KR": ["south korea", "korea"], "TW": ["taiwan"], "AU": ["australia"], "NZ": ["new zealand"], "MY": ["malaysia"],
    "ID": ["indonesia"], "TH": ["thailand"], "PH": ["philippines"], "VN": ["vietnam"], "PK": ["pakistan"],
    "BR": ["brazil", "brasil"], "MX": ["mexico"], "AR": ["argentina"], "CL": ["chile"], "CO": ["colombia"], "PE": ["peru"],
    "UY": ["uruguay"], "UA": ["ukraine"], "RS": ["serbia"], "GE": ["georgia (country)"],
}
EEA = {"NO", "SE", "DK", "FI", "IS", "DE", "FR", "NL", "BE", "LU", "AT", "IE", "ES", "PT", "IT", "PL", "CZ", "HU", "RO",
       "BG", "GR", "EE", "LV", "LT", "MT", "CY", "HR", "SK", "SI", "LI"}
CITIES = {
    "NO": ["oslo", "bergen", "stavanger", "trondheim", "kristiansand", "tromsø", "tromso", "drammen", "ålesund", "bodø",
           "haugesund", "sandnes", "fredrikstad", "tønsberg", "lysaker", "fornebu", "sandvika", "asker", "bærum", "lillestrøm"],
    "GB": ["london", "edinburgh", "manchester", "birmingham", "glasgow", "leeds", "bristol", "belfast", "cardiff", "bournemouth",
           "cambridge", "oxford", "liverpool", "newcastle", "sheffield", "nottingham", "reading", "milton keynes", "canary wharf"],
    "US": ["new york", "new york city", "nyc", "san francisco", "south san francisco", "chicago", "boston", "seattle", "los angeles",
           "austin", "dallas", "houston", "miami", "atlanta", "denver", "washington", "washington dc", "washington d.c.", "philadelphia",
           "charlotte", "stamford", "greenwich", "jersey city", "salt lake city", "phoenix", "san diego", "san jose", "palo alto",
           "menlo park", "minneapolis", "nashville", "pittsburgh", "detroit", "st. louis", "tampa", "wilmington", "bala cynwyd",
           "newark", "princeton", "west palm beach", "plano", "irving", "raleigh", "columbus", "portland", "las vegas"],
    "CA": ["toronto", "vancouver", "montreal", "calgary", "ottawa", "waterloo"],
    "SE": ["stockholm", "gothenburg", "göteborg", "malmö", "malmo"], "DK": ["copenhagen", "københavn", "aarhus"],
    "FI": ["helsinki", "espoo"], "DE": ["berlin", "frankfurt", "frankfurt am main", "munich", "münchen", "hamburg", "düsseldorf",
                                         "dusseldorf", "cologne", "köln", "stuttgart"],
    "FR": ["paris"], "NL": ["amsterdam", "rotterdam", "the hague", "utrecht"], "BE": ["brussels", "antwerp"], "LU": ["luxembourg city"],
    "CH": ["zurich", "zürich", "geneva", "genève", "zug", "basel", "lugano"], "AT": ["vienna", "wien"], "IE": ["dublin", "cork"],
    "ES": ["madrid", "barcelona", "valencia", "málaga", "malaga"], "PT": ["lisbon", "lisboa", "porto"], "IT": ["milan", "milano", "rome"],
    "PL": ["warsaw", "krakow", "kraków", "wroclaw", "wrocław", "gdansk"], "CZ": ["prague"], "HU": ["budapest"], "RO": ["bucharest", "cluj"],
    "BG": ["sofia"], "GR": ["athens"], "EE": ["tallinn"], "LV": ["riga"], "LT": ["vilnius", "kaunas"],
    "AE": ["dubai", "abu dhabi", "difc", "adgm"], "SA": ["riyadh", "jeddah"], "QA": ["doha"], "BH": ["manama"], "IL": ["tel aviv"],
    "TR": ["istanbul"], "EG": ["cairo"], "ZA": ["johannesburg", "cape town", "durban", "sandton", "pretoria", "stellenbosch"],
    "NG": ["lagos"], "KE": ["nairobi"],
    "IN": ["mumbai", "bengaluru", "bangalore", "gurugram", "gurgaon", "new delhi", "delhi", "hyderabad", "pune", "chennai", "noida",
           "kolkata", "ahmedabad", "gift city"],
    "JP": ["tokyo", "osaka"], "CN": ["shanghai", "beijing", "shenzhen"], "KR": ["seoul"], "TW": ["taipei"],
    "AU": ["sydney", "melbourne", "brisbane", "perth"], "NZ": ["auckland", "wellington"], "MY": ["kuala lumpur"], "ID": ["jakarta"],
    "TH": ["bangkok"], "PH": ["manila"], "VN": ["ho chi minh city", "hanoi"], "PK": ["karachi", "lahore", "islamabad"],
    "BR": ["são paulo", "sao paulo", "rio de janeiro", "belo horizonte", "curitiba", "florianópolis", "brasília", "brasilia"],
    "MX": ["mexico city", "ciudad de méxico", "guadalajara", "monterrey"], "AR": ["buenos aires"], "CL": ["santiago"],
    "CO": ["bogotá", "bogota", "medellín", "medellin"], "PE": ["lima"], "UY": ["montevideo"], "UA": ["kyiv"], "RS": ["belgrade"],
}
US_STATES = {"alabama", "alaska", "arizona", "arkansas", "california", "colorado", "connecticut", "delaware", "florida", "georgia",
             "hawaii", "idaho", "illinois", "indiana", "iowa", "kansas", "kentucky", "louisiana", "maine", "maryland", "massachusetts",
             "michigan", "minnesota", "mississippi", "missouri", "montana", "nebraska", "nevada", "new hampshire", "new jersey",
             "new mexico", "new york", "north carolina", "north dakota", "ohio", "oklahoma", "oregon", "pennsylvania", "rhode island",
             "south carolina", "south dakota", "tennessee", "texas", "utah", "vermont", "virginia", "washington", "west virginia",
             "wisconsin", "wyoming", "district of columbia"}
US_STATE_CODES = set("al ak az ar ca co ct de fl ga hi id il in ia ks ky la me md ma mi mn ms mo mt ne nv nh nj nm ny nc nd oh ok or pa ri sc sd tn tx ut vt va wa wv wi wy dc".split())
CA_PROVINCES = {"ontario", "quebec", "british columbia", "alberta", "on", "qc", "bc", "ab"}
VAGUE = {"multiple locations", "multiple", "various", "various locations", "global", "worldwide", "anywhere", "emea", "europe", "eu",
         "apac", "asia", "americas", "latam", "nordics", "nordic", "scandinavia", "middle east", "africa", "international", "tbd",
         "flexible", "hybrid", "on-site", "onsite", "in-office", "see description", "north america", "mena", "dach", "benelux"}

_COUNTRY_BY_NAME = {n: cc for cc, names in COUNTRIES.items() for n in names}
_CITY_COUNTRY = {c: cc for cc, cities in CITIES.items() for c in cities}
# Cities that are also US states: prefer the city reading.
_REMOTE = re.compile(r"\b(remote|work from home|wfh|home[- ]based|distributed|telecommute)\b", re.I)


def norm(s: str) -> str:
    return re.sub(r"\s+", " ", unicodedata.normalize("NFKC", s or "").strip().lower())


def _fold(s: str) -> str:
    """Accent-insensitive form for matching place names (ø/æ/å handled explicitly)."""
    s = norm(s).replace("ø", "o").replace("æ", "ae").replace("å", "a")
    return "".join(c for c in unicodedata.normalize("NFKD", s) if not unicodedata.combining(c))


def country_of(token: str) -> str | None:
    t = norm(token)
    return _COUNTRY_BY_NAME.get(t) or _CITY_COUNTRY.get(t) or {_fold(k): v for k, v in _CITY_COUNTRY.items()}.get(_fold(t))


def _area_hit(text: str, areas: list[dict]) -> dict | None:
    f = _fold(text)
    for area in areas:
        if any(re.search(x, f) for x in area.get("exceptions", [])):
            continue
        for place in area["places"]:
            if re.search(rf"(?<![a-z]){re.escape(_fold(place))}(?![a-z])", f):
                return area
    return None


def _segments(raw: str) -> list[str]:
    """Split a free-text location into candidate places, keeping 'City, State, Country' qualifiers attached."""
    parts = [p.strip() for p in re.split(r"\s*(?:;|\||/|\bor\b|\band\b|•|·|\n)\s*", raw) if p.strip()]
    out: list[str] = []
    for part in parts:
        cur: list[str] = []
        for tok in [t.strip() for t in part.split(",") if t.strip()]:
            n = norm(tok)
            is_qualifier = (n in _COUNTRY_BY_NAME and n not in ("singapore", "hong kong", "luxembourg")) or n in US_STATE_CODES \
                or n in CA_PROVINCES or (n in US_STATES and cur and norm(cur[0]) != n and n not in _CITY_COUNTRY) \
                or (n in US_STATES and cur and norm(cur[-1]) == n)
            if n in ("new york", "washington") and cur and norm(cur[0]) in ("new york", "new york city", "nyc", "seattle"):
                is_qualifier = True
            if is_qualifier and cur and norm(cur[0]) not in _COUNTRY_BY_NAME:
                cur.append(tok)
            else:
                if cur:
                    out.append(", ".join(cur))
                cur = [tok]
        if cur:
            out.append(", ".join(cur))
    return out


def classify_location(raw: str, policy: dict) -> dict:
    """Classify one place string. Returns {raw, kind, country, area, remote, reason}."""
    areas = policy["excluded_areas"]
    text = norm(raw)
    remote = bool(_REMOTE.search(text))
    core = _REMOTE.sub("", text)
    core = re.sub(r"[()\[\]]|\s-\s|–|—|:", " ", core).strip(" ,-")
    core = re.sub(r"\s+", " ", core)
    toks = [t.strip() for t in core.split(",") if t.strip()]
    country = None
    for t in reversed(toks):
        country = country_of(t)
        if country:
            break
    if not country and toks and (norm(toks[-1]) in US_STATE_CODES or norm(toks[-1]) in US_STATES):
        country = "US"
    if not country and toks and norm(toks[-1]) in CA_PROVINCES:
        country = "CA"
    out = {"raw": raw, "country": country, "area": None, "remote": remote}
    area = _area_hit(core, areas)
    if area:
        return out | {"kind": "excluded", "area": area["name"], "country": area.get("country") or country,
                      "reason": f"inside {area['name']}"}
    if not core or core in VAGUE or all(norm(t) in VAGUE for t in toks):
        why = "remote role without stated permitted working locations" if remote else "location is not specific"
        return out | {"kind": "unclear", "reason": why}
    only_country = all(norm(t) in _COUNTRY_BY_NAME for t in toks) and norm(toks[0]) not in ("singapore", "hong kong")
    has_excluded_area = any(a.get("country") == country for a in areas)
    if only_country and has_excluded_area and not remote:
        return out | {"kind": "unclear", "reason": f"country-level location ({raw}) could mean an excluded area"}
    if not country:
        return out | {"kind": "unclear", "reason": f"could not determine the country of '{raw}'"}
    return out | {"kind": "eligible", "reason": "outside the excluded areas"}


_OFFICE_TIE = r"(?:based|located|office|offices|on[- ]site|onsite|hybrid|in[- ]person|relocat\w+|days? (?:a|per) week)"


def evaluate(location_raw: str, structured: list[str] | None, description: str, policy: dict, home: dict | None = None) -> dict:
    """Geographic decision for a vacancy.

    decision: 'auto' (all eligible), 'approval' (only excluded areas),
    'auto_if_selectable' (mixed: needs an explicit eligible choice in the form), 'hold' (unclear).
    `home` is the candidate's own base {'city','country'}; it decides remote roles.
    """
    raws = [s for s in (structured or []) if s and s.strip()] or _segments(location_raw or "")
    locs = [classify_location(r, policy) for r in raws] or [{"raw": "", "kind": "unclear", "country": None, "area": None,
                                                              "remote": False, "reason": "no location given"}]
    reasons: list[str] = []
    # Remote roles: where the candidate would actually work matters, not the employer's base.
    home_loc = classify_location(f"{home.get('city', '')}, {home.get('country', '')}", policy) if home and home.get("city") else None
    for loc in locs:
        if not loc["remote"]:
            continue
        if loc["kind"] == "excluded":
            continue
        if home_loc is None:
            loc.update(kind="unclear", reason="remote role, but the candidate's own base location is not confirmed")
        elif loc["country"] and home_loc["country"] == loc["country"]:
            loc.update(kind=home_loc["kind"], area=home_loc["area"],
                       reason=f"remote within {loc['country']}; candidate would work from {home.get('city')}")
        elif loc["kind"] == "eligible":
            loc["reason"] = f"remote within {loc['country']}; no excluded area required"
    # The description can tie an apparently eligible role to an excluded area.
    desc = _fold(re.sub(r"<[^>]+>", " ", description or ""))
    tied = None
    for area in policy["excluded_areas"]:
        names = "|".join(re.escape(_fold(p)) for p in area["places"])
        m = re.search(rf"{_OFFICE_TIE}[^.]{{0,80}}(?<![a-z])({names})(?![a-z])|(?<![a-z])({names})(?![a-z])[^.]{{0,40}}{_OFFICE_TIE}", desc)
        if m and not any(re.search(x, desc[max(0, m.start() - 40):m.end() + 40]) for x in area.get("exceptions", [])):
            tied = area["name"]
            break
    kinds = {loc["kind"] for loc in locs}
    if "unclear" in kinds:
        decision = "hold"
        reasons += [loc["reason"] for loc in locs if loc["kind"] == "unclear"]
    elif kinds == {"excluded"}:
        decision = "approval"
        reasons.append("located in " + ", ".join(sorted({loc["area"] for loc in locs})) + ": individual approval required")
    elif kinds == {"eligible"}:
        if tied:
            decision = "hold"
            reasons.append(f"location looks eligible but the description ties the role to {tied}")
        else:
            decision = "auto"
            reasons.append("all listed locations are outside the excluded areas")
    else:
        decision = "auto_if_selectable"
        reasons.append("multiple locations including an excluded area: automatic only if the form lets an eligible location be chosen")
    return {"decision": decision, "locations": locs, "reasons": reasons,
            "countries": sorted({loc["country"] for loc in locs if loc["country"] and loc["kind"] == "eligible"}),
            "all_countries": sorted({loc["country"] for loc in locs if loc["country"]}),
            "eligible": [loc["raw"] for loc in locs if loc["kind"] == "eligible"]}
