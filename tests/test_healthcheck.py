"""claude-healthcheck.sh: what it pages about, and what it leaves to the monitor.

Two layers. The dead-man heredoc — "is a task monitor running on this host, and
is it still reporting" — runs in-process from the script's own source. The
morning of an LLM outage is a property of the whole script, so it runs the
script itself, with the LLM call, Telegram and the slow host collectors stubbed.
"""
from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
from datetime import datetime, timedelta
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
CRON = ROOT / "home-claude" / "cron"
SCRIPT = CRON / "claude-healthcheck.sh"

sys.path.insert(0, str(CRON))
import monitor_checks  # noqa: E402
import runs  # noqa: E402

# The monitor that applies on the host running the test.
HERE = "ClaudeTaskMonitor" if os.name == "nt" else "ClaudeTaskMonitorPosix"
NO_MONITOR = 3


def registry(windows_on: bool, posix_on: bool) -> str:
    return ("version: 1\ntasks:\n"
            "  - name: ClaudeTaskMonitor\n    platform: windows\n"
            "    trigger: Daily 09:30\n"
            f"    enabled: {'true' if windows_on else 'false'}\n"
            "  - name: ClaudeTaskMonitorPosix\n    platform: posix\n"
            "    trigger: Daily 09:30\n"
            f"    enabled: {'true' if posix_on else 'false'}\n")


def _heredoc(var: str) -> str:
    text = SCRIPT.read_text(encoding="utf-8")
    m = re.search(rf"^{var}=\$\(.*?<<'PYSCRIPT'\n(.*?)^PYSCRIPT$", text, re.M | re.S)
    assert m, f"no {var} heredoc in {SCRIPT.name}"
    return m.group(1)


@pytest.fixture()
def monitor_check(tmp_path: Path, monkeypatch, capsys):
    """Run the MONITOR_ALERT heredoc; return (exit code, stdout)."""
    (tmp_path / "cron").mkdir()

    def run(registry_text: str, ledger: list[dict]) -> tuple[int, str]:
        (tmp_path / "cron" / "registry.yaml").write_text(registry_text, encoding="utf-8")
        monkeypatch.setitem(sys.modules, "monitor_checks", monitor_checks)
        monkeypatch.setitem(sys.modules, "runs", runs)
        monkeypatch.setattr(runs, "read_latest_runs", lambda log_path=None: list(ledger))
        monkeypatch.setattr(sys, "argv", ["-", str(tmp_path)])
        monkeypatch.setattr(sys, "path", list(sys.path))
        try:
            exec(compile(_heredoc("MONITOR_ALERT"), f"{SCRIPT.name}:MONITOR_ALERT", "exec"),
                 {"__name__": "__main__"})
            code = 0
        except SystemExit as exc:
            code = exc.code or 0
        return code, capsys.readouterr().out

    return run


def test_no_monitor_for_this_platform_has_its_own_exit_code(monitor_check):
    """The healthcheck must be able to tell "no monitor here" from "monitor fine".

    Both used to exit 0 in silence, which was harmless while the healthcheck was
    the only job reporting an LLM outage — and is not now that a running monitor
    reports it: the healthcheck has to know whether anyone else will.
    """
    assert monitor_check(registry(False, False), [])[0] == NO_MONITOR
    other_only = registry(windows_on=os.name != "nt", posix_on=os.name == "nt")
    assert monitor_check(other_only, [])[0] == NO_MONITOR, \
        "a monitor for the OTHER platform does not run here"


def test_an_enabled_monitor_on_a_fresh_install_is_trusted_to_speak(monitor_check):
    assert monitor_check(registry(True, True), []) == (0, "")


def test_the_dead_man_switch_watches_the_monitor_of_this_platform(monitor_check):
    """It named ClaudeTaskMonitor everywhere, so on Linux/macOS it never fired."""
    other = {"task": "ClaudeMemoryUpdate", "ts": "2026-09-16T02:00:00"}
    code, out = monitor_check(registry(True, True), [other])
    assert code == 0
    assert out.startswith(f"{HERE} has NEVER written a run record"), out


