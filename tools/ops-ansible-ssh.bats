#!/usr/bin/env bats
# The ssh command line tools/ops-ansible.sh produces. A fake private
# inventory sets every connection variable the script must override, and
# a stub ssh records its arguments and exits as unreachable, so no run
# leaves this machine. The host address is from the documentation range.
#
# ssh keeps the first value it sees for an option, so each check reads
# the first occurrence. test-ansible runs this file, because it needs the
# uv ops group.

setup() {
  script="$BATS_TEST_DIRNAME/ops-ansible.sh"
  work=$(mktemp -d)
  inv=$work/private/ansible/inventory
  mkdir -p "$inv/group_vars" "$inv/host_vars"
  touch "$work/ro-key" "$work/ops-key.pub" "$work/inventory-key"
  cat >"$inv/host_vars/edge-1.yml" <<EOF
ansible_host: 192.0.2.10
EOF
  cat >"$inv/group_vars/all.yml" <<EOF
devhub_ssh_key_files:
  ops-ro: $work/ro-key
  ops: $work/ops-key.pub
ops_accounts_ro_keys: [ssh-ed25519 AAAA example-ops-ro]
ops_accounts_admin_keys: [ssh-ed25519 AAAA example-ops]
ansible_user: someone-else
ansible_connection: paramiko
ansible_ssh_private_key_file: $work/inventory-key
ansible_ssh_common_args: >-
  -o ControlMaster=auto -o ControlPersist=600 -o ControlPath=$work/cm
  -o IdentityAgent=$work/agent -o IdentitiesOnly=no
  -o StrictHostKeyChecking=no
EOF
  cat >"$work/ssh" <<EOF
#!/bin/sh
[ -f "$work/argv" ] || printf '%s\n' "\$@" >"$work/argv"
exit 255
EOF
  chmod +x "$work/ssh"
}

teardown() {
  rm -rf "$work"
}

# Runs the script, then prints the first value ssh received for $1.
first() {
  awk -v name="$1" 'prev == "-o" && index($0, name "=") == 1 {
    v = substr($0, length(name) + 2); gsub(/"/, "", v); print v; exit
  } { prev = $0 }' "$work/argv"
}

run_ops() {
  DEVHUB_PRIVATE_DIR=$work/private ANSIBLE_SSH_EXECUTABLE=$work/ssh \
    ANSIBLE_SSH_RETRIES=0 run bash "$script" "$@"
  [ -f "$work/argv" ]
}

@test "verify connects as ops-ro with its own key and no agent" {
  run_ops verify
  [ "$(first User)" = ops-ro ]
  [ "$(first IdentityFile)" = "$work/ro-key" ]
  [ "$(first IdentitiesOnly)" = yes ]
  [ "$(first IdentityAgent)" = none ]
  [ "$(first StrictHostKeyChecking)" = yes ]
  [ "$(first ControlPersist)" = 60s ]
}

@test "check connects as ops with its own key and no connection reuse" {
  run_ops check
  [ "$(first User)" = ops ]
  [ "$(first IdentityFile)" = "$work/ops-key.pub" ]
  [ "$(first IdentitiesOnly)" = yes ]
  [ "$(first ControlMaster)" = no ]
  [ "$(first ControlPersist)" = no ]
  [ "$(first ControlPath)" = none ]
  [ "$(first StrictHostKeyChecking)" = yes ]
}

@test "check --root passes no key file and reuses no connection" {
  run_ops check --root
  [ "$(first User)" = root ]
  [ -z "$(first IdentityFile)" ]
  [ "$(first ControlMaster)" = no ]
  [ "$(first ControlPath)" = none ]
  [ "$(first StrictHostKeyChecking)" = yes ]
}
