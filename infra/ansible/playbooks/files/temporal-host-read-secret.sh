#!/usr/bin/env bash
set -euo pipefail
OP_SERVICE_ACCOUNT_TOKEN=$(cat "$1")
export OP_SERVICE_ACCOUNT_TOKEN
scheme=op
exec op read "$scheme://$2/$3/$4"
