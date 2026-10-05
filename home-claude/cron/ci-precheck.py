#!/usr/bin/env python3
"""The local CI gate github-push.sh runs before publishing — what CI would catch.

Why: a red CI run on a public repository used to arrive by e-mail a day after
the push, although nearly every such failure is visible locally in seconds: a
shellcheck directive with a note after its codes (SC1125 — the suppression was
silently not applied), a heredoc that overrode a pipe (SC2259 — an alert script
sent empty text), a syntax error in a file no test imports, a `.sh` saved with a
BOM or CRLF.

The gate is NOT a copy of CI: a workflow may run dozens of steps on three
operating systems, and some of that cannot be reproduced on this machine. It
runs the portable subset that catches most of these failures; ci-watch.py
catches the rest after the push, from the real run.

Checks: shellcheck over tracked shell · compileall over tracked Python · the
project's fast test suite · its cron/tests/*.sh · encodings and line endings
(a BOM or CRLF in .sh, a .ps1 with non-ASCII text and no BOM) · which Python
versions of the CI matrix were not tried here.

Autofix (`--fix`) only does what is deterministic: SC1125 directives, a BOM in
.sh, CRLF in .sh. Having fixed something, the gate STOPS the publication (rc 2):
a push sends commits, and the fix sits in the working tree, so the push would
leave without it.

Exit: 0 — clean (a check that could not run is a skip, not a failure), 1 — a
blocking problem, 2 — an autofix was applied and needs a commit.
"""
from __future__ import annotations

import argparse
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

for _stream in (sys.stdout, sys.stderr):
    if hasattr(_stream, "reconfigure"):
        _stream.reconfigure(encoding="utf-8", errors="replace")

TEST_TIMEOUT = int(os.environ.get("CI_PRECHECK_TEST_TIMEOUT", "300"))
BOM = b"\xef\xbb\xbf"

# A note after the codes is read as part of the directive, and shellcheck then
# IGNORES the rest of it (SC1125) — the suppression may not apply at all.
SC_DIRECTIVE = re.compile(
    r"^(?P<indent>\s*)#\s*shellcheck\s+"
    r"(?P<key>disable|source|shell|source-path|external-sources)=(?P<val>\S+)"
    r"[ \t]+(?P<note>\S.*)$"
)


class Result:
    """The outcome of one check: `ok` / `fail` / `skip`. A skip never blocks."""

    def __init__(self, name: str, status: str, detail: str = ""):
        self.name, self.status, self.detail = name, status, detail

    @property
    def blocking(self) -> bool:
        return self.status == "fail"


def git(repo: Path, *args: str) -> str:
    done = subprocess.run(["git", "-C", str(repo), *args], capture_output=True,
                          text=True, encoding="utf-8", errors="replace")
    return done.stdout if done.returncode == 0 else ""


def tracked(repo: Path, *patterns: str) -> list[str]:
    """Tracked paths only: what is not under version control is not published."""
    out: list[str] = []
    for pat in patterns:
        out += [p for p in git(repo, "ls-files", pat).splitlines() if p.strip()]
    return list(dict.fromkeys(out))      # a file under two patterns, once


def shell_files(repo: Path) -> list[str]:
    return tracked(repo, "*.sh", ".githooks/*")


# --- autofixes ---------------------------------------------------------------
# Each returns the paths it changed. They work on bytes: a script with non-ASCII
# comments must not depend on the interpreter's locale.

def fix_sc_directives(repo: Path) -> list[str]:
    """The note goes on the line above; the directive stays clean."""
    changed = []
    for rel in shell_files(repo):
        path = repo / rel
        if not path.is_file():
            continue
        try:
            text = path.read_text(encoding="utf-8")
        except UnicodeDecodeError:
            # Not UTF-8: rewriting with errors="replace" would turn every
            # non-ASCII byte into U+FFFD for good. Left for a human — the
            # re-check after the autofix names it.
            continue
        out, hit = [], False
        for line in text.split("\n"):
            m = SC_DIRECTIVE.match(line)
            if m:
                hit = True
                note = m["note"].lstrip("—-–:‒ ").strip()
                out.append(f"{m['indent']}# {note}")
                out.append(f"{m['indent']}# shellcheck {m['key']}={m['val']}")
            else:
                out.append(line)
        if hit:
            path.write_text("\n".join(out), encoding="utf-8", newline="")
            changed.append(rel)
    return changed


