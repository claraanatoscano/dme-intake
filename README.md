# DME Order Intake Automation

Turns inbound DME order PDFs into structured, routed work items: extract fields, create a record, notify the team, alert on failure, and show health on a status page.

**Live links**
- Inbound webhook: `POST https://dme-intake.onrender.com/webhook/orders` (header `X-API-Key`, key sent separately by email; open the URL in a browser for usage help)
- Email channel: send a PDF attachment to `5qj4zlbvholqtlr6fho7hhseh32bsg1w@hook.us2.make.com`
- Status dashboard: https://dme-intake.onrender.com/status (add `?key=<KEY>` to unmask patient names and DOB)
- System of record (read-only Airtable): https://airtable.com/invite/l?inviteId=inve8BhIO2hXaOUFG&inviteToken=ae226ce4341fd3a6db91c0e1e62acf7be344acadbba9eeb6a0248796f265e3c6 (read-only invite link; sign in to Airtable to open)
- Code: https://github.com/claraanatoscano/dme-intake

## How to test it

```bash
# 1. New order (multipart upload)
curl -X POST "https://dme-intake.onrender.com/webhook/orders" -H "X-API-Key: <KEY>" -F "file=@order.pdf"

# 2. Same file again -> 200 "duplicate", no new record
curl -X POST "https://dme-intake.onrender.com/webhook/orders" -H "X-API-Key: <KEY>" -F "file=@order.pdf"

# 3. By URL (a Google Drive share link works) or base64 JSON
curl -X POST "https://dme-intake.onrender.com/webhook/orders" -H "X-API-Key: <KEY>" \
     -H "Content-Type: application/json" -d '{"file_url": "https://drive.google.com/file/d/<ID>/view"}'
```
Each call returns a `run_id` and a `status_url`. Results show on the dashboard within ~20-40 seconds (scanned multi-page faxes take longer). A malformed file (e.g. a .txt renamed to .pdf, a corrupt or blank PDF) is rejected, shown as "Rejected: bad document" on the dashboard (the health banner stays green, because the system worked), and alerted in Slack `#dme-alerts`. Real system failures (e.g. the AI or Airtable down after retries) turn the banner red and can be replayed:

```bash
curl -X POST "https://dme-intake.onrender.com/runs/<run_id>/replay" -H "X-API-Key: <KEY>"
```

## Architecture

```
 Email (Make Mailhook) ──┐
                         ├──► POST /webhook/orders  (FastAPI on Render)
 Direct webhook ─────────┘        │ 1. auth (API key), size/type limits, daily cap
                                  │ 2. SHA-256 file key claimed in Postgres  ──► exact duplicate? return 200 + original record
                                  ▼ 3. 202 Accepted, processing continues in background
                        PDF checks (is it a PDF? pages ≤ 40?)  ──► malformed? rejected + alert, no LLM spend
                                  ▼
                        Claude (PDF input, forced tool call = structured JSON, "null, don't guess")
                                  ▼
                        Normalize + validate (dates, plausibility, required fields)
                                  ▼
                        Content key (last name | DOB | DOS | equipment) ──► re-scan of same order? duplicate
                                  ▼
                        Airtable record (idempotent: look up Order ID before create)  ── status New / Needs Review
                                  ▼
                        Slack #dme-orders: summary + deeplink to record
 Any failure after retries ──► run marked failed, Slack #dme-alerts alert with replay command
 Sweeper (every 2 min) ──► runs stuck > 10 min are failed + alerted
 UptimeRobot (every 5 min) ──► /health; emails if the service is down (also keeps the free instance warm)
 Postgres (Neon) ──► run history, idempotency keys, original PDF for replay ──► /status dashboard
```

Every outbound call (Claude, Airtable, Slack) goes through one retry policy: up to 4 attempts, exponential backoff with jitter, only for transient errors (timeouts, 429, 5xx). Permanent errors (bad input, 4xx) fail fast.

| Run status | Meaning |
|---|---|
| Success | Record created and team notified |
| Needs review | Record created, but a required field is missing, implausible, or the document is not a DME order |
| Duplicate | Same file, or same order in a different file; no new record, points to the original |
| Partial | Record created but the Slack notification failed (alerted) |
| Rejected | Bad input: not a PDF, corrupt, encrypted, blank, too many pages, or not an order at all (alerted; no record) |
| Failed | System failure after retries, or a run stuck over 10 minutes (alerted, replayable; turns the health banner red) |

## Why the work is split this way

