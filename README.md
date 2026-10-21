# Techops

A monorepo of services for [devtools](https://github.com/tbhb-dev/devtools), written in Go, Python, and TypeScript and orchestrated by nx on top of mise.

## Development

[mise](https://mise.jdx.dev) pins the toolchain and runs the tasks. Run `mise run bootstrap` once, then `mise run check` before pushing. Bootstrap runs `devtools:prek-install`, then `devtools:install-pre-push-hook` to put commit and leak scans before prek. [AGENTS.md](AGENTS.md) is the contributor guide.

## License

Apache-2.0. See [LICENSE](LICENSE).
