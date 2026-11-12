"""Structured, redacted logging. Secrets, codes, tokens and e-mail bodies must never reach logs."""
import json
import logging
import re
import sys

_PATTERNS = [
    (re.compile(r"(?i)bearer\s+[A-Za-z0-9._~+/=-]+"), "Bearer [REDACTED]"),
    (re.compile(r"(?i)\b(password|passwd|pwd|secret|token|api[_-]?key|authorization|cookie|code|otp)\b(\"?\s*[:=]\s*\"?)([^\s\",;&]+)"), r"\1\2[REDACTED]"),
    (re.compile(r"postgres(?:ql)?://[^\s@]+@"), "postgresql://[REDACTED]@"),
    (re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}"), "[EMAIL]"),
    (re.compile(r"(?<!\d)\+?\d[\d\s-]{8,14}\d(?!\d)"), "[NUMBER]"),
    (re.compile(r"\bgAAAA[A-Za-z0-9_-]{20,}={0,2}"), "[CIPHERTEXT]"),
]
SENSITIVE_KEYS = re.compile(r"(?i)password|secret|token|cookie|code|otp|credential|body|authorization")


def redact(text: str) -> str:
    for pat, repl in _PATTERNS:
        text = pat.sub(repl, text)
    return text


def redact_obj(obj):
    if isinstance(obj, dict):
        return {k: "[REDACTED]" if SENSITIVE_KEYS.search(str(k)) else redact_obj(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [redact_obj(v) for v in obj]
    if isinstance(obj, str):
        return redact(obj)
    return obj


class JsonFormatter(logging.Formatter):
    def format(self, record):
        payload = {"ts": self.formatTime(record), "level": record.levelname, "logger": record.name,
                   "msg": redact(record.getMessage())}
        extra = getattr(record, "data", None)
        if extra:
            payload["data"] = redact_obj(extra)
        if record.exc_info:
            payload["exc"] = redact(self.formatException(record.exc_info))[-2000:]
        return json.dumps(payload, ensure_ascii=False, default=str)


def setup(level=logging.INFO):
    h = logging.StreamHandler(sys.stderr)
    h.setFormatter(JsonFormatter())
    root = logging.getLogger()
    root.handlers[:] = [h]
    root.setLevel(level)


def log(name: str = "jobagent") -> logging.Logger:
    return logging.getLogger(name)
