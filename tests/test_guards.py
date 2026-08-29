"""Unit tests for the fail-closed guards in the cron pipeline.

Each of these covers a mis-configuration that used to pass silently and do the
WRONG thing unattended: delete every log, ship the whole session archive to a
cloud provider, send a transcript to a "local-only" provider that wasn't local,
or ignore a privacy manifest nobody could parse. They need no network and no
provider key — the point is that nothing is sent at all.

Run: pytest tests/ -q
"""
from __future__ import annotations

import importlib
import os
import re
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
CRON = ROOT / "home-claude" / "cron"
HOOKS = CRON / "hooks"


def _import_utils(monkeypatch, bundle_root: Path):
    """Import cron/hooks/utils.py fresh, rooted at a throwaway bundle tree."""
    # utils derives BUNDLE_ROOT from __file__, so the module has to be loaded
    # from a copy inside the tmp tree for its state/manifest paths to land there.
    monkeypatch.syspath_prepend(str(bundle_root / "cron" / "hooks"))
    sys.modules.pop("utils", None)
    return importlib.import_module("utils")


@pytest.fixture()
def bundle_tree(tmp_path: Path) -> Path:
    import shutil
    shutil.copytree(CRON, tmp_path / "cron")
    return tmp_path


# ── log-retention: a negative window must never delete anything ──────────────

@pytest.mark.parametrize("value", ["-1", "abc", "99999999"])
def test_log_retention_refuses_bad_window(tmp_path: Path, value: str):
    """A bad WIKI_LOG_RETENTION_DAYS aborts BEFORE the first unlink.

    -1 puts the cutoff in the future, so every log/jsonl/handoff looks old and
    the sweep wipes the lot — from one typo in .env.
    """
    import shutil
    shutil.copytree(CRON, tmp_path / "cron")
    logs = tmp_path / "cron" / "logs"
    logs.mkdir(parents=True, exist_ok=True)
    victim = logs / "keepme.log"
    victim.write_text("x", encoding="utf-8")

    env = os.environ.copy()
    env["WIKI_LOG_RETENTION_DAYS"] = value
    env["CLAUDE_HOME"] = str(tmp_path / "fake-claude-home")
    r = subprocess.run([sys.executable, str(tmp_path / "cron" / "log-retention.py")],
                       capture_output=True, text=True, env=env, timeout=60,
                       encoding="utf-8", errors="replace")
    assert r.returncode == 2, r.stdout + r.stderr
    assert victim.exists(), "the sweep deleted a log despite refusing to run"


# ── flush: a negative backlog cap must not select the whole archive ──────────

