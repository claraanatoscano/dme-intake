"""Status page a non-engineer can read at a glance."""
import html
from datetime import timedelta
from zoneinfo import ZoneInfo

from . import config
from .db import utcnow

STATUS_STYLE = {
    "success": ("Success", "ok"),
    "needs_review": ("Needs review", "warn"),
    "duplicate": ("Duplicate (skipped)", "muted"),
    "partial": ("Saved, alert failed", "warn"),
    "failed": ("Failed", "bad"),
    "rejected": ("Rejected: bad document", "muted"),
    "processing": ("Processing", "info"),
    "received": ("Queued", "info"),
}


def _e(v) -> str:
    return html.escape(str(v)) if v not in (None, "") else "—"


def _mask(fields: dict | None, reveal: bool) -> tuple[str, str]:
    f = fields or {}
    first, last, dob = f.get("patient_first_name"), f.get("patient_last_name"), f.get("patient_dob")
    if reveal:
        return f"{first or ''} {last or ''}".strip() or "—", dob or "—"
    name = f"{(first or '?')[0]}. {(last or '?')[0]}." if (first or last) else "—"
    return name, (f"{dob[:4]}-**-**" if dob else "—")


def _ago(dt) -> str:
    if dt is None:
        return "never"
    if dt.tzinfo is None:
        from datetime import timezone
        dt = dt.replace(tzinfo=timezone.utc)
    secs = int((utcnow() - dt).total_seconds())
    if secs < 90:
        return f"{secs}s ago"
    if secs < 5400:
        return f"{secs // 60} min ago"
    if secs < 172800:
        return f"{secs // 3600} h ago"
    return f"{secs // 86400} days ago"


def health(runs: list) -> tuple[str, str, str]:
    """Overall state from the last 24h of real (non-duplicate) runs."""
    from datetime import timezone

    def aware(dt):
        return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)

    day_ago = utcnow() - timedelta(days=1)
    recent = [r for r in runs if aware(r.created_at) >= day_ago and r.status != "duplicate"]
    finished = [r for r in runs if r.status not in ("duplicate", "received", "processing", "rejected")]
    rejected = [r for r in recent if r.status == "rejected"]
    note = f" {len(rejected)} malformed document(s) rejected and alerted." if rejected else ""
    if not finished:
        if rejected:
            return "Healthy", "ok", f"No valid orders processed yet.{note}"
        return "Waiting for first order", "info", "No documents processed yet."
    last = finished[0]
    failures = [r for r in recent if r.status in ("failed", "partial")]
    if last.status == "failed":
        return "Failing", "bad", f"The most recent run failed: {last.error or 'see details'}"
    if failures:
        return "Degraded", "warn", f"{len(failures)} failed run(s) in the last 24 h. The latest run succeeded.{note}"
    return "Healthy", "ok", f"No system failures in the last 24 h.{note}"


