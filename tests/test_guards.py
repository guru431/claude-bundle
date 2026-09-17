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
    # monkeypatch, not sys.modules.pop: the module that was there comes back after
    # the test. A bare pop left every later importer with a NEW object while
    # modules loaded earlier kept the old one, so a patch applied to one never
    # reached the other.
    monkeypatch.syspath_prepend(str(bundle_root / "cron" / "hooks"))
    monkeypatch.delitem(sys.modules, "utils", raising=False)
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
    monkeypatch.delitem(sys.modules, "utils", raising=False)
    monkeypatch.delitem(sys.modules, "wiki_flush", raising=False)
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


def test_sensitive_path_tables_are_the_generated_ones():
    """The two path tables in the shell library are generated too.

    Three hand-written copies of "which filenames must never be committed" used
    to exist — in pre-commit, github-push.sh and git-push-all.sh — and they
    disagreed on `.env.example` and knew nothing of `credentials.json`,
    `.npmrc`, `.netrc`, `.pypirc`, `*.ppk`, `*.jks`, `id_ecdsa`,
    `.git-credentials` or `terraform.tfstate`.
    """
    shapes = _shapes()
    text = (CRON / "lib" / "secret-scan.sh").read_text(encoding="utf-8")
    for var, generated in (("SENSITIVE_PATH_PATTERN", shapes.sensitive_path_ere()),
                           ("SENSITIVE_PATH_ALLOW", shapes.sensitive_path_allow_ere())):
        m = re.search(rf"(?m)^{var}='(.*)'$", text)
        assert m, f"{var} not found in cron/lib/secret-scan.sh"
        assert m.group(1) == generated, (
            f"the shell copy of {var} has drifted — regenerate it with "
            f"`python home-claude/cron/lib/secret_shapes.py paths`")


# Concrete strings, and what each detector must say about them. Every entry in
# the first list is a real false positive that blocked a commit, and every entry
# in the second is a real credential format that went through untouched.
_MUST_NOT_MATCH = [
    "task-management-system-v2",              # `sk-` inside an ordinary slug
    "--disk-usage-threshold-pct 90",          # `ccr-` inside a flag name
    "mask-secrets-in-output",
    "kiosk-mode-launcher-2024",               # mask() turned this into `kio[REDACTED]`
    "Python 3.10.0.1",                        # the `10.` branch had 3 octets
    "artifact 1693526400:" + "a" * 40,        # a timestamp plus a sha1
]
_MUST_MATCH = [
    "ghp_" + "A" * 24, "ghs_" + "A" * 24, "ghu_" + "B" * 24,
    "glpat-" + "a" * 22, "npm_" + "b" * 36, "sk_live_" + "c" * 24,
    "hf_" + "d" * 32, "SG." + "e" * 20 + "." + "f" * 20,
    "xoxe-" + "g" * 20, "hooks.slack.com/services/" + "H" * 24,
    "ASIA" + "B" * 16,
    # Assembled rather than written out: a detector's own fixture that spells a
    # real header verbatim matches ITSELF, and then the repo's secret guard
    # blocks every commit touching this file. `# secret-scan:allow` is the
    # documented escape hatch, but not writing the literal at all is better.
    "-----BEGIN PGP PRIVATE" + " KEY BLOCK-----",
    "AccountKey=" + "z" * 44,
    "postgres://user:" + "pass@host/db",
    "aws_secret_access_key = " + "k" * 40,
]


@pytest.mark.parametrize("sample", _MUST_NOT_MATCH)
def test_ordinary_text_is_not_a_secret(sample):
    """A false positive here blocks a commit, a push, CI and the nightly sweep.

    It also mangles output: `mask()` shares this table, so `kiosk-mode-launcher`
    came out of a FINDINGS entry as `kio[REDACTED-API-KEY]`.
    """
    shapes = _shapes()
    assert not shapes.scan_regex().search(sample), f"false positive: {sample!r}"
    assert shapes.mask(sample) == sample, f"mask() mangled ordinary text: {sample!r}"


@pytest.mark.parametrize("sample", _MUST_MATCH)
def test_real_credential_formats_are_caught(sample):
    shapes = _shapes()
    assert shapes.scan_regex().search(sample), f"missed credential format: {sample[:24]!r}"


