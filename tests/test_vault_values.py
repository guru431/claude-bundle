"""Detection by the VALUES of the keys in .env: the masker and the outgoing gates.

What is guarded:
  * which values count — secret names only, 20 characters or more, no paths,
    URLs or pointers: otherwise the gate starts lying about code and prose;
  * a value never comes out — not in the output, not in a marker, only the NAME;
  * no file — nothing is done and nothing is found;
  * a git failure — `scan-error` and rc 2, not "clean";
  * a key the published tree already holds does not block again.

The values are synthetic and have no key shape — no form in `secret_shapes`
knows them, so only the detection by value finds them.
"""
from __future__ import annotations

import importlib.util
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
CRON = ROOT / "home-claude" / "cron"
_spec = importlib.util.spec_from_file_location("vault_values_t", CRON / "lib" / "vault_values.py")
vv = importlib.util.module_from_spec(_spec)
sys.modules["vault_values_t"] = vv
_spec.loader.exec_module(vv)

VALUE_A = "Zq9" * 10                  # 30 characters, no shape
VALUE_B = "Wk4-" * 6                  # 24
VALUE_LONG = VALUE_A + "Xx7" * 4      # contains VALUE_A — struck out whole

VAULT_TEXT = "\n".join([
    "# a comment",
    f"ALPHA_API_KEY={VALUE_A}",
    f'export BETA_TOKEN="{VALUE_B}"',
    f"GAMMA_KEY_2={VALUE_LONG}",
    "SHORT_PASSWORD=" + "s" * 12,                         # under 20
    "SSH_KEY_PATH=" + "~/.ssh/" + "k" * 20,               # a pointer by name
    "SERVICE_URL=https://example.invalid/" + "u" * 20,    # not a secret name
    "DB_SECRET=postgres://u:" + "p@h/" + "d" * 20,        # a URL (split so the gate does not read a password)
    "WIN_TOKEN=C:\\Users\\x\\" + "t" * 20,                # a path
    "PHRASE_PASSWORD=" + "word " * 6,                     # spaces
    "REFRESH_TOKEN=1//0" + "Rf3" * 10,                    # not a path
    "PLAIN_VALUE=" + "Pv5" * 10,                          # the name is not a secret one
    f"DUP_KEY={VALUE_A}",                                 # the same value again
])


@pytest.fixture
def vault(tmp_path, monkeypatch):
    p = tmp_path / "vault.env"
    p.write_text(VAULT_TEXT + "\n", encoding="utf-8")
    monkeypatch.setenv(vv.VAULT_ENV, str(p))
    return p


def test_only_secret_names_of_secret_length_and_no_pointers(vault):
    assert [n for n, _ in vv.load()] == ["ALPHA_API_KEY", "BETA_TOKEN", "GAMMA_KEY_2",
                                         "REFRESH_TOKEN"]


def test_mask_names_the_key_and_removes_the_whole_value(vault):
    text = f"a={VALUE_A} b={VALUE_B} c={VALUE_LONG}"
    out = vv.mask(text)
    assert out == ("a=[REDACTED-VAULT:ALPHA_API_KEY] b=[REDACTED-VAULT:BETA_TOKEN] "
                   "c=[REDACTED-VAULT:GAMMA_KEY_2]")
    assert vv.names_in(text) == ["ALPHA_API_KEY", "BETA_TOKEN", "GAMMA_KEY_2"]


def test_no_file_does_nothing(tmp_path, monkeypatch):
    monkeypatch.setenv(vv.VAULT_ENV, str(tmp_path / "missing.env"))
    assert vv.load() == []
    assert vv.mask(f"x {VALUE_A}") == f"x {VALUE_A}"
    assert vv.main(["text"]) == 0


def test_the_default_file_is_the_bundles_own_env(monkeypatch):
    """The bundle keeps its keys in the deployed `.env`, next to cron/ — the file
    utils.py::_load_dotenv reads. That is what is guarded when nothing is set."""
    monkeypatch.delenv(vv.VAULT_ENV, raising=False)
    assert vv.vault_path() == CRON.parent / ".env"


