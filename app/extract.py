"""PDF checks + LLM field extraction + validation/normalization."""
import base64
import io
import json
import re
from datetime import date, datetime

from pypdf import PdfReader

from . import config
from .retry import PermanentError

REQUIRED = [
    "patient_first_name",
    "patient_last_name",
    "patient_dob",
    "ordering_provider_name",
    "equipment_requested",
    "date_of_service",
]


class MalformedDocument(PermanentError):
    """The file is not a usable PDF order. Retrying will not help."""


def check_pdf(data: bytes) -> int:
    """Cheap checks before spending money on the LLM. Returns page count."""
    if not data:
        raise MalformedDocument("Empty file")
    if len(data) > config.MAX_PDF_MB * 1024 * 1024:
        raise MalformedDocument(f"File larger than {config.MAX_PDF_MB:g} MB")
    if not data.lstrip()[:5].startswith(b"%PDF"):
        raise MalformedDocument("Not a PDF (missing %PDF header)")
    try:
        pages = len(PdfReader(io.BytesIO(data)).pages)
    except Exception as exc:  # corrupt / truncated / encrypted
        raise MalformedDocument(f"PDF could not be opened: {type(exc).__name__}") from exc
    if pages == 0:
        raise MalformedDocument("PDF has no pages")
    if pages > config.MAX_PAGES:
        raise MalformedDocument(f"PDF has {pages} pages; limit is {config.MAX_PAGES}")
    return pages


TOOL = {
    "name": "record_dme_order",
    "description": "Record the fields extracted from a DME order document.",
    "input_schema": {
        "type": "object",
        "properties": {
            "is_dme_order": {"type": "boolean", "description": "True only if this document is a medical/DME order or referral."},
            "patient_first_name": {"type": ["string", "null"]},
            "patient_last_name": {"type": ["string", "null"]},
            "patient_dob": {"type": ["string", "null"], "description": "YYYY-MM-DD"},
            "ordering_provider_name": {"type": ["string", "null"], "description": "Ordering/prescribing practitioner, with credential if shown (e.g. 'Jane Doe, MD')"},
            "equipment_requested": {"type": ["string", "null"], "description": "Short list of items, with HCPCS codes if shown, separated by '; '"},
            "hcpcs_codes": {"type": "array", "items": {"type": "string"}},
            "date_of_service": {"type": ["string", "null"], "description": "YYYY-MM-DD"},
            "patient_dob_as_written": {"type": ["string", "null"], "description": "The DOB exactly as printed, character for character"},
            "date_of_service_as_written": {"type": ["string", "null"], "description": "The date used for date_of_service exactly as printed, even if it is not a real date"},
            "notes": {"type": ["string", "null"], "description": "Anything ambiguous, illegible, or assumed"},
        },
        "required": ["is_dme_order", *REQUIRED, "patient_dob_as_written", "date_of_service_as_written", "hcpcs_codes", "notes"],
    },
}

PROMPT = """You are extracting data from an inbound Durable Medical Equipment (DME) order document for an intake team.

Rules:
- Use ONLY what is written in the document. If a field is missing, illegible, or ambiguous, return null. Never guess.
- Dates must be YYYY-MM-DD. Convert formats like 03/14/1968.
- Whenever you return a date, you MUST also copy it exactly as printed into its *_as_written field. Never correct an impossible date (e.g. 2/30/24); copy it as written and return null for the YYYY-MM-DD field.
- date_of_service: use the date explicitly labeled as date of service / DOS / delivery date. If none exists, use the order date and say so in notes. If neither exists, null.
- ordering_provider_name: the practitioner who ordered/signed, not the supplier.
- equipment_requested: concise, e.g. "E0601 CPAP device; A7030 full face mask".
- If the document is not a DME/medical order at all, set is_dme_order=false.
Call the record_dme_order tool exactly once."""


def llm_extract(data: bytes) -> dict:
    """Send the PDF to Claude and force a structured tool call."""
    import anthropic

    if not config.ANTHROPIC_API_KEY:
        raise PermanentError("ANTHROPIC_API_KEY is not configured")
    client = anthropic.Anthropic(api_key=config.ANTHROPIC_API_KEY, max_retries=0, timeout=90)
    msg = client.messages.create(
        model=config.ANTHROPIC_MODEL,
        max_tokens=1024,
        tools=[TOOL],
        tool_choice={"type": "auto"},  # newer models reject forced tool choice; the prompt requires the call
        messages=[{
            "role": "user",
            "content": [
                {"type": "document", "source": {"type": "base64", "media_type": "application/pdf",
                                                "data": base64.b64encode(data).decode()}},
                {"type": "text", "text": PROMPT},
            ],
        }],
    )
    for block in msg.content:
        if getattr(block, "type", None) == "tool_use":
            return dict(block.input)
    # Fallback: the model answered in text. Accept a JSON object if it contains one.
    text = "".join(getattr(b, "text", "") for b in msg.content)
    match = re.search(r"\{.*\}", text, re.S)
    if match:
        try:
            return json.loads(match.group(0))
        except ValueError:
            pass
    raise RuntimeError("Model returned no structured output")


_DATE_FORMATS = ["%Y-%m-%d", "%m/%d/%Y", "%m/%d/%y", "%m-%d-%Y", "%m-%d-%y", "%m.%d.%Y", "%B %d, %Y", "%b %d, %Y", "%d %B %Y"]