def fix_sh_bom(repo: Path) -> list[str]:
    """A BOM in a .sh breaks its shebang."""
    changed = []
    for rel in tracked(repo, "*.sh"):
        path = repo / rel
        if path.is_file() and (data := path.read_bytes()).startswith(BOM):
            path.write_bytes(data[len(BOM):])
            changed.append(rel)
    return changed


def fix_sh_crlf(repo: Path) -> list[str]:
    """CRLF in a .sh breaks its shebang and gives `$'\\r'` under a Linux bash."""
    changed = []
    for rel in tracked(repo, "*.sh"):
        path = repo / rel
        if path.is_file() and b"\r\n" in (data := path.read_bytes()):
            path.write_bytes(data.replace(b"\r\n", b"\n"))
            changed.append(rel)
    return changed


AUTOFIXES = (
    ("shellcheck directives (SC1125)", fix_sc_directives),
    ("BOM in .sh", fix_sh_bom),
    ("CRLF in .sh", fix_sh_crlf),
)


# --- checks ------------------------------------------------------------------

def check_shellcheck(repo: Path) -> Result:
    files = shell_files(repo)
    if not files:
        return Result("shellcheck", "skip", "no tracked shell files")
    exe = shutil.which("shellcheck")
    if not exe:
        # A missing tool is "not checked", not "the check failed".
        return Result("shellcheck", "skip", "shellcheck not found on PATH")
    done = subprocess.run([exe, "--severity=warning", "-e", "SC1091", *files],
                          cwd=str(repo), capture_output=True, text=True,
                          encoding="utf-8", errors="replace", timeout=600)
    if done.returncode == 0:
        return Result("shellcheck", "ok", f"{len(files)} files")
    return Result("shellcheck", "fail", (done.stdout + done.stderr).strip())


def check_compileall(repo: Path) -> Result:
    files = tracked(repo, "*.py")
    if not files:
        return Result("compileall", "skip", "no tracked Python files")
    done = subprocess.run([sys.executable, "-m", "compileall", "-q", *files],
                          cwd=str(repo), capture_output=True, text=True,
                          encoding="utf-8", errors="replace", timeout=600)
    if done.returncode == 0:
        return Result("compileall", "ok", f"{len(files)} files")
    return Result("compileall", "fail", (done.stdout + done.stderr).strip())


def project_python(repo: Path) -> str:
    """The project's interpreter: its `.venv` when it has one, else the gate's.

    The gate runs under whatever Python github-push.sh found, and a project
    whose dependencies live in its `.venv` failed every test at collection
    (`No module named …`) — a publication blocked for nothing.
    """
    for rel in (".venv/Scripts/python.exe", ".venv/bin/python"):
        if (repo / rel).is_file():
            return str(repo / rel)
    return sys.executable


def _test_command(repo: Path) -> list[str] | None:
    """How this project runs its fast suite. None — nothing to run."""
    py = project_python(repo)
    configs = [repo / n for n in ("pytest.ini", "pyproject.toml", "setup.cfg", "tox.ini")
               if (repo / n).is_file()]
    # A config that declares `testpaths` has already said what the fast suite is,
    # and a bare `pytest` is exactly that. A hard-coded `pytest tests` overrode
    # the decision and ran one of the declared suites only.
    for cfg in configs:
        if re.search(r"^\s*testpaths\s*=", cfg.read_text(encoding="utf-8", errors="replace"),
                     re.M):
            return [py, "-m", "pytest", "-q", "-p", "no:cacheprovider"]
    if configs and (repo / "tests").is_dir():
        return [py, "-m", "pytest", "tests", "-q", "-p", "no:cacheprovider"]
    return None


