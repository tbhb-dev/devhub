# Techops

A monorepo of services for [devtools](https://github.com/tbhb-dev/devtools), written in Go, Python, and TypeScript and orchestrated by nx on top of mise.

## Development

[mise](https://mise.jdx.dev) pins the toolchain and runs the tasks. Run `mise run bootstrap` once, then `mise run check` before pushing. Bootstrap runs `devtools:prek-install`, then `devtools:install-pre-push-hook` to put commit and leak scans before prek. [AGENTS.md](AGENTS.md) is the contributor guide.

Run `mise run check-commit` for the local gate. Commit and publish gates use its scenario selection through `mise run test`.

The local gate runs lint, playbook syntax checks, and unit checks before selecting Molecule scenarios from branch and working changes against `origin/main`. Role dependencies and imported playbooks expand the selection. Shared infrastructure and unmapped paths select the full suite. Before deployment, run `mise run test-ansible` for the full Molecule suite. The required Ansible CI job uses this command too.

Images come from the [image definition](infra/ansible/molecule/image/Dockerfile), which pins the base by digest. The runner builds a missing image and reuses its content tag until the definition changes. Collections use the pins in [requirements.yml](infra/ansible/requirements.yml). Run `mise run install-ansible` to install them on the controller.

Record gate timing with `uv run --group ops python -m tools.gate_timing --record timings.tsv gate mise run check-commit`. Read elapsed times with `uv run --group ops python -m tools.gate_timing --summary timings.tsv`.

The Molecule runner prints the path to its phase record. Pass `--timings` to choose a path.

## License

Apache-2.0. See [LICENSE](LICENSE).