def test_sensitive_paths_cover_the_common_credential_files():
    shapes = _shapes()
    for p in (".env", "conf/.env.production.local", "id_ecdsa", "app/credentials.json",
              ".npmrc", ".netrc", ".pypirc", "keys/site.ppk", "keys/store.jks",
              ".git-credentials", "infra/terraform.tfstate", "cfg/secrets.yaml"):
        assert shapes.is_sensitive_path(p), f"not treated as sensitive: {p}"
    for p in (".env.example", "config/llm-providers.example.env", ".env.sample",
              "README.md", "keys/site.pub"):
        assert not shapes.is_sensitive_path(p), f"wrongly blocked: {p}"


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
    """A bash that understands the paths we hand it — not merely the first in PATH.

    Windows ships `C:\\Windows\\System32\\bash.exe`, the WSL launcher. It is a
    `bash` by name only: given a Windows-shaped script path it prints nothing and
    exits, so this test failed while passing by hand from Git Bash. Task
    Scheduler's session 0 has System32 in PATH and Git\\bin not, which is exactly
    where the nightly sweep runs.
    """
    import os
    import shutil
    import subprocess

    # An EXPLICIT override first, then git's own answer, then PATH, and only
    # then the two hardcoded Program Files locations. The hardcoded pair used to
    # come first and was the only real path: on a scoop/portable/D:-drive Git
    # this returned None, the test SKIPPED, and the invariant it protects —
    # env > dotenv, in the shell parser — vanished with no signal at all.
    candidates = []
    for env_name in ("CLAUDE_CODE_GIT_BASH_PATH", "BASH_EXE"):
        if os.environ.get(env_name):
            candidates.append(os.environ[env_name])
    try:
        exec_path = subprocess.run(["git", "--exec-path"], capture_output=True,
                                   text=True, timeout=15).stdout.strip()
        if exec_path:
            # <git>/mingw64/libexec/git-core → <git>/usr/bin/bash.exe
            git_root = Path(exec_path)
            for _ in range(3):
                git_root = git_root.parent
            candidates += [str(git_root / "usr" / "bin" / "bash.exe"),
                           str(git_root / "bin" / "bash.exe")]
    except (OSError, subprocess.SubprocessError):
        pass
    found = shutil.which("bash")
    if found and Path(found).parent.name.lower() != "system32":
        candidates.append(found)         # System32\bash.exe is the WSL launcher
    # `usr\bin` before `bin`: the latter prepends /mingw64/bin:/usr/bin to any
    # PATH handed to it, which quietly outranks a caller's own entries.
    candidates += [r"C:\Program Files\Git\usr\bin\bash.exe",
                   r"C:\Program Files\Git\bin\bash.exe"]
    for candidate in candidates:
        if candidate and Path(candidate).is_file():
            return candidate
    return None


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


# The parser-parity test that lived here moved to tests/test_dotenv_parity.py,
# which runs ONE shared fixture through all four .env parsers.


def test_python_dotenv_does_not_override_the_environment(bundle_tree: Path, monkeypatch):
    (bundle_tree / ".env").write_text("ALREADY_SET=from-dotenv\nONLY_IN_FILE=from-dotenv\n",
                                      encoding="utf-8")
    monkeypatch.setenv("ALREADY_SET", "from-environment")
    utils = _import_utils(monkeypatch, bundle_tree)
    utils._load_dotenv()
    assert os.environ["ALREADY_SET"] == "from-environment"
    assert os.environ["ONLY_IN_FILE"] == "from-dotenv"


# ── UTF-16: the encoding all three detectors used to be blind to ────────────

def _utf16_blob(tmp_path: Path) -> Path:
    """A UTF-16LE file carrying a token-shaped string, BOM and all.

    UTF-16 is what `>` and `Out-File` produce by default in Windows
    PowerShell 5.1, and this is a Windows-first bundle. `grep -I` calls such a
    file binary and reports nothing, so pre-commit, pre-push and the nightly
    sweep all answered "clean" for a key a text editor shows plainly.
    """
    path = tmp_path / "notes.txt"
    body = "hello\n" + "ghp_" + "A" * 30 + "\n"          # secret-scan:allow
    path.write_bytes(b"\xff\xfe" + body.encode("utf-16-le"))
    return path


@pytest.mark.skipif(_bash() is None, reason="bash not available")
def test_secret_scan_reads_utf16(tmp_path: Path):
    """secret_scan_text transcodes before grepping; the raw bytes match nothing."""
    blob = _utf16_blob(tmp_path)
    lib = (CRON / "lib" / "secret-scan.sh").as_posix()
    script = tmp_path / "probe.sh"
    script.write_text(
        f". '{lib}'\n"
        f"if secret_scan_text < '{blob.as_posix()}'; then echo MISSED; "
        f"else echo CAUGHT; fi\n",
        encoding="utf-8", newline="\n")
    res = subprocess.run([_bash(), str(script)], capture_output=True, text=True,
                         timeout=60)
    assert "CAUGHT" in res.stdout, \
        f"a UTF-16 file with a token scanned clean:\n{res.stdout}\n{res.stderr}"


