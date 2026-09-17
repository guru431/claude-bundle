#!/usr/bin/env python3
"""CLI wrapper around cron/hooks/utils.llm_call_ex().

Reads the prompt from stdin, dispatches to the configured LLM provider
(see WIKI_LLM_PROVIDER), prints the answer to stdout. Used by .sh scripts
in place of `claude -p`.

Usage:
  echo "prompt" | python llm-call.py [timeout_seconds]
  cat prompt.md | python llm-call.py 600

Exit codes — the LLMResult kind, so a shell task can tell "no LLM tonight" from
"your key is wrong" without parsing stderr, where the reason is printed:
  0 — success (answer printed to stdout)
  1 — deterministic: an empty or unusable answer, or a 400/413/415/422
  2 — usage: empty stdin, or a timeout that is not a positive whole number
  3 — transient: network, 408, 429/529, 5xx — waiting fixes it
  4 — config: no key or model, a refusal by a gate, a redirect not followed,
      401/402/403/404 and every other 4xx — it will not fix itself
"""
import sys
from pathlib import Path

if sys.platform == "win32":
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stderr.reconfigure(encoding="utf-8")
    sys.stdin.reconfigure(encoding="utf-8")

sys.path.insert(0, str(Path(__file__).resolve().parent / "hooks"))
from utils import llm_call_ex  # noqa: E402

try:
    timeout = int(sys.argv[1]) if len(sys.argv) > 1 else 600
except ValueError:
    timeout = 0
if timeout <= 0:
    print(f"ERROR: timeout must be a positive number of seconds, got {sys.argv[1]!r}",
          file=sys.stderr)
    sys.exit(2)
prompt = sys.stdin.read()

if not prompt.strip():
    print("ERROR: empty stdin", file=sys.stderr)
    sys.exit(2)

res = llm_call_ex(prompt, timeout=timeout)
if not res.ok:
    print(f"ERROR: no answer — {res.kind}: {res.detail or 'no detail'}", file=sys.stderr)
    sys.exit({"transient": 3, "config": 4}.get(res.kind, 1))

sys.stdout.write(res.text)
if not res.text.endswith("\n"):
    sys.stdout.write("\n")
