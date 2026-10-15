"""The workflow: intake -> checks -> extract -> dedupe -> record -> notify, with run tracking."""
import hashlib
import logging
import time
from datetime import timedelta

from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError

from . import config, extract, notify, sor
from .db import DocKey, Run, SessionLocal, utcnow
from .retry import PermanentError, call_with_retry, is_transient

log = logging.getLogger("intake.pipeline")

ACTIVE_OR_DONE = {"received", "processing", "success", "needs_review", "partial"}


class DailyLimitReached(Exception):
    pass


def sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _release_claims(session, run_id: str):
    for claim in session.scalars(select(DocKey).where(DocKey.run_id == run_id)):
        session.delete(claim)


def intake(data: bytes, filename: str, channel: str, replay_of: str | None = None) -> tuple[Run, bool]:
    """Register a document. Returns (run, should_process). Exact duplicates are answered immediately,
    without spending an LLM call."""
    digest = sha256(data)
    file_key = f"file:{digest}"
    filename = (filename or "document.pdf").replace("\x00", "")[:255]
    with SessionLocal() as s:
        since = utcnow() - timedelta(days=1)
        today_count = s.scalar(select(func.count()).select_from(Run).where(
            Run.created_at >= since, Run.status != "duplicate"))
        if today_count >= config.MAX_RUNS_PER_DAY:
            raise DailyLimitReached(f"Daily limit of {config.MAX_RUNS_PER_DAY} runs reached")

        claim = s.get(DocKey, file_key)
        if claim:
            original = s.get(Run, claim.run_id)
            if original and original.status in ACTIVE_OR_DONE:
                dup = Run(channel=channel, filename=filename, file_sha256=digest, status="duplicate",
                          duplicate_of=original.id, record_id=original.record_id,
                          record_url=original.record_url, fields=original.fields, duration_ms=0)
                s.add(dup)
                s.commit()
                return dup, False
            s.delete(claim)  # original failed -> allow a clean retry
            s.flush()

        run = Run(channel=channel, filename=filename[:255], file_sha256=digest, pdf=data, replay_of=replay_of)
        s.add(run)
        s.flush()
        s.add(DocKey(key=file_key, kind="file", run_id=run.id))
        try:
            s.commit()
        except IntegrityError:  # identical file arrived at the same instant
            s.rollback()
            claim = s.get(DocKey, file_key)
            dup = Run(channel=channel, filename=filename, file_sha256=digest, status="duplicate",
                      duplicate_of=claim.run_id if claim else None, duration_ms=0)
            s.add(dup)
            s.commit()
            return dup, False
        return run, True


def process(run_id: str) -> None:
    started = time.monotonic()
    attempts = [0]
    with SessionLocal() as s:
        run = s.get(Run, run_id)
        if run is None or run.status not in ("received",):
            return
        run.status = "processing"
        s.commit()
        data = run.pdf
        try:
            extract.check_pdf(data)
            raw = call_with_retry(extract.llm_extract, data, step="extract", counter=attempts)
            fields, missing = extract.normalize(raw)
            run.fields, run.missing = fields, missing
            if extract.is_empty_non_order(raw, fields):
                raise extract.MalformedDocument("Not a DME order: no patient, provider or equipment found")
            status = "needs_review" if missing else "success"

            # Content-level duplicate: same order re-sent as a different file (re-scan, re-fax).
            ckey = extract.content_key(fields)
            if ckey:
                existing = s.get(DocKey, ckey)
                original = s.get(Run, existing.run_id) if existing else None
                if original and original.id != run.id and original.status in ACTIVE_OR_DONE:
                    run.status, run.duplicate_of = "duplicate", original.id
                    run.record_id, run.record_url = original.record_id, original.record_url
                    # point this file's claim at the original, so re-sending this exact file is free
                    file_claim = s.get(DocKey, f"file:{run.file_sha256}")
                    if file_claim:
                        file_claim.run_id = original.id
                    s.commit()
                    return
                if existing:
                    s.delete(existing)
                    s.flush()
                s.add(DocKey(key=ckey, kind="content", run_id=run.id))
                s.commit()

            run.record_id, run.record_url = call_with_retry(
                sor.create_record, run, fields, status, missing, step="record", counter=attempts)
            run.status = status
            s.commit()

            try:
                notify.send_order(run, fields, status, missing, counter=attempts)
            except Exception as exc:  # record exists; flag the partial failure instead of losing it
                run.status, run.error = "partial", f"Record created but team notification failed: {exc}"
                s.commit()
                notify.send_alert(run, "Order saved but team notification failed", str(exc))
        except extract.MalformedDocument as exc:
            run.status, run.error = "failed", f"Malformed document: {exc}"
            _release_claims(s, run.id)
            s.commit()
            notify.send_alert(run, "Inbound document rejected (malformed)", str(exc))
        except Exception as exc:
            kind = "after retries" if is_transient(exc) else "not retryable"
            run.status, run.error = "failed", f"{type(exc).__name__} ({kind}): {exc}"
            _release_claims(s, run.id)
            s.commit()
            log.exception("run %s failed", run.id)
            notify.send_alert(run, "Intake run failed", run.error)
        finally:
            run.attempts = attempts[0]
            run.duration_ms = int((time.monotonic() - started) * 1000)
            s.commit()


def replay(run_id: str) -> Run:
    """Manually re-run a failed/partial run from the stored original document."""
    with SessionLocal() as s:
        old = s.get(Run, run_id)
        if old is None:
            raise KeyError(run_id)
        if old.status not in ("failed", "partial"):
            raise ValueError(f"Only failed or partial runs can be replayed (this one is {old.status})")
        data, filename, channel = old.pdf, old.filename, old.channel
        if data is None:
            raise ValueError("Original document is no longer stored")
        _release_claims(s, old.id)
        s.commit()
    run, _ = intake(data, filename, f"replay:{channel}"[:32], replay_of=run_id)
    return run


def sweep_stuck_runs() -> int:
    """Monitoring: a run stuck in received/processing (e.g. the server restarted mid-run) is failed + alerted."""
    cutoff = utcnow() - timedelta(minutes=config.STUCK_RUN_MINUTES)
    count = 0
    with SessionLocal() as s:
        stuck = s.scalars(select(Run).where(Run.status.in_(["received", "processing"]), Run.updated_at < cutoff)).all()
        for run in stuck:
            run.status, run.error = "failed", f"Timed out: stuck for over {config.STUCK_RUN_MINUTES} minutes"
            _release_claims(s, run.id)
            count += 1
        s.commit()
        for run in stuck:
            notify.send_alert(run, "Intake run timed out", run.error)
    return count
