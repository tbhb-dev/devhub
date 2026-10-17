#!/usr/bin/env bats
# The refusals in front of the ops:* tasks. Every case stops before
# ansible runs, so none of them reaches a host.

setup() {
  script="$BATS_TEST_DIRNAME/ops-ansible.sh"
  empty=$(mktemp -d)
}

teardown() {
  rm -rf "$empty"
}

@test "an unknown mode prints the usage" {
  run bash "$script" deploy
  [ "$status" -eq 2 ]
  [[ $output == usage:* ]]
}

@test "verify refuses without DEVHUB_PRIVATE_DIR" {
  DEVHUB_PRIVATE_DIR='' run bash "$script" verify
  [ "$status" -eq 1 ]
  [[ $output == *"ops:verify: DEVHUB_PRIVATE_DIR is unset"* ]]
}

@test "check refuses without DEVHUB_PRIVATE_DIR" {
  DEVHUB_PRIVATE_DIR='' run bash "$script" check
  [ "$status" -eq 1 ]
  [[ $output == *"ops:check: DEVHUB_PRIVATE_DIR is unset"* ]]
}

@test "check refuses a private directory with no inventory, without naming it" {
  DEVHUB_PRIVATE_DIR=$empty run bash "$script" check
  [ "$status" -eq 1 ]
  [[ $output == *"DEVHUB_PRIVATE_DIR has no ansible/inventory directory"* ]]
  [[ $output != *"$empty"* ]]
}

@test "apply refuses without a terminal, whatever the private directory" {
  DEVHUB_PRIVATE_DIR=$empty run bash "$script" apply </dev/null
  [ "$status" -eq 1 ]
  [[ $output == *"operator-only until tbhb-dev/devtools#165"* ]]
  [[ $output == *"no terminal"* ]]
}
