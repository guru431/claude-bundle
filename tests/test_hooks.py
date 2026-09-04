"""Every shipped hook, driven end-to-end through its real stdin/stdout contract.

A grep for `session-start|session-end|pre-compact|block-iptables|md2pdf` across
`tests/` used to return nothing, and `scripts/self-test.ps1` exercised two of the
tier-1 hooks with one payload each — on Windows only. So the whole hook layer,
which runs on every session of every project, had no regression coverage at all:
a traversal in a session id, a list where a string was expected, a denied project
still writing to `.pending/` — none of it would have been caught.

Every test here is cross-platform and runs in the fast suite.
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
CRON_SRC = ROOT / "home-claude" / "cron"
TIER1_HOOKS = ROOT / "home-claude" / "hooks"

# (path relative to the hook root, which root it lives under)
CRON_HOOKS = ["session-start.py", "session-end.py", "pre-compact.py"]
PLAIN_HOOKS = ["block-iptables-save-to-rules.py", "md2pdf-on-edit.py"]


@pytest.fixture()
def hook_bundle(tmp_path: Path) -> Path:
    """A throwaway bundle whose hooks write into tmp, never into the repo."""
    shutil.copytree(CRON_SRC, tmp_path / "cron")
    (tmp_path / "wiki" / "daily" / ".pending").mkdir(parents=True)
    return tmp_path


def _run_hook(script: Path, payload, env_extra: dict | None = None,
              cwd: Path | None = None) -> subprocess.CompletedProcess:
    env = os.environ.copy()
    env["WIKI_LLM_PROVIDER"] = "mock"
    env.update(env_extra or {})
    body = payload if isinstance(payload, str) else json.dumps(payload)
    return subprocess.run(
        [sys.executable, str(script)], input=body,
        cwd=str(cwd or script.parent), env=env,
        capture_output=True, text=True, encoding="utf-8", errors="replace",
        timeout=60,
    )


def _hook_paths(bundle: Path) -> list[Path]:
    return ([bundle / "cron" / "hooks" / n for n in CRON_HOOKS]
            + [TIER1_HOOKS / n for n in PLAIN_HOOKS])


MALFORMED = [
    "not json at all",
    "[1, 2, 3]",                                  # valid JSON, not an object
    '"a bare string"',
    "{}",
    '{"tool_input": "x"}',                        # not an object
    '{"tool_input": {"command": [1, 2]}}',        # not a string
    '{"transcript_path": ["/tmp/x"]}',            # not a string
    '{"session_id": null, "transcript_path": ""}',
]


@pytest.mark.parametrize("payload", MALFORMED)
def test_hooks_never_raise_on_malformed_input(hook_bundle: Path, payload: str):
    """hooks/README.md: "They never raise on malformed input."

    Exit 1 does not block the tool call, but the traceback is shown to the user —
    and four hooks did exactly that on a payload whose `tool_input` was a string
    or whose `command` was a list.
    """
    for hook in _hook_paths(hook_bundle):
        r = _run_hook(hook, payload)
        assert r.returncode == 0, (
            f"{hook.name} exited {r.returncode} on {payload!r}:\n{r.stderr}")
        assert "Traceback" not in r.stderr, f"{hook.name} raised:\n{r.stderr}"


def test_hooks_emit_valid_json_or_nothing(hook_bundle: Path):
    """Whatever a hook prints on stdout must be parseable — Claude Code reads it."""
    payload = {"session_id": "abc", "transcript_path": "", "cwd": str(hook_bundle)}
    for hook in _hook_paths(hook_bundle):
        r = _run_hook(hook, payload)
        assert r.returncode == 0, f"{hook.name}: {r.stderr}"
        if r.stdout.strip():
            json.loads(r.stdout)          # raises → the test fails, as intended


def _seed_session(home: Path, dirname: str, session_id: str) -> Path:
    d = home / ".claude" / "projects" / dirname
    d.mkdir(parents=True, exist_ok=True)
    lines = []
    for i in range(6):
        lines.append(json.dumps({"type": "user", "message": {
            "role": "user", "content": f"message {i} " + "x" * 200}}))
        lines.append(json.dumps({"type": "assistant", "message": {
            "role": "assistant", "content": f"reply {i} " + "x" * 200}}))
    p = d / f"{session_id}.jsonl"
    p.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return p


def test_session_end_writes_a_pending_draft(hook_bundle: Path, tmp_path: Path):
    home = tmp_path / "h_ok"
    transcript = _seed_session(home, "C--work-myapp", "sess-ok")
    r = _run_hook(hook_bundle / "cron" / "hooks" / "session-end.py",
                  {"session_id": "sess-ok", "transcript_path": str(transcript)},
                  {"HOME": str(home), "USERPROFILE": str(home)})
    assert r.returncode == 0, r.stderr
    pending = hook_bundle / "wiki" / "daily" / ".pending" / "sess-ok.md"
    assert pending.is_file(), f"no pending draft written:\n{r.stderr}"
    assert "message 0" in pending.read_text(encoding="utf-8")


def test_session_end_honours_skip_projects(hook_bundle: Path, tmp_path: Path):
    """The privacy policy is promised for EVERY source collector.

    This one ignored it: the tail of a denied project was written to `.pending/`
    the moment the session ended, and only the nightly flush dropped it — so the
    name and content of a hidden project sat on disk in between, where the task
    monitor could carry it off-box.
    """
    pytest.importorskip("yaml")
    (hook_bundle / "bundle.local.yaml").write_text(
        "skip_projects:\n  - secretproj\n", encoding="utf-8")
    home = tmp_path / "h_denied"
    transcript = _seed_session(home, "C--work-secretproj", "sess-denied")
    r = _run_hook(hook_bundle / "cron" / "hooks" / "session-end.py",
                  {"session_id": "sess-denied", "transcript_path": str(transcript)},
                  {"HOME": str(home), "USERPROFILE": str(home)})
    assert r.returncode == 0, r.stderr
    written = list((hook_bundle / "wiki" / "daily" / ".pending").glob("*.md"))
    assert not written, f"a denied project's tail was written: {written}"


def test_session_end_sanitizes_the_session_id(hook_bundle: Path, tmp_path: Path):
    """A session id becomes a FILENAME. `../../evil` wrote wiki/evil.md."""
    home = tmp_path / "h_trav"
    transcript = _seed_session(home, "C--work-myapp", "trav")
    r = _run_hook(hook_bundle / "cron" / "hooks" / "session-end.py",
                  {"session_id": "../../evil", "transcript_path": str(transcript)},
                  {"HOME": str(home), "USERPROFILE": str(home)})
    assert r.returncode == 0, r.stderr
    assert not (hook_bundle / "wiki" / "evil.md").exists(), "traversal escaped .pending/"
    assert not (hook_bundle / "evil.md").exists(), "traversal escaped the wiki"
    written = list((hook_bundle / "wiki" / "daily" / ".pending").glob("*.md"))
    assert written, "the draft was not written at all"
    assert all("/" not in p.name and "\\" not in p.name for p in written)


def test_iptables_hook_blocks_the_documented_spellings(hook_bundle: Path):
    hook = TIER1_HOOKS / "block-iptables-save-to-rules.py"
    must_block = [
        "iptables-save > /etc/iptables/rules.v4",
        "iptables-legacy-save > /etc/iptables/rules.v4",
        "iptables-nft-save >> /etc/iptables/rules.v6",
        "netfilter-persistent save > /etc/iptables/rules.v4",
        "iptables-save | tee /etc/iptables/rules.v4",
        "iptables-save -f /etc/iptables/rules.v4",
        "ssh h 'sudo ip6tables-save > /etc/iptables/rules.v6'",
    ]
    for cmd in must_block:
        r = _run_hook(hook, {"tool_input": {"command": cmd}})
        assert r.returncode == 0
        assert '"permissionDecision": "deny"' in r.stdout, f"not blocked: {cmd}"


def test_iptables_hook_allows_ordinary_commands(hook_bundle: Path):
    """False positives here block real work — `-f` used to match `grep -f`."""
    hook = TIER1_HOOKS / "block-iptables-save-to-rules.py"
    must_allow = [
        "iptables -L -n",
        "cat /etc/iptables/rules.v4",
        "grep -f patterns.txt rules.v4.txt",
        "iptables-save > /root/backup/rules.v4.bak",
        "task-management-system-v2 --disk-usage-threshold-pct 90",
    ]
    for cmd in must_allow:
        r = _run_hook(hook, {"tool_input": {"command": cmd}})
        assert r.returncode == 0
        assert "deny" not in r.stdout, f"false positive on: {cmd}\n{r.stdout}"
