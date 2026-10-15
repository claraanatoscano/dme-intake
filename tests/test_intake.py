"""End-to-end behaviour tests. External services (Claude, Airtable, Slack) are faked; everything else is real."""
import base64
from datetime import timedelta

from app import pipeline
from app.db import Run, SessionLocal, utcnow
from app.extract import normalize

from .conftest import AUTH, make_pdf


def post_pdf(client, data, name="order.pdf", channel=None, headers=AUTH):
    url = "/webhook/orders" + (f"?channel={channel}" if channel else "")
    return client.post(url, files={"file": (name, data, "application/pdf")}, headers=headers)


def get_run(client, run_id):
    return client.get(f"/runs/{run_id}", headers=AUTH).json()


# ---------- Happy path ----------
def test_happy_path_creates_record_and_notifies(client, fakes):
    r = post_pdf(client, make_pdf())
    assert r.status_code == 202
    run = get_run(client, r.json()["run_id"])
    assert run["status"] == "success"
    assert run["fields"]["patient_dob"] == "1968-03-14"            # normalized from 03/14/1968
    assert run["fields"]["hcpcs_codes"] == ["E0601", "A7030"]      # normalized to upper case
    assert len(fakes.records) == 1
    orders = [t for u, t in fakes.messages if "orders" in u]
    assert len(orders) == 1 and "Maria L." in orders[0] and "Lopez" not in orders[0]  # PHI minimised
    assert "airtable.com" in orders[0]                                # deeplink to record


# ---------- Channels ----------
def test_raw_pdf_body_and_json_base64_and_channel_label(client, fakes):
    r1 = client.post("/webhook/orders?filename=a.pdf", content=make_pdf(marker="a"),
                     headers={**AUTH, "content-type": "application/pdf"})
    fakes.extract_result["patient_last_name"] = "Other"
    r2 = client.post("/webhook/orders?channel=email",
                     json={"filename": "b.pdf", "content_base64": base64.b64encode(make_pdf(marker="b")).decode()},
                     headers=AUTH)
    assert r1.status_code == 202 and r2.status_code == 202
    assert get_run(client, r2.json()["run_id"])["channel"] == "email"
    assert len(fakes.records) == 2


# ---------- Security ----------
def test_rejects_missing_or_wrong_api_key(client, fakes):
    assert post_pdf(client, make_pdf(), headers={}).status_code == 401
    assert post_pdf(client, make_pdf(), headers={"X-API-Key": "nope"}).status_code == 401
    assert fakes.extract_calls == 0


def test_dashboard_masks_phi_unless_authorized(client, fakes):
    post_pdf(client, make_pdf())
    public = client.get("/status").text
    assert "Lopez" not in public and "1968-**-**" in public
    private = client.get("/status?key=test-key").text
    assert "Lopez" in private


# ---------- Idempotency ----------
def test_exact_duplicate_is_blocked_without_llm_call(client, fakes):
    pdf = make_pdf()
    first = post_pdf(client, pdf)
    second = post_pdf(client, pdf)
    assert second.status_code == 200
    body = second.json()
    assert body["status"] == "duplicate" and body["duplicate_of"] == first.json()["run_id"]
    assert body["record_url"]                      # points back to the original record
    assert len(fakes.records) == 1 and fakes.extract_calls == 1


def test_same_order_different_file_is_blocked(client, fakes):
    post_pdf(client, make_pdf(marker="scan-1"))
    r = post_pdf(client, make_pdf(marker="rescan-2"))
    run = get_run(client, r.json()["run_id"])
    assert run["status"] == "duplicate"
    assert len(fakes.records) == 1


def test_different_order_same_patient_is_not_a_duplicate(client, fakes):
    post_pdf(client, make_pdf(marker="1"))
    fakes.extract_result["equipment_requested"] = "E0562 heated humidifier"
    post_pdf(client, make_pdf(marker="2"))
    assert len(fakes.records) == 2


# ---------- Malformed input ----------
def test_non_pdf_fails_cleanly_and_alerts(client, fakes):
    r = post_pdf(client, b"this is not a pdf", name="note.txt")
    run = get_run(client, r.json()["run_id"])
    assert run["status"] == "failed" and "Malformed" in run["error"]
    assert fakes.extract_calls == 0 and not fakes.records       # no LLM spend, no record
    assert any("alerts" in u and "malformed" in t.lower() for u, t in fakes.messages)


def test_truncated_pdf_fails(client, fakes):
    r = post_pdf(client, make_pdf()[:120])
    assert get_run(client, r.json()["run_id"])["status"] == "failed"


def test_empty_upload_fails(client, fakes):
    r = post_pdf(client, b"")
    assert get_run(client, r.json()["run_id"])["status"] == "failed"