@pytest.mark.skipif(_bash() is None, reason="bash not available")
def test_secret_scan_diff_names_the_file(tmp_path: Path):
    """A hit has to say WHICH file. `grep -n` numbered the already-filtered
    stream, so the report was an ordinal matching nothing the author could open.
    """
    lib = (CRON / "lib" / "secret-scan.sh").as_posix()
    diff = tmp_path / "sample.diff"
    diff.write_text(
        "diff --git a/src/app.py b/src/app.py\n"
        "--- a/src/app.py\n"
        "+++ b/src/app.py\n"
        '+token = "ghp_' + "B" * 30 + '"\n',             # secret-scan:allow
        encoding="utf-8", newline="\n")
    script = tmp_path / "probe.sh"
    script.write_text(
        f". '{lib}'\n"
        f"secret_scan_diff < '{diff.as_posix()}' || true\n",
        encoding="utf-8", newline="\n")
    res = subprocess.run([_bash(), str(script)], capture_output=True, text=True,
                         timeout=60)
    assert "src/app.py" in res.stdout, \
        f"the hit did not name the file:\n{res.stdout}\n{res.stderr}"


def test_a_flag_that_exists_only_in_dotenv_reaches_its_constant(bundle_tree: Path,
                                                                monkeypatch):
    """`.env` is loaded BEFORE the constants are computed, not after.

    WIKI_RETRY_LIMIT was read at import time from os.environ while _load_dotenv()
    ran further down the module, so under Task Scheduler — session 0, where the
    user environment does not exist and `.env` is the only source — the ceiling
    was always the hardcoded 3 and `0` ("no ceiling") could not be set at all.
    Every other flag was read after the load; this one was the single ordering
    violation, which is exactly the kind that survives review.
    """
    (bundle_tree / ".env").write_text("WIKI_RETRY_LIMIT=7\n", encoding="utf-8")
    monkeypatch.delenv("WIKI_RETRY_LIMIT", raising=False)
    utils = _import_utils(monkeypatch, bundle_tree)
    assert utils.RETRY_LIMIT == 7, \
        "a value present only in .env did not reach the module constant"


def test_a_typo_in_the_provider_name_sends_nothing(bundle_tree: Path, monkeypatch):
    """An unknown WIKI_LLM_PROVIDER refuses every call instead of falling back.

    The old behaviour warned on stderr and routed to `deepseek`. The plausible
    typo is a privacy-motivated one — `=lokal`, meant to be `local` — and Task
    Scheduler discards stderr, so the entire pipeline shipped its transcripts to
    three off-box gateways while the person who set it believed nothing was
    leaving the machine.
    """
    monkeypatch.setenv("WIKI_LLM_PROVIDER", "lokal")
    utils = _import_utils(monkeypatch, bundle_tree)
    assert utils.LLM_PROVIDER_INVALID is True
    res = utils.llm_call_ex("anything")
    assert res.text is None
    assert res.kind == "config", "a typo is not something that clears on its own"
    assert utils.llm_call("anything") is None
    assert "INVALID" in " ".join(utils.config_report())


@pytest.mark.parametrize("value", ["2999-01-02", "2999-01-02 10:00",
                                   "2999-01-02 10:00:00"])
def test_dry_run_until_accepts_a_value_carrying_a_time(bundle_tree: Path, monkeypatch,
                                                       value: str):
    """Both spellings of "with a time" used to defeat the brake, differently.

    `10:00:00` is a valid YAML timestamp, so PyYAML returns a datetime — and
    datetime IS a date subclass, so it passed the isinstance check and
    `date.today() < DRY_RUN_UNTIL` raised TypeError inside load_state(), i.e. in
    every phase. `10:00` is NOT a valid YAML timestamp, so it stayed a string,
    date.fromisoformat rejected it, and the field was ignored — which for this
    field means the first night shipped the archive off-box before anyone read a
    preview.
    """
    pytest.importorskip("yaml")
    (bundle_tree / "bundle.local.yaml").write_text(
        f"dry_run_until: {value}\n", encoding="utf-8")
    utils = _import_utils(monkeypatch, bundle_tree)
    assert utils.DRY_RUN_UNTIL is not None, f"{value!r} was ignored, not parsed"
    assert utils.is_dry_run([]) is True


def test_the_dry_run_banner_prints_once_per_process(bundle_tree: Path, monkeypatch,
                                                    capsys):
    """is_dry_run() is called from load_state(), which every helper touches, so
    an unguarded banner put dozens of identical lines into a night's log and
    buried everything else in it."""
    pytest.importorskip("yaml")
    (bundle_tree / "bundle.local.yaml").write_text(
        "dry_run_until: 2999-01-02\n", encoding="utf-8")
    utils = _import_utils(monkeypatch, bundle_tree)
    capsys.readouterr()
    assert utils.is_dry_run([]) is True
    first = capsys.readouterr()
    for _ in range(5):
        assert utils.is_dry_run([]) is True
    again = capsys.readouterr()
    assert "dry-run" in (first.out + first.err)
    assert "dry-run" not in (again.out + again.err), \
        "the dry-run banner repeats on every call"


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


# ── integer flags: out of range is an error, not a silent clamp ─────────────

