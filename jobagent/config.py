"""Runtime configuration from environment variables. No secret is ever read from the repository."""
import os
from pathlib import Path

HOME = Path(os.environ.get("JOBAGENT_HOME", Path.home() / ".jobagent"))


def database_url() -> str:
    url = os.environ.get("JOBAGENT_DATABASE_URL", "").strip()
    if url:
        return url
    HOME.mkdir(parents=True, exist_ok=True)
    return f"sqlite:///{HOME / 'jobagent.db'}"


def master_key() -> str | None:
    return os.environ.get("JOBAGENT_MASTER_KEY") or None


def ui_token() -> str | None:
    return os.environ.get("JOBAGENT_UI_TOKEN") or None


def chromium_path() -> str | None:
    p = os.environ.get("JOBAGENT_CHROMIUM")
    if p:
        return p
    return "/opt/pw-browsers/chromium" if os.path.exists("/opt/pw-browsers/chromium") else None


def https_proxy() -> str | None:
    return os.environ.get("HTTPS_PROXY") or os.environ.get("https_proxy")