def test_too_many_pages_rejected_before_llm(client, fakes):
    r = post_pdf(client, make_pdf(pages=20))
    run = get_run(client, r.json()["run_id"])
    assert run["status"] == "failed" and "pages" in run["error"]
    assert fakes.extract_calls == 0


def test_missing_fields_creates_needs_review_record(client, fakes):
    fakes.extract_result.update(ordering_provider_name=None, date_of_service=None)
    r = post_pdf(client, make_pdf())
    run = get_run(client, r.json()["run_id"])
    assert run["status"] == "needs_review"
    assert set(run["missing"]) == {"ordering_provider_name", "date_of_service"}
    assert fakes.records[0]["status"] == "needs_review"
    assert any("needs review" in t.lower() for u, t in fakes.messages if "orders" in u)


def test_not_a_dme_order_needs_review(client, fakes):
    fakes.extract_result = {k: None for k in fakes.extract_result} | {"is_dme_order": False, "hcpcs_codes": []}
    r = post_pdf(client, make_pdf())
    run = get_run(client, r.json()["run_id"])
    assert run["status"] == "needs_review" and run["missing"][0] == "not a DME order"


# ---------- Retries & failure handling ----------
def test_transient_llm_failure_is_retried(client, fakes):
    fakes.extract_failures = 2
    r = post_pdf(client, make_pdf())
    run = get_run(client, r.json()["run_id"])
    assert run["status"] == "success" and fakes.extract_calls == 3


def test_record_write_retried_then_succeeds(client, fakes):
    fakes.record_failures = 1
    r = post_pdf(client, make_pdf())
    assert get_run(client, r.json()["run_id"])["status"] == "success"
    assert len(fakes.records) == 1


def test_exhausted_retries_fail_alert_and_allow_resend(client, fakes):
    fakes.record_failures = 10
    pdf = make_pdf()
    r = post_pdf(client, pdf)
    run = get_run(client, r.json()["run_id"])
    assert run["status"] == "failed" and "after retries" in run["error"]
    assert any("alerts" in u for u, _ in fakes.messages)
    fakes.record_failures = 0
    r2 = post_pdf(client, pdf)                     # same file again after a failure is NOT a duplicate
    assert r2.status_code == 202
    assert get_run(client, r2.json()["run_id"])["status"] == "success"


def test_notification_failure_marks_partial_and_alerts(client, fakes):
    fakes.notify_fail = True
    r = post_pdf(client, make_pdf())
    run = get_run(client, r.json()["run_id"])
    assert run["status"] == "partial" and len(fakes.records) == 1
    assert any("alerts" in u for u, _ in fakes.messages)


def test_manual_replay_of_failed_run(client, fakes):
    fakes.record_failures = 10
    r = post_pdf(client, make_pdf())
    failed_id = r.json()["run_id"]
    fakes.record_failures = 0
    rep = client.post(f"/runs/{failed_id}/replay", headers=AUTH)
    assert rep.status_code == 200
    new = get_run(client, rep.json()["new_run_id"])
    assert new["status"] == "success" and new["replay_of"] == failed_id
    assert client.post(f"/runs/{new['run_id']}/replay", headers=AUTH).status_code == 409  # can't replay success


def test_stuck_run_is_failed_and_alerted(client, fakes):
    with SessionLocal() as s:
        run = Run(file_sha256="x" * 64, status="processing")
        s.add(run)
        s.commit()
        run.updated_at = utcnow() - timedelta(hours=1)
        s.commit()
    assert pipeline.sweep_stuck_runs() == 1
    assert any("timed out" in t.lower() for u, t in fakes.messages)


# ---------- Dashboard / health ----------
def test_health_turns_failing_after_failed_run(client, fakes):
    assert client.get("/health").json()["automation"] == "Waiting for first order"
    post_pdf(client, make_pdf(marker="ok"))
    assert client.get("/health").json()["automation"] == "Healthy"
    post_pdf(client, b"junk")
    assert client.get("/health").json()["automation"] == "Failing"
    assert "Failing" in client.get("/status").text


# ---------- Unit: normalization ----------
def test_normalize_rejects_implausible_dates():
    from datetime import date
    fields, missing = normalize({"is_dme_order": True, "patient_first_name": "A", "patient_last_name": "B",
                                 "patient_dob": "2090-01-01", "ordering_provider_name": "X",
                                 "equipment_requested": "E0601", "date_of_service": "13/45/2026"},
                                today=date(2026, 10, 15))
    assert fields["patient_dob"] is None and "patient_dob (implausible)" in missing
    assert "date_of_service" in missing
