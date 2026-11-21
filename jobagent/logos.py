"""Employer logos for cover letters, fetched from public sources and cached in the database.

Order tried: the employer's Greenhouse job-board logo, the company's Wikipedia article image when its file name says
it is a logo, then the icon of the company's own website. Every download is untrusted: https only, image content
types only, at most 3 MB, decoded and re-encoded to PNG with Pillow before it is stored. A missing logo is fine:
the letter is then rendered without one. A logo you supply with `python -m jobagent logo-set` always wins.
"""
from __future__ import annotations

import base64
import io
import json
import re
from urllib.parse import quote, urlsplit

import requests

from .db import Conn, ago, now, unj

UA = {"User-Agent": "jobagent/0.1 (personal job search tool; cover letter logos)"}
MAX_BYTES = 3_000_000
RETRY_DAYS = 30


_LAST: dict[str, float] = {}
MIN_GAP = {"en.wikipedia.org": 1.5}  # Wikimedia asks API clients not to burst


def _get(session, url: str, **kw):
    import time
    u = urlsplit(url)
    if u.scheme != "https":
        return None
    for attempt in range(2):
        gap = MIN_GAP.get(u.hostname, 0) - (time.monotonic() - _LAST.get(u.hostname, 0))
        if gap > 0:
            time.sleep(gap)
        try:
            r = session.get(url, headers=UA, timeout=15, **kw)
        except requests.RequestException:
            return None
        finally:
            _LAST[u.hostname] = time.monotonic()
        if r.status_code == 429 and attempt == 0:
            time.sleep(min(10, float(r.headers.get("Retry-After", 3) or 3)))
            continue
        return r if r.status_code == 200 else None
    return None


def _json(r) -> dict:
    try:
        return r.json() if r is not None else {}
    except ValueError:
        return {}


def _to_png(data: bytes, min_side: int = 48) -> tuple[bytes, int, int] | None:
    from PIL import Image

    if not data or len(data) > MAX_BYTES:
        return None
    try:
        img = Image.open(io.BytesIO(data))
        img.verify()
        img = Image.open(io.BytesIO(data))
        img.load()
    except Exception:  # noqa: BLE001 - not an image we can trust
        return None
    if min(img.size) < min_side and max(img.size) < 120:
        return None  # a tiny favicon looks wrong on a letter
    img = img.convert("RGBA")
    bbox = Image.new("RGBA", img.size, (255, 255, 255, 0))
    if img.getbbox():
        img = img.crop(img.getbbox())
    if max(img.size) > 800:
        img.thumbnail((800, 800))
    out = io.BytesIO()
    img.save(out, "PNG")
    del bbox
    return out.getvalue(), img.size[0], img.size[1]


def _image(session, url: str, min_side: int = 48):
    r = _get(session, url)
    if r is None or not r.headers.get("Content-Type", "").lower().startswith("image/") or "svg" in r.headers.get("Content-Type", "").lower():
        return None
    return _to_png(r.content, min_side)


def candidates(conn: Conn, employer_id: str, session) -> list[tuple[str, str]]:
    """(label, url-or-search) attempts for one employer, cheapest first."""
    emp = conn.one("SELECT name, domain FROM employers WHERE id=?", (employer_id,))
    out = []
    boards = [r["alias"].split(":", 1)[1] for r in conn.q("SELECT alias FROM employer_aliases WHERE employer_id=? AND alias_type='source' AND alias LIKE 'greenhouse:%'", (employer_id,))]
    for b in boards:
        out.append(("greenhouse", f"https://job-boards.greenhouse.io/{b}"))
    out.append(("wikipedia", emp["name"]))
    domains = {emp["domain"]} if emp["domain"] else set()
    for r in conn.q("SELECT vs.raw FROM vacancy_sources vs JOIN vacancies v ON v.id=vs.vacancy_id WHERE v.employer_id=? LIMIT 5", (employer_id,)):
        host = urlsplit((unj(r["raw"], {}) or {}).get("absolute_url") or "").hostname or ""
        if host and "greenhouse" not in host:
            domains.add(re.sub(r"^(www|careers|jobs)\.", "", host))
    for d in sorted(d for d in domains if d):
        out.append(("site-icon", d))
    return out


