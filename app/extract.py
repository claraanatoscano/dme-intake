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
            "notes": {"type": ["string", "null"], "description": "Anything ambiguous, illegible, or assumed"},
        },
        "required": ["is_dme_order", *REQUIRED, "hcpcs_codes", "notes"],
    },
}

PROMPT = """You are extracting data from an inbound Durable Medical Equipment (DME) order document for an intake team.

Rules:
- Use ONLY what is written in the document. If a field is missing, illegible, or ambiguous, return null. Never guess.
- Dates must be YYYY-MM-DD. Convert formats like 03/14/1968.
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


_DATE_FORMATS = ["%Y-%m-%d", "%m/%d/%Y", "%m/%d/%y", "%m-%d-%Y", "%B %d, %Y", "%b %d, %Y", "%d %B %Y"]


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


def _clean(value) -> str | None:
    if value is None:
        return None
    s = re.sub(r"\s+", " ", str(value)).strip()
    return s or None


def normalize(raw: dict, today: date | None = None) -> tuple[dict, list[str]]:
    """Clean the model output and list what is missing or implausible."""
    today = today or date.today()
    out = {
        "patient_first_name": _clean(raw.get("patient_first_name")),
        "patient_last_name": _clean(raw.get("patient_last_name")),
        "ordering_provider_name": _clean(raw.get("ordering_provider_name")),
        "equipment_requested": _clean(raw.get("equipment_requested")),
        "hcpcs_codes": [c.strip().upper() for c in (raw.get("hcpcs_codes") or []) if isinstance(c, str) and c.strip()],
        "notes": _clean(raw.get("notes")),
        "patient_dob": None,
        "date_of_service": None,
    }
    problems: list[str] = []

    dob = _to_date(raw.get("patient_dob"))
    if dob and (dob.year < 1900 or dob > today):
        problems.append("patient_dob (implausible)")
        dob = None
    out["patient_dob"] = dob.isoformat() if dob else None

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
    parts = [fields.get("patient_last_name"), fields.get("patient_dob"), fields.get("date_of_service"),
             fields.get("equipment_requested")]
    if not all(parts):
        return None
    norm = [re.sub(r"[^a-z0-9]", "", p.lower()) for p in parts]
    return "content:" + "|".join(norm)[:280]


_real_llm_extract = llm_extract  # used by the SDK contract test