def check_fast_tests(repo: Path) -> Result:
    cmd = _test_command(repo)
    if cmd is None:
        return Result("fast tests", "skip", "no pytest config or tests/")
    env = dict(os.environ, CI="1")   # parity with CI, where a skipped dependency fails
    try:
        done = subprocess.run(cmd, cwd=str(repo), capture_output=True, text=True,
                              encoding="utf-8", errors="replace", timeout=TEST_TIMEOUT,
                              env=env)
    except subprocess.TimeoutExpired:
        # "Did not finish here in N seconds" is not "red": a suite that hangs on
        # this machine can take seconds in CI. Not checked, so not blocking.
        return Result("fast tests", "skip",
                      f"did not finish in {TEST_TIMEOUT}s — ci-watch checks after the push")
    if done.returncode == 0:
        lines = [ln for ln in done.stdout.splitlines() if ln.strip()]
        return Result("fast tests", "ok", lines[-1].strip() if lines else "")
    tail = "\n".join((done.stdout + done.stderr).strip().splitlines()[-25:])
    return Result("fast tests", "fail", tail)


def find_bash() -> str | None:
    """A bash for the shell tests: BASH_EXE, else on Windows Git's MSYS bash.

    `Git\\usr\\bin\\bash.exe`, not `Git\\bin\\bash.exe`: the latter puts
    /mingw64/bin:/usr/bin at the FRONT of PATH and shadows the stubs the tests
    put there, so the scenarios would quietly reach the real curl or git. Never
    System32's bash.exe — that is the WSL launcher.
    """
    explicit = os.environ.get("BASH_EXE")
    if explicit and os.path.isfile(explicit):
        return explicit
    if os.name != "nt":
        return shutil.which("bash")
    git_exe = shutil.which("git")
    roots = list(Path(git_exe).resolve().parents) if git_exe else []
    roots.append(Path(os.environ.get("PROGRAMFILES", r"C:\Program Files")) / "Git")
    for root in roots:
        candidate = root / "usr" / "bin" / "bash.exe"
        if candidate.is_file():
            return str(candidate)
    return None


def check_shell_tests(repo: Path) -> Result:
    """Run `cron/tests/test_*.sh` — pytest does not collect them.

    Two places, because a repository laid out like this bundle keeps `cron/`
    inside `home-claude/`: a gate that looked only at the top found none and
    skipped every live suite — a check that does not find its tests is no
    better than none.
    """
    tests = sorted(set((repo / "cron" / "tests").glob("test_*.sh"))
                   | set((repo / "home-claude" / "cron" / "tests").glob("test_*.sh")))
    if not tests:
        return Result("shell tests", "skip", "no cron/tests/*.sh")
    bash = find_bash()
    if not bash:
        return Result("shell tests", "skip", "no bash found — CI checks after the push")
    failed = []
    for t in tests:
        try:
            done = subprocess.run([bash, str(t)], cwd=str(repo), capture_output=True,
                                  text=True, encoding="utf-8", errors="replace",
                                  timeout=TEST_TIMEOUT)
        except subprocess.TimeoutExpired:
            failed.append(f"{t.name}: did not finish in {TEST_TIMEOUT}s")
            continue
        if done.returncode != 0:
            tail = "\n".join((done.stdout + done.stderr).strip().splitlines()[-8:])
            failed.append(f"{t.name}:\n{tail}")
    if failed:
        return Result("shell tests", "fail", "\n".join(failed))
    return Result("shell tests", "ok", f"{len(tests)} suites green")


def check_encodings(repo: Path) -> Result:
    """The File Encoding rules of home-claude/CLAUDE.md, as this bundle's CI
    checks them."""
    bad = []
    for rel in tracked(repo, "*.sh"):
        path = repo / rel
        if not path.is_file():
            continue
        data = path.read_bytes()
        if data.startswith(BOM):
            bad.append(f"{rel}: a BOM in .sh breaks the shebang (autofix: --fix)")
        if b"\r\n" in data:
            bad.append(f"{rel}: CRLF in .sh breaks the shebang under Linux (autofix: --fix)")
    for rel in tracked(repo, "*.ps1"):
        path = repo / rel
        if path.is_file() and not (data := path.read_bytes()).startswith(BOM) \
                and any(b > 0x7F for b in data):
            bad.append(f"{rel}: non-ASCII without a BOM — PowerShell 5.1 reads it as ANSI")
    if bad:
        return Result("encodings", "fail", "\n".join(bad))
    return Result("encodings", "ok", "")