def fetch(conn: Conn, employer_id: str, session=None) -> dict:
    session = session or requests.Session()
    for kind, target in candidates(conn, employer_id, session):
        got, src = None, None
        if kind == "greenhouse":
            r = _get(session, target)
            m = re.search(r'"logo":\{"href":[^}]*?"url":"(https://[^"]+)"', r.text if r is not None else "")
            if m:
                src = m.group(1).encode().decode("unicode_escape")
                got = _image(session, src)
        elif kind == "wikipedia":
            api = "https://en.wikipedia.org/w/api.php"
            r = _get(session, api, params={"action": "query", "list": "search", "srsearch": f"{target} company", "srlimit": 1, "format": "json"})
            hits = _json(r).get("query", {}).get("search", [])
            if not hits:  # "Robinhood company" finds nothing, "Robinhood" does
                r = _get(session, api, params={"action": "query", "list": "search", "srsearch": target, "srlimit": 1, "format": "json"})
                hits = _json(r).get("query", {}).get("search", [])
            if hits and re.sub(r"\W", "", target.lower())[:6] in re.sub(r"\W", "", hits[0]["title"].lower()):
                r = _get(session, api, params={"action": "query", "titles": hits[0]["title"], "prop": "pageimages", "piprop": "thumbnail|name",
                                               "pithumbsize": 600, "format": "json"})
                for page in (_json(r).get("query", {}).get("pages", {}) or {}).values():
                    if "logo" in (page.get("pageimage") or "").lower() and page.get("thumbnail", {}).get("source"):
                        src = page["thumbnail"]["source"]
                        got = _image(session, src)
                if not got:  # the lead image is often a building; look for a file named as the company's logo
                    first = re.sub(r"\W", "", target.split()[0].lower())
                    r = _get(session, api, params={"action": "query", "titles": hits[0]["title"], "prop": "images", "imlimit": 50, "format": "json"})
                    files = [im["title"] for pg in (_json(r).get("query", {}).get("pages", {}) or {}).values()
                             for im in pg.get("images", []) if first in re.sub(r"\W", "", im["title"].lower())
                             and ("logo" in im["title"].lower() or re.fullmatch(rf"file:{re.escape(first)}\.(svg|png)", im["title"].lower().replace(" ", "")))]
                    for f in files[:3]:
                        r = _get(session, api, params={"action": "query", "titles": f, "prop": "imageinfo", "iiprop": "url", "iiurlwidth": 600, "format": "json"})
                        info = [ii for pg in (_json(r).get("query", {}).get("pages", {}) or {}).values()
                                for ii in pg.get("imageinfo", [])]
                        src = info[0].get("thumburl") or info[0].get("url") if info else None
                        got = _image(session, src) if src else None
                        if got:
                            break
        else:
            for src in (f"https://{target}/apple-touch-icon.png", f"https://www.google.com/s2/favicons?domain={quote(target)}&sz=256"):
                got = _image(session, src, min_side=100)
                if got:
                    break
        if got:
            png, w, h = got
            save(conn, employer_id, "ok", png, src, w, h)
            return {"status": "ok", "source": kind, "url": src}
    save(conn, employer_id, "none", None, None, None, None)
    return {"status": "none"}


def save(conn: Conn, employer_id: str, status: str, png: bytes | None, src: str | None, w, h):
    conn.q("INSERT INTO logos (employer_id, status, png_b64, source_url, width, height, checked_at) VALUES (?,?,?,?,?,?,?) "
           "ON CONFLICT (employer_id) DO UPDATE SET status=excluded.status, png_b64=excluded.png_b64, source_url=excluded.source_url, "
           "width=excluded.width, height=excluded.height, checked_at=excluded.checked_at",
           (employer_id, status, base64.b64encode(png).decode() if png else None, src, w, h, now()))
    conn.event("logo." + status, "employer", employer_id, {"source": src})


def set_manual(conn: Conn, employer_id: str, data: bytes) -> bool:
    got = _to_png(data, min_side=16)
    if not got:
        return False
    save(conn, employer_id, "manual", got[0], "supplied by you", got[1], got[2])
    return True


def get(conn: Conn, employer_id: str, allow_fetch: bool = True, session=None) -> bytes | None:
    row = conn.one("SELECT * FROM logos WHERE employer_id=?", (employer_id,))
    if row and row["status"] in ("ok", "manual"):
        return base64.b64decode(row["png_b64"])
    import os
    if os.environ.get("JOBAGENT_LOGO_FETCH", "1") == "0":
        allow_fetch = False
    if allow_fetch and (not row or row["checked_at"] < ago(days=RETRY_DAYS)):
        if fetch(conn, employer_id, session)["status"] == "ok":
            return get(conn, employer_id, allow_fetch=False)
    return None
