#!/usr/bin/env python3
"""Check MCP server declarations — by handshake, not by "the process started".

    python tools/mcp-probe.py                     # probe servers in ~/.claude.json
    python tools/mcp-probe.py path/to/.mcp.json   # probe a specific config
    python tools/mcp-probe.py CONFIG server-name  # probe one server
    python tools/mcp-probe.py --check-wrappers    # audit declarations, launch nothing

Probing launches each declared stdio server exactly as configured, sends `initialize`
and `tools/list`, and reports stray stdout separately: MCP speaks JSON-RPC over stdout,
so a single banner line there breaks the session while the process still looks healthy.

`--check-wrappers` launches nothing. It flags resolver wrappers (`npx -y`, `uv run`) in
every config it can find, including plugin-provided ones, and looks for wrapper
processes already running. See docs/mcp-servers.md for why those are worth removing.

Exit code is 1 if anything failed or a wrapper was found, so this can gate a script.
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
from pathlib import Path

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

HOME = Path.home()
DEFAULT_CONFIG = HOME / ".claude.json"
PLUGIN_CACHE = HOME / ".claude" / "plugins" / "cache"

# A server is "wrapped" when the command resolves the package instead of running it.
WRAPPERS = ("npx", "npm", "pnpm", "yarn", "bunx", "uv", "uvx", "pipx")
HTTP_TYPES = ("http", "sse", "streamable-http")

INITIALIZE = {
    "jsonrpc": "2.0", "id": 1, "method": "initialize",
    "params": {"protocolVersion": "2025-06-18", "capabilities": {},
               "clientInfo": {"name": "mcp-probe", "version": "1"}},
}
TOOLS_LIST = {"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}}


def load_servers(path: Path) -> dict:
    """Read a config in either shape: {"mcpServers": {...}} or a bare mapping."""
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        print(f"!! cannot read {path}: {exc}")
        return {}
    if isinstance(data, dict) and "mcpServers" in data:
        return data["mcpServers"] or {}
    # Some plugin configs omit the wrapper key and map names to specs directly.
    if isinstance(data, dict) and all(isinstance(v, dict) for v in data.values()):
        return data
    return {}


def is_wrapper(spec: dict) -> str | None:
    command = (spec.get("command") or "").strip()
    if not command:
        return None
    base = Path(command).name.lower()
    for stem in (base, base.removesuffix(".exe").removesuffix(".cmd")):
        if stem in WRAPPERS:
            return command
    return None


def probe(name: str, spec: dict, timeout: float = 25.0) -> bool:
    """Launch one stdio server and complete a handshake. True if it is usable."""
    if spec.get("type") in HTTP_TYPES:
        print(f"{name:<14} skipped — declared over {spec['type']}, nothing to launch")
        return True

    env = dict(os.environ)
    for key, value in (spec.get("env") or {}).items():
        # ${VAR} placeholders are expanded by the client; substitute so the server
        # sees something plausible instead of a literal and dies during config.
        env[key] = "probe-placeholder" if str(value).startswith("${") else str(value)

    command = [spec.get("command", ""), *spec.get("args", [])]
    if not command[0]:
        print(f"{name:<14} FAIL — no command in declaration")
        return False

    wrapper = is_wrapper(spec)
    if wrapper:
        print(f"{name:<14} warning — launched through '{wrapper}' "
              f"(see docs/mcp-servers.md)")

    try:
        proc = subprocess.Popen(
            command, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
            stderr=subprocess.PIPE, env=env, text=True,
            encoding="utf-8", errors="replace", bufsize=1)
    except OSError as exc:
        print(f"{name:<14} FAIL — cannot start: {exc}")
        return False

    def read_reply(want_id: int, deadline: float, junk: list[str]):
        while time.time() < deadline:
            line = proc.stdout.readline()
            if not line:
                if proc.poll() is not None:
                    return None
                continue
            line = line.strip()
            if not line:
                continue
            try:
                message = json.loads(line)
            except json.JSONDecodeError:
                junk.append(line)          # stray stdout — this is what breaks MCP
                continue
            if message.get("id") == want_id:
                return message
        return None

    ok = False
    try:
        junk: list[str] = []
        proc.stdin.write(json.dumps(INITIALIZE) + "\n")
        proc.stdin.flush()
        reply = read_reply(1, time.time() + timeout, junk)

        if junk:
            print(f"{name:<14} FAIL — {len(junk)} non-JSON line(s) on stdout, "
                  f"this breaks JSON-RPC: {junk[0][:60]!r}")
            return False
        if reply is None:
            stderr = (proc.stderr.read() or "")[:200].replace("\n", " ")
            print(f"{name:<14} FAIL — no reply to initialize. stderr: {stderr}")
            return False

        info = (reply.get("result") or {}).get("serverInfo") or {}
        label = f"{info.get('name', '?')} {info.get('version', '')}".strip()

        proc.stdin.write(json.dumps({"jsonrpc": "2.0",
                                     "method": "notifications/initialized"}) + "\n")
        proc.stdin.write(json.dumps(TOOLS_LIST) + "\n")
        proc.stdin.flush()
        tools_reply = read_reply(2, time.time() + timeout, junk)
        count = len((tools_reply.get("result") or {}).get("tools") or []) if tools_reply else None

        if count is None:
            print(f"{name:<14} OK initialize ({label}), but tools/list timed out")
        else:
            print(f"{name:<14} OK — {label}, {count} tool(s)")
        ok = True
    finally:
        try:
            proc.kill()
        except OSError:
            pass
    return ok


def running_wrappers() -> list[str]:
    """Wrapper processes already running, if the platform lets us look cheaply."""
    found = []
    try:
        if os.name == "nt":
            out = subprocess.run(["wmic", "process", "get", "commandline"],
                                 capture_output=True, text=True, timeout=60,
                                 errors="replace").stdout
        else:
            out = subprocess.run(["ps", "-eo", "args"], capture_output=True,
                                 text=True, timeout=60, errors="replace").stdout
    except (OSError, subprocess.SubprocessError):
        return found
    for line in out.splitlines():
        low = line.lower()
        if "mcp" in low and ("npx-cli" in low or "npx " in low or "uv run" in low):
            found.append(line.strip()[:110])
    return found


def check_wrappers(configs: list[Path]) -> int:
    print("Auditing MCP declarations (nothing is launched)\n")
    problems = 0
    seen = 0
    for path in configs:
        servers = load_servers(path)
        if not servers:
            continue
        for name, spec in servers.items():
            if not isinstance(spec, dict):
                continue
            seen += 1
            wrapper = is_wrapper(spec)
            if wrapper:
                problems += 1
                print(f"  WRAPPER  {name:<14} '{wrapper}' in {path}")
    print(f"\n  checked {seen} declaration(s) in {len(configs)} config(s)")

    live = running_wrappers()
    if live:
        problems += len(live)
        print(f"\n  Wrapper processes currently running ({len(live)}):")
        for line in live[:10]:
            print(f"    {line}")

    if problems:
        print("\n  → see docs/mcp-servers.md for how to replace these")
    else:
        print("  no resolver wrappers found")
    return 1 if problems else 0


def default_configs() -> list[Path]:
    configs = [DEFAULT_CONFIG]
    if PLUGIN_CACHE.is_dir():
        configs += sorted(PLUGIN_CACHE.glob("*/*/*/.mcp.json"))
    return [p for p in configs if p.is_file()]


def main() -> int:
    parser = argparse.ArgumentParser(add_help=True, description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("config", nargs="?", help="path to .mcp.json / .claude.json")
    parser.add_argument("servers", nargs="*", help="probe only these server names")
    parser.add_argument("--check-wrappers", action="store_true",
                        help="audit declarations for npx/uv wrappers, launch nothing")
    args = parser.parse_args()

    if args.check_wrappers:
        configs = [Path(args.config)] if args.config else default_configs()
        return check_wrappers(configs)

    path = Path(args.config) if args.config else DEFAULT_CONFIG
    if not path.is_file():
        print(f"config not found: {path}")
        return 1

    servers = load_servers(path)
    if not servers:
        print(f"no MCP servers declared in {path}")
        return 0

    print(f"Probing {len(servers)} server(s) from {path}\n")
    failures = 0
    for name, spec in servers.items():
        if args.servers and name not in args.servers:
            continue
        if not isinstance(spec, dict):
            continue
        if not probe(name, spec):
            failures += 1
    print()
    print("all good" if not failures else f"{failures} server(s) failed")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
