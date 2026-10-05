#!/usr/bin/env python3
"""Wait for the GitHub Actions run of a commit github-push.sh just published.

Why: the local gate (ci-precheck.py) is portable, not equivalent — another OS, a
fresh machine, other versions. A test that silently depends on the machine (its
uptime, its locale, a tool that happens to be installed) is green here and red
on the runner, and without this step the red arrived by e-mail a day later.

The token: GITHUB_TOKEN from the environment, else that one key from the
bundle's .env (read with cron/lib/vault_values.py — the .env is never loaded
into the environment of the project whose tests just ran). A public repository
can be watched without one, at the anonymous API rate; a private one cannot.

A failed job's log is served as a redirect to a signed blob URL, and the
Authorization header must NOT follow it there (the storage answers 401), so the
redirect is handled by hand.

Exit: 0 — the run is green, or there is none to wait for (no workflows, no
github remote, Actions never ran in this repository); 1 — red, or it did not
finish in time.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

for _stream in (sys.stdout, sys.stderr):
    if hasattr(_stream, "reconfigure"):
        _stream.reconfigure(encoding="utf-8", errors="replace")

sys.path.insert(0, str(Path(__file__).resolve().parent / "lib"))
import vault_values  # noqa: E402

DEFAULT_WAIT = int(os.environ.get("CI_WATCH_TIMEOUT", "900"))
# How long to wait for the FIRST run of a repository that has never had one. A
# fork does not run Actions until they are switched on in its Actions tab, while
# the API still lists its workflows as active: the only sign is an empty history.
NO_HISTORY_GRACE = 90
LOG_TAIL_LINES = 25


def token() -> str | None:
    value = os.environ.get("GITHUB_TOKEN", "").strip()
    if value:
        return value
    try:
        env = vault_values.read_env(vault_values.vault_path().read_text(
            encoding="utf-8-sig", errors="replace"))
    except OSError:
        return None
    return (env.get("GITHUB_TOKEN") or "").strip() or None


def poll_seconds(tok: str | None) -> int:
    """Anonymous calls get 60 an hour: half the rate keeps a 15-minute wait in it."""
    return 15 if tok else 30


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise urllib.error.HTTPError(newurl, code, msg, headers, fp)


def api(path: str, tok: str | None, raw: bool = False):
    headers = {"Accept": "application/vnd.github+json", "User-Agent": "claude-bundle-ci-watch"}
    if tok:
        headers["Authorization"] = "Bearer " + tok
    req = urllib.request.Request("https://api.github.com" + path, headers=headers)
    try:
        with urllib.request.build_opener(NoRedirect).open(req, timeout=60) as r:
            data = r.read()
    except urllib.error.HTTPError as e:
        loc = e.headers.get("Location") if e.code in (301, 302, 303, 307, 308) else None
        if not loc:
            raise
        # The signed blob URL: no Authorization there, or it answers 401.
        plain = urllib.request.Request(loc, headers={"User-Agent": "claude-bundle-ci-watch"})
        with urllib.request.urlopen(plain, timeout=120) as r:
            data = r.read()
    return data if raw else json.loads(data)


def slug(repo: Path) -> str | None:
    """owner/name of the `github` remote, if it is on github.com."""
    done = subprocess.run(["git", "-C", str(repo), "remote", "get-url", "github"],
                          capture_output=True, text=True, encoding="utf-8", errors="replace")
    if done.returncode != 0:
        return None
    m = re.search(r"github\.com[:/]+([^/]+)/(.+?)(?:\.git)?\s*$", done.stdout)
    return f"{m.group(1)}/{m.group(2)}" if m else None


def head_sha(repo: Path) -> str:
    done = subprocess.run(["git", "-C", str(repo), "rev-parse", "HEAD"],
                          capture_output=True, text=True, encoding="utf-8", errors="replace")
    return done.stdout.strip()


def has_workflows(repo: Path) -> bool:
    wf = repo / ".github" / "workflows"
    return wf.is_dir() and any(wf.glob("*.y*ml"))


def failed_job_logs(slugname: str, run_id: int, tok: str | None) -> list[tuple[str, str]]:
    out = []
    # A failed job list (network, a 403 rate limit) is a line in the report, not
    # a traceback — which would print nothing about the other red runs exactly
    # when the report is needed.
    try:
        jobs = api(f"/repos/{slugname}/actions/runs/{run_id}/jobs?per_page=50", tok)["jobs"]
    except Exception as e:                           # noqa: BLE001 — the log is a bonus
        return [("(job list unavailable)", f"(log not fetched: {e})")]
    for j in jobs:
        if j.get("conclusion") == "success":
            continue
        try:
            text = api(f"/repos/{slugname}/actions/jobs/{j['id']}/logs", tok,
                       raw=True).decode("utf-8", errors="replace")
            # The tail: the failure and the exit code are at the end.
            lines = [ln.rstrip() for ln in text.splitlines() if ln.strip()]
            body = "\n".join(lines[-LOG_TAIL_LINES:])
        except Exception as e:                       # noqa: BLE001 — the log is a bonus
            body = f"(log not fetched: {e})"
        out.append((f"{j['name']} [{j.get('conclusion')}]", body))
    return out


def main() -> int:
    ap = argparse.ArgumentParser(description="wait for the CI run of HEAD")
    ap.add_argument("repo", nargs="?", default=".")
    ap.add_argument("--sha", default=None, help="the commit (default: HEAD)")
    ap.add_argument("--timeout", type=int, default=DEFAULT_WAIT)
    args = ap.parse_args()

    repo = Path(args.repo).resolve()
    if not has_workflows(repo):
        print("  ci-watch: no .github/workflows in the repository — nothing to wait for")
        return 0
    slugname = slug(repo)
    if not slugname:
        print("  ci-watch: no `github` remote on github.com — nothing to wait for",
              file=sys.stderr)
        return 0
    tok = token()
    poll = poll_seconds(tok)

    sha = args.sha or head_sha(repo)
    print(f"  ci-watch: waiting for the run of {slugname} @ {sha[:8]} (up to {args.timeout}s)")
    deadline = time.time() + args.timeout
    # No history: wait for a first run only briefly. A run for a fresh push can
    # register late, but 900 s of waiting for one that will not come, then "CI is
    # red", is false. A failed request: wait as usual.
    grace_until = None
    try:
        if api(f"/repos/{slugname}/actions/runs?per_page=1", tok)["total_count"] == 0:
            grace_until = time.time() + NO_HISTORY_GRACE
    except urllib.error.HTTPError as e:
        if e.code in (401, 403, 404) and not tok:
            print(f"  ci-watch: the runs of {slugname} are not visible without a token "
                  f"(HTTP {e.code}) — set GITHUB_TOKEN; not waiting")
            return 0
        print(f"  ci-watch: the run history did not load ({e}) — waiting as usual",
              file=sys.stderr)
    except Exception as e:                           # noqa: BLE001 — the check is optional
        print(f"  ci-watch: the run history did not load ({e}) — waiting as usual",
              file=sys.stderr)
    finished = None
    # Runs register asynchronously after a push: on a partial list `all
    # (completed)` holds before the second workflow's run appears. So wait until
    # the set of runs is the same on two polls in a row — otherwise this would
    # report green in exactly the case it exists for.
    prev_ids = None
    while time.time() < deadline:
        try:
            runs = api(f"/repos/{slugname}/actions/runs?per_page=20&head_sha={sha}",
                       tok)["workflow_runs"]
        except Exception as e:                       # noqa: BLE001 — network / limits
            print(f"  ci-watch: the request failed ({e}) — retrying", file=sys.stderr)
            runs = None
        if runs == [] and grace_until is not None and time.time() >= grace_until:
            print(f"  ci-watch: Actions do not run in {slugname} — not one run in its "
                  f"history; a fork switches them on in https://github.com/{slugname}/actions. "
                  f"Nothing to wait for: CI not checked.")
            return 0
        runs = runs or []
        ids = {r["id"] for r in runs}
        if runs and ids == prev_ids and all(r["status"] == "completed" for r in runs):
            finished = runs
            break
        prev_ids = ids
        time.sleep(poll)

    if finished is None:
        print(f"  ci-watch: the run did not finish in {args.timeout}s — "
              f"see https://github.com/{slugname}/actions")
        return 1

    bad = [r for r in finished if r["conclusion"] not in ("success", "neutral", "skipped")]
    for r in finished:
        print(f"  ci-watch: {r['name']} — {r['conclusion']}")
    if not bad:
        return 0
    for r in bad:
        print(f"\n  CI RED: {r['name']} — {r['html_url']}")
        for name, body in failed_job_logs(slugname, r["id"], tok):
            print(f"    --- {name}")
            for line in body.splitlines():
                print(f"        {line}")
    return 1


if __name__ == "__main__":
    sys.exit(main())