def test_mask_secrets_strikes_a_value_no_shape_knows(vault, tmp_path, monkeypatch):
    # utils derives its root from __file__, so it is loaded from a copy.
    shutil.copytree(CRON, tmp_path / "b" / "cron", ignore=shutil.ignore_patterns("logs", "state"))
    monkeypatch.syspath_prepend(str(tmp_path / "b" / "cron" / "hooks"))
    monkeypatch.delitem(sys.modules, "utils", raising=False)
    monkeypatch.delitem(sys.modules, "vault_values", raising=False)
    utils = importlib.import_module("utils")
    out = utils.mask_secrets(f"the key {VALUE_B} in a log line")
    assert VALUE_B not in out
    assert "[REDACTED-VAULT:BETA_TOKEN]" in out


# ── the outgoing gate over git objects ─────────────────────────────────────
# `integration` by measurement: a repository takes 0.5–0.7 s to build, the call
# puts it at the one-second line of the fast suite.

GIT = shutil.which("git")
git_based = pytest.mark.integration


def _git(cwd, *args):
    subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True, timeout=60)


@pytest.fixture
def repo(tmp_path, vault, monkeypatch):
    if GIT is None:
        pytest.skip("needs git")
    r = tmp_path / "r"
    r.mkdir()
    _git(r, "init", "-q", "-b", "main")
    _git(r, "config", "user.email", "t@example.invalid")
    _git(r, "config", "user.name", "T")
    (r / "old.cfg").write_text(f"k = {VALUE_B}\n", encoding="utf-8")
    _git(r, "add", ".")
    _git(r, "commit", "-qm", "base")
    _git(r, "update-ref", "refs/remotes/origin/main", "HEAD")      # "published"
    (r / "new.cfg").write_text(f"k = {VALUE_A}\n", encoding="utf-8")
    (r / "ps.txt").write_bytes(b"\xff\xfe" + f"t = {VALUE_LONG}\r\n".encode("utf-16-le"))
    (r / "old.cfg").write_text(f"k = {VALUE_B}\nx = 1\n", encoding="utf-8")
    _git(r, "add", ".")
    _git(r, "commit", "-qm", "work")
    monkeypatch.chdir(r)
    return r


@git_based
def test_range_names_keys_in_blobs_and_messages_never_values(repo, capsys):
    rc = vv.main(["range", "--", "origin/main..main"])
    out = capsys.readouterr().out
    assert rc == 1
    assert "vault value ALPHA_API_KEY: new.cfg" in out
    assert "vault value GAMMA_KEY_2: ps.txt" in out          # UTF-16 as well
    assert "vault value BETA_TOKEN: old.cfg" in out          # no exemption asked — blocks
    for v in (VALUE_A, VALUE_B, VALUE_LONG):
        assert v not in out


@git_based
def test_value_the_published_tree_holds_does_not_block(repo, capsys):
    _git(repo, "reset", "-q", "--hard", "origin/main")
    (repo / "old.cfg").write_text(f"k = {VALUE_B}\ny = 2\n", encoding="utf-8")
    _git(repo, "commit", "-qam", "edit published file")
    rc = vv.main(["range", "--published", "origin/main", "--", "origin/main..main"])
    out = capsys.readouterr().out
    assert rc == 0, out
    assert "BETA_TOKEN: old.cfg — already in the published tree" in out


@git_based
def test_key_in_a_commit_message_blocks(repo, capsys):
    _git(repo, "commit", "-q", "--allow-empty", "-m", f"token {VALUE_A}")
    rc = vv.main(["range", "--published", "origin/main", "--", "HEAD~1..HEAD"])
    out = capsys.readouterr().out
    assert rc == 1
    assert "ALPHA_API_KEY: commit message" in out


@git_based
def test_git_failure_is_a_scan_error_not_clean(repo, capsys):
    rc = vv.main(["range", "--", "no-such-ref..main"])
    out = capsys.readouterr().out
    assert rc == 2
    assert out.startswith("scan-error:")
