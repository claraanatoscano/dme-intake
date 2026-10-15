#!/usr/bin/env bash
# Live test against the deployed service.
# Usage: BASE=https://your-app.onrender.com KEY=your-intake-key ./scripts/smoke_test.sh path/to/sample.pdf
set -euo pipefail
PDF="${1:?pass a PDF path}"
echo "== T1 new document (expect 202 accepted)"
curl -s -X POST "$BASE/webhook/orders" -H "X-API-Key: $KEY" -F "file=@$PDF"; echo
sleep 25
echo "== T2 same document again (expect 200 duplicate)"
curl -s -X POST "$BASE/webhook/orders" -H "X-API-Key: $KEY" -F "file=@$PDF"; echo
echo "== T3 malformed document (expect 202, then run status failed + Slack alert)"
echo "not a pdf" > /tmp/malformed.pdf
curl -s -X POST "$BASE/webhook/orders" -H "X-API-Key: $KEY" -F "file=@/tmp/malformed.pdf"; echo
echo "== T4 no API key (expect 401)"
curl -s -o /dev/null -w "%{http_code}\n" -X POST "$BASE/webhook/orders" -F "file=@$PDF"
echo "== T5 health"
curl -s "$BASE/health"; echo
echo "Now open $BASE/status"
