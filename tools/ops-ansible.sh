#!/usr/bin/env bash
# ops-ansible.sh -- run a playbook against the real hosts for the ops:*
# tasks.
#
# Both inventories go in: infra/ansible/inventory with the aliases, and
# $TECHOPS_INTERNAL_DIR/ansible/inventory with the connection variables.
# The remote account follows from the mode:
#
#   verify   ops-ro, playbooks/verify.yml, no become
#   check    ops, --check --diff
#   apply    ops; refused without a terminal, because only the operator
#            runs it until tbhb-dev/devtools#165 lands
#   apply-scoped
#            ops, playbooks/ssh.yml or playbooks/runner-pool.yml and
#            nothing else; allowed without a terminal, so an agent can run
#            it unattended
#
# apply-scoped never uses the operator's agent. It starts its own
# ssh-agent, with its socket in a fresh mode-700 directory, loads the ops
# key into it with op read from a 1Password service account, points ssh
# at that agent alone, and kills the agent when the run ends. The token
# file and the key's vault, item and field come from
# TECHOPS_APPLY_OP_TOKEN_FILE, TECHOPS_APPLY_OP_KEY_VAULT,
# TECHOPS_APPLY_OP_KEY_ITEM and TECHOPS_APPLY_OP_KEY_FIELD in
# mise.local.toml, and the mode refuses to start without every one. It
# takes exactly one playbook short name, ssh or runner-pool, matched
# against a fixed list before any path is built, and of the
# ansible-playbook options only --check, --diff and -v.
#
# For ops-ro and ops the key file comes from techops_ssh_key_files[<user>]
# in the private group_vars and goes in with IdentitiesOnly=yes. ops-ro also gets IdentityAgent=none, so a read-only
# run never reaches the agent that holds the ops key. Runs as ops
# turn connection reuse off, so one approved connection can't serve later
# commands. Every mode sets StrictHostKeyChecking=yes, so a run with no
# pinned host key stops instead of asking. These options go in
# ansible_ssh_args, which ssh reads before the private common args, and
# ssh keeps the first value it sees.
#
# The user, the key file, the connection type and the ssh args all go in
# as extra vars, because an inventory variable beats a command-line
# option such as --private-key and only an extra var beats both.
#
# Messages never print a path or a value from the private inventory.
#
# Usage:
#   ops-ansible.sh verify [ansible-playbook args...]
#   ops-ansible.sh check|apply [playbook] [ansible-playbook args...]
#   ops-ansible.sh apply-scoped ssh|runner-pool [--check] [--diff] [-v...]
#
# playbook names a file under infra/ansible/playbooks without .yml and
# defaults to accounts.
set -euo pipefail

usage() {
  echo "usage: ops-ansible.sh verify|check|apply [playbook] [ansible-playbook args...]" >&2
  echo "       ops-ansible.sh apply-scoped ssh|runner-pool [--check] [--diff] [-v...]" >&2
  exit 2
}

mode=${1:-}
[[ $mode == verify || $mode == check || $mode == apply || $mode == apply-scoped ]] || usage
shift

# Reject the removed flag before the terminal guard or inventory reads.
for arg in "$@"; do
  [[ $arg != --root ]] || usage
done

if [[ $mode == apply ]]; then
  echo "ops:apply is operator-only until tbhb-dev/devtools#165 lands" >&2
  if [[ ! -t 0 || ! -t 1 ]]; then
    echo "ops:apply: no terminal, so this is not the operator; refusing" >&2
    exit 1
  fi
fi

# The unattended mode runs one of two fixed playbooks, so nothing on its
# command line may name another, add a variable or point at other code.
# The name is matched as a whole word before it goes into any path.
if [[ $mode == apply-scoped ]]; then
  case ${1:-} in
  ssh | runner-pool) scoped_playbook=$1 ;;
  *) usage ;;
  esac
  shift
  for arg in "$@"; do
    [[ $arg == --check || $arg == --diff || $arg =~ ^-v+$ ]] || usage
  done
  for name in TECHOPS_APPLY_OP_TOKEN_FILE TECHOPS_APPLY_OP_KEY_VAULT TECHOPS_APPLY_OP_KEY_ITEM TECHOPS_APPLY_OP_KEY_FIELD; do
    if [[ -z ${!name:-} ]]; then
      echo "ops:$mode: $name is unset; set it in mise.local.toml" >&2
      exit 1
    fi
  done
  op_token_file=$TECHOPS_APPLY_OP_TOKEN_FILE
  # shellcheck disable=SC2088 # matches the literal tilde TOML carries
  if [[ $op_token_file == "~/"* ]]; then
    op_token_file=$HOME/${op_token_file#"~/"}
  fi
  if [[ ! -f $op_token_file || ! -r $op_token_file ]]; then
    echo "ops:$mode: the service-account token file TECHOPS_APPLY_OP_TOKEN_FILE names does not exist" >&2
    exit 1
  fi
fi

if [[ -z ${TECHOPS_INTERNAL_DIR:-} ]]; then
  echo "ops:$mode: TECHOPS_INTERNAL_DIR is unset; set it in mise.local.toml to the techops-internal checkout" >&2
  exit 1
fi
private_inventory=$TECHOPS_INTERNAL_DIR/ansible/inventory
if [[ ! -d $private_inventory ]]; then
  echo "ops:$mode: TECHOPS_INTERNAL_DIR has no ansible/inventory directory" >&2
  exit 1
fi

user=ops
playbook=accounts
mode_args=()
case $mode in
verify)
  user=ops-ro
  playbook=verify
  ;;