def test_backlog_max_negative_disables_sweep(bundle_tree: Path, monkeypatch):
    """WIKI_BACKLOG_MAX=-1 must mean "disabled", not "everything but one file".

    `all_candidates[:-1]` is a valid Python slice, which is exactly why this was
    dangerous: the first night would have shipped the whole historical archive.
    """
    monkeypatch.setenv("WIKI_BACKLOG_MAX", "-1")
    monkeypatch.setenv("CLAUDE_HOME", str(bundle_tree / "fake-home"))
    monkeypatch.syspath_prepend(str(bundle_tree / "cron" / "hooks"))
    monkeypatch.syspath_prepend(str(bundle_tree / "cron" / "wiki"))
    sys.modules.pop("utils", None)
    sys.modules.pop("wiki_flush", None)
    spec = importlib.util.spec_from_file_location(
        "wiki_flush", bundle_tree / "cron" / "wiki" / "wiki-flush-sessions.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    assert mod.BACKLOG_MAX == 0


# ── local-only provider: the endpoint must actually be local ────────────────

@pytest.mark.parametrize("url,expected", [
    ("http://localhost:11434/v1", True),
    ("http://127.0.0.1:8080/v1", True),
    ("http://[::1]:8080/v1", True),
    ("https://api.example.com/v1", False),
    # TEST-NET-2 (RFC 5737): reserved for documentation, never a real host.
    ("http://198.51.100.7:11434/v1", False),
])
def test_is_local_endpoint(bundle_tree: Path, monkeypatch, url: str, expected: bool):
    utils = _import_utils(monkeypatch, bundle_tree)
    assert utils._is_local_endpoint(url) is expected


def test_local_provider_refuses_remote_endpoint(bundle_tree: Path, monkeypatch, capsys):
    """`local` promises "nothing leaves this machine" — so a remote URL sends nothing.

    requests is never even imported on this path: the refusal happens before the
    POST, which is the only place it still helps.
    """
    monkeypatch.setenv("LOCAL_LLM_BASE_URL", "https://api.example.com/v1")
    monkeypatch.setenv("LOCAL_LLM_MODEL", "whatever")
    monkeypatch.setenv("WIKI_LLM_PROVIDER", "local")
    utils = _import_utils(monkeypatch, bundle_tree)
    assert utils.llm_call("prompt") is None
    assert "REFUSED" in capsys.readouterr().err


def test_local_provider_allows_named_host(bundle_tree: Path, monkeypatch):
    """An explicitly allow-listed host is a deliberate decision, so it passes the gate."""
    monkeypatch.setenv("LOCAL_LLM_BASE_URL", "http://inference.lan:11434/v1")
    monkeypatch.setenv("LOCAL_LLM_ALLOWED_HOSTS", "inference.lan")
    utils = _import_utils(monkeypatch, bundle_tree)
    assert utils._is_local_endpoint("http://inference.lan:11434/v1") is True


# ── privacy manifest: malformed = deny everything, uniformly ────────────────

@pytest.mark.parametrize("body", [
    "skip_projects: notalist\n",              # wrong list type
    "project_map: [a, b]\n",                  # wrong map type
    "project_map:\n  dir: 1.0\n",             # non-string value
    "collect_plans: 'yes'\n",                 # string where a bool belongs
    "- just\n- a\n- list\n",                  # not a mapping at all
])
def test_broken_manifest_denies_every_project(bundle_tree: Path, monkeypatch, body: str):
    pytest.importorskip("yaml")
    (bundle_tree / "bundle.local.yaml").write_text(body, encoding="utf-8")
    utils = _import_utils(monkeypatch, bundle_tree)
    assert utils.project_allowed("anything") is False


def test_broken_manifest_is_visible_not_just_enforced(bundle_tree: Path, monkeypatch):
    """Denying everything silently is the failure this pair of helpers closes.

    The status page and the flush log both printed `allow_projects=ALL` while
    project_allowed() was refusing every project, so an unreadable policy read
    as a healthy night with nothing to do.
    """
    pytest.importorskip("yaml")
    (bundle_tree / "bundle.local.yaml").write_text("skip_projects: notalist\n",
                                                   encoding="utf-8")
    utils = _import_utils(monkeypatch, bundle_tree)
    assert utils.manifest_broken() is True
    assert "DENIED" in utils.policy_summary()
    assert "ALL" not in utils.policy_summary()


def test_valid_manifest_allows(bundle_tree: Path, monkeypatch):
    pytest.importorskip("yaml")
    (bundle_tree / "bundle.local.yaml").write_text(
        "allow_projects:\n  - alpha\ncollect_plans: false\n", encoding="utf-8")
    utils = _import_utils(monkeypatch, bundle_tree)
    assert utils.project_allowed("alpha") is True
    assert utils.project_allowed("beta") is False
    assert utils.manifest_broken() is False
    assert "allow_projects=['alpha']" in utils.policy_summary()


# ── state migration: the @size suffix is part of the key ────────────────────

# ── secret shapes: three detectors, one table, one fixture set ──────────────
# The three consumers each kept their own list and had drifted: mask_secrets
# knew nothing about JWTs, `ccr-…` keys or a GCP private_key_id, and the
# public-repo gate knew about none of those plus AKIA and Telegram tokens. The
# failure that follows is concrete: a failing test prints a JWT, the tail is
# "masked" (miss), the token lands in a project's FINDINGS.md and in Telegram,
# and the nightly push guard then blocks that repo every night.
#
# One example per format. Every one must be caught by ALL THREE consumers, or
# the copies have drifted again.

def _shapes():
    sys.path.insert(0, str(CRON / "lib"))
    sys.modules.pop("secret_shapes", None)
    return importlib.import_module("secret_shapes")


SECRET_FIXTURES = {
    # The only fixture that is a literal rather than a concatenation, so it is
    # the only one the guards would flag in this very file. That is what the
    # inline marker is for (cron/lib/secret-scan.sh): a detector's own test
    # data must look exactly like the thing it detects.
    "pem": "-----BEGIN RSA PRIVATE KEY-----",  # secret-scan:allow
    "github-pat": "ghp_" + "a" * 30,
    "github-fine-grained": "github_pat_" + "b" * 30,
    "aws": "AKIA" + "C" * 16,
    "slack": "xoxb-" + "1" * 20,
    "openai": "sk-" + "d" * 32,
    "google": "AIza" + "e" * 32,
    "ccr": "ccr-" + "f" * 20,
    "jwt": "eyJ" + "a" * 20 + "." + "b" * 20 + "." + "c" * 20,
    "gcp-key-id": '"private_key_id": "' + "0" * 40 + '"',
    "telegram": "1234567890:" + "A" * 35,
}


@pytest.mark.parametrize("name,sample", sorted(SECRET_FIXTURES.items()))
def test_every_secret_shape_is_caught_by_the_commit_guard(name: str, sample: str):
    shapes = _shapes()
    assert shapes.scan_regex().search(sample), f"{name} would be committable"


@pytest.mark.parametrize("name,sample", sorted(SECRET_FIXTURES.items()))
def test_every_secret_shape_is_masked(name: str, sample: str):
    shapes = _shapes()
    masked = shapes.mask(f"the value is {sample} ok")
    assert sample not in masked, f"{name} survived masking into a log/alert"


@pytest.mark.parametrize("name,sample", sorted(SECRET_FIXTURES.items()))
def test_every_secret_shape_is_refused_for_a_public_repo(name: str, sample: str):
    shapes = _shapes()
    assert shapes.leak_regex().search(sample), \
        f"{name} could be written into a public repo's AGENTS.md"


def test_shell_scan_pattern_is_the_generated_one():
    """cron/lib/secret-scan.sh carries a LITERAL copy — this is what checks it.

    The shell hook must work with no Python on PATH, so the alternation cannot
    be generated at run time. It can be verified, and that is the difference
    between a copy and a fork.
    """
    shapes = _shapes()
    text = (CRON / "lib" / "secret-scan.sh").read_text(encoding="utf-8")
    m = re.search(r"(?m)^SECRET_SCAN_PATTERN='(.*)'$", text)
    assert m, "SECRET_SCAN_PATTERN not found in cron/lib/secret-scan.sh"
    assert m.group(1) == shapes.shell_ere(), (
        "the shell copy has drifted — regenerate it with "
        "`python home-claude/cron/lib/secret_shapes.py`")


def test_private_addresses_are_leak_only():
    """A LAN address is not a credential.

    It must never block a commit (documentation legitimately discusses RFC1918
    ranges) and must never be masked out of a log (that is debugging
    information) — but it has no business in a public repo's AGENTS.md.
    """
    shapes = _shapes()
    # 10.x rather than a 192.168.x address that looks like somebody's home LAN:
    # this is a PUBLIC repo, and its own `.sanitize-patterns` denylist rightly
    # refuses the latter. Same RFC1918 class, so the assertion is unchanged.
    sample = "the box at 10.0.0.1 answers"
    assert shapes.leak_regex().search(sample)
    assert not shapes.scan_regex().search(sample)
    assert shapes.mask(sample) == sample


# ── .env parsing: env wins over the file, in BOTH implementations ───────────

def _bash():
    import shutil
    return (shutil.which("bash")
            or next((p for p in (r"C:\Program Files\Git\bin\bash.exe",)
                     if Path(p).is_file()), None))


@pytest.mark.skipif(_bash() is None, reason="bash not available")
def test_shell_dotenv_does_not_override_the_environment(tmp_path: Path):
    """`export "$key=$val"` was unconditional in all five shell copies.

    So .env beat the real environment — the opposite of the Python loader and
    of what the comment above it claimed. With no attacker involved: a
    PYTHON_EXE exported for the task was silently replaced by a stale value
    from the file, and a `PATH=` line changed which curl and python ran.
    """
    env_file = tmp_path / ".env"
    env_file.write_text("ALREADY_SET=from-dotenv\nONLY_IN_FILE=from-dotenv\n",
                        encoding="utf-8")
    script = tmp_path / "probe.sh"
    lib = (CRON / "lib" / "dotenv.sh").as_posix()
    script.write_text(
        f". '{lib}'\n"
        f"dotenv_load '{env_file.as_posix()}'\n"
        'printf "%s|%s\\n" "$ALREADY_SET" "$ONLY_IN_FILE"\n',
        encoding="utf-8", newline="\n")
    env = dict(os.environ, ALREADY_SET="from-environment")
    out = subprocess.run([_bash(), str(script)], capture_output=True, text=True,
                         env=env, timeout=60).stdout.strip()
    assert out == "from-environment|from-dotenv"


def test_python_dotenv_does_not_override_the_environment(bundle_tree: Path, monkeypatch):
    (bundle_tree / ".env").write_text("ALREADY_SET=from-dotenv\nONLY_IN_FILE=from-dotenv\n",
                                      encoding="utf-8")
    monkeypatch.setenv("ALREADY_SET", "from-environment")
    utils = _import_utils(monkeypatch, bundle_tree)
    utils._load_dotenv()
    assert os.environ["ALREADY_SET"] == "from-environment"
    assert os.environ["ONLY_IN_FILE"] == "from-dotenv"


# ── markdown: a heading inside fenced code is not a heading ─────────────────

def test_fenced_headings_are_not_treated_as_markup(bundle_tree: Path, monkeypatch):
    """Six functions honored this rule and two did not.

    `_LLM_H2_RE.sub` in the flush demoted a `## …` line inside a ``` block —
    editing the user's own markdown example into the daily log — and
    parse_daily_by_project started a new project section on it, cutting the
    code block in half across two projects.
    """
    utils = _import_utils(monkeypatch, bundle_tree)
    text = "## real\nbody\n```\n## not a heading\n```\n## also real\n"
    flagged = [line for line, in_code in utils.iter_md_lines(text) if in_code]
    assert "## not a heading" in flagged
    out = utils.sub_outside_fences(r"(?m)^## (.+)$", r"### \1", text)
    assert "## not a heading" in out
    assert "### real" in out and "### also real" in out


# ── retry ceiling: a deterministic failure must not replay forever ──────────

def test_attempt_counter_bumps_and_resets(bundle_tree: Path, monkeypatch):
    """Without a ceiling, a source rejected the same way every night replays

    identically: same call, same rejection, same exit 1, same 03:00 alert — and
    no run brings the next one closer to succeeding.
    """
    utils = _import_utils(monkeypatch, bundle_tree)
    assert utils.attempt_count("compile_sessions", "k") == 0
    assert utils.attempt_bump("compile_sessions", "k") == 1
    assert utils.attempt_bump("compile_sessions", "k") == 2
    assert utils.attempt_count("compile_sessions", "k") == 2
    utils.attempt_reset("compile_sessions", "k")
    assert utils.attempt_count("compile_sessions", "k") == 0


def test_bundle_finding_is_filed_once(bundle_tree: Path, monkeypatch):
    """The alternative to an unbounded retry loop must not be an unbounded
    pile of identical findings."""
    utils = _import_utils(monkeypatch, bundle_tree)
    assert utils.append_bundle_finding("gave up on X", "ctx", "what", "how") is True
    assert utils.append_bundle_finding("gave up on X", "ctx", "what", "how") is False
    body = (bundle_tree / "FINDINGS.md").read_text(encoding="utf-8")
    assert body.count("gave up on X") == 1
    assert body.startswith("# Findings")


# ── the off-box gate applies to the FIRST call, not just the fallback ───────

def test_allow_offbox_zero_refuses_the_primary_provider(bundle_tree: Path,
                                                        monkeypatch, capsys):
    """WIKI_OFFBOX_FALLBACK=0 never did this, though its comment claimed it.

    That flag only stops the chain STEPPING to the next provider; the first
    one — DeepSeek on the shipped default — was called either way.
    """
    monkeypatch.delenv("WIKI_LLM_PROVIDER", raising=False)  # CI runs with mock
    monkeypatch.setenv("WIKI_ALLOW_OFFBOX", "0")
    monkeypatch.setenv("DEEPSEEK_KEY", "irrelevant")
    utils = _import_utils(monkeypatch, bundle_tree)
    assert utils.llm_call("prompt") is None
    assert "WIKI_ALLOW_OFFBOX=0" in capsys.readouterr().err


def test_state_migration_keeps_jsonl_size(bundle_tree: Path, monkeypatch):
    """Dropping @size yields a legacy key that matches at ANY size, so a growing
    session file would never be re-read after a corrupt-state rebuild."""
    wiki = bundle_tree / "wiki"
    wiki.mkdir(parents=True, exist_ok=True)
    (wiki / "log.md").write_text(
        "- [flush] processed: proj/abc.jsonl@4096 (project: proj)\n", encoding="utf-8")
    utils = _import_utils(monkeypatch, bundle_tree)
    migrated = utils._migrated_state_from_log()
    assert migrated["flush"]["processed_jsonls"] == ["proj/abc.jsonl@4096"]