- **Custom Python service (core)**: the requirements that matter most for trust (idempotency with atomic claims, retries with backoff, run history, a public status page, replay) are awkward and costly to do in no-code (operations per step, limited state). In code they are ~700 lines, testable (40 automated tests), and portable.
- **Make (email channel)**: Mailhook → Iterator (one bundle per attachment) → filter "PDFs only" → HTTP POST to the webhook, with a Retry error handler (3 attempts, 1 min apart) and incomplete executions stored for manual replay. Receiving email is commodity plumbing. A Make Mailhook gives an inbox in minutes and forwards attachments to the same webhook, so every channel shares one pipeline and one set of guarantees. Swapping Make for Zapier, Postmark or SendGrid inbound changes nothing downstream.
- **Claude for extraction**: handles unseen layouts and scanned PDFs without templates. Forced tool calling gives structured output; validation catches what the model gets wrong.
- **Airtable (system of record)**: readable by non-engineers, easy read-only sharing, stable record deeplinks.
- **Slack**: separate channels for work (`#dme-orders`) and on-call (`#dme-alerts`).

## Considerations I prioritized
- **Idempotency**: file hash checked before any LLM spend; content key catches re-scans; record writes look up the Order ID first so a retry after a timeout can't create a second row.
- **Bad input vs. broken system**: malformed documents are "Rejected" (alerted, no record) and don't make the dashboard look broken; only real system failures do.
- **Failure visibility**: every run is persisted with status, attempts, duration and error; failures alert with a one-line replay command.
- **PHI**: minimum necessary in chat (first name + last initial); dashboard masks names and DOB unless opened with the key; no PHI in logs; secrets only in environment variables.
- **LLM cost controls**: duplicates and malformed files never reach the model; page/size limits; daily run cap; `max_tokens` capped.
- **Webhook security**: API key compared in constant time; HTTPS only; file size limits.

## What testing the live system taught me
- **The monitoring caught real failures.** My first two live runs failed (an SDK upgrade removed a parameter; the newest model rejects forced tool choice). Both were alerted in Slack with the run ID, fixed, and recovered with the replay endpoint without re-sending the document. I added a contract test so an SDK change can't silently break extraction again.
- **The sample fax has traps.** It is dated "2/30/24" (not a real date) and the DOB is 12/05/1900. The model "helpfully" corrected the date to 2024-03-01, which is a guess. The model now also returns each date exactly as printed; impossible or mismatched dates, and DOBs over 120 years ago, are routed to Needs Review instead of being trusted. On the sample, the model still returned 2024-03-01 without quoting the printed date, so there is now a second guard: if the model gives a date but doesn't quote it, and the PDF's text layer can't confirm it (scanned faxes have none), the date is flagged "unconfirmed" and the order goes to Needs Review.

## What I'd do differently with more time
- Move processing from in-process background tasks to a durable queue (e.g. a Postgres-backed job table or SQS) with automatic delayed retries, instead of "fail + alert + manual replay".
- Signed webhooks (HMAC) per client, and per-client API keys.
- Confidence scoring and field-level evidence (page/quote) shown to reviewers; a review UI to correct and approve.
- Store PDFs in encrypted object storage with a retention policy instead of the database; BAAs with every vendor before real PHI.
- Alert de-duplication and an escalation path (PagerDuty) so a burst of failures pages once.
- Multi-tenant config (per-client sheet/base, Slack channel, field mapping) to onboard a new client without code changes.

## What breaks first at 100x volume
1. **In-process background work on one free instance**: long LLM calls hold workers; a restart loses in-flight runs (the sweeper catches them, but they need replay). Fix: queue + separate workers, autoscaling.
2. **Rate limits**: Airtable (5 req/s per base) and the LLM provider's rate limits. Fix: queue with concurrency limits and token-bucket throttling; batch Airtable writes.
3. **Storing PDFs in Postgres**: database size and cost grow fast. Fix: object storage.
4. **Alert noise**: one alert per failed run becomes a flood during an outage. Fix: grouping and thresholds.
5. **Make operations quota** on the email channel. Fix: provider-native inbound email to the webhook.

## Run locally / tests
```bash
pip install -r requirements.txt
pytest -q                     # 40 tests: happy path, both channel formats, auth, PHI masking, exact + content duplicates,
                              # malformed/empty/truncated/oversized PDFs, needs-review, retries, exhausted retries,
                              # partial failure, replay, stuck-run sweeper, health states
uvicorn app.main:app --reload
```

## How I used AI
Claude helped with the architecture and the tests.
