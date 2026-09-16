"""bash-deny.yaml, rule by rule: what each must stop and what it must let through.

The rule table is data, and nothing checked the data. Two spellings the rules'
own reasons promise to catch passed (`git push origin main --force`,
`rm -r -f /`), two ordinary commands were stopped (`cat .env.example`,
`git commit -m 'fix @'`), and an `ask` early in the table shadowed a `deny`
later in it for the same command line. Every case below is one of those or a
neighbour that must keep passing.

The table runs in-process against the SHIPPED rules file: a hundred commands
through a subprocess each would cost the fast suite seconds. The stdin/stdout
contract and CLAUDE_BASH_DENY are driven end to end separately.
"""
from __future__ import annotations

import importlib.util
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
HOOKS = ROOT / "home-claude" / "hooks"
GUARD = HOOKS / "bash-guard.py"


def _load(path: Path, name: str):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="module")
def guard():
    return _load(GUARD, "bash_guard_under_test")


@pytest.fixture(scope="module")
def shipped_rules(guard):
    pytest.importorskip("yaml")
    rules = guard.load_rules(str(HOOKS / "bash-deny.yaml"))
    assert len(rules) == 6, "a rule in bash-deny.yaml no longer compiles"
    return rules


DENY, ASK, PASS = "deny", "ask", None

TABLE = [
    # iptables-save into the persisted ruleset
    ("iptables-save > /etc/iptables/rules.v4", DENY),
    ("ssh h 'sudo ip6tables-save > /etc/iptables/rules.v6'", DENY),
    ("iptables-save > /root/backup/rules.v4.bak", PASS),
    ("grep -f patterns.txt rules.v4.txt", PASS),
    # a PowerShell here-string in a commit run through bash
    ("git commit -m @'\nfix: thing\n'@", DENY),
    ("git commit -m @'\r\nfix: thing\r\n'@", DENY),
    ("git commit -m 'fix @'", PASS),
    ("git commit -m \"reply to @'s review\"", PASS),
    ("git commit -F msg.txt", PASS),
    # force-push to main/master — flag and branch in either order
    ("git push --force origin main", ASK),
    ("git push origin main --force", ASK),
    ("git push -f origin master", ASK),
    ("git push origin master -f", ASK),
    ("git push -uf origin main", ASK),
    ("git push --force origin HEAD:main", ASK),
    ("git push --force origin refs/heads/main", ASK),
    ("git -C /work/repo push origin main --force", ASK),
    ('git -C "C:/work/my repo" push -f origin main', ASK),
    ("git push --force-with-lease origin main", PASS),
    ("git push --force-with-lease --force-if-includes origin main", PASS),
    ("git push -f origin feature/main-fix", PASS),
    ("git push -f origin main-v2", PASS),
    ("git push origin main", PASS),
    ("git push --force origin feature && git checkout main", PASS),
    # rm -rf aimed at a root or a home directory
    ("rm -rf /", DENY),
    ("rm -rf /*", DENY),
    ("rm -rf ~", DENY),
    ("rm -rf ~/", DENY),
    ("rm -rf ~/*", DENY),
    ("rm -r -f /", DENY),
    ("rm -f -r /", DENY),
    ("rm --recursive --force /", DENY),
    ("rm -rf -- /", DENY),
    ("rm -rf --no-preserve-root /", DENY),
    ("rm -rf / --no-preserve-root", DENY),
    ("sudo rm -Rf $HOME", DENY),
    ('rm -rf "$HOME"', DENY),
    ("rm -rf ${HOME}/", DENY),
    ("rm -rf C:\\", DENY),
    ("rm -rf C:/*", DENY),
    ("rm -rf /c/", DENY),
    ("rm -rfv /; echo done", DENY),
    ("\\rm -rf /", DENY),
    ("rm -rf ./build", PASS),
    ("rm -rf /tmp/build", PASS),
    ("rm -rf ~/projects/old", PASS),
    ('rm -rf "$HOME/.cache/pip"', PASS),
    ("rm -r build/", PASS),
    ("rm -rf '~'", PASS),                    # a directory literally named ~
    # printing a .env
    ("cat .env", ASK),
    ("cat ./.env", ASK),
    ("cat .env.local", ASK),
    ("head -5 /srv/app/.env.production", ASK),
    ("cat C:\\proj\\.env", ASK),
    ('cat "C:\\proj\\.env"', ASK),
    ("'cat' .env", ASK),
    ("c\\at .env", ASK),
    ("c'a't .env", ASK),
    ("cat .env.example", PASS),
    ("cat .env.sample", PASS),
    ("cat config/.env.template", PASS),
    ("cat .env.dist", PASS),
    ("grep -c '^NAME=' .env", PASS),
    ("cat .environment-notes.md", PASS),
    # --no-verify
    ("git commit --no-verify -m wip", ASK),
    ("git -C /work/repo push --no-verify", ASK),
    ("git commit -m wip", PASS),
    # several rules on one line: the strictest one decides
    ("git push --force origin main && rm -rf /", DENY),
    ("cat .env; rm -rf ~/", DENY),
]


