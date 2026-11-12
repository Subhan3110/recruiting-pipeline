"""Source adapter interface. Discovery and submission capabilities are declared independently."""
from __future__ import annotations

import time
import tomllib
from dataclasses import asdict, dataclass
from pathlib import Path

import requests

CONFIG = Path(__file__).resolve().parents[2] / "config" / "sources.toml"


@dataclass(frozen=True)
class Capabilities:
    discover: bool = False
    describe: bool = False
    detect_closed: bool = False
    locate_form: bool = False
    authenticate: bool = False     # False can also mean "no account needed"
    prepare: bool = False
    submit: bool = False
    verify_outcome: bool = False
    notes: str = ""

    def as_dict(self):
        return asdict(self)


class SourceAdapter:
    name = "base"
    capabilities = Capabilities()
    min_interval = 1.0  # seconds between requests to the same source; stay well inside published limits

    def __init__(self, settings: dict | None = None, session: requests.Session | None = None):
        self.settings = settings or {}
        self.http = session or requests.Session()
        self.http.headers["User-Agent"] = "jobagent/0.1 (personal job search)"
        self._last = 0.0

    def get_json(self, url: str, **params):
        wait = self.min_interval - (time.monotonic() - self._last)
        if wait > 0:
            time.sleep(wait)
        for attempt in range(4):
            r = self.http.get(url, params=params or None, timeout=30)
            self._last = time.monotonic()
            if r.status_code == 404:
                return None
            if r.status_code in (429, 500, 502, 503, 504):
                time.sleep(min(30, float(r.headers.get("Retry-After", 2 ** attempt * 2))))
                continue
            r.raise_for_status()
            return r.json()
        r.raise_for_status()

    # --- interface ------------------------------------------------------------
    def discover(self):
        """Yield vacancy dicts accepted by vacancies.upsert()."""
        raise NotImplementedError

    def is_open(self, source_id: str) -> bool | None:
        """True/False when the source can tell, None when unknown."""
        return None

    def form(self, source_id: str) -> dict | None:
        """Application form schema: {'url', 'questions': [...], 'location_options': [...]} or None."""
        return None

    def submitter(self):
        """Return an object with submit(package, hooks) -> SubmitResult, or None if unsupported."""
        return None


def load_config() -> dict:
    return tomllib.loads(CONFIG.read_text()) if CONFIG.exists() else {}


def registry() -> dict:
    from .greenhouse import GreenhouseAdapter

    return {"greenhouse": GreenhouseAdapter}


def get_adapter(name: str, **kw) -> SourceAdapter:
    cfg = load_config().get(name, {})
    return registry()[name](settings=cfg, **kw)