PY_MATRIX = re.compile(r"python(?:-version)?s?:\s*\[(?P<list>[^\]]+)\]")


def check_python_matrix(repo: Path) -> Result:
    """Say which Python versions of the CI matrix were NOT tried here.

    Never blocking — a statement of the gate's limits. A 3.12-only keyword
    argument passes a local run on 3.14 and fails CI's 3.10 job; knowing about
    that gap is cheaper than learning it from the e-mail.
    """
    wf = repo / ".github" / "workflows"
    if not wf.is_dir():
        return Result("python matrix", "skip", "no workflows")
    versions: set[str] = set()
    for path in sorted(wf.glob("*.y*ml")):
        for m in PY_MATRIX.finditer(path.read_text(encoding="utf-8", errors="replace")):
            for raw in m["list"].split(","):
                v = raw.strip().strip("\"'")
                if re.fullmatch(r"3\.\d+", v):      # "3.x" / pypy are not versions
                    versions.add(v)
    if not versions:
        return Result("python matrix", "skip", "the matrix declares no versions")
    # The version the tests ran under — for a project with a .venv, not the gate's.
    local = subprocess.run([project_python(repo), "-c",
                            "import sys; print('%d.%d' % sys.version_info[:2])"],
                           capture_output=True, text=True, timeout=60).stdout.strip()
    missing = sorted(v for v in versions if v != local)
    if not missing:
        return Result("python matrix", "ok", f"the local {local} covers the matrix")
    return Result("python matrix", "skip",
                  f"CI also runs {', '.join(missing)}; only {local} here — "
                  "version differences are ci-watch's to catch")


CHECKS = (check_shellcheck, check_compileall, check_fast_tests, check_shell_tests,
          check_encodings, check_python_matrix)


def run_checks(repo: Path) -> list[Result]:
    return [c(repo) for c in CHECKS]


def main() -> int:
    ap = argparse.ArgumentParser(description="the local CI gate before a publication")
    ap.add_argument("repo", nargs="?", default=".", help="path to the repository")
    ap.add_argument("--fix", action="store_true",
                    help="fix what is mechanical, then stop (rc 2)")
    args = ap.parse_args()

    repo = Path(args.repo).resolve()
    if not (repo / ".git").exists():
        print(f"ci-precheck: not a git repository: {repo}", file=sys.stderr)
        return 1

    results = run_checks(repo)
    for r in results:
        mark = {"ok": "OK  ", "fail": "FAIL", "skip": "skip"}[r.status]
        if r.status == "fail":
            print(f"  [{mark}] {r.name}")
            for line in r.detail.splitlines():
                print(f"         {line}")
        else:
            print(f"  [{mark}] {r.name}" + (f" — {r.detail}" if r.detail else ""))

    if not any(r.blocking for r in results):
        return 0
    if not args.fix:
        print("\nBLOCKED: the local CI checks failed. Mechanical fixes: ci-precheck.py --fix")
        return 1

    fixed: list[str] = []
    for label, fn in AUTOFIXES:
        if changed := fn(repo):
            fixed.append(f"{label}: {', '.join(changed)}")
    if not fixed:
        print("\nBLOCKED: nothing found here can be fixed automatically — fix it by hand.")
        return 1
    print("\nAutofix applied:")
    for line in fixed:
        print(f"  {line}")
    still = [r for r in run_checks(repo) if r.blocking]
    if still:
        print("\nBLOCKED: still failing after the autofix:")
        for r in still:
            print(f"  [{r.name}]")
            for line in r.detail.splitlines():
                print(f"      {line}")
        return 1
    print("\nThe checks pass now. The publication is STOPPED on purpose: the fixes "
          "sit in the working tree and a push sends commits — review the diff, "
          "commit, and publish again.")
    return 2


if __name__ == "__main__":
    sys.exit(main())
