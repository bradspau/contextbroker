#!/usr/bin/env bash
# Run before: docker compose --profile tmf921 up -d --build tmf921-server
# Verifies the licensed TIO ontology directory (bind-mounted read-only) is present.
set -eu
dir="${TIO_ONTOLOGY_DIR:-../BBF_921_java/ontology}"
if [ ! -d "$dir" ]; then
  echo "ERROR: TIO_ONTOLOGY_DIR '$dir' does not exist. Point it at the licensed TIO ontology directory." >&2
  exit 1
fi
if ! find "$dir" -maxdepth 1 -name '*.ttl' -print -quit | grep -q .; then
  echo "ERROR: TIO_ONTOLOGY_DIR '$dir' contains no *.ttl files; the TMF921 server needs them at start-up." >&2
  exit 1
fi
echo "OK: ontology directory '$dir' present"
