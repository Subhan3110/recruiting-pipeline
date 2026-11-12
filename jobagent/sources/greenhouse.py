"""Greenhouse Job Board API adapter.

Verified live on 2026-11-12 against boards-api.greenhouse.io:
  GET /v1/boards/{board}/jobs?content=true          listing with descriptions, no auth, no pagination
  GET /v1/boards/{board}/jobs/{id}?questions=true   full application form schema (fields, types, options)
The API's POST application endpoint needs the *employer's* API key, so candidates
cannot use it. Submission therefore goes through the hosted form at
job-boards.greenhouse.io with browser automation. No candidate account exists on
Greenhouse-hosted forms; some employers require an e-mailed security code.
"""
from __future__ import annotations

from ..vacancies import clean_text
from .base import Capabilities, SourceAdapter

API = "https://boards-api.greenhouse.io/v1/boards"


class GreenhouseAdapter(SourceAdapter):
    name = "greenhouse"
    capabilities = Capabilities(
        discover=True, describe=True, detect_closed=True, locate_form=True, authenticate=False, prepare=True,
        submit=True, verify_outcome=True,
        notes="No account needed. Submit via hosted form (browser). Outcome: confirmation page and/or acknowledgement e-mail. "
              "Invisible reCAPTCHA may block headless submission; that surfaces as a user action.")
    portal_origin = "https://job-boards.greenhouse.io"
    allowed_hosts = ("job-boards.greenhouse.io", "boards.greenhouse.io", "job-boards.eu.greenhouse.io")
    mail_domains = ("greenhouse.io", "greenhouse-mail.io", "us.greenhouse-mail.io", "eu.greenhouse-mail.io")

    def boards(self) -> list[str]:
        return list(self.settings.get("boards", []))

    def discover(self, boards: list[str] | None = None):
        for board in boards or self.boards():
            data = self.get_json(f"{API}/{board}/jobs", content="true")
            if not data:
                continue
            for job in data.get("jobs", []):
                yield self._to_vacancy(board, job)

    def listing_ids(self, board: str) -> set[str] | None:
        data = self.get_json(f"{API}/{board}/jobs")
        return None if data is None else {f"{board}/{job['id']}" for job in data.get("jobs", [])}

    def _to_vacancy(self, board: str, job: dict) -> dict:
        offices = [o.get("location") or o.get("name") for o in job.get("offices") or [] if (o.get("location") or o.get("name"))]
        loc = (job.get("location") or {}).get("name") or ""
        return {
            "source": self.name, "source_id": f"{board}/{job['id']}", "employer_name": job.get("company_name") or board,
            "employer_source_key": f"greenhouse:{board}", "title": (job.get("title") or "").strip(),
            "url": f"{self.portal_origin}/{board}/jobs/{job['id']}", "description": job.get("content") or "",
            "location_raw": loc, "locations": [], "language": job.get("language"),
            "deadline": job.get("application_deadline"),
            "raw": {"absolute_url": job.get("absolute_url"), "updated_at": job.get("updated_at"), "offices": offices,
                    "first_published": job.get("first_published"), "requisition_id": job.get("requisition_id"),
                    "departments": [d.get("name") for d in job.get("departments") or []]},
        }

    def is_open(self, source_id: str) -> bool | None:
        board, job_id = source_id.split("/", 1)
        try:
            return self.get_json(f"{API}/{board}/jobs/{job_id}") is not None
        except Exception:  # noqa: BLE001 - network trouble is "unknown", not "closed"
            return None

    def form(self, source_id: str) -> dict | None:
        board, job_id = source_id.split("/", 1)
        job = self.get_json(f"{API}/{board}/jobs/{job_id}", questions="true")
        if not job:
            return None
        questions = []
        for group, required_default in ((job.get("questions") or [], None), (job.get("location_questions") or [], None)):
            for q in group:
                questions.append({
                    "label": clean_text(q.get("label")), "description": clean_text(q.get("description")),
                    "required": bool(q.get("required")),
                    "fields": [{"name": f["name"], "type": f["type"],
                                "options": [{"label": str(v.get("label")), "value": v.get("value")} for v in f.get("values") or []]}
                               for f in q.get("fields") or []],
                })
        demographic = []
        for q in (job.get("demographic_questions") or {}).get("questions") or []:
            demographic.append({"id": q.get("id"), "label": clean_text(q.get("label")), "required": bool(q.get("required")),
                                "options": [{"label": str(o.get("label")), "value": o.get("id")} for o in q.get("answer_options") or []]})
        return {"url": f"{self.portal_origin}/{board}/jobs/{job_id}", "questions": questions, "demographic": demographic,
                "compliance": job.get("compliance") or [], "tenant": board}

    def submitter(self):
        from ..browser.greenhouse_form import GreenhouseFormSubmitter

        return GreenhouseFormSubmitter(allowed_hosts=self.allowed_hosts)