def test_a_negative_integer_flag_keeps_its_default_and_says_so(bundle_tree: Path,
                                                               monkeypatch):
    """`minimum` used to CLAMP, and for these two flags the clamp inverted them.

    WIKI_RETRY_LIMIT=-1 became 0, which the code reads as "no ceiling", so a typo
    meant unbounded retries; WIKI_LLM_LOCK_WAIT=-5 became 0 and switched the
    provider queue off. config_report then showed a plain `0` as if asked for.
    """
    monkeypatch.setenv("WIKI_RETRY_LIMIT", "-1")
    monkeypatch.setenv("WIKI_LLM_LOCK_WAIT", "-5")
    utils = _import_utils(monkeypatch, bundle_tree)
    assert utils.RETRY_LIMIT == 3, "a negative ceiling was clamped to 'no ceiling'"
    assert utils.LLM_LOCK_WAIT == 900, "a negative wait switched the queue off"
    errors = " ".join(utils.config_errors())
    assert "WIKI_RETRY_LIMIT" in errors and "WIKI_LLM_LOCK_WAIT" in errors
    assert "INVALID '-1'" in " ".join(utils.config_report())


def test_zero_is_still_a_valid_retry_limit(bundle_tree: Path, monkeypatch):
    """0 is documented ("restores unbounded retries") and is at the minimum."""
    monkeypatch.setenv("WIKI_RETRY_LIMIT", "0")
    utils = _import_utils(monkeypatch, bundle_tree)
    assert utils.RETRY_LIMIT == 0
    assert not utils.config_errors()


# ── manifest keys: a typo'd policy field must not silently do nothing ──────

@pytest.mark.parametrize("key", ["skip_project", "allow_project", "Skip_Projects",
                                 "skip-projects", "dry_run_untl"])
def test_a_near_miss_manifest_key_denies_every_project(bundle_tree: Path, monkeypatch,
                                                       key: str):
    """`skip_project: [secret]` used to be a WARNING the launcher discarded.

    Nothing else knew: config_report, policy_summary and bundle-status all
    carried on as if the key were absent, so the project the user meant to
    exclude went to the provider — the one fail-open in the manifest block.
    """
    pytest.importorskip("yaml")
    (bundle_tree / "bundle.local.yaml").write_text(f"{key}:\n  - secret\n",
                                                   encoding="utf-8")
    utils = _import_utils(monkeypatch, bundle_tree)
    assert utils.manifest_broken() is True
    assert utils.project_allowed("secret") is False
    assert "DENIED" in utils.policy_summary()
    assert any(key in e for e in utils.config_errors())


def test_an_unrelated_unknown_manifest_key_is_reported_not_denied(bundle_tree: Path,
                                                                  monkeypatch):
    """Not every unknown key is a policy typo — but it must still be visible."""
    pytest.importorskip("yaml")
    (bundle_tree / "bundle.local.yaml").write_text(
        "notes: my own reminder\n1: numeric\n", encoding="utf-8")
    utils = _import_utils(monkeypatch, bundle_tree)
    assert utils.manifest_broken() is False
    assert utils.project_allowed("anything") is True
    assert any("notes" in e for e in utils.config_errors())
    assert "ERRORS:" in utils.config_report()


# ── the privacy gate speaks ONE namespace: the normalized project name ─────

@pytest.mark.parametrize("policy,raw", [
    ("skip_projects:\n  - claudebundle\n", "ClaudeBundle"),   # the wiki folder name
    ("skip_projects:\n  - ClaudeBundle\n", "claudebundle"),   # the directory's spelling
    ("skip_projects:\n  - my-app\n", "My App"),
])
def test_skip_projects_matches_every_spelling_of_the_project(bundle_tree: Path,
                                                             monkeypatch,
                                                             policy: str, raw: str):
    """The JSONL collector normalized before asking, the feedback/incidents
    collectors and the hooks did not — so one spelling in the policy closed one
    set of sources and left the other open."""
    pytest.importorskip("yaml")
    (bundle_tree / "bundle.local.yaml").write_text(policy, encoding="utf-8")
    utils = _import_utils(monkeypatch, bundle_tree)
    assert utils.project_allowed(raw) is False
    assert utils.project_allowed(utils.normalize_project_name(raw)) is False
    assert utils.project_allowed("other") is True


def test_allow_projects_matches_the_raw_directory_name(bundle_tree: Path, monkeypatch):
    pytest.importorskip("yaml")
    (bundle_tree / "bundle.local.yaml").write_text(
        "allow_projects:\n  - claudebundle\n", encoding="utf-8")
    utils = _import_utils(monkeypatch, bundle_tree)
    assert utils.project_allowed("ClaudeBundle") is True
    assert utils.project_allowed("Other") is False


def test_an_allow_entry_without_a_usable_name_does_not_allow_main(bundle_tree: Path,
                                                                  monkeypatch):
    """Normalizing policy entries must not WIDEN the allowlist: an entry the
    normalizer cannot slug falls into `main`, and `main` is every unattributed
    source."""
    pytest.importorskip("yaml")
    long_name = "x" * 45
    (bundle_tree / "bundle.local.yaml").write_text(
        f"allow_projects:\n  - {long_name}\n  - alpha\n", encoding="utf-8")
    utils = _import_utils(monkeypatch, bundle_tree)
    assert utils.project_allowed("alpha") is True
    assert utils.project_allowed("main") is False
    assert any(long_name in e for e in utils.config_errors())


