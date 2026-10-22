#!/usr/bin/env bash
# Certificates live only in the user unit's runtime directory.
set -euo pipefail
umask 077
OP_SERVICE_ACCOUNT_TOKEN=$(cat "$TEMPORAL_UI_TOKEN_FILE")
export OP_SERVICE_ACCOUNT_TOKEN
for field in ca certificate private-key; do
  # Build the reference from private inventory selectors; no literal reference.
  printf -v reference '%s//%s/%s/%s' 'op:' "$TEMPORAL_UI_CERTIFICATE_VAULT" "$TEMPORAL_UI_CERTIFICATE_ITEM" "$field"
  op read "$reference" >"$RUNTIME_DIRECTORY/$field.pem"
done