def _to_date(value) -> date | None:
    if not value or not isinstance(value, str):
        return None
    s = value.strip()
    for fmt in _DATE_FORMATS:
        try:
            return datetime.strptime(s, fmt).date()
        except ValueError:
            continue
    return None


def _codes(value) -> list[str]:
    if isinstance(value, str):
        value = re.split(r"[,;\s]+", value)
    if not isinstance(value, list):
        return []
    return [c.strip().upper() for c in value if isinstance(c, str) and c.strip()]


def is_empty_non_order(raw: dict, fields: dict) -> bool:
    """Blank pages, invoices, random PDFs: nothing usable at all -> reject, don't create a record."""
    found = any(fields.get(k) for k in ("patient_first_name", "patient_last_name", "ordering_provider_name",
                                         "equipment_requested"))
    return raw.get("is_dme_order") is False and not found


def _clean(value) -> str | None:
    if value is None:
        return None
    s = re.sub(r"\s+", " ", str(value)).strip()
    return s or None


def pdf_text(data: bytes) -> str:
    """Text layer of the PDF ('' for scanned faxes). Used to double-check dates the model returns."""
    try:
        return " ".join((p.extract_text() or "") for p in PdfReader(io.BytesIO(data)).pages)
    except Exception:
        return ""


def _date_in_text(d: date, text: str) -> bool:
    for m in re.finditer(r"\b(\d{1,2})[/.-](\d{1,2})[/.-](\d{2}|\d{4})\b", text):
        mo, dy, yr = (int(x) for x in m.groups())
        yr = yr + 2000 if yr < 100 and yr <= (date.today().year % 100) + 1 else (yr + 1900 if yr < 100 else yr)
        if (yr, mo, dy) == (d.year, d.month, d.day):
            return True
    return d.isoformat() in text or d.strftime("%B %d, %Y").replace(" 0", " ") in text or d.strftime("%B %d, %Y") in text


def normalize(raw: dict, today: date | None = None, text: str = "") -> tuple[dict, list[str]]:
    """Clean the model output and list what is missing or implausible."""
    today = today or date.today()
    problems: list[str] = []
    out = {
        "patient_first_name": _clean(raw.get("patient_first_name")),
        "patient_last_name": _clean(raw.get("patient_last_name")),
        "ordering_provider_name": _clean(raw.get("ordering_provider_name")),
        "equipment_requested": _clean(raw.get("equipment_requested")),
        "hcpcs_codes": _codes(raw.get("hcpcs_codes")),
        "notes": _clean(raw.get("notes")),
        "patient_dob": None,
        "date_of_service": None,
    }

    # Guard against the model "fixing" an impossible printed date (e.g. 2/30/24 -> 2024-03-01).
    for key, label in (("patient_dob", "patient_dob"), ("date_of_service", "date_of_service")):
        written = _clean(raw.get(f"{key}_as_written"))
        if written and _to_date(written) is None and re.search(r"\d", written):
            problems.append(f"{label} (invalid as written: '{written}')")
            raw = {**raw, key: None}
        elif written and _to_date(written) and raw.get(key) and _to_date(raw.get(key)) != _to_date(written):
            problems.append(f"{label} (model value differs from document: '{written}')")
            raw = {**raw, key: None}
        elif not written and _to_date(raw.get(key)) and not _date_in_text(_to_date(raw.get(key)), text):
            # The model gave a date but did not quote it, and the PDF text can't confirm it (e.g. a scanned
            # fax printed "2/30/24" that the model silently turned into 2024-03-01). Keep it, but flag it.
            problems.append(f"{label} (unconfirmed: model did not quote the printed date)")

    dob = _to_date(raw.get("patient_dob"))
    if dob and (dob > today or (today.year - dob.year) > 120):
        problems.append(f"patient_dob (implausible: {dob.isoformat()})")
        dob = None
    out["patient_dob"] = dob.isoformat() if dob else None
    out["patient_dob_as_written"] = _clean(raw.get("patient_dob_as_written"))
    out["date_of_service_as_written"] = _clean(raw.get("date_of_service_as_written"))

    dos = _to_date(raw.get("date_of_service"))
    if dos and (dos.year < 2000 or (dos - today).days > 366):
        problems.append("date_of_service (implausible)")
        dos = None
    out["date_of_service"] = dos.isoformat() if dos else None

    if raw.get("is_dme_order") is False:
        problems.insert(0, "not a DME order")
    for f in REQUIRED:
        if not out.get(f) and not any(p.startswith(f) for p in problems):
            problems.append(f)
    return out, problems


def content_key(fields: dict) -> str | None:
    """Same patient + DOB + date of service + equipment = same order, even if the file bytes differ."""
    parts = [fields.get("patient_last_name"),
             fields.get("patient_dob") or fields.get("patient_dob_as_written"),
             fields.get("date_of_service") or fields.get("date_of_service_as_written"),
             fields.get("equipment_requested")]
    if not all(parts):
        return None
    norm = [re.sub(r"[^a-z0-9]", "", p.lower()) for p in parts]
    return "content:" + "|".join(norm)[:280]


_real_llm_extract = llm_extract  # used by the SDK contract test