def test_feedback_of_a_camelcase_project_is_not_collected_when_skipped(
        bundle_tree: Path, monkeypatch):
    """End to end on the collector that leaked: feedback files of `…-ClaudeBundle`
    went to the provider under `skip_projects: [claudebundle]`."""
    pytest.importorskip("yaml")
    (bundle_tree / "bundle.local.yaml").write_text(
        "skip_projects:\n  - claudebundle\n", encoding="utf-8")
    home = bundle_tree / "fake-home"
    mem = home / "projects" / "C--work-ClaudeBundle" / "memory"
    mem.mkdir(parents=True)
    (mem / "feedback_rule.md").write_text("never do X\n", encoding="utf-8")
    (mem / "incidents.md").write_text("an incident\n", encoding="utf-8")
    monkeypatch.setenv("CLAUDE_HOME", str(home))
    monkeypatch.syspath_prepend(str(bundle_tree / "cron" / "hooks"))
    for name in ("utils", "untrusted", "runs"):
        monkeypatch.delitem(sys.modules, name, raising=False)
    spec = importlib.util.spec_from_file_location(
        "wiki_flush_f2", bundle_tree / "cron" / "wiki" / "wiki-flush-sessions.py")
    flush = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(flush)
    assert flush.collect_feedback_files() == {}
    assert flush.collect_incidents_sessions() == {}


# ── the state ledger's lock belongs to the OS, not to a file on disk ────────

def test_a_lock_file_left_behind_does_not_block_the_state_ledger(bundle_tree: Path,
                                                                 monkeypatch):
    """A crashed writer's lock file used to hold the ledger hostage.

    The exclusive-create lock trusted the PID written in the file. Windows
    reuses PIDs quickly, and once a live process owned that number the lock
    looked held for the whole 600-second stale window — every phase in it
    skipped its state write after its sources had already gone to the provider.
    An OS lock dies with its process, so a file on disk proves nothing.
    """
    import functools
    utils = _import_utils(monkeypatch, bundle_tree)
    utils.STATE_LOCK.parent.mkdir(parents=True, exist_ok=True)
    utils.STATE_LOCK.write_text(f"{os.getpid()} 2026-01-01T00:00:00\n", encoding="utf-8")
    monkeypatch.setattr(utils, "_state_lock",
                        functools.partial(utils._state_lock, timeout=0.5))
    utils.state_add("flush", "processed_jsonls", ["p/s.jsonl@10"])
    assert utils.state_get("flush", "processed_jsonls") == {"p/s.jsonl@10"}, \
        "a leftover lock file blocked the ledger write"


def test_the_state_lock_still_excludes_a_second_holder(bundle_tree: Path, monkeypatch):
    utils = _import_utils(monkeypatch, bundle_tree)
    with utils._state_lock(timeout=0) as held:
        assert held is True
        with utils._state_lock(timeout=0) as second:
            assert second is False, "two writers held the ledger lock at once"
    with utils._state_lock(timeout=0) as again:
        assert again is True, "the lock was not released"


def test_the_state_lock_falls_back_where_os_locks_are_unsupported(bundle_tree: Path,
                                                                  monkeypatch):
    """A filesystem without OS locks (NFS without a lock daemon, some FUSE and
    SMB mounts) must not turn every state write into a skipped one."""
    import errno
    utils = _import_utils(monkeypatch, bundle_tree)

    def unsupported(_fd):
        raise OSError(errno.ENOLCK, "No locks available")

    monkeypatch.setattr(utils, "_os_lock_try", unsupported)
    utils.state_add("compile_sessions", "compiled_pairs", ["2026-01-01#p@abc"])
    assert utils.state_get("compile_sessions", "compiled_pairs") == {"2026-01-01#p@abc"}
    assert not list(utils.STATE_LOCK.parent.glob(f"{utils.STATE_LOCK.name}.excl*")), \
        "the fallback lock was not released"


# ── state writes say whether they happened ──────────────────────────────────

def test_state_writes_report_a_marker_they_could_not_record(bundle_tree: Path,
                                                            monkeypatch):
    """A skipped write returned None, exactly like a successful one, so a phase
    logged a source as finished that the next run would pay to send again."""
    utils = _import_utils(monkeypatch, bundle_tree)
    assert utils.state_add("flush", "processed_jsonls", []) is True
    assert utils.state_add("flush", "processed_jsonls", ["p/a.jsonl@1"]) is True
    real_lock = utils._state_lock
    with real_lock(timeout=0) as held:
        assert held
        monkeypatch.setattr(utils, "_state_lock", lambda timeout=60.0: real_lock(timeout=0))
        assert utils.state_add("flush", "processed_jsonls", ["p/b.jsonl@1"]) is False
        assert utils.state_replace_prefix("flush", "processed_jsonls", ["p/a.jsonl@"],
                                          ["p/a.jsonl@2"]) is False
        assert utils.state_remove("flush", "processed_jsonls", ["p/a.jsonl@1"]) is False
    assert utils.state_get("flush", "processed_jsonls") == {"p/a.jsonl@1"}


