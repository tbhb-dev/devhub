#!/usr/bin/env bash
# ops-ansible.sh -- run a playbook against the real hosts for the ops:*
# tasks.
#
# Both inventories go in: infra/ansible/inventory with the aliases, and
# $DEVHUB_PRIVATE_DIR/ansible/inventory with the connection variables.
# The remote account follows from the mode:
#
#   verify   ops-ro, playbooks/verify.yml, no become
#   check    ops, --check --diff
#   apply    ops; refused without a terminal, because only the operator
#            runs it until tbhb-dev/devtools#165 lands
#
# check and apply take --root for the first apply, before ops exists.
# A root run passes no key file and uses the operator's own root key.
#
# For ops-ro and ops the key file comes from devhub_ssh_key_files[<user>]
# in the private group_vars and goes in with IdentitiesOnly=yes. ops-ro also gets IdentityAgent=none, so a read-only
# run never reaches the agent that holds the ops key. Runs as ops or root
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
#   ops-ansible.sh check|apply [--root] [playbook] [ansible-playbook args...]
#
# playbook names a file under infra/ansible/playbooks without .yml and
# defaults to accounts.
set -euo pipefail

usage() {
  echo "usage: ops-ansible.sh verify|check|apply [--root] [playbook] [ansible-playbook args...]" >&2
  exit 2
}

mode=${1:-}
[[ $mode == verify || $mode == check || $mode == apply ]] || usage
shift

if [[ $mode == apply ]]; then
  echo "ops:apply is operator-only until tbhb-dev/devtools#165 lands" >&2
  if [[ ! -t 0 || ! -t 1 ]]; then
    echo "ops:apply: no terminal, so this is not the operator; refusing" >&2
    exit 1
  fi
fi

if [[ -z ${DEVHUB_PRIVATE_DIR:-} ]]; then
  echo "ops:$mode: DEVHUB_PRIVATE_DIR is unset; set it in mise.local.toml to the devhub-private checkout" >&2
  exit 1
fi
private_inventory=$DEVHUB_PRIVATE_DIR/ansible/inventory
if [[ ! -d $private_inventory ]]; then
  echo "ops:$mode: DEVHUB_PRIVATE_DIR has no ansible/inventory directory" >&2
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
esac
if [[ $mode != verify ]]; then
  if [[ ${1:-} == --root ]]; then
    user=root
    shift
  fi
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
root) ssh_args='-C -o StrictHostKeyChecking=yes -o ControlMaster=no -o ControlPersist=no -o ControlPath=none' ;;
esac

key=
if [[ $user != root ]]; then
  key=$(uv run --group ops ansible-inventory "${inventories[@]}" --list |
    jq -r --arg u "$user" '[._meta.hostvars[] | .devhub_ssh_key_files[$u]? // empty] | unique | if length == 1 then .[0] else empty end')
  if [[ -z $key ]]; then
    echo "ops:$mode: the private inventory has no single devhub_ssh_key_files entry for $user" >&2
    exit 1
  fi
  # shellcheck disable=SC2088 # matches the literal tilde YAML carries
  if [[ $key == "~/"* ]]; then
    key=$HOME/${key#"~/"}
  fi
  if [[ ! -f $key ]]; then
    echo "ops:$mode: the key file devhub_ssh_key_files names for $user does not exist" >&2
    exit 1
  fi
fi

# The ssh plugin skips an empty key file, so a root run uses the
# operator's own key whatever the inventory names. The inline key, which
# would win over the key file, stays empty for every user.
extra=$(jq -cn --arg u "$user" --arg a "$ssh_args" --arg k "$key" \
  '{ansible_connection: "ssh", ansible_user: $u, ansible_ssh_args: $a,
    ansible_ssh_private_key_file: $k, ansible_ssh_private_key: ""}')

# The ${a[@]+...} form keeps an empty array from tripping set -u in bash 3.2.
exec uv run --group ops ansible-playbook "${inventories[@]}" \
  -e "$extra" ${mode_args[@]+"${mode_args[@]}"} "playbooks/$playbook.yml" "$@"
