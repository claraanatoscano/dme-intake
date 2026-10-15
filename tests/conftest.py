import io
import os
import tempfile

# Configure before the app is imported
_tmp = tempfile.mkdtemp()
os.environ["DATABASE_URL"] = f"sqlite:///{_tmp}/test.db"
os.environ["INTAKE_API_KEY"] = "test-key"
os.environ["ANTHROPIC_API_KEY"] = "dummy"
os.environ["ORDERS_WEBHOOK_URL"] = "https://hooks.slack.com/services/TEST/orders"
os.environ["ALERTS_WEBHOOK_URL"] = "https://hooks.slack.com/services/TEST/alerts"
os.environ["PUBLIC_BASE_URL"] = "https://intake.example.com"
os.environ["RETRY_ATTEMPTS"] = "3"

import pytest  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402
from pypdf import PdfWriter  # noqa: E402

from app import db, extract, notify, sor  # noqa: E402
from app import retry as retry_mod  # noqa: E402
from app.main import app  # noqa: E402

GOOD = {
    "is_dme_order": True, "patient_first_name": "Maria", "patient_last_name": "Lopez",
    "patient_dob": "03/14/1968", "ordering_provider_name": "Jordan Reyes, MD",
    "equipment_requested": "E0601 CPAP device; A7030 full face mask", "hcpcs_codes": ["E0601", "a7030"],
    "date_of_service": "2026-10-01", "notes": None,
    "patient_dob_as_written": "03/14/1968", "date_of_service_as_written": "10/01/2026",
}


def make_pdf(pages: int = 1, marker: str = "") -> bytes:
    w = PdfWriter()
    for _ in range(pages):
        w.add_blank_page(width=612, height=792)
    if marker:
        w.add_metadata({"/Subject": marker})  # different bytes, same "content"
    buf = io.BytesIO()
    w.write(buf)
    return buf.getvalue()


class Fakes:
    """Records every outbound call so tests can assert on side effects."""

    def __init__(self):
        self.extract_result = dict(GOOD)
        self.extract_calls = 0
        self.extract_failures = 0      # transient failures before success
        self.records = []
        self.record_failures = 0
        self.messages = []             # (url, text)
        self.notify_fail = False

    def llm_extract(self, data):
        self.extract_calls += 1
        if self.extract_failures:
            self.extract_failures -= 1
            raise TimeoutError("simulated model timeout")
        return dict(self.extract_result)

    def create_record(self, run, fields, status, missing):
        if self.record_failures:
            self.record_failures -= 1
            raise ConnectionError("simulated 503 from system of record")
        rid = f"rec{len(self.records) + 1}"
        self.records.append({"run": run.id, "fields": fields, "status": status, "missing": missing})
        return rid, f"https://airtable.com/app/tbl/{rid}"

    def post(self, url, text):
        if self.notify_fail and "orders" in url:
            raise retry_mod.PermanentError("simulated Slack 404")
        self.messages.append((url, text))


@pytest.fixture()
def fakes(monkeypatch):
    db.Base.metadata.drop_all(db.engine)
    db.Base.metadata.create_all(db.engine)
    f = Fakes()
    monkeypatch.setattr(extract, "llm_extract", f.llm_extract)
    monkeypatch.setattr(sor, "create_record", f.create_record)
    monkeypatch.setattr(notify, "_post", f.post)
    # no real sleeping between retries in tests
    monkeypatch.setattr(retry_mod, "wait_exponential_jitter", lambda **kw: (lambda state: 0))
    return f


@pytest.fixture()
def client(fakes):
    with TestClient(app) as c:
        yield c


AUTH = {"X-API-Key": "test-key"}
