# Declaring MCP servers — measurements, examples, traps

`~/.claude/CLAUDE.md` § Declaring MCP servers carries the rule: a direct
interpreter path or an HTTP url, never `npx -y` / `uv run`. This is why, and
what to do when a server still misbehaves.

## What a resolver wrapper costs

- **It stays alive.** `npx` does not replace itself with the server — it parents
  it and keeps sitting there. Measured on a real setup: an idle `npx` wrapper
  held **95 MB** of commit, roughly twice the server it had launched.
- **It re-resolves on every session start.** Measured: `npx -y <server>
  --version` took **6.4 s**, of which 4.0 s was a round-trip to the npm
  registry. Multiply by servers × open sessions — that is the pause you feel
  when a new editor window opens, and it makes your tooling depend on the
  network being up.
- **On Windows each wrapper drags a shell and a console host with it**, so one
  server can cost six processes instead of one.

```jsonc
// bad — extra process, re-resolve, network access on every start
{ "command": "npx", "args": ["-y", "some-mcp-server"] }

// good — hosted endpoint, zero local processes
{ "type": "http", "url": "https://mcp.example.com/mcp" }

// good — local server, direct interpreter path
{ "command": "/path/to/.venv/bin/python", "args": ["/path/to/server.py"] }
```

## Two traps when a local stdio server misbehaves

- **stdout belongs to the protocol.** Any stray line there breaks JSON-RPC —
  banners and diagnostics must go to stderr. `dotenv` v17, for example, prints
  `injected env … from .env` to *stdout*; silence it with
  `DOTENV_CONFIG_QUIET=true`.
- **`bin` is not always the working entry point.** A package can ship a broken
  CLI while its `main` module starts fine. Check what actually runs before
  blaming your config — and remember `npx` always launches `bin`.

## Verify with a handshake

Not with "the process started": `scripts/mcp-probe.py` in the bundle runs each
server declared for Claude Code, performs `initialize` + `tools/list`, and
reports stray stdout separately; `--check-wrappers` lists the declarations
still going through a resolver wrapper. Where servers are declared and how to
choose between them — `docs/mcp-servers.md` in the bundle repository.

**MCP config is per tool — there is no shared file.** Claude Code keeps
user/local-scope servers in `~/.claude.json` and project-scope servers in
`<project>/.mcp.json`; Codex CLI reads `~/.codex/config.toml`. To run one server
under both, declare it in each tool's own format and keep them in step by hand.
