#!/usr/bin/env bash
# Molecule only. Record commands without contacting a tailnet.
set -euo pipefail
[[ ${1:-} == serve ]] || exit 2
printf '%s\n' "$*" >>/opt/temporal-playbook-test/tailscale.calls
