#!/usr/bin/env bash
# Check that the query-only Fuseki user can read the network graph and cannot change it.
#
# Usage: FUSEKI_READER_PASSWORD=... scripts/check_fuseki_reader.sh [DATASET_URL]
#   DATASET_URL defaults to http://127.0.0.1:3030/network-dataset
#
# Sends one no-op update (a DELETE WHERE on a subject that never exists, so a server that wrongly
# accepts it still changes nothing) and one ASK query, both as the reader user, and prints exactly
# two fixed lines. The password comes from the environment only, goes to curl on its standard input
# (never on a command line), and is never printed. No response body is printed.
#
# Exit codes:
#   0  the update was refused (HTTP 401 or 403) and the query was answered (HTTP 200)
#   1  the update was ACCEPTED (any 2xx): the query-only user can change the graph
#   2  anything else, including a connection failure (code 000) or no FUSEKI_READER_PASSWORD
set -eu

DATASET_URL="${1:-http://127.0.0.1:3030/network-dataset}"
DATASET_URL="${DATASET_URL%/}"

if [ -z "${FUSEKI_READER_PASSWORD:-}" ]; then
  echo "reader update: unexpected (FUSEKI_READER_PASSWORD is not set)"
  exit 2
fi

UPDATE='DELETE WHERE { <urn:contextbroker:auth-check> ?p ?o }'
QUERY='ASK { ?s ?p ?o }'

# The credential reaches curl as a config file on stdin, so it is not in the process list.
status() {
  local code
  code=$(printf 'user = "reader:%s"\n' "$FUSEKI_READER_PASSWORD" \
    | curl -s -o /dev/null -w '%{http_code}' --max-time 10 --noproxy '*' -K - "$@" 2>/dev/null) || true
  case "$code" in
    [0-9][0-9][0-9]) echo "$code" ;;
    *) echo 000 ;;
  esac
}

UPDATE_CODE=$(status -X POST -H 'Content-Type: application/sparql-update' \
  --data "$UPDATE" "$DATASET_URL/update")
QUERY_CODE=$(status -G --data-urlencode "query=$QUERY" "$DATASET_URL/query")

result=0
case "$UPDATE_CODE" in
  401|403) echo "reader update: refused ($UPDATE_CODE)" ;;
  2??)     echo "reader update: ACCEPTED ($UPDATE_CODE)"; result=1 ;;
  *)       echo "reader update: unexpected ($UPDATE_CODE)"; result=2 ;;
esac
case "$QUERY_CODE" in
  200) echo "reader query: answered ($QUERY_CODE)" ;;
  *)   echo "reader query: unexpected ($QUERY_CODE)"; [ "$result" -eq 1 ] || result=2 ;;
esac
exit "$result"