def test_state_replace_prefix_keeps_one_key_per_source(bundle_tree: Path, monkeypatch):
    utils = _import_utils(monkeypatch, bundle_tree)
    utils.state_add("flush", "processed_jsonls",
                    ["p/s.jsonl@100", "p/s.jsonl@200", "p/o.jsonl@5", "p/s.jsonl.bak@1"])
    assert utils.state_replace_prefix("flush", "processed_jsonls", ["p/s.jsonl@"],
                                      ["p/s.jsonl@300"]) is True
    assert utils.state_get("flush", "processed_jsonls") == \
        {"p/o.jsonl@5", "p/s.jsonl.bak@1", "p/s.jsonl@300"}


# ── transcripts: one bad byte is not an empty (or fatal) session ────────────

def test_a_non_utf8_byte_does_not_empty_or_crash_a_transcript(bundle_tree: Path,
                                                              monkeypatch, capsys):
    """Strict UTF-8 everywhere else in the bundle is `errors="replace"`.

    parse_jsonl_delta returned no messages at the start offset — "nothing new" —
    so the session was filed as trivial every night without a log line, and
    is_subagent_jsonl raised UnicodeDecodeError, which is not an OSError, and
    took the whole flush down.
    """
    utils = _import_utils(monkeypatch, bundle_tree)
    jf = bundle_tree / "s.jsonl"
    good = b'{"type": "user", "message": {"role": "user", "content": "hello"}}\n'
    bad = b'{"type": "assistant", "message": {"role": "assistant", "content": "caf\xe9"}}\n'
    jf.write_bytes(good + bad + good)

    assert utils.is_subagent_jsonl(str(jf)) is False
    messages, end = utils.parse_jsonl_delta(str(jf), 0)
    assert [m["role"] for m in messages] == ["user", "assistant", "user"]
    assert end == jf.stat().st_size
    assert "not valid UTF-8" in capsys.readouterr().err
    # Resuming from a byte offset still lands on a line boundary.
    assert utils.parse_jsonl_delta(str(jf), len(good) + len(bad))[0][0]["text"] == "hello"
    assert len(utils.parse_jsonl_messages(str(jf), last_n=0)) == 3


def test_an_unreadable_transcript_is_a_failure_not_an_empty_delta(bundle_tree: Path,
                                                                 monkeypatch):
    utils = _import_utils(monkeypatch, bundle_tree)
    assert utils.parse_jsonl_delta(str(bundle_tree / "missing.jsonl"), 128) == ([], None)


def test_flush_carries_an_unreadable_transcript_over_instead_of_filing_it(
        bundle_tree: Path, monkeypatch):
    """A failed read was indistinguishable from a trivial session: the file was
    marked processed at the offset the read never passed."""
    import json
    home = bundle_tree / "fake-home"
    proj = home / "projects" / "C--work-readme"
    proj.mkdir(parents=True)
    (proj / "s.jsonl").write_bytes(b"x" * 20000)       # over the 10 KB floor
    monkeypatch.setenv("CLAUDE_HOME", str(home))
    monkeypatch.setenv("WIKI_LLM_PROVIDER", "mock")
    monkeypatch.setattr(sys, "argv", ["wiki-flush-sessions.py"])
    monkeypatch.syspath_prepend(str(bundle_tree / "cron"))
    monkeypatch.syspath_prepend(str(bundle_tree / "cron" / "hooks"))
    for name in ("utils", "untrusted", "runs"):
        monkeypatch.delitem(sys.modules, name, raising=False)
    spec = importlib.util.spec_from_file_location(
        "wiki_flush_unreadable", bundle_tree / "cron" / "wiki" / "wiki-flush-sessions.py")
    flush = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(flush)
    monkeypatch.setattr(flush, "parse_jsonl_delta", lambda path, start=0: ([], None))

    flush.main()

    state = json.loads((bundle_tree / "wiki" / ".processed.json").read_text(encoding="utf-8"))
    assert not state.get("flush", {}).get("processed_jsonls"), \
        "an unreadable transcript was marked processed"
    assert state["flush"]["seen_unprocessed"] == ["readme/s.jsonl"]


