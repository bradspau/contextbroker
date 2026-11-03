#!/usr/bin/env bash
# Check that Fuseki refuses an unauthenticated SPARQL update.
#
# Usage: scripts/check_fuseki_auth.sh [DATASET_URL]
#   DATASET_URL defaults to http://127.0.0.1:3030/network-dataset
#
# Sends one no-credential no-op update (a DELETE WHERE on a subject that never
# exists, so a server that wrongly accepts it still changes nothing) and prints
# exactly one fixed line. It never sends credentials and never prints a
# response body.
#
# Exit codes:
#   0  refused (HTTP 401 or 403)
#   1  ACCEPTED (any 2xx): the dataset is writable without a credential
#   2  anything else, including a connection failure (code 000)
set -eu

DATASET_URL="${1:-http://127.0.0.1:3030/network-dataset}"
UPDATE='DELETE WHERE { <urn:contextbroker:auth-check> ?p ?o }'

CODE=$(curl -s -o /dev/null -w '%{http_code}' --max-time 10 --noproxy '*' \
  -X POST -H 'Content-Type: application/sparql-update' \
  --data "$UPDATE" "${DATASET_URL%/}/update" 2>/dev/null) || true
case "$CODE" in
  [0-9][0-9][0-9]) ;;
  *) CODE=000 ;;
esac

case "$CODE" in
  401|403)
    echo "unauthenticated update: refused ($CODE)"
    exit 0
    ;;
  2??)
    echo "unauthenticated update: ACCEPTED ($CODE)"
    exit 1
    ;;
  *)
    echo "unauthenticated update: unexpected ($CODE)"
    exit 2
    ;;
esac
