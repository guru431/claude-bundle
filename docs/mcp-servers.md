# Declaring MCP servers

How you *declare* an MCP server matters as much as which server you pick. The wrong
declaration costs you a process per session, a few seconds every time a window opens,
and a dependency on the network being up — none of it visible until you go looking.

## The rule

1. **HTTP url** when the project publishes a hosted endpoint.
2. **Direct path to the interpreter** for a local server.
3. **Never `npx -y` or `uv run`** inside an MCP config.

```jsonc
// bad — extra process, re-resolve, network access on every start
{ "command": "npx", "args": ["-y", "some-mcp-server"] }
{ "command": "uv",  "args": ["run", "--directory", "/path", "python", "server.py"] }

// good — hosted endpoint, zero local processes
{ "type": "http", "url": "https://mcp.example.com/mcp" }

// good — local server, direct interpreter path
{ "command": "/path/to/.venv/bin/python", "args": ["/path/to/server.py"] }
```

## Why resolver wrappers are worse than they look

Measured on a real Windows setup with several editor windows open:

| | |
|---|---|
| `npx -y <server> --version` | **6.4 s**, of which **4.0 s** was a round-trip to the npm registry |
| Idle `npx` wrapper process | **95 MB** of commit — about twice the server it launched |
| Cost of one wrapped server on Windows | **6 processes**: wrapper + server, each with a shell and a console host |

Three separate problems:

- **The wrapper does not go away.** `npx` spawns the real server and stays alive as its
  parent. You pay for both, forever.
- **It re-resolves the package on every session start.** Not cached away: the registry
  round-trip happens each time. Open a window with several sessions in it and those
  seconds are exactly the pause you feel.
- **It needs the network.** A server that would run fine offline stops starting when the
  registry is unreachable. Worse, if the package was never installed, `npx -y` silently
  downloads it on first use — inside your editor's startup path.

The same reasoning applies to `uv run`: it resolves the environment on every launch
when the venv is already built and could be called directly.

## Two traps with local stdio servers

**stdout belongs to the protocol.** MCP stdio speaks JSON-RPC over stdout; a single
stray line breaks the session. Banners, progress, warnings — all must go to stderr.
Real example: `dotenv` v17 prints `injected env … from .env` to *stdout*, which is
enough to break a server that otherwise works. Silence it with
`DOTENV_CONFIG_QUIET=true` in the server's `env` block.

**`bin` is not always the working entry point.** A package can ship a broken CLI while
its `main` module starts fine — mismatched module formats are a common way for this to
happen. Since `npx` always launches `bin`, such a package appears completely broken via
`npx` and works when you point at `main` directly. If a server refuses to start, try the
`main` entry from its `package.json` before concluding it's dead.

## Verify with a handshake

"The process started" is not a check — a server can start and still be unusable because
its stdout is dirty or it never answers `initialize`.

```bash
python scripts/mcp-probe.py ~/.claude.json        # or any .mcp.json
python scripts/mcp-probe.py --check-wrappers      # audit declarations only, nothing launched
```

The probe launches each declared stdio server exactly as configured, runs `initialize`
and `tools/list`, and reports stray stdout separately. `--check-wrappers` skips launching
and just flags resolver wrappers and running processes that shouldn't be there.

## When a plugin declares the server for you

Plugins can ship their own MCP config, and that config can drift from what the plugin
metadata claims. Seen in practice with `context7`: an update fetched an HTTP-based
version and recorded its commit, but `installPath` stayed pinned to an older npx-based
copy in the plugin cache. Result — a local server per session, six processes, seconds of
registry round-trip on every window open, and no indication anything was wrong.

Editing `installed_plugins.json` did not stick: the value was rewritten when a session
ended. If you hit this, stop relying on the plugin for that server and declare it
yourself:

1. **Disable the plugin** — set `enabledPlugins["<plugin>@<marketplace>"]` to `false` in
   `~/.claude/settings.json`. Skipping this declares the server twice.
2. **Declare the server** in `mcpServers` in `~/.claude.json`:
   ```json
   "context7": { "type": "http", "url": "https://mcp.context7.com/mcp" }
   ```
3. **Fix permissions.** Tool names depend on how the server is declared, so
   `mcp__plugin_<plugin>_<server>__*` becomes `mcp__<server>__*`. Update
   `permissions.allow` or Claude will ask for approval on every call.

Before doing this, check what else the plugin provides — if it ships skills or commands,
you lose those too. A plugin that contains only an `.mcp.json` is safe to replace this
way.

## What to watch after updates

Plugin and package updates can quietly reintroduce a wrapper. A cheap periodic check:

```bash
python scripts/mcp-probe.py --check-wrappers
```

Run it after updating plugins, or when a new window starts opening noticeably slower.