check)
  mode_args=(--check --diff)
  ;;
apply-scoped)
  playbook=$scoped_playbook
  ;;
esac
if [[ $mode == check || $mode == apply ]]; then
  if [[ $# -gt 0 && $1 != -* ]]; then
    playbook=$1
    shift
  fi
fi

root=$(git rev-parse --show-toplevel)
cd "$root/infra/ansible"
export ANSIBLE_CONFIG=$PWD/ansible.cfg

if [[ ! -f playbooks/$playbook.yml ]]; then
  echo "ops:$mode: no playbook named $playbook under infra/ansible/playbooks" >&2
  exit 1
fi

inventories=(-i inventory -i "$private_inventory")

case $user in
ops-ro) ssh_args='-C -o StrictHostKeyChecking=yes -o ControlMaster=auto -o ControlPersist=60s -o IdentitiesOnly=yes -o IdentityAgent=none' ;;
ops) ssh_args='-C -o StrictHostKeyChecking=yes -o ControlMaster=no -o ControlPersist=no -o ControlPath=none -o IdentitiesOnly=yes' ;;
esac

key=$(uv run --group ops ansible-inventory "${inventories[@]}" --list |
  jq -r --arg u "$user" '[._meta.hostvars[] | .techops_ssh_key_files[$u]? // empty] | unique | if length == 1 then .[0] else empty end')
if [[ -z $key ]]; then
  echo "ops:$mode: the private inventory has no single techops_ssh_key_files entry for $user" >&2
  exit 1
fi
# shellcheck disable=SC2088 # matches the literal tilde YAML carries
if [[ $key == "~/"* ]]; then
  key=$HOME/${key#"~/"}
fi
if [[ ! -f $key ]]; then
  echo "ops:$mode: the key file techops_ssh_key_files names for $user does not exist" >&2
  exit 1
fi

agent_dir=
if [[ $mode == apply-scoped ]]; then
  agent_dir=$(mktemp -d)
  chmod 700 "$agent_dir"
  agent_pid=
  # shellcheck disable=SC2329 # runs through the EXIT trap below
  stop_agent() {
    if [[ -n $agent_pid ]]; then
      SSH_AGENT_PID=$agent_pid ssh-agent -k >/dev/null 2>&1 || true
    fi
    rm -rf "$agent_dir"
  }
  trap stop_agent EXIT
  # The lifetime only matters if this script dies before its trap runs.
  agent_env=$(ssh-agent -s -a "$agent_dir/agent.sock" -t 3600)
  agent_pid=$(sed -n 's/^SSH_AGENT_PID=\([0-9][0-9]*\);.*/\1/p' <<<"$agent_env")
  if [[ -z $agent_pid ]]; then
    echo "ops:$mode: ssh-agent did not start" >&2
    exit 1
  fi
  export SSH_AUTH_SOCK=$agent_dir/agent.sock
  # The reference is built from its parts; the key goes from op to ssh-add
  # through a pipe and never touches the disk.
  scheme=op
  op_token=$(<"$op_token_file")
  OP_SERVICE_ACCOUNT_TOKEN=$op_token op read \
    "$scheme://$TECHOPS_APPLY_OP_KEY_VAULT/$TECHOPS_APPLY_OP_KEY_ITEM/$TECHOPS_APPLY_OP_KEY_FIELD" |
    ssh-add -q -
  unset op_token
  ssh_args="$ssh_args -o IdentityAgent=$SSH_AUTH_SOCK"
fi

# The inline key would win over the key file, so it stays empty.
extra=$(jq -cn --arg u "$user" --arg a "$ssh_args" --arg k "$key" \
  '{ansible_connection: "ssh", ansible_user: $u, ansible_ssh_args: $a,
    ansible_ssh_private_key_file: $k, ansible_ssh_private_key: ""}')

# The ${a[@]+...} form keeps an empty array from tripping set -u in bash 3.2.
run=(uv run --group ops ansible-playbook "${inventories[@]}"
  -e "$extra" ${mode_args[@]+"${mode_args[@]}"} "playbooks/$playbook.yml" "$@")
if [[ -n $agent_dir ]]; then
  # No exec: the EXIT trap has to outlive the run to kill the agent.
  "${run[@]}"
  exit
fi
exec "${run[@]}"
