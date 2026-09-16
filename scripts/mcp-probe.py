#!/usr/bin/env python3
"""Check MCP server declarations — by handshake, not by "the process started".

    python tools/mcp-probe.py                     # probe servers in ~/.claude.json
    python tools/mcp-probe.py path/to/.mcp.json   # probe a specific config
    python tools/mcp-probe.py CONFIG server-name  # probe one server
    python tools/mcp-probe.py --check-wrappers    # audit declarations, launch nothing

Probing launches each declared stdio server exactly as configured, sends `initialize`
and `tools/list`, and reports stray stdout separately: MCP speaks JSON-RPC over stdout,
so a single banner line there breaks the session while the process still looks healthy.

`--check-wrappers` launches nothing. It flags resolver wrappers (`npx -y`, `uv run`, also
behind a shell such as `cmd /c` or `sh -c`) in every config it can find, including
plugin-provided ones, and looks for wrapper processes already running. See
docs/mcp-servers.md for why those are worth removing.

Exit code is 1 if anything failed or a wrapper was found, so this can gate a script.
"""
from __future__ import annotations

import argparse
import json
import os
import queue
import re
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

HOME = Path.home()
DEFAULT_CONFIG = HOME / ".claude.json"
PLUGIN_CACHE = HOME / ".claude" / "plugins" / "cache"

# A server is "wrapped" when the command resolves the package instead of running it.
WRAPPERS = ("npx", "npm", "pnpm", "yarn", "bunx", "uv", "uvx", "pipx")
# Shells that run a command line given to them — so the wrapper can hide one level down.
SHELLS = ("cmd", "powershell", "pwsh", "sh", "bash", "zsh", "dash")
HTTP_TYPES = ("http", "sse", "streamable-http")

