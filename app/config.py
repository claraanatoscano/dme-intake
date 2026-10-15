"""All configuration comes from environment variables (secrets never live in code)."""
import os


def _env(name: str, default: str = "") -> str:
    return os.getenv(name, default).strip()


# --- Security ---
INTAKE_API_KEY = _env("INTAKE_API_KEY")          # required to call the webhook / replay
DASHBOARD_KEY = _env("DASHBOARD_KEY") or INTAKE_API_KEY  # unmasks PHI on the dashboard

# --- Extraction (LLM) ---
ANTHROPIC_API_KEY = _env("ANTHROPIC_API_KEY")
ANTHROPIC_MODEL = _env("ANTHROPIC_MODEL", "claude-sonnet-5-5")

# --- System of record: "airtable" or "sheets" ---
SOR_BACKEND = _env("SOR_BACKEND", "airtable").lower()
AIRTABLE_TOKEN = _env("AIRTABLE_TOKEN")
AIRTABLE_BASE_ID = _env("AIRTABLE_BASE_ID")      # app...
AIRTABLE_TABLE_ID = _env("AIRTABLE_TABLE_ID")    # tbl...
GOOGLE_SERVICE_ACCOUNT_JSON = _env("GOOGLE_SERVICE_ACCOUNT_JSON")
GOOGLE_SHEET_ID = _env("GOOGLE_SHEET_ID")
GOOGLE_SHEET_TAB = _env("GOOGLE_SHEET_TAB", "Orders")

# --- Notifications ---
ORDERS_WEBHOOK_URL = _env("ORDERS_WEBHOOK_URL")              # Slack or Discord incoming webhook
ALERTS_WEBHOOK_URL = _env("ALERTS_WEBHOOK_URL") or ORDERS_WEBHOOK_URL

# --- Storage / runtime ---
DATABASE_URL = _env("DATABASE_URL", "sqlite:///./local.db")
PUBLIC_BASE_URL = _env("PUBLIC_BASE_URL", "http://localhost:8000").rstrip("/")
DASHBOARD_TZ = _env("DASHBOARD_TZ", "America/Chicago")

# --- Guardrails / cost controls ---
MAX_PDF_MB = float(_env("MAX_PDF_MB", "25"))
MAX_PAGES = int(_env("MAX_PAGES", "40"))
MAX_RUNS_PER_DAY = int(_env("MAX_RUNS_PER_DAY", "300"))
STUCK_RUN_MINUTES = int(_env("STUCK_RUN_MINUTES", "10"))
RETRY_ATTEMPTS = int(_env("RETRY_ATTEMPTS", "4"))
