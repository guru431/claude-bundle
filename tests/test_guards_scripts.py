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


def test_env_ref_sees_a_variable_only_powershell_reads(repo: Path):
    """A knob read only by a `.ps1` still has to be in the template.

    The scan covered `.py` and `.sh` only, so anything `claude-switch.ps1` read
    out of `.env` was invisible: `API_TIMEOUT_MS` reached the template because
    somebody noticed by hand, and the next such variable would not have. The
    scan is deliberately narrow — the bundle's own `.env` readers, not a blanket
    `$env:` sweep, which on Windows admin scripts is all noise.
    """
    tmpl = repo / "config" / "llm-providers.example.env"
    text = tmpl.read_text(encoding="utf-8")
    assert "API_TIMEOUT_MS" in text
    tmpl.write_text(
        "\n".join(l for l in text.splitlines() if "API_TIMEOUT_MS" not in l) + "\n",
        encoding="utf-8")
    r = _run_guard("check-env-ref.py", repo)
    assert r.returncode == 1, \
        f"a PowerShell-only env var missing from the template went unnoticed:\n{r.stdout}"
    assert "API_TIMEOUT_MS" in r.stdout


@pytest.mark.parametrize("rel, addition, name", [
    # A helper the guard has never heard of: found by parsing, not by a list.
    ("home-claude/cron/log-retention.py",
     '\n\ndef _knob(var, default):\n    return os.environ.get(var) or default\n\n\n'
     '_NEW = _knob("WIKI_HELPER_ONLY_KNOB", "1")\n',
     "WIKI_HELPER_ONLY_KNOB"),
    # The shipped helpers themselves.
    ("home-claude/cron/md2pdf-sync.py",
     '\n_NEW = _env_int("MD2PDF_NEW_INT_KNOB", 3, minimum=1)\n',
     "MD2PDF_NEW_INT_KNOB"),
    # `X="${X:-default}"` assigns the name, but from the environment.
    ("home-claude/cron/claude-healthcheck.sh",
     '\nHEALTHCHECK_SELF_DEFAULT_KNOB="${HEALTHCHECK_SELF_DEFAULT_KNOB:-7}"\n',
     "HEALTHCHECK_SELF_DEFAULT_KNOB"),
    # Python embedded in a shell heredoc reads the environment the shell passes.
    ("home-claude/cron/claude-task-monitor.sh",
     "\n\"$PYTHON\" - <<'PY'\nimport os\nprint(os.environ.get('MONITOR_HEREDOC_KNOB', ''))\nPY\n",
     "MONITOR_HEREDOC_KNOB"),
    # claude-switch.ps1 reads every provider key through Require-Key.
    ("scripts/claude-switch.ps1",
     '\n$k = Require-Key @("SWITCH_NEW_KEY", "SWITCH_NEW_KEY_ALIAS")\n',
     "SWITCH_NEW_KEY_ALIAS"),
])
def test_env_ref_sees_a_read_hidden_behind_a_helper(repo: Path, rel: str,
                                                     addition: str, name: str):
    """Every form in which the shipped code reads a knob without a literal
    `os.environ.get("X")` at the call site.

    Each one was invisible: docs/config-reference.md listed the flag as read by
    nobody (`—`) — WIKI_ALLOW_OFFBOX and WIKI_MASK_SECRETS among them — and
    WIKI_LLM_PACE_SECONDS / WIKI_PROJECT_LOG_MAX_LINES were missing from the
    template while nothing failed.
    """
    target = repo / rel
    target.write_text(target.read_text(encoding="utf-8") + addition, encoding="utf-8")
    r = _run_guard("check-env-ref.py", repo)
    assert r.returncode == 1, f"a read of {name} went unnoticed:\n{r.stdout}"
    assert name in r.stdout


def test_env_ref_does_not_mistake_a_shell_append_for_a_knob(repo: Path):
    """The other side of the self-default rule: `X="$X more"` and
    `X="${X:+$X, }y"` build a local from itself and must stay locals — the
    alert accumulators in the monitors are written exactly that way."""
    target = repo / "home-claude" / "cron" / "claude-healthcheck.sh"
    target.write_text(
        target.read_text(encoding="utf-8")
        + '\nLOCAL_ACCUMULATOR=""\nLOCAL_ACCUMULATOR="$LOCAL_ACCUMULATOR more"\n'
          'LOCAL_ACCUMULATOR="${LOCAL_ACCUMULATOR:+$LOCAL_ACCUMULATOR, }x"\n',
        encoding="utf-8")
    r = _run_guard("check-env-ref.py", repo)
    assert r.returncode == 0, f"a shell local was reported as a knob:\n{r.stdout}"


def test_env_ref_check_table_catches_an_edited_cell(repo: Path):
    """`--check-table` has to fail on the page's CONTENT, not only its header.

    The generated reference is only trustworthy while a hand edit of one row —
    the likeliest way it rots — turns the build red.
    """
    def check_table() -> subprocess.CompletedProcess:
        return subprocess.run(
            [sys.executable, str(repo / "scripts" / "check-env-ref.py"), "--check-table"],
            cwd=str(repo), capture_output=True, text=True, encoding="utf-8",
            errors="replace", timeout=120)

    before = check_table()
    assert before.returncode == 0, f"the committed page is already stale:\n{before.stdout}"
    page = repo / "docs" / "config-reference.md"
    text = page.read_text(encoding="utf-8")
    row = next(l for l in text.splitlines() if l.startswith("| `WIKI_RETRY_LIMIT` |"))
    page.write_text(text.replace(row, row.replace("optional (commented)", "declared")),
                    encoding="utf-8")
    r = check_table()
    assert r.returncode == 1, f"an edited config-reference cell went unnoticed:\n{r.stdout}"
    assert "config-reference.md" in r.stdout


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
