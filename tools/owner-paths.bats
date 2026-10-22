#!/usr/bin/env bats
# CODEOWNERS patterns must cover tracked paths and stay within owner_paths.

setup() {
  root=$(git -C "$BATS_TEST_DIRNAME" rev-parse --show-toplevel)
  work=$(mktemp -d)
  git -C "$root" ls-files >"$work/paths"
  awk '!/^[[:space:]]*(#|$)/ { print $1 }' "$root/.github/CODEOWNERS" >"$work/patterns"
  re=$(awk '
    /^[ \t]*#/ || !/=/ { next }
    {
      key = $0
      sub(/=.*/, "", key)
      gsub(/^[ \t]+|[ \t]+$/, "", key)
      if (key != "owner_paths") next
      sub(/^[^=]*=/, "")
      gsub(/^[ \t]+|[ \t]+$/, "")
      print
      exit
    }
  ' "$root/.agents/agent-skills.conf")

  unset GIT_DIR GIT_COMMON_DIR GIT_WORK_TREE GIT_INDEX_FILE
  # A user's global excludes file must not add matches beyond CODEOWNERS.
  export GIT_CONFIG_GLOBAL=/dev/null GIT_CONFIG_SYSTEM=/dev/null XDG_CONFIG_HOME="$work/xdg"
  cd "$work" || exit 1
  git init -q -b main .
}

teardown() {
  rm -rf "$work"
}

@test "each CODEOWNERS pattern matches a tracked path" {
  while IFS= read -r pattern; do
    printf '%s\n' "$pattern" >.gitignore
    run git check-ignore --no-index --stdin <"$work/paths"
    if [ "$status" -ne 0 ]; then
      printf 'CODEOWNERS pattern matches no tracked path: %s\n' "$pattern"
      return 1
    fi
  done <"$work/patterns"
}

@test "tracked CODEOWNERS paths match owner_paths" {
  cp "$work/patterns" .gitignore
  run git check-ignore --no-index --stdin <"$work/paths"
  [ "$status" -eq 0 ]
  while IFS= read -r path; do
    if [[ ! $path =~ $re ]]; then
      printf 'CODEOWNERS path does not match owner_paths: %s\n' "$path"
      return 1
    fi
  done <<<"$output"
}