INITIALIZE = {
    "jsonrpc": "2.0", "id": 1, "method": "initialize",
    "params": {"protocolVersion": "2025-06-18", "capabilities": {},
               "clientInfo": {"name": "mcp-probe", "version": "1"}},
}
TOOLS_LIST = {"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}}

# How long a server gets to exit by itself once its stdin is closed — the MCP
# stdio shutdown sequence — before it is killed. Closing stdin first matters for
# wrapped servers: `npx` does not forward a kill to the node process it started,
# but that process does see EOF and exit, instead of outliving the probe.
SHUTDOWN_GRACE = 2.0


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


def _stem(token: str) -> str:
    """`C:\\nodejs\\npx.cmd` -> `npx`, on any OS (Path splits `\\` only on Windows)."""
    base = re.split(r"[\\/]", token.strip().strip("\"'"))[-1].lower()
    for suffix in (".exe", ".cmd", ".bat", ".ps1"):
        base = base.removesuffix(suffix)
    return base


def _shell_run_flag(shell: str, arg: str) -> bool:
    """True for the flag after which `shell` runs its remaining arguments."""
    arg = arg.lower()
    if shell == "cmd":
        return arg in ("/c", "/k", "/r")
    if shell in ("powershell", "pwsh"):
        # PowerShell takes any unambiguous prefix of -Command; -c is the usual one.
        return len(arg) > 1 and arg[0] in "-/" and "command".startswith(arg[1:])
    return re.fullmatch(r"-[a-z]*c[a-z]*", arg) is not None      # sh -c, bash -lc


def is_wrapper(spec: dict) -> str | None:
    command = (spec.get("command") or "").strip()
    if not command:
        return None
    if _stem(command) in WRAPPERS:
        return command
    # A shell that runs the wrapper for you. `cmd /c npx -y pkg` is the form
    # Claude Code's own docs give for Windows, and looking at `command` alone
    # called it clean — so `--check-wrappers` (and self-test on top of it)
    # reported "no resolver wrappers" for exactly the declaration it exists for.
    shell = _stem(command)
    if shell not in SHELLS:
        return None
    args = [str(a) for a in spec.get("args") or []]
    run_at = next((i for i, a in enumerate(args) if _shell_run_flag(shell, a)), None)
    if run_at is None:
        return None
    # The program is the first word of the command line, past what may precede
    # it: `exec`, `call`, PowerShell's `&` and `VAR=value` assignments.
    words = [w.lstrip("&") for w in " ".join(args[run_at + 1:]).split()]
    program = next((w for w in words if w and w.lower() not in ("exec", "call")
                    and not re.match(r"\w+=", w)), "")
    return f"{command} {args[run_at]} {program}" if _stem(program) in WRAPPERS else None


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

    # stderr goes to a file, not a pipe. Nothing read the pipe until the
    # handshake had already failed, so a server that logged more than the pipe
    # buffer blocked on its own stderr and never answered — a deadlock that no
    # timeout could break, because the probe was blocked too.
    errlog = tempfile.TemporaryFile()
    try:
        proc = subprocess.Popen(
            command, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
            stderr=errlog, env=env)
    except OSError as exc:
        errlog.close()
        print(f"{name:<14} FAIL — cannot start: {exc}")
        return False

    # readline() has no timeout, and Windows has no select() on a pipe. The
    # deadline used to be checked only BETWEEN lines, so a server that read
    # stdin and never wrote a byte hung the probe forever. The blocking read
    # lives on this thread now, and the deadline is enforced on the queue.
    lines: queue.Queue = queue.Queue()

    def pump() -> None:
        for raw in proc.stdout:
            lines.put(raw.decode("utf-8", errors="replace"))
        lines.put(None)                    # EOF: the server closed stdout

    reader = threading.Thread(target=pump, daemon=True)
    reader.start()

    def send(*messages: dict) -> bool:
        """False when the server is already gone (a broken pipe, or EINVAL on
        Windows) — which used to escape as an exception and end the whole run."""
        try:
            for message in messages:
                proc.stdin.write((json.dumps(message) + "\n").encode("utf-8"))
            proc.stdin.flush()
            return True
        except OSError:
            return False

    def read_reply(want_id: int, deadline: float, junk: list[str]):
        while True:
            try:
                line = lines.get(timeout=max(0.0, deadline - time.monotonic()))
            except queue.Empty:
                return None
            if line is None:
                lines.put(None)            # stays EOF for the next read too
                return None
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

    def stderr_text() -> str:
        # Read only after shutdown(): the child writes through a handle that
        # shares this file's position, so reading while it runs could interleave.
        errlog.seek(0)
        return errlog.read().decode("utf-8", errors="replace")[:200].replace("\n", " ")

    def shutdown() -> None:
        """Stop the server the way an MCP client does, and always reap it."""
        try:
            proc.stdin.close()             # EOF is stdio's "please exit"
        except OSError:
            pass
        try:
            proc.wait(timeout=SHUTDOWN_GRACE)
        except subprocess.TimeoutExpired:
            proc.kill()
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                pass                       # unkillable; nothing more to do here
        # A grandchild that inherited stdout can keep the pipe open after its
        # parent is gone; closing it under a blocked reader would block us too.
        reader.join(timeout=1)
        if not reader.is_alive():
            proc.stdout.close()

    ok = False
    try:
        junk: list[str] = []
        reply = None
        if send(INITIALIZE):
            reply = read_reply(1, time.monotonic() + timeout, junk)

        if junk:
            print(f"{name:<14} FAIL — {len(junk)} non-JSON line(s) on stdout, "
                  f"this breaks JSON-RPC: {junk[0][:60]!r}")
            return False
        if reply is None:
            shutdown()
            print(f"{name:<14} FAIL — no reply to initialize. stderr: {stderr_text()}")
            return False

        info = (reply.get("result") or {}).get("serverInfo") or {}
        label = f"{info.get('name', '?')} {info.get('version', '')}".strip()

        tools_reply = None
        if send({"jsonrpc": "2.0", "method": "notifications/initialized"}, TOOLS_LIST):
            tools_reply = read_reply(2, time.monotonic() + timeout, junk)
        count = len((tools_reply.get("result") or {}).get("tools") or []) if tools_reply else None

        if count is None:
            print(f"{name:<14} OK initialize ({label}), but tools/list timed out")
        else:
            print(f"{name:<14} OK — {label}, {count} tool(s)")
        ok = True
    finally:
        if proc.returncode is None:
            shutdown()
        errlog.close()
    return ok


def running_wrappers() -> list[str]:
    """Wrapper processes already running, if the platform lets us look cheaply.

    `wmic` is REMOVED from Windows 11 24H2 and Server 2025. It used to be the
    only Windows path here, and an OSError was swallowed into an empty list —
    so on a current Windows the audit printed a confident "no resolver wrappers
    running" whether or not any were. Three attempts now, and a failure of all
    three says so instead of reading as a clean result.
    """
    found = []
    out = ""
    if os.name == "nt":
        attempts = [
            ["powershell", "-NoProfile", "-NonInteractive", "-Command",
             "Get-CimInstance Win32_Process | ForEach-Object { $_.CommandLine }"],
            ["tasklist", "/v", "/fo", "csv"],
            ["wmic", "process", "get", "commandline"],
        ]
    else:
        attempts = [["ps", "-eo", "args"]]
    for cmd in attempts:
        try:
            res = subprocess.run(cmd, capture_output=True, text=True, timeout=60,
                                 errors="replace")
        except (OSError, subprocess.SubprocessError):
            continue
        if res.returncode == 0 and res.stdout.strip():
            out = res.stdout
            break
    if not out:
        print("  WARN: could not enumerate running processes "
              f"({' / '.join(c[0] for c in attempts)} all failed) — "
              "'no resolver wrappers' below is NOT a verified result",
              file=sys.stderr)
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