def render(runs: list, reveal: bool) -> str:
    tz = ZoneInfo(config.DASHBOARD_TZ)
    title, tone, detail = health(runs)
    real = [r for r in runs if r.status != "duplicate"]
    last_ok = next((r for r in runs if r.status in ("success", "needs_review")), None)
    counts = {k: sum(1 for r in runs if r.status == k) for k in STATUS_STYLE}

    rows = []
    for r in runs:
        label, cls = STATUS_STYLE.get(r.status, (r.status, "info"))
        name, dob = _mask(r.fields, reveal)
        f = r.fields or {}
        when = r.created_at if r.created_at.tzinfo else r.created_at.replace(tzinfo=ZoneInfo("UTC"))
        record = f'<a href="{html.escape(r.record_url)}" target="_blank">Open</a>' if r.record_url else "—"
        note = r.error or (f"Needs review: {', '.join(m.replace('_', ' ') for m in r.missing)}" if r.missing else "")
        if r.duplicate_of:
            note = f"Duplicate of run {r.duplicate_of}"
        rows.append(f"""<tr id="run-{r.id}">
<td><span class="pill {cls}">{_e(label)}</span></td>
<td>{when.astimezone(tz).strftime('%b %d, %I:%M:%S %p')}<div class="sub">{_ago(r.created_at)}</div></td>
<td>{_e(r.channel)}<div class="sub">{_e(r.filename)}</div></td>
<td>{_e(name)}<div class="sub" style="white-space:nowrap">DOB {_e(dob)}</div></td>
<td>{_e(f.get('equipment_requested'))}</td>
<td>{_e(f.get('ordering_provider_name'))}</td>
<td>{_e(f.get('date_of_service'))}</td>
<td>{f"{r.duration_ms/1000:.1f}s" if r.duration_ms else "—"}<div class="sub">{r.attempts or 0} call(s)</div></td>
<td>{record}</td>
<td class="note">{_e(note)}<div class="sub mono">{r.id}</div></td>
</tr>""")

    table = "\n".join(rows) or '<tr><td colspan="10" class="empty">No runs yet. Send a PDF to the webhook to start.</td></tr>'
    phi_note = ("Full patient details shown (authorized view)." if reveal
                else "Patient names and birth dates are masked on this public page.")
    return f"""<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1"><meta http-equiv="refresh" content="30">
<title>DME Intake Status</title>
<style>
:root{{--bg:#f6f7f9;--card:#fff;--ink:#14213d;--sub:#5b6475;--line:#e3e7ee;--ok:#1a7f4b;--okbg:#e3f5ea;--warn:#8a5a00;--warnbg:#fff3d6;--bad:#b42318;--badbg:#fde7e5;--info:#1d4ed8;--infobg:#e6eeff;--muted:#5b6475;--mutedbg:#eef0f3}}
@media (prefers-color-scheme:dark){{:root{{--bg:#0f141c;--card:#171e29;--ink:#e8ecf3;--sub:#9aa4b5;--line:#273142;--okbg:#123524;--ok:#6fdc9f;--warnbg:#3a2c0b;--warn:#ffcf66;--badbg:#3d1714;--bad:#ff8a80;--infobg:#16244a;--info:#8fb0ff;--mutedbg:#222b38;--muted:#a3adbd}}}}
*{{box-sizing:border-box}}body{{margin:0;font:14px/1.45 -apple-system,Segoe UI,Roboto,Helvetica,Arial,sans-serif;background:var(--bg);color:var(--ink)}}
.wrap{{max-width:1240px;margin:0 auto;padding:24px 16px}}h1{{font-size:20px;margin:0 0 4px}}.lede{{color:var(--sub);margin:0 0 20px}}
.hero{{display:flex;gap:16px;flex-wrap:wrap;margin-bottom:20px}}.card{{background:var(--card);border:1px solid var(--line);border-radius:10px;padding:16px}}
.state{{flex:2 1 320px}}.state .big{{font-size:26px;font-weight:700}}.stat{{flex:1 1 140px}}.stat .n{{font-size:24px;font-weight:700}}.stat .l{{color:var(--sub);font-size:12px}}
.ok .big,.big.ok{{color:var(--ok)}}.big.warn{{color:var(--warn)}}.big.bad{{color:var(--bad)}}.big.info{{color:var(--info)}}
.tablewrap{{overflow-x:auto;background:var(--card);border:1px solid var(--line);border-radius:10px}}
table{{border-collapse:collapse;width:100%;min-width:1000px}}th,td{{text-align:left;padding:10px 12px;border-bottom:1px solid var(--line);vertical-align:top}}
th{{font-size:12px;color:var(--sub);font-weight:600;background:var(--card);position:sticky;top:0}}
.sub{{color:var(--sub);font-size:12px}}.mono{{font-family:ui-monospace,Menlo,monospace}}.note{{max-width:260px}}
.pill{{display:inline-block;padding:2px 8px;border-radius:999px;font-size:12px;font-weight:600;white-space:nowrap}}
.pill.ok{{background:var(--okbg);color:var(--ok)}}.pill.warn{{background:var(--warnbg);color:var(--warn)}}.pill.bad{{background:var(--badbg);color:var(--bad)}}.pill.info{{background:var(--infobg);color:var(--info)}}.pill.muted{{background:var(--mutedbg);color:var(--muted)}}
tr:target{{background:var(--infobg)}}a{{color:var(--info)}}.empty{{text-align:center;color:var(--sub);padding:32px}}
footer{{color:var(--sub);font-size:12px;margin-top:16px}}
</style></head><body><div class="wrap">
<h1>DME Order Intake: Status</h1>
<p class="lede">Every inbound order document and what happened to it. Refreshes every 30 seconds.</p>
<div class="hero">
 <div class="card state"><div class="l sub">Automation health</div><div class="big {tone}">{_e(title)}</div><div>{_e(detail)}</div>
 <div class="sub" style="margin-top:6px">Last successful order: {_ago(last_ok.created_at) if last_ok else 'never'}</div></div>
 <div class="card stat"><div class="n">{len(real)}</div><div class="l">Documents processed (last 100)</div></div>
 <div class="card stat"><div class="n">{counts['success']}</div><div class="l">Succeeded</div></div>
 <div class="card stat"><div class="n">{counts['needs_review']}</div><div class="l">Need human review</div></div>
 <div class="card stat"><div class="n">{counts['failed'] + counts['partial']}</div><div class="l">System failures</div></div>
 <div class="card stat"><div class="n">{counts['rejected']}</div><div class="l">Bad documents rejected</div></div>
 <div class="card stat"><div class="n">{counts['duplicate']}</div><div class="l">Duplicates blocked</div></div>
</div>
<div class="tablewrap"><table><thead><tr><th>Status</th><th>Received ({config.DASHBOARD_TZ.split('/')[-1].replace('_',' ')})</th><th>Channel / file</th>
<th>Patient</th><th>Equipment</th><th>Ordering provider</th><th>Date of service</th><th>Duration</th><th>Record</th><th>Notes / run ID</th></tr></thead>
<tbody>{table}</tbody></table></div>
<footer>{phi_note} Failures alert the on-call channel automatically. JSON: <a href="/api/runs">/api/runs</a> · Health check: <a href="/health">/health</a></footer>
</div></body></html>"""
