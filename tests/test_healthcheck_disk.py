"""claude-healthcheck.sh: the disk check pages on free space, not on the share used.

An 85%-used default paged every morning on a development box whose build caches
keep the system drive at 85-100%, so the morning the disk really filled read
like the thirty before it. What decides now is the free space left on the
TIGHTEST local filesystem (HEALTHCHECK_DISK_FREE_GB, default 5); a percent pages
too, but only when HEALTHCHECK_DISK_PCT is set.

The measuring block is taken from the script itself, not copied here, and `df`
is a shell function — no real disk is read.
"""
from __future__ import annotations

import os
import re
import subprocess
from pathlib import Path

import pytest

from test_healthcheck import SCRIPT, _stub, registry, run_healthcheck

GB = 1048576  # KB


def _block() -> str:
    text = SCRIPT.read_text(encoding="utf-8")
    exclude = re.search(r"^HEALTHCHECK_DISK_EXCLUDE=.*$", text, re.M)
    block = re.search(r"^# --- Deterministic severity: free space on the tightest local "
                      r"filesystem ---\n(.*?)^# --- Optional: remote Linux server", text,
                      re.M | re.S)
    assert exclude and block, "the disk block moved — update this test"
    return exclude.group(0) + "\n" + block.group(1)


def measure(bash: str, tmp_path: Path, df_rows: list[tuple], **env) -> dict:
    """Run the block over a fake `df -P -l`; return its variables and the log.

    Rows: (filesystem, size_kb, used_kb, free_kb, pct, mount) — the POSIX columns.
    """
    lines = ["Filesystem 1024-blocks Used Available Capacity Mounted on"]
    lines += [f"{fs} {size} {used} {free} {pct}% {mount}"
              for fs, size, used, free, pct, mount in df_rows]
    (tmp_path / "df.txt").write_text("\n".join(lines) + "\n", encoding="utf-8")
    log = tmp_path / "hc.log"
    script = (f'LOG_FILE="{log.as_posix()}"\n'
              f'df() {{ cat "{(tmp_path / "df.txt").as_posix()}"; }}\n'
              + _block() +
              '\nfor v in MIN_FREE_KB MIN_FREE_FS MIN_FREE_PCT MAX_DISK_PCT MAX_DISK_FS '
              'DISK_FREE_MIN_GB DISK_THRESHOLD; do printf "%s=%s\\n" "$v" "${!v}"; done\n')
    full_env = {k: v for k, v in os.environ.items() if not k.startswith("HEALTHCHECK_")}
    full_env.update(env)
    res = subprocess.run([bash, "-c", script], capture_output=True, text=True,
                         env=full_env, timeout=20)
    assert res.returncode == 0, res.stderr
    out = dict(line.split("=", 1) for line in res.stdout.splitlines() if "=" in line)
    out["log"] = log.read_text(encoding="utf-8") if log.exists() else ""
    return out


def test_the_tightest_filesystem_decides_not_the_fullest(bash, tmp_path):
    """A busy-but-roomy drive is the fullest; the small one is the tight one."""
    m = measure(bash, tmp_path, [
        ("/dev/sda1", 2000 * GB, 1900 * GB, 100 * GB, 95, "/"),
        ("/dev/sdb1", 20 * GB, 17 * GB, 3 * GB, 85, "/data"),
    ])
    assert (m["MIN_FREE_FS"], int(m["MIN_FREE_KB"]), m["MIN_FREE_PCT"]) == ("/data", 3 * GB, "85")
    assert (m["MAX_DISK_PCT"], m["MAX_DISK_FS"]) == ("95", "/")
    assert m["DISK_FREE_MIN_GB"] == "5" and m["DISK_THRESHOLD"] == "", \
        "default: 5 GB free, no percent threshold"


