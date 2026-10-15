"""System of record adapters. Pick one with SOR_BACKEND=airtable|sheets."""
import json

import httpx

from . import config
from .retry import PermanentError

COLUMNS = [
    "Order ID", "Status", "Patient First Name", "Patient Last Name", "Patient DOB", "Ordering Provider",
    "Equipment Requested", "HCPCS Codes", "Date of Service", "Needs Review Reason", "Extraction Notes",
    "Source Channel", "Source File", "Received At", "Run Link",
]


def _row(run, fields: dict, status: str, missing: list[str]) -> dict:
    return {
        "Order ID": run.id,
        "Status": "Needs Review" if status == "needs_review" else "New",
        "Patient First Name": fields.get("patient_first_name") or "",
        "Patient Last Name": fields.get("patient_last_name") or "",
        "Patient DOB": fields.get("patient_dob") or "",
        "Ordering Provider": fields.get("ordering_provider_name") or "",
        "Equipment Requested": fields.get("equipment_requested") or "",
        "HCPCS Codes": ", ".join(fields.get("hcpcs_codes") or []),
        "Date of Service": fields.get("date_of_service") or "",
        "Needs Review Reason": ", ".join(missing),
        "Extraction Notes": fields.get("notes") or "",
        "Source Channel": run.channel,
        "Source File": run.filename,
        "Received At": run.created_at.isoformat(timespec="seconds"),
        "Run Link": f"{config.PUBLIC_BASE_URL}/status#run-{run.id}",
    }


def _raise_for_status(resp: httpx.Response):
    if resp.status_code >= 400:
        if resp.status_code == 429 or resp.status_code >= 500:
            resp.raise_for_status()  # transient -> retried
        raise PermanentError(f"System of record rejected the write ({resp.status_code}): {resp.text[:300]}")


# ---------------- Airtable ----------------
def _airtable_create(row: dict) -> tuple[str, str]:
    if not (config.AIRTABLE_TOKEN and config.AIRTABLE_BASE_ID and config.AIRTABLE_TABLE_ID):
        raise PermanentError("Airtable is not configured")
    url = f"https://api.airtable.com/v0/{config.AIRTABLE_BASE_ID}/{config.AIRTABLE_TABLE_ID}"
    resp = httpx.post(url, timeout=20,
                      headers={"Authorization": f"Bearer {config.AIRTABLE_TOKEN}"},
                      json={"records": [{"fields": row}], "typecast": True})
    _raise_for_status(resp)
    rec_id = resp.json()["records"][0]["id"]
    return rec_id, f"https://airtable.com/{config.AIRTABLE_BASE_ID}/{config.AIRTABLE_TABLE_ID}/{rec_id}"


def _airtable_find(order_id: str) -> tuple[str, str] | None:
    """Used before writing, so a retry after a timeout can't create a second record."""
    url = f"https://api.airtable.com/v0/{config.AIRTABLE_BASE_ID}/{config.AIRTABLE_TABLE_ID}"
    resp = httpx.get(url, timeout=20, headers={"Authorization": f"Bearer {config.AIRTABLE_TOKEN}"},
                     params={"filterByFormula": f"{{Order ID}}='{order_id}'", "maxRecords": 1})
    _raise_for_status(resp)
    recs = resp.json().get("records", [])
    if not recs:
        return None
    rec_id = recs[0]["id"]
    return rec_id, f"https://airtable.com/{config.AIRTABLE_BASE_ID}/{config.AIRTABLE_TABLE_ID}/{rec_id}"


# ---------------- Google Sheets ----------------
def _sheet():
    import gspread
    from google.oauth2.service_account import Credentials

    if not (config.GOOGLE_SERVICE_ACCOUNT_JSON and config.GOOGLE_SHEET_ID):
        raise PermanentError("Google Sheets is not configured")
    creds = Credentials.from_service_account_info(
        json.loads(config.GOOGLE_SERVICE_ACCOUNT_JSON),
        scopes=["https://www.googleapis.com/auth/spreadsheets"])
    ws = gspread.authorize(creds).open_by_key(config.GOOGLE_SHEET_ID).worksheet(config.GOOGLE_SHEET_TAB)
    if ws.row_values(1) != COLUMNS:
        ws.update(range_name="A1", values=[COLUMNS])
    return ws


def _sheets_link(ws, row_num: int) -> str:
    return f"https://docs.google.com/spreadsheets/d/{config.GOOGLE_SHEET_ID}/edit#gid={ws.id}&range=A{row_num}"


def _sheets_find(order_id: str):
    ws = _sheet()
    cell = ws.find(order_id, in_column=1)
    return (f"row{cell.row}", _sheets_link(ws, cell.row)) if cell else None


def _sheets_create(row: dict) -> tuple[str, str]:
    ws = _sheet()
    resp = ws.append_row([row[c] for c in COLUMNS], value_input_option="RAW")
    rng = resp["updates"]["updatedRange"]           # e.g. Orders!A7:O7
    row_num = int(rng.split("!")[1].split(":")[0][1:])
    return f"row{row_num}", _sheets_link(ws, row_num)


# ---------------- public API ----------------
def create_record(run, fields: dict, status: str, missing: list[str]) -> tuple[str, str]:
    """Idempotent create: look up by Order ID first, so retries never duplicate rows."""
    if config.SOR_BACKEND == "sheets":
        return _sheets_find(run.id) or _sheets_create(_row(run, fields, status, missing))
    return _airtable_find(run.id) or _airtable_create(_row(run, fields, status, missing))