def test_a_journaled_compile_kb_failure_is_not_migrated_as_processed(bundle_tree: Path,
                                                                    monkeypatch):
    """compile-kb journals a failure as `(ERROR: <kind>)`; the migration skipped
    only the exact `(ERROR)`, so on a machine with no state file yet the first
    failed article was rebuilt as processed and never retried."""
    wiki = bundle_tree / "wiki"
    wiki.mkdir(parents=True, exist_ok=True)
    (wiki / "log.md").write_text(
        "- [compile-kb] processed: articles/bad.md → (ERROR: deterministic)\n"
        "- [compile-kb] processed: articles/empty.md → (ERROR: 0 applied)\n"
        "- [compile-kb] processed: articles/ok.md → created: kb/concepts/X.md\n",
        encoding="utf-8")
    utils = _import_utils(monkeypatch, bundle_tree)
    assert utils._migrated_state_from_log()["compile_kb"]["processed"] == ["articles/ok.md"]


# ── the end of the preview window: announced, or chosen ─────────────────────

def test_dry_run_until_confirm_previews_until_a_date_is_written(bundle_tree: Path,
                                                                 monkeypatch):
    pytest.importorskip("yaml")
    from datetime import date
    (bundle_tree / "bundle.local.yaml").write_text("dry_run_until: confirm\n",
                                                   encoding="utf-8")
    utils = _import_utils(monkeypatch, bundle_tree)
    assert utils.is_dry_run([]) is True
    assert utils.dry_run_last_night(date(9999, 12, 30)) is False, \
        "`confirm` has no last night — it ends when someone writes a date"
    assert "dry_run_until     = confirm" in "\n".join(utils.config_report())
    assert not utils.config_errors()


@pytest.mark.parametrize("today,expected", [
    ("2030-01-09", True), ("2030-01-08", False), ("2030-01-10", False), ("2030-01-11", False),
])
def test_the_last_night_of_a_dated_preview_window(bundle_tree: Path, monkeypatch,
                                                  today: str, expected: bool):
    """The night before the first real send — the one the pipeline announces."""
    pytest.importorskip("yaml")
    from datetime import date
    (bundle_tree / "bundle.local.yaml").write_text("dry_run_until: 2030-01-10\n",
                                                   encoding="utf-8")
    utils = _import_utils(monkeypatch, bundle_tree)
    assert utils.dry_run_last_night(date.fromisoformat(today)) is expected


# ── .env: the shared parser contract, Python side ───────────────────────────

def test_python_dotenv_follows_the_shared_quote_and_key_contract(bundle_tree: Path,
                                                                 monkeypatch):
    """ONE matching pair of surrounding quotes comes off, and keys are ASCII.

    `strip('"').strip("'")` removed every quote of either kind at the ends, so
    `""x""` and a mismatched `"x'` both came out as `x` where the other parsers
    keep what is inside one pair; and str.isalnum() let a Cyrillic key through
    that the shell parser drops. Precedence is unchanged: the environment beats
    the file, and the file's first occurrence of a key beats a later one.
    """
    expected = {
        "BDT_PAIR": '"double"',
        "BDT_MIXED": "\"abc'",
        "BDT_SINGLE": "single",
        "BDT_LONE": '"',
        "BDT_EMPTYQ": "",
        "BDT_INNER": 'a "quoted" word',
        "BDT_DUP": "first",
        "BDT_ENV": "from-env",
    }
    non_ascii = ["ПЕРЕМЕННАЯ", "BDT_ÄKEY"]
    for name in [*expected, *non_ascii]:
        monkeypatch.setenv(name, "placeholder")   # recorded, so removed after the test
        monkeypatch.delenv(name)
    monkeypatch.setenv("BDT_ENV", "from-env")
    (bundle_tree / ".env").write_text(
        'BDT_PAIR=""double""\n'
        "BDT_MIXED=\"abc'\n"
        "BDT_SINGLE='single'\n"
        'BDT_LONE="\n'
        'BDT_EMPTYQ=""\n'
        'BDT_INNER=a "quoted" word\n'
        "BDT_DUP=first\n"
        "BDT_DUP=second\n"
        "BDT_ENV=from-file\n"
        "ПЕРЕМЕННАЯ=cyrillic\n"
        "BDT_ÄKEY=latin\n",
        encoding="utf-8", newline="\n")
    _import_utils(monkeypatch, bundle_tree)          # importing runs _load_dotenv()
    assert {k: os.environ.get(k, "<unset>") for k in expected} == expected
    assert all(name not in os.environ for name in non_ascii), \
        "a non-ASCII key was set, which the shell parser would drop"


# ── project names: `Project X` is a label, `project-alpha` is a name ────────

