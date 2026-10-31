"""Execute the CI shell gates with failing scanners and unusual tracked paths."""
from __future__ import annotations

import os
import shlex
import subprocess
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parent.parent
WORKFLOW = yaml.safe_load((ROOT / ".github" / "workflows" / "ci.yml").read_text(encoding="utf-8"))


def _step(name: str) -> str:
    return next(step["run"] for job in WORKFLOW["jobs"].values() for step in job["steps"]
                if step.get("name", "").startswith(name))


def _run(bash: str, tmp_path: Path, script: str) -> subprocess.CompletedProcess:
    path = tmp_path / "gate.sh"
    path.write_text(script, encoding="utf-8", newline="\n")
    return subprocess.run([bash, "-e", "-o", "pipefail", str(path)], cwd=tmp_path,
                          capture_output=True, text=True, encoding="utf-8", errors="replace",
                          timeout=10)


@pytest.mark.parametrize("failure", ["git", "allow", "placeholder"])
def test_ci_secret_guard_blocks_a_failed_scanner(tmp_path, bash, failure):
    lib = tmp_path / "home-claude" / "cron" / "lib"
    lib.mkdir(parents=True)
    # The detector's actual regex is irrelevant to an execution failure. A
    # fixture library isolates each pipeline stage without printing credentials.
    (lib / "secret-scan.sh").write_text(
        'SECRET_SCAN_PATTERN=needle\nSECRET_SCAN_ALLOW=fixture-allow\n'
        + ('secret_scan_drop_placeholders() { return 7; }\n' if failure == "placeholder"
           else 'secret_scan_drop_placeholders() { cat; }\n'), encoding="utf-8")
    prefix = 'git() { return ' + ('2' if failure == "git" else '1') + '; }\n'
    if failure == "allow":
        prefix += 'grep() { return 2; }\n'
    result = _run(bash, tmp_path, prefix + _step("Secret guard"))
    assert result.returncode != 0, result.stdout + result.stderr
    assert "secret guard: clean" not in result.stdout


@pytest.mark.parametrize("case", ["clean", "secret", "allow", "placeholder"])
def test_ci_secret_guard_keeps_the_content_verdict(tmp_path, bash, case):
    lib = tmp_path / "home-claude" / "cron" / "lib"
    lib.mkdir(parents=True)
    (lib / "secret-scan.sh").write_bytes(
        (ROOT / "home-claude" / "cron" / "lib" / "secret-scan.sh").read_bytes())
    token = "ghp_" + "A" * 30
    line = {"clean": "", "secret": "probe.py:1:" + token,
            "allow": "probe.py:1:" + token + " # secret-scan:allow",
            "placeholder": "probe.md:1:postgres://user:password@localhost"}[case]
    prefix = 'git() { printf "%s" ' + shlex.quote(line) + '; return ' + (
        "0" if line else "1") + '; }\n'
    result = _run(bash, tmp_path, prefix + _step("Secret guard"))
    assert (result.returncode != 0) == (case == "secret"), result.stdout + result.stderr


def test_ci_shellcheck_keeps_spaces_and_newlines_in_paths(tmp_path, bash):
    expected = ["nested/a script.sh", "nested/line\nbreak.sh", ".githooks/pre-commit"]
    # Feed the exact bytes git ls-files -z returns. The mock also supports the
    # old line-based commands so this checks the regression before the fix.
    listing = "\n".join(shlex.quote(name) for name in expected)
    checker = tmp_path / "shellcheck"
    checker.write_text(
        '#!/usr/bin/env bash\n'
        'while [ "$#" -gt 0 ]; do\n'
        '  case "$1" in\n'
        '    -e|-f) shift 2;;\n'
        '    -*) shift;;\n'
        '    *) printf "%s\\0" "$1" >> checked.bin; shift;;\n'
        '  esac\n'
        'done\n', encoding="utf-8", newline="\n")
    checker.chmod(0o755)
    prefix = (
        'export PATH=' + shlex.quote(
            '/' + tmp_path.drive[0].lower() + tmp_path.as_posix()[2:]
            if os.name == "nt" else tmp_path.as_posix()) + ':"$PATH"\n'
        "git() {\n"
        "  case \" $* \" in\n"
        "    *' -z '*) printf '%s\\0' " + " ".join(shlex.quote(name) for name in expected) + ";;\n"
        "    *'.githooks/'*) printf '%s\\n' '.githooks/pre-commit';;\n"
        "    *) printf '%s\\n' " + " ".join(shlex.quote(name) for name in expected[:2]) + ";;\n"
        "  esac\n}\n")
    result = _run(bash, tmp_path, prefix + _step("ShellCheck"))
    assert result.returncode == 0, result.stdout + result.stderr
    checked = (tmp_path / "checked.bin").read_bytes().split(b"\0")[:-1]
    assert checked == [name.encode() for name in expected], listing


@pytest.mark.parametrize("preview", [False, True])
def test_push_guard_blocks_a_silent_binary_scan_failure(tmp_path, bash, preview):
    script = ROOT / "home-claude" / "cron" / "git-push-all.sh"
    log = tmp_path / "guard.log"
    prefix = (
        'GIT_PUSH_ALL_LIB=1 source ' + shlex.quote(script.as_posix()) + '\n'
        'LOG_FILE=' + shlex.quote(log.as_posix()) + '\n'
        'BUNDLE_ROOT=' + shlex.quote(tmp_path.as_posix()) + '\n'
        'git() { return 0; }\n'
        'secret_scan_diff() { cat >/dev/null; }\n'
        'secret_scan_changed_binaries() { return 7; }\n'
        'secret_scan_git_paths() { return 0; }\n')
    call = 'set +e\n' + ('guard_secrets_preview probe\n' if preview else 'guard_secrets probe\n')
    result = _run(bash, tmp_path, prefix + call)
    if preview:
        assert result.returncode == 0, result.stdout + result.stderr
    else:
        assert result.returncode != 0, result.stdout + result.stderr
    assert log.is_file() and "binary scan failed" in log.read_text(encoding="utf-8")