def test_names_with_spaces_are_measured_not_dropped(bash, tmp_path):
    """Git Bash lists `C:/Program Files/Git`; macOS mounts `/Volumes/My Disk`.

    Counted from the left, either shifted the columns, and the row was dropped
    without a word — Git Bash's `/` was never checked.
    """
    m = measure(bash, tmp_path, [
        ("C:/Program Files/Git", 500 * GB, 498 * GB, 2 * GB, 99, "/"),
        ("/dev/disk3s1", 100 * GB, 10 * GB, 90 * GB, 10, "/Volumes/My Disk"),
    ])
    assert (m["MIN_FREE_FS"], int(m["MIN_FREE_KB"])) == ("/", 2 * GB)
    m = measure(bash, tmp_path, [("/dev/disk3s1", 100 * GB, 99 * GB, 1 * GB, 99, "/Volumes/My Disk")])
    assert m["MIN_FREE_FS"] == "/Volumes/My Disk"


def test_pseudo_filesystems_stay_excluded(bash, tmp_path):
    """`/snap/*` squashfs images are 100% full and have 0 free by design."""
    m = measure(bash, tmp_path, [
        ("/dev/loop0", 64000, 64000, 0, 100, "/snap/core/123"),
        ("/dev/sda1", 200 * GB, 100 * GB, 100 * GB, 50, "/"),
    ])
    assert m["MIN_FREE_FS"] == "/" and m["MAX_DISK_FS"] == "/"


@pytest.mark.parametrize("env, free_gb, pct, warns", [
    ({"HEALTHCHECK_DISK_FREE_GB": "20"}, "20", "", False),
    ({"HEALTHCHECK_DISK_FREE_GB": "five"}, "5", "", True),
    ({"HEALTHCHECK_DISK_PCT": "90"}, "5", "90", False),
    ({"HEALTHCHECK_DISK_PCT": "150"}, "5", "85", True),
])
def test_a_bad_threshold_falls_back_instead_of_disabling_the_check(bash, tmp_path, env,
                                                                   free_gb, pct, warns):
    m = measure(bash, tmp_path, [("/dev/sda1", 200 * GB, 100 * GB, 100 * GB, 50, "/")], **env)
    assert (m["DISK_FREE_MIN_GB"], m["DISK_THRESHOLD"]) == (free_gb, pct)
    assert ("WARNING" in m["log"]) == warns, m["log"]


@pytest.mark.integration
@pytest.mark.parametrize("free_gb, pct, env, alerts", [
    (100, 95, {}, False),                            # busy, roomy: silent by default
    (3, 60, {}, True),                               # tight: pages whatever the share
    (100, 95, {"HEALTHCHECK_DISK_PCT": "90"}, True),  # the percent pages when asked to
])
def test_the_page_follows_free_space(cron_copy: Path, tmp_path: Path, bash: str,
                                     free_gb, pct, env, alerts):
    cron = cron_copy / "cron"
    _stub(cron / "llm-call.py", "import sys\nsys.stdin.read()\nprint('analysis')\n")
    (cron / "registry.yaml").write_text(registry(True, True), encoding="utf-8")
    # A function, through BASH_ENV: Git for Windows' bash.exe puts /usr/bin at
    # the front of PATH, so a `df` stub on PATH is never the one that runs.
    size = free_gb * 100 // (100 - pct)
    stub = tmp_path / "df-stub.sh"
    stub.write_text("df() {\n"
                    "  echo 'Filesystem 1024-blocks Used Available Capacity Mounted on'\n"
                    f"  echo '/dev/sda1 {size * GB} {(size - free_gb) * GB} {free_gb * GB} {pct}% /'\n"
                    "}\n", encoding="utf-8", newline="\n")

    res, messages, log = run_healthcheck(cron, tmp_path, bash,
                                         disk_env={"BASH_ENV": stub.as_posix(), **env})

    assert res.returncode == 0, f"{res.stderr}\n{log}"
    assert bool(messages) == alerts, f"{messages}\n{log}"
    if alerts and not env:
        assert f"disk: {free_gb} GB free on / (threshold 5 GB)" in messages[0], messages
