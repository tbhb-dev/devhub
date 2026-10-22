#!/usr/bin/env bats
# The refusals in front of the ops:* tasks, and the agent handling of
# apply-scoped. Every case stops before ansible runs or reaches a
# stub of it, so none of them reaches a host.
#
# op, ssh-agent, ssh-add and uv resolve to stubs in $stubs, which record
# each call in $CALLS. No case runs the real op or ssh-agent.

setup() {
  script="$BATS_TEST_DIRNAME/ops-ansible.sh"
  empty=$(mktemp -d)
  work=$(mktemp -d)
  stubs=$work/stubs
  mkdir -p "$stubs" "$work/private/ansible/inventory"
  export TECHOPS_APPLY_KEY_FILE=$work/missing-key
  export CALLS=$work/calls
  : >"$CALLS"
  export OPS_KEY=$work/ops-key.pub
  touch "$OPS_KEY"
  printf 'example-service-account-token\n' >"$work/op-token"
  cat >"$stubs/op" <<'EOF'
#!/bin/sh
printf 'op %s token=%s\n' "$*" "$OP_SERVICE_ACCOUNT_TOKEN" >>"$CALLS"
printf 'example-private-key\n'
EOF
  cat >"$stubs/ssh-agent" <<'EOF'
#!/bin/sh
printf 'ssh-agent %s pid=%s\n' "$*" "${SSH_AGENT_PID:-}" >>"$CALLS"
[ "$1" = -k ] && exit 0
printf 'SSH_AUTH_SOCK=%s; export SSH_AUTH_SOCK;\nSSH_AGENT_PID=4242; export SSH_AGENT_PID;\necho Agent pid 4242;\n' "$3"
EOF
  cat >"$stubs/ssh-add" <<'EOF'
#!/bin/sh
printf 'ssh-add %s sock=%s stdin=%s\n' "$*" "$SSH_AUTH_SOCK" "$(cat)" >>"$CALLS"
EOF
  # uv run --group ops <tool> ...: an inventory with the ops key file,
  # and an ansible-playbook that records what it was given.
  cat >"$stubs/uv" <<'EOF'
#!/bin/sh
shift 3
case $1 in
ansible-inventory)
  printf '{"_meta":{"hostvars":{"edge-1":{"techops_ssh_key_files":{"ops":"%s"}}}}}\n' "$OPS_KEY"
  ;;
