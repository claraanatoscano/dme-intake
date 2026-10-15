"""Team notifications (Slack or Discord incoming webhooks) and failure alerts."""
import logging

import httpx

from . import config
from .retry import PermanentError, call_with_retry

log = logging.getLogger("intake.notify")


def _post(url: str, text: str):
    if not url:
        raise PermanentError("Notification webhook URL is not configured")
    payload = {"content": text[:1900]} if "discord.com" in url else {"text": text}
    resp = httpx.post(url, json=payload, timeout=15)
    if resp.status_code == 429 or resp.status_code >= 500:
        resp.raise_for_status()
    if resp.status_code >= 400:
        raise PermanentError(f"Notification rejected ({resp.status_code}): {resp.text[:200]}")


def mask_name(first: str | None, last: str | None) -> str:
    """Minimum necessary PHI in chat: first name + last initial."""
    if not first and not last:
        return "Unknown patient"
    return f"{first or ''} {(last or '?')[0]}.".strip()


def order_message(run, fields: dict, status: str, missing: list[str]) -> str:
    head = ":warning: *New DME order needs review*" if status == "needs_review" else ":inbox_tray: *New DME order*"
    lines = [
        head,
        f"Patient: {mask_name(fields.get('patient_first_name'), fields.get('patient_last_name'))}",
        f"Equipment: {fields.get('equipment_requested') or '—'}",
        f"Ordering provider: {fields.get('ordering_provider_name') or '—'}",
        f"Date of service: {fields.get('date_of_service') or '—'}",
        f"Source: {run.channel} · {run.filename}",
    ]
    if missing:
        lines.append(f"Needs review: {', '.join(missing)}")
    lines.append(f"<{run.record_url}|Open record>  ·  <{config.PUBLIC_BASE_URL}/status#run-{run.id}|Run details>")
    return "\n".join(lines)


def send_order(run, fields: dict, status: str, missing: list[str], counter: list | None = None):
    call_with_retry(_post, config.ORDERS_WEBHOOK_URL, order_message(run, fields, status, missing),
                    step="notify", counter=counter)


def send_alert(run, title: str, detail: str):
    """Never raises: an alerting failure must not crash the pipeline."""
    text = (f":rotating_light: *{title}*\nRun `{run.id}` · {run.channel} · {run.filename}\n"
            f"Error: {detail[:500]}\n<{config.PUBLIC_BASE_URL}/status#run-{run.id}|Open dashboard>  ·  "
            f"Replay: `POST {config.PUBLIC_BASE_URL}/runs/{run.id}/replay` with X-API-Key")
    try:
        call_with_retry(_post, config.ALERTS_WEBHOOK_URL, text, step="alert")
    except Exception as exc:  # last resort: logs (visible in Render logs)
        log.error("ALERT DELIVERY FAILED for run %s: %s | original: %s", run.id, exc, detail)
