#!/usr/bin/env python3
"""CLI wrapper around cron/hooks/utils.llm_call().

Reads the prompt from stdin, dispatches to the configured LLM provider
(see WIKI_LLM_PROVIDER), prints the answer to stdout. Used by .sh scripts
in place of `claude -p`.

Usage:
  echo "prompt" | python llm-call.py [timeout_seconds]
  cat prompt.md | python llm-call.py 600

Exit codes:
  0 — success (answer printed to stdout)
  1 — empty answer or LLM error
  2 — empty stdin
"""
import sys
from pathlib import Path

if sys.platform == "win32":
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stderr.reconfigure(encoding="utf-8")
    sys.stdin.reconfigure(encoding="utf-8")

sys.path.insert(0, str(Path(__file__).resolve().parent / "hooks"))
from utils import llm_call  # noqa: E402

timeout = int(sys.argv[1]) if len(sys.argv) > 1 else 600
prompt = sys.stdin.read()

if not prompt.strip():
    print("ERROR: empty stdin", file=sys.stderr)
    sys.exit(2)

out = llm_call(prompt, timeout=timeout)
if out is None:
    print("ERROR: llm_call returned None (see stderr)", file=sys.stderr)
    sys.exit(1)

sys.stdout.write(out)
if not out.endswith("\n"):
    sys.stdout.write("\n")