@pytest.mark.parametrize("command,expected", TABLE, ids=[c for c, _ in TABLE])
def test_rule_table(guard, shipped_rules, command: str, expected):
    verdict = guard.decide(command, shipped_rules)
    got = verdict[0] if verdict else None
    assert got == expected, f"{command!r}: expected {expected}, got {got}"


def test_every_denial_on_a_line_is_reported(guard, shipped_rules):
    """Two denials in one command: the model is told about both, not the first."""
    severity, reason = guard.decide(
        "rm -rf / ; iptables-save > /etc/iptables/rules.v4", shipped_rules)
    assert severity == "deny"
    assert "rules.v4" in reason and "rm -rf" in reason


def _write_table(path: Path, body: str) -> Path:
    path.write_text(body, encoding="utf-8")
    return path


def _run(command: str, env_extra: dict) -> subprocess.CompletedProcess:
    env = os.environ.copy()
    env.update(env_extra)
    return subprocess.run(
        [sys.executable, str(GUARD)], input=json.dumps({"tool_input": {"command": command}}),
        capture_output=True, text=True, encoding="utf-8", env=env, timeout=60)


def test_deny_beats_an_earlier_ask_through_the_real_contract(tmp_path: Path):
    """The hook stopped at the first match; the table's order must not matter."""
    pytest.importorskip("yaml")
    table = _write_table(tmp_path / "rules.yaml", (
        "rules:\n"
        "  - pattern: 'alpha'\n    severity: ask\n    reason: first, milder\n"
        "  - pattern: 'beta'\n    severity: deny\n    reason: second, stricter\n"))
    r = _run("alpha && beta", {"CLAUDE_BASH_DENY": str(table)})
    assert r.returncode == 0, r.stderr
    out = json.loads(r.stdout)["hookSpecificOutput"]
    assert out["hookEventName"] == "PreToolUse"
    assert out["permissionDecision"] == "deny"
    assert out["permissionDecisionReason"] == "second, stricter"


def test_a_bad_regex_drops_only_that_rule(tmp_path: Path):
    pytest.importorskip("yaml")
    table = _write_table(tmp_path / "rules.yaml", (
        "rules:\n"
        "  - pattern: '([unclosed'\n    severity: deny\n"
        "  - pattern: 'gamma'\n    severity: ask\n    reason: still here\n"))
    r = _run("gamma", {"CLAUDE_BASH_DENY": str(table)})
    assert r.returncode == 0
    assert json.loads(r.stdout)["hookSpecificOutput"]["permissionDecision"] == "ask"
    assert "bad regex" in r.stderr
    assert _run("([unclosed", {"CLAUDE_BASH_DENY": str(table)}).stdout.strip() == ""


def test_fails_open_without_pyyaml(guard, monkeypatch):
    """No PyYAML must disable the guard, not block every Bash call on the machine."""
    monkeypatch.setitem(sys.modules, "yaml", None)
    rules = guard.load_rules(str(HOOKS / "bash-deny.yaml"))
    assert rules == []
    assert guard.decide("rm -rf /", rules) is None


def test_iptables_rule_is_the_standalone_hooks_pattern(shipped_rules):
    """settings.example-with-hooks.json wires bash-guard.py only.

    block-iptables-save-to-rules.py stays for configs that already name it, and
    the example dropped it on the premise that the table's first rule is the
    same regex. If the two drift, that premise silently stops being true.
    """
    standalone = _load(HOOKS / "block-iptables-save-to-rules.py", "iptables_under_test")
    assert shipped_rules[0]["re"].pattern == standalone.PATTERN.pattern
    assert shipped_rules[0]["severity"] == "deny"
