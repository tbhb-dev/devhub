# Agent instructions

Guidance for AI coding agents working in this repository. This file is the canonical source for every harness. `CLAUDE.md` points here, and the agy command-line tool reads only this file.

## Scope

techops-public is a monorepo of services for devtools, written in Go, Python, and TypeScript. The Go code is one root module, `github.com/tbhb-dev/techops-public`: `cmd/techopsctl` is the command-line entry point, `internal/` holds private packages, and `apps/` and `packages/` hold the web and service apps and the shared TypeScript and Python packages. `tools/` holds the toolchain scripts. nx wraps mise: the devtools contract stays `mise run ...`, and an nx target in a `project.json` calls `mise run` or the underlying `go`, `uv` or `pnpm` command, which buys caching and `nx affected`. nx never connects to a cloud workspace.

## Public repository

This repository is public. Never commit a hostname, an address outside the documentation ranges, an account, project or organization identifier, a token, a password-manager reference, or any other credential. Placeholders use `example.com`, `example.ts.net` and the reserved documentation address ranges. Private values live only in `mise.local.toml`, which a gitignore entry keeps out of history, and tasks read them from the environment. gitleaks scans the history with `mise run devtools:gitleaks`.

## Tools

mise pins the tools and defines the tasks. The shared half arrives from `tbhb-dev/devtools` through vendir into `.config/mise/conf.d/` and `.devtools/tasks/`. Never edit those two directories by hand. Run `mise run bootstrap` once to sync the payload and install the tools plus the git hooks.

- `mise run lint` runs every lint gate in order.
- `mise run test` runs the test suites.
- `mise run check` runs both.
- `mise run devtools:update-pins <version>` moves every devtools pin together, both Renovate presets included. Redeploy the agent primitives with `mise run apm-sync` instead of a bare `apm install`.

The `guard-markdown` binary must be on PATH for `lint-markdown-wrap` and the Claude write hook. `mise run bootstrap` installs it through `mise run install-guard-markdown`, which takes the version from the devtools pin in `apm.yml`. A pin bump moves it too. Keep the Go bin directory on PATH.

## Operations

`infra/ansible/` holds the Ansible tree for host-level operations, and every run reads two inventories. Hosts appear there only as aliases such as `edge-1`, and the inventory in this repository sets groups and defaults that identify nothing. The private companion repository, cloned beside this one, stores the second inventory. That inventory maps each alias to its real host and pins `known_hosts`. Its variables add the authorized public keys and `techops_ssh_key_files`, which gives the key file for each remote account. Each `ops:*` task finds the private one through `TECHOPS_INTERNAL_DIR` in `mise.local.toml`. Without it, the task stops. mise reads every `mise.local.toml` from the working directory up, so a worktree inside this checkout still sets the variable. To see the refusal, run `TECHOPS_INTERNAL_DIR= bash tools/ops-ansible.sh verify` directly, never `mise run ops:verify`.

Use the alias for a host everywhere in this repository and its history. Only the operator edits the private repository. An agent keeps every real host name, address, tailnet name, key, fingerprint, and console URL out of what it writes.

An agent may run these tasks on its own:

- `mise run lint-ansible` runs `ansible-lint` and a syntax check against a stub inventory.
- `mise run test-ansible` runs the Molecule scenarios in Docker.
- `mise run ops:verify` runs the verification playbook against the real hosts as `ops-ro`, which has no sudo.

The other two need the operator. `mise run ops:check` runs as `ops` with `--check --diff`, and the `ops` key asks the operator to approve each new session. `mise run ops:apply` stays operator-only until tbhb-dev/devtools#165 merges, and it refuses to run without a terminal.

## Commit messages

Run the `commit` skill. Draft the message in the repo-root file `COMMIT_AGENTMSG`, which a gitignore entry keeps out of history, then run `mise run lint-commit-msg`. The prek commit-msg hook is the real gate. Stage paths by name, and never run `git add -A` or `git add .`.

A message needs a Conventional Commits subject, a blank line before the body, an `Assisted-by:` trailer in the form `agent:model tool` such as `claude:opus-5-5 claude-code`, and a `Signed-off-by:` trailer after it. Never use `--no-verify` on a real commit.

## Pull requests

Run the `pr` skill. Draft the description in the repo-root file `PR_AGENTDESC.md`, following `.github/pull_request_template.md`, and run `mise run lint-pr-description`. The `watch-pr`, `fix-pr` and `merge-pr` skills cover what follows.

## Markdown

Write each paragraph on one line, with no hard wrapping. `mise run lint-markdown-wrap` enforces it.

## Stash and work in progress

Agent sessions can run in parallel worktrees, and every worktree shares one global `git stash` stack.

- Use `git rebase --autostash` instead of a manual stash when you rebase a dirty worktree.
- Never run a bare `git stash pop` or `git stash apply`. Name a manual stash with `git stash push -m`, check `git stash list` right before restoring, and pop the explicit `stash@{n}`.
- To park work across a rebase or branch switch, commit it with `git commit -am wip --no-verify` and unwind it with `git reset --soft HEAD~1`. This throwaway commit is the one sanctioned use of `--no-verify`, and real commits never use it.
