"""Mutation tests for `scripts/check-*.py`.

`tests/` contained no test for a single guard script, and that is precisely why
one of them shipped broken: `check-agents-sync.py` reported "Coding Discipline
compared by content" while comparing two EMPTY strings, because an H2 whose body
is entirely H3 subsections has no text of its own. Nothing noticed, because
nothing ever fed the guard a file with a deliberate defect in it.

Each test here introduces ONE drift into a fixture copy of the repo and asserts
the guard exits 1 and names the offending file or task. A guard that cannot fail
is not a guard.
"""
from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent


def _run_guard(script: str, root: Path) -> subprocess.CompletedProcess:
    """Run a guard against a fixture tree by pointing it at that tree's root."""
    return subprocess.run(
        [sys.executable, str(root / "scripts" / script)],
        cwd=str(root), capture_output=True, text=True,
        encoding="utf-8", errors="replace", timeout=120,
    )


@pytest.fixture()
def repo(tmp_path: Path) -> Path:
    """A copy of the parts of the repo the guards read."""
    dest = tmp_path / "repo"
    for rel in ("scripts", "docs", "config", "codex", "home-claude"):
        shutil.copytree(ROOT / rel, dest / rel,
                        ignore=shutil.ignore_patterns("__pycache__", "*.pyc",
                                                      "logs", "state", ".env",
                                                      "bundle.local.yaml"))
    for rel in ("README.md", "INSTALL.md", "CLAUDE.md", "AGENT-INSTRUCTIONS.md",
                "CHANGELOG.md", "pytest.ini", "requirements.txt",
                "requirements-dev.txt"):
        src = ROOT / rel
        if src.is_file():
            shutil.copy2(src, dest / rel)
    (dest / ".githooks").mkdir(exist_ok=True)
    for f in (ROOT / ".githooks").glob("*"):
        if f.is_file():
            shutil.copy2(f, dest / ".githooks" / f.name)
    return dest


def test_guards_pass_on_an_unmodified_tree(repo: Path):
    """The baseline. Without it a guard that always fails would 'pass' below."""
    for script in ("check-registry.py", "check-doc-counts.py", "check-env-ref.py",
                   "check-io-matrix.py", "check-agents-sync.py"):
        r = _run_guard(script, repo)
        assert r.returncode == 0, f"{script} fails on a clean tree:\n{r.stdout}\n{r.stderr}"


def test_agents_sync_catches_a_reworded_universal_rule(repo: Path):
    """Rewrite a COMPARED section in one file only — the guard must object.

    This is the mutation the shipped guard let through: `## Coding Discipline`
    has no body of its own before its first `###`, so the comparison ran on two
    empty strings and every one of the four Karpathy rules could be rewritten in
    one file with the guard still green.
    """
    claude = repo / "home-claude" / "CLAUDE.md"
    text = claude.read_text(encoding="utf-8")
    assert "No features beyond what was asked" in text, "fixture rule not found"
    claude.write_text(
        text.replace("No features beyond what was asked",
                     "Add whichever features seem useful"),
        encoding="utf-8")
    r = _run_guard("check-agents-sync.py", repo)
    assert r.returncode == 1, f"a reworded universal rule went unnoticed:\n{r.stdout}"
    assert "Coding Discipline" in r.stdout


def test_agents_sync_catches_a_missing_universal_section(repo: Path):
    agents = repo / "codex" / "AGENTS.md"
    text = agents.read_text(encoding="utf-8")
    marker = next(l for l in text.splitlines()
                  if l.startswith("## ") and "Test policy" in l)
    agents.write_text(text.replace(marker, "## Something Else Entirely"),
                      encoding="utf-8")
    r = _run_guard("check-agents-sync.py", repo)
    assert r.returncode == 1, f"a deleted universal section went unnoticed:\n{r.stdout}"
    assert "Test policy" in r.stdout


def test_io_matrix_catches_an_undisclosed_offbox_task(repo: Path):
    """A task that sends data but declares `offbox=nothing` must be caught.

    The header used to be pure self-declaration, so this exact mutation — the
    cheapest possible way to make the privacy matrix lie — was invisible.
    """
    target = repo / "home-claude" / "cron" / "claude-healthcheck.sh"
    text = target.read_text(encoding="utf-8")
    line = next(l for l in text.splitlines() if l.startswith("# bundle-io:"))
    target.write_text(
        text.replace(line, "# bundle-io: offbox=nothing money=no writes=nothing"),
        encoding="utf-8")
    r = _run_guard("check-io-matrix.py", repo)
    assert r.returncode == 1, f"a false offbox=nothing went unnoticed:\n{r.stdout}"
    assert "ClaudeHealthcheck" in r.stdout


