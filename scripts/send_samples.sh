#!/usr/bin/env bash
# POST the sample Redox/FHIR payloads to a running ingestion endpoint,
# simulating a Redox destination delivering an ADT stream.
#
#   ./scripts/send_samples.sh [BASE_URL]
#
# BASE_URL defaults to http://localhost:8000. If REDOX_VERIFICATION_TOKEN is
# set, it is sent as the verification-token header.
set -euo pipefail
cd "$(dirname "$0")/.."

BASE="${1:-http://localhost:8000}"
URL="$BASE/api/ingest/redox"
HDR=(-H "Content-Type: application/json")
[[ -n "${REDOX_VERIFICATION_TOKEN:-}" ]] && HDR+=(-H "verification-token: $REDOX_VERIFICATION_TOKEN")

for f in \
  samples/redox_01_arrival.json \
  samples/redox_02_admit.json \
  samples/redox_03_transfer.json \
  samples/fhir_admit_bundle.json \
  samples/redox_04_discharge.json \
  samples/fhir_discharge_bundle.json
do
  echo "→ POST $f"
  curl -sS "${HDR[@]}" --data-binary "@$f" "$URL"; echo
  sleep 0.3
done
echo "Done. Open $BASE to view the dashboard."
