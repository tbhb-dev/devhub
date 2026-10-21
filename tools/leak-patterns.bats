#!/usr/bin/env bats
# techops-public's .leak-patterns and .leak-allow under the shared leak gate, in a
# throwaway repository. Each sample is assembled at run time from made-up
# values, so this file never spells out a shape the rules look for, and
# every real *_PRIVATE_STRINGS variable is cleared before each case.
# cspell:ignore compgen AKIA

setup() {
  root=$(git -C "$BATS_TEST_DIRNAME" rev-parse --show-toplevel)
  checker="$root/.devtools/scripts/check-leaks.sh"

  unset GIT_DIR GIT_COMMON_DIR GIT_WORK_TREE GIT_INDEX_FILE GITHUB_ACTIONS CI
  for name in $(compgen -e); do
    if [[ $name == *_PRIVATE_STRINGS ]]; then unset "$name"; fi
  done

  work=$(mktemp -d)
  cd "$work" || exit 1
  git init -q -b main .
  cp "$root/.leak-patterns" "$root/.leak-allow" .
}

teardown() {
  rm -rf "$work"
}

# caught SAMPLE: a file holding SAMPLE fails on a repository rule, and the
# output never repeats the sample.
caught() {
  printf 'value: %s\n' "$1" >leak.txt
  run bash "$checker" leak.txt
  [ "$status" -eq 1 ]
  [[ $output == *"leak.txt:1: pattern: repository rule"* ]]
  [[ $output != *"$1"* ]]
}

@test "the rule file itself passes, because .leak-allow exempts it" {
  run bash "$checker" .leak-patterns
  [ "$status" -eq 0 ]
}

@test "a tailnet IPv6 address fails" {
  prefix=fd7a:115c
  caught "${prefix}:a1e0:ab12:4843:cd96:6258:b240"
}

@test "a private IPv4 address after a host name label fails" {
  caught "app.$(printf '%d.%d.%d.%d' 10 0 0 5).nip.io"
  caught "node.$(printf '%d.%d.%d.%d' 100 101 2 3).example.com"
}

@test "a Hetzner Cloud API token shape fails, and a hex digest or base64 hash passes" {
  run_ab="$(printf 'Ab1%.0s' {1..21})x"
  caught "$run_ab"
  caught "HCLOUD_TOKEN=${run_ab}"
  printf 'digest: %s\n' "$(printf 'a1%.0s' {1..32})" >ok.txt
  printf 'integrity: sha512-Zq+%s/A==\n' "$run_ab" >>ok.txt
  printf 'integrity: sha512-Zq+%s==\n' "$run_ab" >>ok.txt
  run bash "$checker" ok.txt
  [ "$status" -eq 0 ]
}

# A commit signature reaches the gate as base64 lines that each start with
# a space, and a line can hold no plus sign or slash by chance.
@test "a token-shaped run alone on a space-indented line passes, as in a commit signature" {
  printf ' %s\n' "$(printf 'Ab1%.0s' {1..21})x" >sig.txt
  run bash "$checker" sig.txt
  [ "$status" -eq 0 ]
}

@test "an AWS access key ID fails" {
  caught "AKIA$(printf 'Z7%.0s' {1..8})"
  caught "(ASIA$(printf 'Q%.0s' {1..16}))"
}

@test "the GCP service-account key markers fail" {
  q='"'
  caught "${q}type${q}: ${q}service_account${q}"
  caught "${q}private_key_id${q}: ${q}0${q}"
}

@test "a private key PEM header without its closing dashes fails" {
  dashes=$(printf -- '-%.0s' {1..5})
  caught "${dashes}BEGIN RSA PRIVATE KEY"
}

@test ".leak-allow exempts a vendored path from the rules but not from a private literal" {
  mkdir -p vendor/example.com/mod
  token="$(printf 'Ab1%.0s' {1..21})x"
  printf '%s\n' "$token" >vendor/example.com/mod/a.txt
  run bash "$checker" vendor/example.com/mod/a.txt
  [ "$status" -eq 0 ]

  export FOO_PRIVATE_STRINGS="synthetic-zz-host"
  printf '%s\n' "$token" "synthetic-zz-host" >vendor/example.com/mod/a.txt
  run bash "$checker" vendor/example.com/mod/a.txt
  [ "$status" -eq 1 ]
  [[ $output == *"vendor/example.com/mod/a.txt:2: private literal FOO_PRIVATE_STRINGS entry 1"* ]]
  [[ $output != *"pattern:"* ]]
  [[ $output != *"synthetic-zz-host"* ]]
}

@test ".leak-skip excludes only vendor descendants in working-tree mode" {
  printf '^vendor/\n' >.leak-skip
  mkdir vendor vendor-neighbor
  export FOO_PRIVATE_STRINGS="synthetic-zz-host"
  printf '%s\n' "$FOO_PRIVATE_STRINGS" >vendor/a.txt
  git add -- .leak-skip vendor/a.txt
  run bash "$checker"
  [ "$status" -eq 0 ]

  run bash "$checker" --staged
  [ "$status" -eq 1 ]
  [[ $output == *"vendor/a.txt:1: private literal"* ]]

  run bash "$checker" vendor/a.txt
  [ "$status" -eq 1 ]

  printf '%s\n' "$FOO_PRIVATE_STRINGS" >vendor-neighbor/a.txt
  run bash "$checker"
  [ "$status" -eq 1 ]
  [[ $output == *"vendor-neighbor/a.txt:1: private literal"* ]]
  [[ $output != *"vendor/a.txt:1:"* ]]
}