ansible-playbook)
  shift
  printf 'ansible-playbook %s\n' "$*" >>"$CALLS"
  [ -n "${SSH_AUTH_SOCK:-}" ] || exit 0
  printf 'agent-dir-mode=%s\n' "$(stat -c %a "${SSH_AUTH_SOCK%/*}")" >>"$CALLS"
  ;;
esac
EOF
  chmod +x "$stubs"/*
}

teardown() {
  rm -rf "$empty" "$work"
}

# Runs the script with the stubs first on PATH, every apply-scoped
# variable set, and no terminal; extra VAR=value words go before it.
run_pool() {
  run env PATH="$stubs:$PATH" TECHOPS_INTERNAL_DIR="$work/private" \
    TECHOPS_APPLY_OP_TOKEN_FILE="$work/op-token" TECHOPS_APPLY_OP_KEY_VAULT=example-vault \
    TECHOPS_APPLY_OP_KEY_ITEM=example-item TECHOPS_APPLY_OP_KEY_FIELD=example-field \
    "$@" </dev/null
}

@test "an unknown mode prints the usage" {
  run bash "$script" deploy
  [ "$status" -eq 2 ]
  [[ $output == usage:* ]]
}

@test "verify refuses without TECHOPS_INTERNAL_DIR" {
  TECHOPS_INTERNAL_DIR='' run bash "$script" verify
  [ "$status" -eq 1 ]
  [[ $output == *"ops:verify: TECHOPS_INTERNAL_DIR is unset"* ]]
}

@test "check refuses without TECHOPS_INTERNAL_DIR" {
  TECHOPS_INTERNAL_DIR='' run bash "$script" check
  [ "$status" -eq 1 ]
  [[ $output == *"ops:check: TECHOPS_INTERNAL_DIR is unset"* ]]
}

@test "check refuses a private directory with no inventory, without naming it" {
  TECHOPS_INTERNAL_DIR=$empty run bash "$script" check
  [ "$status" -eq 1 ]
  [[ $output == *"TECHOPS_INTERNAL_DIR has no ansible/inventory directory"* ]]
  [[ $output != *"$empty"* ]]
}

@test "apply refuses without a terminal, whatever the private directory" {
  TECHOPS_INTERNAL_DIR=$empty run bash "$script" apply </dev/null
  [ "$status" -eq 1 ]
  [[ $output == *"operator-only until tbhb-dev/devtools#165"* ]]
  [[ $output == *"no terminal"* ]]
}

@test "apply refuses a playbook name without a terminal, the scoped ones included" {
  for name in ssh runner-pool temporal; do
    run_pool bash "$script" apply "$name"
    [ "$status" -eq 1 ]
    [[ $output == *"no terminal"* ]]
  done
  [ ! -s "$CALLS" ]
}

@test "the stubs stand in for op and ssh-agent" {
  run env PATH="$stubs:$PATH" sh -c 'for c in op ssh-agent ssh-add; do command -v "$c"; done'
  [ "$status" -eq 0 ]
  [ "${lines[0]}" = "$stubs/op" ]
  [ "${lines[1]}" = "$stubs/ssh-agent" ]
  [ "${lines[2]}" = "$stubs/ssh-add" ]
}

@test "apply-scoped refuses each missing variable before op or ssh-agent runs" {
  for name in TECHOPS_APPLY_OP_TOKEN_FILE TECHOPS_APPLY_OP_KEY_VAULT TECHOPS_APPLY_OP_KEY_ITEM TECHOPS_APPLY_OP_KEY_FIELD; do
    run_pool "$name=" bash "$script" apply-scoped ssh
    [ "$status" -eq 1 ]
    [[ $output == *"ops:apply-scoped: $name is unset"* ]]
  done
  [ ! -s "$CALLS" ]
}

@test "apply-scoped refuses a missing token file without naming it" {
  run_pool TECHOPS_APPLY_OP_TOKEN_FILE="$work/missing-token" bash "$script" apply-scoped runner-pool
  [ "$status" -eq 1 ]
  [[ $output == *"the service-account token file TECHOPS_APPLY_OP_TOKEN_FILE names does not exist"* ]]
  [[ $output != *"$work"* ]]
  [ ! -s "$CALLS" ]
}

@test "apply-scoped refuses any playbook name but ssh, runner-pool and temporal before an agent starts" {
  for arg in site accounts verify temporal-remove playbooks/ssh.yml ../ssh ssh.yml '' --check; do
    run_pool bash "$script" apply-scoped "$arg"
    [ "$status" -eq 2 ]
    [[ $output == usage:* ]]
  done
  run_pool bash "$script" apply-scoped
  [ "$status" -eq 2 ]
  [[ $output == usage:* ]]
  run_pool bash "$script" apply-scoped ssh runner-pool
  [ "$status" -eq 2 ]
  [[ $output == usage:* ]]
  [ ! -s "$CALLS" ]
}

@test "apply-scoped takes no ansible-playbook option but --check, --diff and -v" {
  for arg in -e --extra-vars=x -i --limit -vx; do
    run_pool bash "$script" apply-scoped ssh "$arg"
    [ "$status" -eq 2 ]
    [[ $output == usage:* ]]
  done
  [ ! -s "$CALLS" ]
}

@test "apply-scoped runs each playbook without a terminal through its own agent, then kills it" {
  for name in ssh runner-pool temporal; do
    : >"$CALLS"
    run_pool bash "$script" apply-scoped "$name" --check --diff -vv
    [ "$status" -eq 0 ]
    [[ $output != *"no terminal"* ]]
    [[ $output != *"operator-only"* ]]
    [[ $output != *example-private-key* ]]
    [[ $output != *example-service-account-token* ]]

    run grep '^ssh-agent -s ' "$CALLS"
    [ "${#lines[@]}" -eq 1 ]
    sock=$(sed -n 's/^ssh-agent -s -a \([^ ]*\) -t 3600 pid=$/\1/p' "$CALLS")
    [ -n "$sock" ]
    [[ $sock == */agent.sock ]]

    # The reference is built from its parts, as the script builds it.
    scheme=op
    grep -qxF "op read $scheme://example-vault/example-item/example-field token=example-service-account-token" "$CALLS"
    grep -qxF "ssh-add -q - sock=$sock stdin=example-private-key" "$CALLS"
    grep -qxF 'agent-dir-mode=700' "$CALLS"

    playbook=$(grep '^ansible-playbook ' "$CALLS")
    [[ $playbook == *" playbooks/$name.yml --check --diff -vv" ]]
    [[ $playbook == *'"ansible_user":"ops"'* ]]
    [[ $playbook == *"-o IdentitiesOnly=yes -o IdentityAgent=$sock"* ]]
    [[ $playbook == *"\"ansible_ssh_private_key_file\":\"$OPS_KEY\""* ]]
    [[ $playbook != *accounts.yml* ]]

    grep -qxF 'ssh-agent -k pid=4242' "$CALLS"
    [ ! -e "${sock%/*}" ]
  done
}

@test "apply-scoped prefers the file provider without op settings or an agent" {
  printf 'example-file-private-key\n' >"$work/apply-key"
  chmod 600 "$work/apply-key"
  run_pool TECHOPS_APPLY_KEY_FILE="$work/apply-key" TECHOPS_APPLY_OP_TOKEN_FILE= \
    SSH_AUTH_SOCK="$work/operator.sock" bash "$script" apply-scoped temporal
  [ "$status" -eq 0 ]
  [[ $output != *example-file-private-key* ]]
  [[ $output != *example-private-key* ]]
  [[ $output != *example-service-account-token* ]]
  ! grep -E '^(op |ssh-agent |ssh-add )' "$CALLS"
  grep -F '"ansible_ssh_private_key_file":"'"$work/apply-key"'"' "$CALLS"
  grep -F 'IdentitiesOnly=yes -o IdentityAgent=none' "$CALLS"
}

@test "apply-scoped refuses a file key with the wrong mode without exposing it" {
  printf 'example-file-private-key\n' >"$work/apply-key"
  chmod 644 "$work/apply-key"
  run_pool TECHOPS_APPLY_KEY_FILE="$work/apply-key" bash "$script" apply-scoped temporal
  [ "$status" -eq 1 ]
  [[ $output == *"owned by the caller with mode 600"* ]]
  [[ $output != *"$work"* ]]
  [[ $output != *example-file-private-key* ]]
  [ ! -s "$CALLS" ]
}