def test_io_matrix_catches_a_missing_bundle_io_header(repo: Path):
    target = repo / "home-claude" / "cron" / "test-sweep.py"
    text = target.read_text(encoding="utf-8")
    line = next(l for l in text.splitlines() if l.startswith("# bundle-io:"))
    target.write_text(text.replace(line, "# (header removed)"), encoding="utf-8")
    r = _run_guard("check-io-matrix.py", repo)
    assert r.returncode == 1, f"a missing bundle-io header went unnoticed:\n{r.stdout}"
    assert "bundle-io" in r.stdout


def test_registry_catches_a_trigger_the_posix_generator_would_skip(repo: Path):
    """`repeat_every` on a Weekly trigger is valid for Task Scheduler and
    UNEXPRESSIBLE for systemd — the "silent skip" this guard exists to prevent."""
    pytest.importorskip("yaml")
    import yaml
    reg = repo / "home-claude" / "cron" / "registry.yaml"
    text = reg.read_text(encoding="utf-8")
    data = yaml.safe_load(text)
    victim = next(t for t in data["tasks"]
                  if str(t.get("trigger", "")).startswith("Weekly")
                  and t.get("enabled") is not False
                  and str(t.get("platform", "")).lower() != "windows")
    lines = text.splitlines()
    idx = next(i for i, l in enumerate(lines)
               if l.strip() == f"trigger: {victim['trigger']}")
    indent = lines[idx][:len(lines[idx]) - len(lines[idx].lstrip())]
    lines.insert(idx + 1, f"{indent}repeat_every: PT4H")
    reg.write_text("\n".join(lines) + "\n", encoding="utf-8")
    r = _run_guard("check-registry.py", repo)
    assert r.returncode == 1, f"an unschedulable POSIX trigger went unnoticed:\n{r.stdout}"
    assert victim["name"] in r.stdout


def test_registry_catches_an_unknown_field(repo: Path):
    """A typo'd key is ignored by BOTH parsers — the quietest way to lose a setting."""
    pytest.importorskip("yaml")
    reg = repo / "home-claude" / "cron" / "registry.yaml"
    lines = reg.read_text(encoding="utf-8").splitlines()
    idx = next(i for i, l in enumerate(lines) if l.strip().startswith("- name: "))
    indent = lines[idx][:len(lines[idx]) - len(lines[idx].lstrip())] + "  "
    lines.insert(idx + 1, f"{indent}timeout_hour: 2")   # note the typo
    reg.write_text("\n".join(lines) + "\n", encoding="utf-8")
    r = _run_guard("check-registry.py", repo)
    assert r.returncode == 1, f"a typo'd registry field went unnoticed:\n{r.stdout}"
    assert "timeout_hour" in r.stdout


def test_env_ref_catches_an_undocumented_env_var(repo: Path):
    """A new knob that never reaches the .env template is the whole point."""
    target = repo / "home-claude" / "cron" / "log-retention.py"
    text = target.read_text(encoding="utf-8")
    target.write_text(
        text + '\n_UNDOCUMENTED = os.environ.get("WIKI_TOTALLY_NEW_KNOB")\n',
        encoding="utf-8")
    r = _run_guard("check-env-ref.py", repo)
    assert r.returncode == 1, f"an undocumented env var went unnoticed:\n{r.stdout}"
    assert "WIKI_TOTALLY_NEW_KNOB" in r.stdout


def test_env_ref_sees_variables_declared_in_the_PROVIDERS_table(repo: Path):
    """Provider vars are read through a variable, not a literal.

    All twelve were therefore invisible to the guard: adding a provider by the
    documented recipe produced three env names nothing required to be in the
    template. The AST pass is what closes it, so removing one from the template
    must now fail.
    """
    tmpl = repo / "config" / "llm-providers.example.env"
    text = tmpl.read_text(encoding="utf-8")
    assert "DEEPINFRA_BASE_URL" in text
    tmpl.write_text(
        "\n".join(l for l in text.splitlines()
                  if "DEEPINFRA_BASE_URL" not in l) + "\n",
        encoding="utf-8")
    r = _run_guard("check-env-ref.py", repo)
    assert r.returncode == 1, f"a PROVIDERS var missing from the template went unnoticed:\n{r.stdout}"
    assert "DEEPINFRA_BASE_URL" in r.stdout


def test_doc_counts_catches_a_task_count_that_drifted(repo: Path):
    pytest.importorskip("yaml")
    reg = repo / "home-claude" / "cron" / "registry.yaml"
    text = reg.read_text(encoding="utf-8")
    # Remove one whole task block: the counts in README/docs now disagree.
    lines = text.splitlines()
    start = next(i for i, l in enumerate(lines)
                 if l.strip().startswith("- name: ClaudeLogRetention"))
    end = next((i for i in range(start + 1, len(lines))
                if lines[i].strip().startswith("- name: ")), len(lines))
    reg.write_text("\n".join(lines[:start] + lines[end:]) + "\n", encoding="utf-8")
    r = _run_guard("check-doc-counts.py", repo)
    assert r.returncode == 1, f"a dropped task left the doc counts unchecked:\n{r.stdout}"
