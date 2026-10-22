#!/usr/bin/env bats
# cspell:ignore runpy

setup() {
  root=$(git -C "$BATS_TEST_DIRNAME" rev-parse --show-toplevel)
  helper="$root/infra/ansible/roles/temporal_ui/molecule/default/files/deployment.py"
}

@test "listener check accepts loopback and pre-existing system listeners" {
  run python3 - "$helper" <<'PY'
import runpy, sys
check = runpy.run_path(sys.argv[1])['check_listeners']
check(['*:22'], ['*:22', '127.0.0.1:7233', '127.0.0.1:8080'], [7233, 8080])
PY
  [ "$status" -eq 0 ]
}

@test "listener check refuses wildcard IPv4 IPv6 and DNS-stub exposure" {
  run python3 - "$helper" <<'PY'
import runpy, sys
check = runpy.run_path(sys.argv[1])['check_listeners']
for bad in ['0.0.0.0:7233', '[::]:7233', '192.0.2.1:7233', '127.0.0.53:53', '127.0.0.2:7233']:
    try:
        check([], ['127.0.0.1:7233', bad], [7233])
    except AssertionError:
        continue
    raise AssertionError(bad)
PY
  [ "$status" -eq 0 ]
}

@test "listener check refuses a missing actual frontend" {
  run python3 - "$helper" <<'PY'
import runpy, sys
check = runpy.run_path(sys.argv[1])['check_listeners']
check([], ['127.0.0.1:8080'], [7233, 8080])
PY
  [ "$status" -ne 0 ]
}
