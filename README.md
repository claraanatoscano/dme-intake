# DME Order Intake Automation

Turns inbound DME order PDFs into structured, routed work items: extract fields, create a record, notify the team, alert on failure, and show health on a status page.

**Live links**
- Inbound webhook: `POST https://<APP>.onrender.com/webhook/orders` (header `X-API-Key: <KEY>`)
- Email channel: send a PDF attachment to 5qj4zlbvholqtlr6fho7hhseh32bsg1w@hook.us2.make.com
- Status dashboard: `https://<APP>.onrender.com/status`
- System of record (read-only): `<AIRTABLE_SHARED_VIEW_LINK>`

## How to test it

```bash
# 1. New order (multipart upload)
curl -X POST "https://<APP>.onrender.com/webhook/orders" -H "X-API-Key: <KEY>" -F "file=@order.pdf"

# 2. Same file again -> 200 "duplicate", no new record
curl -X POST "https://<APP>.onrender.com/webhook/orders" -H "X-API-Key: <KEY>" -F "file=@order.pdf"

# 3. By URL (a Google Drive share link works) or base64 JSON
curl -X POST "https://<APP>.onrender.com/webhook/orders" -H "X-API-Key: <KEY>" \
     -H "Content-Type: application/json" -d '{"file_url": "https://drive.google.com/file/d/<ID>/view"}'
```
Each call returns a `run_id` and a `status_url`. Results show on the dashboard within ~20 seconds.

## Architecture

```
 Email (Make Mailhook) ──┐
                         ├──► POST /webhook/orders  (FastAPI on Render)
 Direct webhook ─────────┘        │ 1. auth (API key), size/type limits, daily cap
                                  │ 2. SHA-256 file key claimed in Postgres  ──► exact duplicate? return 200 + original record
                                  ▼ 3. 202 Accepted, processing continues in background
                        PDF checks (is it a PDF? pages ≤ 40?)  ──► malformed? fail + alert, no LLM spend
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
 UptimeRobot (every 5 min) ──► /health; emails if the service is down
 Postgres (Neon) ──► run history, idempotency keys, original PDF for replay ──► /status dashboard
```

Every outbound call (Claude, Airtable, Slack) goes through one retry policy: up to 4 attempts, exponential backoff with jitter, only for transient errors (timeouts, 429, 5xx). Permanent errors (bad input, 4xx) fail fast.

| Run status | Meaning |
|---|---|
| Success | Record created and team notified |
| Needs review | Record created, but a required field is missing, implausible, or the document is not a DME order |
| Duplicate | Same file, or same order in a different file; no new record, points to the original |
| Partial | Record created but the Slack notification failed (alerted) |
| Failed | Malformed document or failure after retries (alerted, replayable) |

## Why the work is split this way

- **Custom Python service (core)**: the requirements that matter most for trust (idempotency with atomic claims, retries with backoff, run history, a public status page, replay) are awkward and costly to do in no-code (operations per step, limited state). In code they are ~700 lines, testable (37 automated tests), and portable.
- **Make (email channel)**: receiving email is commodity plumbing. A Make Mailhook gives an inbox in minutes and forwards attachments to the same webhook, so every channel shares one pipeline and one set of guarantees. Swapping Make for Zapier, Postmark or SendGrid inbound changes nothing downstream.
- **Claude for extraction**: handles unseen layouts and scanned PDFs without templates. Forced tool calling gives structured output; validation catches what the model gets wrong.
- **Airtable (system of record)**: readable by non-engineers, easy read-only sharing, stable record deeplinks.
- **Slack**: separate channels for work (`#dme-orders`) and on-call (`#dme-alerts`).

## Considerations I prioritized
- **Idempotency**: file hash checked before any LLM spend; content key catches re-scans; record writes look up the Order ID first so a retry after a timeout can't create a second row.
- **Failure visibility**: every run is persisted with status, attempts, duration and error; failures alert with a one-line replay command.
- **PHI**: minimum necessary in chat (first name + last initial); dashboard masks names and DOB unless opened with the key; no PHI in logs; secrets only in environment variables.
- **LLM cost controls**: duplicates and malformed files never reach the model; page/size limits; daily run cap; `max_tokens` capped.
- **Webhook security**: API key compared in constant time; HTTPS only; file size limits.

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
pytest -q                     # 37 tests: happy path, both channel formats, auth, PHI masking, exact + content duplicates,
                              # malformed/empty/truncated/oversized PDFs, needs-review, retries, exhausted retries,
                              # partial failure, replay, stuck-run sweeper, health states
uvicorn app.main:app --reload
```

## How I used AI
I used Claude to help plan the architecture and draft the code and tests. I chose the tools and trade-offs, reviewed the code, ran the test suite, and tested the live deployment end to end (results in the email).
