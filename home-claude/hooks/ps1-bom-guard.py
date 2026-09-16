#!/usr/bin/env python3
"""PostToolUse Write|Edit hook — kept under this name for existing settings.json.

This file used to BE the hook, and it only knew `.ps1`. The rules now live in one
table in `text-encoding-guard.py` next to it (`.ps1` needs a BOM, `.sh` must not
have one and must use LF), and a configuration that still names this file gets
exactly what a new one gets: this runs that.

Opt-in — see home-claude/settings.example-with-hooks.json.
"""
import json
import os
import runpy
import sys

GUARD = os.path.join(os.path.dirname(os.path.abspath(__file__)), "text-encoding-guard.py")

if __name__ == "__main__":
    if not os.path.isfile(GUARD):
        # A hooks/ directory copied only in part: a no-op, not a traceback.
        print(json.dumps({"suppressOutput": True}))
        sys.exit(0)
    runpy.run_path(GUARD, run_name="__main__")