# ── the morning of an outage, end to end ─────────────────────────────────────

def _bash() -> str | None:
    found = shutil.which("bash")
    if found and Path(found).parent.name.lower() != "system32":   # WSL launcher
        return found
    for cand in (r"C:\Program Files\Git\usr\bin\bash.exe",
                 r"C:\Program Files\Git\bin\bash.exe"):
        if Path(cand).is_file():
            return cand
    return None


def _stub(path: Path, body: str) -> None:
    path.write_text(body, encoding="utf-8", newline="\n")
    path.chmod(0o755)


@pytest.mark.integration
@pytest.mark.skipif(_bash() is None, reason="bash not available")
@pytest.mark.parametrize("chain_down, monitor_on", [(True, False), (True, True), (False, True)])
def test_an_llm_outage_morning_says_it_once(cron_copy: Path, tmp_path: Path,
                                            chain_down: bool, monitor_on: bool):
    """With the chain known to be down the analysis is not attempted, and the
    outage is reported by exactly one job.

    Before: "healthcheck: LLM analysis failed" at 09:00, then "LLM chain is DOWN"
    in the verdict, then the monitor's list of the tasks the outage failed at
    09:30 — three messages about one event, and the healthcheck itself exited 1,
    which the monitor then reported as a fourth.
    """
    cron = cron_copy / "cron"
    sent, llm_mark = tmp_path / "sent.txt", tmp_path / "llm-called"
    _stub(cron / "llm-call.py",
          "import sys\nfrom pathlib import Path\nsys.stdin.read()\n"
          f"Path({str(llm_mark)!r}).write_text('x')\nprint('OK')\n")
    _stub(cron / "telegram-send.sh", '#!/bin/bash\nprintf "%s\\n---\\n" "$1" >> "$SENT_FILE"\n')
    (cron / "registry.yaml").write_text(registry(monitor_on, monitor_on), encoding="utf-8")
    if chain_down:
        now = datetime.now()
        (cron / "state").mkdir(exist_ok=True)
        (cron / "state" / "chain-dead.json").write_text(json.dumps({
            "first_iso": (now - timedelta(hours=7)).isoformat(timespec="seconds"),
            "last_iso": (now - timedelta(hours=5)).isoformat(timespec="seconds"),
            "fails": 40, "depleted": {"deepseek": "403"}}), encoding="utf-8")
    # The host collectors that fall back to PowerShell on Git Bash, stubbed.
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    for tool in ("uptime", "free", "ps"):
        _stub(fake_bin / tool, "#!/bin/bash\nexit 0\n")

    env = dict(os.environ, PYTHON_EXE=sys.executable, BASH_EXE=_bash(),
               SENT_FILE=sent.as_posix(), HEALTHCHECK_DISK_PCT="100",
               REMOTE_SSH_HOST="", WIN_REMOTE_HOST="",
               PATH=os.pathsep.join([str(fake_bin), os.environ.get("PATH", "")]))
    res = subprocess.run([_bash(), (cron / "claude-healthcheck.sh").as_posix()],
                         capture_output=True, text=True, env=env, timeout=120)
    log = "\n".join(p.read_text(encoding="utf-8", errors="replace")
                    for p in (cron / "logs").glob("healthcheck_*.log"))
    messages = sent.read_text(encoding="utf-8").split("\n---\n")[:-1] if sent.exists() else []

    assert res.returncode == 0, f"{res.stderr}\n{log}"
    assert llm_mark.exists() == (not chain_down), \
        f"LLM analysis {'ran' if llm_mark.exists() else 'did not run'} with chain_down={chain_down}"
    assert not any("LLM analysis failed" in m for m in messages), messages
    if chain_down and not monitor_on:
        assert len(messages) == 1 and "LLM chain is DOWN (deepseek: 403)" in messages[0], messages
    else:
        assert messages == [], f"expected silence, got {messages}"
    if chain_down and monitor_on:
        assert "left to the task monitor" in log, log
