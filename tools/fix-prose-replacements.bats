#!/usr/bin/env bats
# fix_prose_replacements.py must fail when vale cannot run or a finding
# cannot be parsed, rather than report success.

setup() {
  root=$(git -C "$BATS_TEST_DIRNAME" rev-parse --show-toplevel)
  cd "$root" || exit 1
  work=$(mktemp -d)
  mkdir "$work/bin"
  printf 'Some text.\n' >"$work/draft.md"
  PATH="$work/bin:$PATH"
}

teardown() {
  rm -rf "$work"
}

fake_vale() {
  printf '#!/usr/bin/env bash\n%s\n' "$1" >"$work/bin/vale"
  chmod +x "$work/bin/vale"
}

@test "a vale that fails to run makes the task fail" {
  fake_vale 'echo "config error" >&2; exit 2'
  run mise run fix-prose-replacements "$work/draft.md"
  [ "$status" -eq 1 ]
  [[ $output == *"vale-failed"* ]]
  [[ $output == *"config error"* ]]
}

@test "a finding with unparsable quoting makes the task fail" {
  fake_vale 'printf "%s\n" "1:1-4 [error] Google.Contractions match=\"does not\" replace_with=\"\\q\""; exit 1'
  run mise run fix-prose-replacements "$work/draft.md"
  [ "$status" -eq 1 ]
  [[ $output == *"unparsable-quoting"* ]]
}

@test "a clean vale run passes" {
  fake_vale 'exit 0'
  run mise run fix-prose-replacements "$work/draft.md"
  [ "$status" -eq 0 ]
}

@test "a failing vale makes lint-draft fail" {
  fake_vale 'echo "config error" >&2; exit 2'
  run mise run lint-draft "$work/draft.md"
  [ "$status" -eq 1 ]
  [[ $output == *"vale-failed"* ]]
  [[ $output == *"config error"* ]]
}
