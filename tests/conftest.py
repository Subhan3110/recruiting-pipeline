import os

import pytest
from cryptography.fernet import Fernet

os.environ.setdefault("JOBAGENT_MASTER_KEY", Fernet.generate_key().decode())

from jobagent import answers, db, policy, profile  # noqa: E402
from jobagent.sources.base import Capabilities, SourceAdapter  # noqa: E402

SRC = "fixture profile (synthetic)"


def _url(tmp_path):
    base = os.environ.get("JOBAGENT_TEST_PG")
    if base:  # run the whole suite against PostgreSQL: JOBAGENT_TEST_PG=postgresql://user@host/db
        import psycopg, uuid
        schema = "t_" + uuid.uuid4().hex[:10]
        with psycopg.connect(base, autocommit=True) as c:
            c.execute(f"CREATE SCHEMA {schema}")
        return f"{base}{'&' if '?' in base else '?'}options=-csearch_path%3D{schema}"
    return f"sqlite:///{tmp_path / 'test.db'}"


@pytest.fixture
def url(tmp_path):
    return _url(tmp_path)


@pytest.fixture
def conn(url):
    c = db.connect(url)
    db.migrate(c)
    policy.current(c)
    yield c
    c.close()


def seed_profile(c, confirmed=True, work_auth=("NL", "NO")):
    st = "confirmed" if confirmed else "unconfirmed"
    f = lambda k, v, s=st: profile.set_fact(c, k, v, s, SRC)  # noqa: E731
    f("identity/name", {"first": "Test", "last": "Candidate", "full": "Test Candidate"})
    f("contact/email", "test.candidate@example.com")
    f("contact/phone", "+47 40000000")
    f("contact/phone_country", "Norway")
    f("contact/location", {"city": "Kristiansand", "country": "Norway"})
    f("education/uni", {"institution": "Example Business School", "degree": "Bachelor in Finance", "level": "Bachelor's",
                        "field": "Finance", "graduation": "2027-06", "gpa_text": "4.1/5.0"})
    f("employment/acme", {"employer": "Acme Audit AS", "title": "AML Assistant", "start": "2025-09", "end": None,
                          "bullets": ["Screened transactions against sanctions lists"]})
    f("languages/en", {"language": "English", "level": "fluent"})
    f("languages/no", {"language": "Norwegian", "level": "native"})
    f("skills/excel", {"name": "Excel", "level": "advanced"})
    f("stories/aml", {"tags": ["compliance", "aml"], "text": "At Acme Audit I screen transactions against sanctions lists."})
    f("history/prior_applications_complete", True)
    for cc in work_auth:
        f(f"work_auth/{cc}", {"authorised": True, "needs_sponsorship": False, "basis": "citizen"})
    answers.set_answer(c, "heard_from", "Company website", source=SRC)


class FakeAdapter(SourceAdapter):
    """Source adapter with scripted behaviour; submission goes to the local fixture form."""
    name = "greenhouse"
    capabilities = Capabilities(discover=True, submit=True)
    portal_origin = "https://job-boards.greenhouse.io"
    allowed_hosts = ("127.0.0.1",)
    mail_domains = ("greenhouse-mail.io",)

    def __init__(self, form_url=None, submitter=None, open_=True, questions=None):
        super().__init__()
        self.form_url, self._submitter, self.open_, self.questions = form_url, submitter, open_, questions

    def is_open(self, source_id):
        return self.open_

    def form(self, source_id):
        q = lambda label, name, typ="input_text", req=True, opts=(): {  # noqa: E731
            "label": label, "description": "", "required": req,
            "fields": [{"name": name, "type": typ, "options": [{"label": o, "value": i + 11} for i, o in enumerate(opts)]}]}
        qs = self.questions or [
            q("First Name", "first_name"), q("Last Name", "last_name"), q("Email", "email"), q("Phone", "phone"),
            {"label": "Resume/CV", "description": "", "required": True, "fields": [{"name": "resume", "type": "input_file", "options": []},
                                                                                 {"name": "resume_text", "type": "textarea", "options": []}]},
            {"label": "Cover Letter", "description": "", "required": False, "fields": [{"name": "cover_letter", "type": "input_file", "options": []}]},
            q("Are you legally authorized to work in the Netherlands?", "question_1", "multi_value_single_select", True, ("Yes", "No")),
            q("Will you now or in the future require visa sponsorship?", "question_2", "multi_value_single_select", True, ("Yes", "No")),
            q("How did you hear about this position?", "question_3", "multi_value_single_select", True, ("Company website", "LinkedIn", "Other")),
        ]
        return {"url": self.form_url or "http://127.0.0.1:1/ok/x", "questions": qs, "demographic": [], "tenant": source_id.split("/")[0]}

    def submitter(self):
        return self._submitter


def add_vacancy(c, title="Compliance Analyst Intern", location="Amsterdam", employer="Fixture Bank", sid="fixturebank/1",
                description="Join our compliance team. AML, KYC and transaction monitoring. Bachelor degree in finance."):
    from jobagent import vacancies
    vid, _ = vacancies.upsert(c, {"source": "greenhouse", "source_id": sid, "employer_name": employer,
                                  "employer_source_key": f"greenhouse:{sid.split('/')[0]}", "title": title, "url": "https://x/y",
                                  "description": description, "location_raw": location, "language": "en"})
    return vid