@pytest.mark.parametrize("raw,expected", [
    # Names that merely start with the word — they used to lose it.
    ("project-alpha", "project-alpha"),
    ("Project-Alpha", "project-alpha"),
    ("project.alpha", "project-alpha"),
    ("project", "project"),                  # the trailing segment of `…-project`
    ("project_alpha", "project_alpha"),
    ("projects", "projects"),
    # The label forms the rule exists for — unchanged.
    ("Project Alpha", "alpha"),
    ("Project: alpha", "alpha"),
    ("Project:alpha", "alpha"),
    ("Project — extracted facts (claude-bundle)", "claude-bundle"),
    ("Project—extracted facts (claude-bundle)", "claude-bundle"),
    ("Project (finance)", "finance"),
    ("Project(finance)", "finance"),
    ("Project `finance` (notes)", "finance"),
    ("Project:", "main"),
])
def test_a_project_label_is_stripped_but_a_name_starting_with_project_is_kept(
        bundle_tree: Path, monkeypatch, raw: str, expected: str):
    """`^project\\b` also matched before the hyphen of `project-alpha` and at the
    end of a bare `project`, so those names were cut to nothing and fell into
    `main` — together, and with every unattributed source."""
    utils = _import_utils(monkeypatch, bundle_tree)
    assert utils.normalize_project_name(raw) == expected


def test_projects_named_project_something_are_separate_and_can_be_allowed(
        bundle_tree: Path, monkeypatch):
    """With F2 an `allow_projects: [project-alpha]` entry was reported unusable
    and allowed nothing, and two such projects shared one bucket."""
    pytest.importorskip("yaml")
    (bundle_tree / "bundle.local.yaml").write_text(
        "allow_projects:\n  - project-alpha\n", encoding="utf-8")
    utils = _import_utils(monkeypatch, bundle_tree)
    assert not utils.config_errors(), utils.config_errors()
    assert utils.project_allowed("project-alpha") is True
    assert utils.project_allowed("project-beta") is False
    assert utils.project_allowed("main") is False


def test_a_denial_that_covered_a_project_under_its_old_name_keeps_covering_it(
        bundle_tree: Path, monkeypatch):
    """Fixing the name must not start sending what a policy denied yesterday.

    Every `…-project` directory used to land in `main`; a policy denying `main`
    therefore denied them, and giving them a name of their own would otherwise
    have sent them the next night. The same for `project.alpha`, which used to
    become `alpha`.
    """
    pytest.importorskip("yaml")
    (bundle_tree / "bundle.local.yaml").write_text(
        "skip_projects:\n  - main\n  - alpha\n", encoding="utf-8")
    utils = _import_utils(monkeypatch, bundle_tree)
    assert utils.project_allowed("project") is False
    assert utils.project_allowed("project-beta") is False
    assert utils.project_allowed("project.alpha") is False
    assert utils.project_allowed("beta") is True
    assert utils.project_allowed("project_beta") is True   # never lost its name


# ── a corrupt ledger: reads leave no trace, the overwrite keeps one copy ────

def test_reading_a_corrupt_ledger_writes_nothing_and_its_overwrite_keeps_one_copy(
        bundle_tree: Path, monkeypatch):
    """load_state copied a broken .processed.json into cron/logs/rejected/ on
    every call — including from the READ-ONLY helpers, which a run or a monitor
    calls again and again — so a broken ledger multiplied into one time-stamped
    copy per second per caller. The copy exists to survive the overwrite, so it is
    taken there, once per content."""
    from datetime import datetime, timedelta
    utils = _import_utils(monkeypatch, bundle_tree)

    class Ticking(datetime):
        """Every now() is a second later: each old-style copy got its own name."""
        at = datetime(2030, 1, 1)

        @classmethod
        def now(cls, tz=None):
            cls.at += timedelta(seconds=1)
            return cls.at

    monkeypatch.setattr(utils, "datetime", Ticking)
    rejected = bundle_tree / "cron" / "logs" / "rejected"

    def copies() -> list[Path]:
        return sorted(rejected.glob("*corrupt-state*")) if rejected.is_dir() else []

    utils.STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
    broken = '{"flush": {"processed_jsonls": ["p/a.jsonl@1"'       # cut mid-write
    utils.STATE_PATH.write_text(broken, encoding="utf-8")
    for _ in range(3):
        assert utils.state_get("flush", "processed_jsonls") == set()
        assert utils.attempt_count("flush", "project:x") == 0
        assert utils.quarantined("flush") == []
        assert utils.quarantined_count("flush") == 0
    assert copies() == [], "reading a broken ledger wrote copies of it"
    assert utils.STATE_PATH.read_text(encoding="utf-8") == broken, "a read touched the ledger"

    # The write that replaces it keeps ONE copy of exactly what it destroys.
    assert utils.state_add("flush", "processed_jsonls", ["p/b.jsonl@9"]) is True
    assert len(copies()) == 1
    assert copies()[0].read_text(encoding="utf-8") == broken
    assert utils.state_get("flush", "processed_jsonls") == {"p/b.jsonl@9"}

    # The same corruption again is already preserved; a different one is not.
    utils.STATE_PATH.write_text(broken, encoding="utf-8")
    utils.state_add("flush", "processed_jsonls", ["p/c.jsonl@1"])
    assert len(copies()) == 1
    utils.STATE_PATH.write_text("[]", encoding="utf-8")
    utils.state_add("flush", "processed_jsonls", ["p/d.jsonl@1"])
    assert len(copies()) == 2
