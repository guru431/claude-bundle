"""The git guards, driven end to end through REAL git.

`.githooks/` and `cron/lib/secret-scan.sh` are how this PUBLIC repository keeps
its cardinal rule, and until this file nothing executed them: shellcheck read
them, and tests/test_guards_scripts.py copied `.githooks/` into a fixture without
ever running a hook. Every scenario here is a way a secret or a personal string
used to reach a remote while each guard reported success — a quoted non-ASCII
path, one bad line in `.sanitize-patterns`, a merge commit, a binary blob, an
annotated tag — plus the invariants those fixes must keep: a commit that REMOVES
a leak goes through, deleting a branch publishes nothing.

Each test builds a throwaway repository with its own bare remote and its own git
configuration, so nothing here reads the developer's settings or touches this
checkout. The hooks run under the shell git picks for them (dash on Ubuntu), which
is what catches a bash-only construct in a `#!/bin/sh` hook. Most tests spawn git
and a shell dozens of times — seconds on Windows — hence `integration`, per the
one-second rule of the test policy; the library probes at the bottom stay fast.
"""
from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

import pytest

from test_guards import _bash   # the one resolver that avoids the WSL launcher

ROOT = Path(__file__).resolve().parent.parent
HOOKS = ROOT / ".githooks"
LIB = ROOT / "home-claude" / "cron" / "lib" / "secret-scan.sh"
GITHUB_PUSH = ROOT / "home-claude" / "cron" / "github-push.sh"

BASH = _bash()
GIT = shutil.which("git")

pytestmark = pytest.mark.skipif(BASH is None or GIT is None,
                                reason="needs git and a POSIX shell")

# Assembled, never written out: a literal token in this file would be caught by
# the very guards it tests.
TOKEN = "ghp_" + "A" * 30
# Denylist entries that mean nothing outside these tests.
HOST = "corp-host-4711"
CYRILLIC_HOST = "стенд-сборки-4711"


def _out(cp: subprocess.CompletedProcess) -> str:
    return (cp.stdout or b"").decode("utf-8", "replace") + (cp.stderr or b"").decode("utf-8", "replace")


def _git_env(tmp_path: Path) -> dict:
    """An environment in which git reads no configuration but ours.

    GIT_* variables are dropped as well: run from inside a hook, the suite would
    otherwise inherit GIT_DIR / GIT_INDEX_FILE and operate on the outer repo.
    """
    cfg = tmp_path / "gitconfig"
    cfg.write_text("[user]\n\tname = Test\n\temail = test@example.invalid\n"
                   "[init]\n\tdefaultBranch = main\n", encoding="utf-8")
    env = {k: v for k, v in os.environ.items() if not k.startswith("GIT_")}
    env.update(GIT_CONFIG_GLOBAL=str(cfg), GIT_CONFIG_NOSYSTEM="1", GIT_TERMINAL_PROMPT="0")
    return env


class Repo:
    def __init__(self, path: Path, env: dict):
        self.path = path
        self.env = env

    def git(self, *args: str) -> subprocess.CompletedProcess:
        return subprocess.run([GIT, *args], cwd=self.path, env=self.env,
                              capture_output=True, timeout=300)

    def write(self, rel: str, data: str | bytes) -> None:
        target = self.path / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(data if isinstance(data, bytes) else data.encode("utf-8"))

    def commit(self, message: str = "change", verify: bool = True) -> subprocess.CompletedProcess:
        added = self.git("add", "-A")
        assert added.returncode == 0, _out(added)
        return self.git("commit", "-q", "-m", message, *(() if verify else ("--no-verify",)))

    def plant(self, message: str = "planted past the guard") -> None:
        """Commit whatever is in the tree with the hooks OFF — the state a push
        guard exists for: committed before the guard was enabled, or --no-verify."""
        cp = self.commit(message, verify=False)
        assert cp.returncode == 0, _out(cp)

    def head(self) -> str:
        return self.git("rev-parse", "HEAD").stdout.decode().strip()

    def remote_head(self, branch: str = "main") -> str:
        out = self.git("ls-remote", "origin", f"refs/heads/{branch}").stdout.decode()
        return out.split()[0] if out.strip() else ""


def _new_repo(tmp_path: Path, name: str, remote_name: str) -> Repo:
    env = _git_env(tmp_path)
    remote = tmp_path / f"{name}.remote.git"
    for args in (["init", "-q", "--bare", str(remote)], ["init", "-q", str(tmp_path / name)]):
        cp = subprocess.run([GIT, *args], env=env, capture_output=True, timeout=120)
        assert cp.returncode == 0, _out(cp)
    repo = Repo(tmp_path / name, env)
    assert repo.git("remote", "add", remote_name, str(remote)).returncode == 0
    return repo


@pytest.fixture()
def guarded(tmp_path: Path) -> Repo:
    """A repository with this checkout's hooks and scan library wired in.

    Both are copied in but kept out of the fixture's history (info/exclude), so
    every blob a test pushes is one the test itself wrote.
    """
    repo = _new_repo(tmp_path, "repo", "origin")
    shutil.copytree(HOOKS, repo.path / ".githooks")
    for hook in (repo.path / ".githooks").iterdir():
        hook.chmod(0o755)
    lib_dir = repo.path / "home-claude" / "cron" / "lib"
    lib_dir.mkdir(parents=True)
    shutil.copy2(LIB, lib_dir / "secret-scan.sh")
    (repo.path / ".git" / "info").mkdir(exist_ok=True)
    (repo.path / ".git" / "info" / "exclude").write_text(
        "/.githooks/\n/home-claude/\n/.sanitize-patterns\n", encoding="utf-8")
    assert repo.git("config", "core.hooksPath", ".githooks").returncode == 0
    repo.write("README.md", "hello\n")
    cp = repo.commit("init")
    assert cp.returncode == 0, f"a clean first commit was refused:\n{_out(cp)}"
    cp = repo.git("push", "-q", "origin", "main")
    assert cp.returncode == 0, f"a clean first push was refused:\n{_out(cp)}"
    return repo


integration = pytest.mark.integration


# ── pre-commit ──────────────────────────────────────────────────────────────

@integration
def test_pre_commit_blocks_a_rename_into_dotenv(guarded: Repo):
    guarded.write("notes.txt", "DB_PASSWORD=hunter2\n")
    assert guarded.commit("notes").returncode == 0
    assert guarded.git("mv", "notes.txt", ".env").returncode == 0
    cp = guarded.git("commit", "-q", "-m", "rename")
    assert cp.returncode != 0, "a rename into .env was committed"
    assert ".env" in _out(cp)


@integration
def test_pre_commit_blocks_dotenv_in_a_non_ascii_directory(guarded: Repo):
    """git quotes a non-ASCII path by default — `"\\320\\277…/.env"` — and the
    anchored name table never matched the quoted form."""
    guarded.write("проект/.env", "DB_PASSWORD=hunter2\n")
    cp = guarded.commit("config")
    assert cp.returncode != 0, f"a .env under a Cyrillic folder was committed:\n{_out(cp)}"
    assert "проект/.env" in _out(cp)


@integration
def test_pre_commit_reads_a_utf16_file_in_a_non_ascii_directory(guarded: Repo):
    """The UTF-16 pass read each file with `git show ":$f"` — which fails on a
    quoted name, and the failure was swallowed as "nothing to scan"."""
    guarded.write("проект/notes.txt", b"\xff\xfe" + f"key {TOKEN}\n".encode("utf-16-le"))
    cp = guarded.commit("notes")
    assert cp.returncode != 0, f"a token in a UTF-16 file was committed:\n{_out(cp)}"


@integration
@pytest.mark.parametrize("what", ["token", "denylist"])
def test_pre_commit_reads_a_staged_binary_file(guarded: Repo, what: str):
    """A diff shows a binary file as "Binary files differ" and no `+` line, and
    step 2b read UTF-16 files only: a key inside a staged SQLite file was
    committed, while pre-push would have stopped the same bytes."""
    guarded.write(".sanitize-patterns", HOST + "\n")
    secret = TOKEN if what == "token" else HOST
    guarded.write("data/cache.db", b"SQLite format 3\x00\x10\x00" + b"\x00" * 8
                  + f"value={secret}".encode() + b"\x00\x01\n")
    cp = guarded.commit("cache")
    assert cp.returncode != 0, f"a {what} inside a staged binary file was committed:\n{_out(cp)}"
    assert "data/cache.db" in _out(cp)


@integration
def test_pre_commit_refuses_to_run_with_an_invalid_denylist(guarded: Repo):
    """`grep -f` exits 2 on a pattern it cannot compile, which `|| true` read as
    "no match": one typo switched the whole personal denylist off."""
    guarded.write(".sanitize-patterns", f"{HOST}\nbuild-box-(7\n")
    guarded.write("docs/note.md", f"deploy to {HOST}\n")
    cp = guarded.commit("note")
    assert cp.returncode != 0, f"committed with a broken denylist:\n{_out(cp)}"
    assert "sanitize-patterns" in _out(cp)


@integration
def test_pre_commit_denylist_blocks_adding_a_name_but_not_removing_it(guarded: Repo):
    guarded.write(".sanitize-patterns", HOST + "\r\n")        # CRLF, as Windows saves it
    guarded.write("docs/note.md", f"deploy to {HOST}\n")
    cp = guarded.commit("add")
    assert cp.returncode != 0, "a denylisted name was committed"
    guarded.plant()
    guarded.write("docs/note.md", "deploy to the build host\n")
    cp = guarded.commit("scrub")
    assert cp.returncode == 0, f"the commit that REMOVES the name was refused:\n{_out(cp)}"


@integration
def test_pre_commit_fails_closed_without_the_scan_library(guarded: Repo):
    """docs/decisions.md D-10: a missing secret-scan.sh blocks the commit. This
    hook printed a WARNING and let the commit through with the token scan off."""
    (guarded.path / "home-claude" / "cron" / "lib" / "secret-scan.sh").unlink()
    guarded.write("src/app.py", f"token = '{TOKEN}'\n")
    assert guarded.git("add", "-A").returncode == 0
    cp = guarded.git("hook", "run", "pre-commit")
    assert cp.returncode != 0, f"pre-commit passed with no scan library:\n{_out(cp)}"


@integration
def test_pre_commit_blocks_the_denylist_file_itself(guarded: Repo):
    guarded.write(".sanitize-patterns", HOST + "\n")
    assert guarded.git("add", "-f", ".sanitize-patterns").returncode == 0
    cp = guarded.git("commit", "-q", "-m", "oops")
    assert cp.returncode != 0 and ".sanitize-patterns" in _out(cp)


# ── commit-msg ──────────────────────────────────────────────────────────────

@integration
def test_commit_msg_blocks_a_token_in_a_crlf_message(guarded: Repo, tmp_path: Path):
    guarded.write("a.txt", "a\n")
    assert guarded.git("add", "-A").returncode == 0
    msg = tmp_path / "msg.txt"
    msg.write_bytes(f"subject\r\n\r\nkey {TOKEN}\r\n".encode())
    cp = guarded.git("commit", "-q", "-F", str(msg))
    assert cp.returncode != 0, f"a token in the commit message was committed:\n{_out(cp)}"


@integration
def test_commit_msg_refuses_to_run_with_an_invalid_denylist(guarded: Repo, tmp_path: Path):
    guarded.write(".sanitize-patterns", f"{HOST}\n(unclosed\n")
    msg = tmp_path / "msg.txt"
    msg.write_bytes(f"deployed to {HOST}\n".encode())
    cp = guarded.git("hook", "run", "commit-msg", "--", str(msg))
    assert cp.returncode != 0, f"commit-msg passed with a broken denylist:\n{_out(cp)}"


# ── pre-push ────────────────────────────────────────────────────────────────

@integration
@pytest.mark.parametrize("name", ["config/.env", "проект/.env", "deploy/credentials.json"])
def test_pre_push_blocks_a_sensitive_file_name(guarded: Repo, name: str):
    """The push guard scanned CONTENT only, so `DB_PASSWORD=hunter2` — no token
    shape — was published under any name, `.env` included."""
    guarded.write(name, "DB_PASSWORD=hunter2\n")
    guarded.plant()
    cp = guarded.git("push", "origin", "main")
    assert cp.returncode != 0, f"{name} was pushed:\n{_out(cp)}"
    assert name in _out(cp)
    assert guarded.remote_head() != guarded.head()


@integration
def test_pre_push_scans_a_binary_blob(guarded: Repo):
    """The fast path flagged it and the precise pass (`grep -I`) called it binary
    and "clean" — the push went through without a word."""
    guarded.write("data/cache.db", b"SQLite format 3\x00\x10\x00" + b"\x00" * 8
                  + f"token={TOKEN}".encode() + b"\x00\x01\n")
    guarded.plant()
    cp = guarded.git("push", "origin", "main")
    assert cp.returncode != 0, f"a token in a binary blob was pushed:\n{_out(cp)}"
    assert "binary" in _out(cp).lower()


@integration
def test_pre_push_decodes_a_utf16_blob_for_a_non_ascii_denylist_entry(guarded: Repo):
    """The fast path grepped the raw stream with NULs deleted: that shows the
    ASCII of a UTF-16 file, but a Cyrillic character is two bytes there and a
    UTF-8 pattern never matched — the precise pass never ran and the push went
    out."""
    guarded.write(".sanitize-patterns", CYRILLIC_HOST + "\n")
    guarded.write("notes.txt", b"\xff\xfe" + f"deploy to {CYRILLIC_HOST}\r\n".encode("utf-16-le"))
    guarded.plant()
    cp = guarded.git("push", "origin", "main")
    assert cp.returncode != 0, f"a UTF-16 blob with a Cyrillic denylist entry was pushed:\n{_out(cp)}"
    assert "notes.txt" in _out(cp)


@integration
def test_pre_push_refuses_to_run_with_an_invalid_denylist(guarded: Repo):
    guarded.write("docs/note.md", f"deploy to {HOST}\n")
    guarded.plant()
    guarded.write(".sanitize-patterns", f"{HOST}\nbuild-box-(7\n")
    cp = guarded.git("push", "origin", "main")
    assert cp.returncode != 0, f"pushed with a broken denylist:\n{_out(cp)}"
    assert "sanitize-patterns" in _out(cp)


@integration
def test_pre_push_checks_commit_messages_against_the_denylist(guarded: Repo):
    """commit-msg holds a message to the denylist; the push guard, which exists
    for commits that bypassed commit-msg, did not."""
    guarded.write(".sanitize-patterns", HOST + "\n")
    guarded.write("a.txt", "a\n")
    guarded.plant(f"deployed to {HOST}")
    cp = guarded.git("push", "origin", "main")
    assert cp.returncode != 0, f"a denylisted name in a commit message was pushed:\n{_out(cp)}"


@integration
def test_pre_push_blocks_a_new_branch_carrying_a_secret(guarded: Repo):
    assert guarded.git("checkout", "-q", "-b", "feature").returncode == 0
    guarded.write("src/leak.py", f"t = '{TOKEN}'\n")
    guarded.plant()
    cp = guarded.git("push", "origin", "feature")
    assert cp.returncode != 0, f"a new branch with a token was pushed:\n{_out(cp)}"
    assert "src/leak.py" in _out(cp)


@integration
def test_pre_push_blocks_a_force_push_that_rewrites_a_secret_in(guarded: Repo):
    guarded.write("src/app.py", "x = 1\n")
    assert guarded.commit("app").returncode == 0
    assert guarded.git("push", "-q", "origin", "main").returncode == 0
    guarded.write("src/app.py", f"x = '{TOKEN}'\n")
    assert guarded.git("add", "-A").returncode == 0
    assert guarded.git("commit", "-q", "--amend", "--no-verify", "--no-edit").returncode == 0
    cp = guarded.git("push", "--force", "origin", "main")
    assert cp.returncode != 0, f"a force-push rewriting a token in was accepted:\n{_out(cp)}"


@integration
def test_pre_push_lets_the_commit_that_removes_a_leak_through(guarded: Repo):
    guarded.write(".sanitize-patterns", HOST + "\n")
    guarded.write("src/app.py", f"x = '{TOKEN}'  # {HOST}\n")
    guarded.plant()
    assert guarded.git("push", "-q", "--no-verify", "origin", "main").returncode == 0
    guarded.write("src/app.py", "x = None\n")
    assert guarded.commit("remove the leak").returncode == 0
    cp = guarded.git("push", "origin", "main")
    assert cp.returncode == 0, f"the push that REMOVES a leak was refused:\n{_out(cp)}"


@integration
def test_pre_push_lets_a_branch_deletion_through(guarded: Repo):
    assert guarded.git("push", "-q", "origin", "main:refs/heads/old").returncode == 0
    cp = guarded.git("push", "origin", "--delete", "old")
    assert cp.returncode == 0, _out(cp)


@integration
def test_pre_push_sees_what_an_evil_merge_introduces(guarded: Repo):
    """A file that exists in neither parent and is born on the merge commit."""
    assert guarded.git("checkout", "-q", "-b", "side").returncode == 0
    guarded.write("side.txt", "side\n")
    assert guarded.commit("side").returncode == 0
    assert guarded.git("checkout", "-q", "main").returncode == 0
    guarded.write("main.txt", "main\n")
    assert guarded.commit("main").returncode == 0
    assert guarded.git("merge", "-q", "--no-ff", "--no-commit", "side").returncode == 0
    guarded.write(".env", "DB_PASSWORD=hunter2\n")
    guarded.plant("merge side")
    cp = guarded.git("push", "origin", "main")
    assert cp.returncode != 0, f"an evil merge carrying .env was pushed:\n{_out(cp)}"
    assert ".env" in _out(cp)


@integration
def test_pre_push_scans_an_annotated_tag_message(guarded: Repo):
    """No hook read a tag message: commit-msg never runs for `git tag -a`, and
    the push guard walked commits and blobs only."""
    cp = guarded.git("tag", "-a", "v1", "-m", f"release notes\n\nkey {TOKEN}")
    assert cp.returncode == 0, _out(cp)
    cp = guarded.git("push", "origin", "v1")
    assert cp.returncode != 0, f"a token in an annotated tag message was pushed:\n{_out(cp)}"
    assert "v1" in _out(cp)
    assert guarded.git("ls-remote", "--tags", "origin").stdout.strip() == b""


@integration
def test_pre_push_lets_a_clean_annotated_tag_through(guarded: Repo):
    assert guarded.git("tag", "-a", "v1", "-m", "release notes").returncode == 0
    cp = guarded.git("push", "origin", "v1")
    assert cp.returncode == 0, _out(cp)


# ── pre-merge-commit ────────────────────────────────────────────────────────

@integration
def test_a_clean_merge_runs_the_pre_commit_checks(guarded: Repo):
    """`git merge` never runs pre-commit: a branch whose commits bypassed the
    guard was merged — and committed — without a single check."""
    base = guarded.head()
    assert guarded.git("checkout", "-q", "-b", "side").returncode == 0
    guarded.write("src/leak.py", f"t = '{TOKEN}'\n")
    guarded.plant()
    assert guarded.git("checkout", "-q", "main").returncode == 0
    guarded.write("main.txt", "main\n")
    assert guarded.commit("main").returncode == 0
    main_head = guarded.head()
    cp = guarded.git("merge", "--no-ff", "-m", "merge side", "side")
    assert cp.returncode != 0, f"a merge bringing in a token was committed:\n{_out(cp)}"
    assert guarded.head() == main_head != base, "a merge commit was created"


# ── cron/github-push.sh --check-only ────────────────────────────────────────
# The gate for projects that have no hooks of their own: before this script
# pushes to a public `github` remote, it is the ONLY check.

@pytest.fixture()
def published(tmp_path: Path) -> Repo:
    """A hook-less project whose `github` remote already has the first commit."""
    repo = _new_repo(tmp_path, "project", "github")
    (repo.path / ".git" / "info").mkdir(exist_ok=True)
    (repo.path / ".git" / "info" / "exclude").write_text("/.sanitize-patterns\n",
                                                         encoding="utf-8")
    repo.write("README.md", "hello\n")
    repo.plant("init")
    cp = repo.git("push", "-q", "github", "main")
    assert cp.returncode == 0, _out(cp)
    return repo


def _check_only(repo: Repo) -> subprocess.CompletedProcess:
    return subprocess.run([BASH, GITHUB_PUSH.as_posix(), "--check-only",
                           repo.path.as_posix(), "main"],
                          env=repo.env, capture_output=True, timeout=600)


@integration
def test_github_push_passes_a_clean_publication(published: Repo):
    published.write(".sanitize-patterns", HOST + "\n")
    published.write("src/app.py", "print('hello')\n")
    published.plant()
    cp = _check_only(published)
    assert cp.returncode == 0, _out(cp)


@integration
def test_github_push_sees_what_an_evil_merge_introduces(published: Repo):
    """Steps 1 and 3 read `git log --name-only` / `git log -p`, which print
    nothing for a merge commit — only the token step had moved to the object
    walk, and neither `PASSWORD=` nor a hostname has a token shape."""
    published.write(".sanitize-patterns", HOST + "\n")
    assert published.git("checkout", "-q", "-b", "side").returncode == 0
    published.write("side.txt", "side\n")
    published.plant("side")
    assert published.git("checkout", "-q", "main").returncode == 0
    published.write("main.txt", "main\n")
    published.plant("main")
    assert published.git("merge", "-q", "--no-ff", "--no-commit", "side").returncode == 0
    published.write(".env", "PASSWORD=hunter2\n")
    published.write("docs/deploy.md", f"ssh {HOST}\n")
    published.plant("merge side")
    cp = _check_only(published)
    out = _out(cp)
    assert cp.returncode != 0, f"an evil merge passed the public-remote gate:\n{out}"
    assert ".env" in out and f"ssh {HOST}" in out, out


@integration
def test_github_push_reads_a_utf16_file_against_the_denylist(published: Repo):
    published.write(".sanitize-patterns", HOST + "\n")
    published.write("notes.txt", b"\xff\xfe" + f"deploy to {HOST}\r\n".encode("utf-16-le"))
    published.plant()
    cp = _check_only(published)
    assert cp.returncode != 0, f"a UTF-16 file with a denylisted name passed:\n{_out(cp)}"


@integration
def test_github_push_blocks_dotenv_in_a_non_ascii_directory(published: Repo):
    published.write("проект/.env", "PASSWORD=hunter2\n")
    published.plant()
    cp = _check_only(published)
    assert cp.returncode != 0, f"a .env under a Cyrillic folder passed:\n{_out(cp)}"
    assert "проект/.env" in _out(cp)


@integration
def test_github_push_refuses_an_invalid_denylist(published: Repo):
    published.write(".sanitize-patterns", f"{HOST}\nbuild-box-(7\n")
    published.write("docs/deploy.md", f"ssh {HOST}\n")
    published.plant()
    cp = _check_only(published)
    assert cp.returncode != 0, f"published with a broken denylist:\n{_out(cp)}"


@integration
@pytest.mark.parametrize("deny", [b"^exports/\r\n", b"^exports/(\n"], ids=["crlf", "invalid"])
def test_github_push_path_denylist_cannot_be_switched_off_by_accident(published: Repo,
                                                                     deny: bytes):
    """`.github-push-deny` is hand-written too: a CRLF line never matched, and a
    line grep cannot compile read as "no match" — both published the path."""
    published.write(".github-push-deny", deny)
    published.write("exports/customers.csv", "id,name\n1,someone\n")
    published.plant()
    cp = _check_only(published)
    assert cp.returncode != 0, f"a .github-push-deny path was published:\n{_out(cp)}"


@integration
def test_github_push_lets_the_commit_that_scrubs_a_name_through(published: Repo):
    published.write("docs/deploy.md", f"ssh {HOST}\n")
    published.plant()
    assert published.git("push", "-q", "github", "main").returncode == 0
    published.write(".sanitize-patterns", HOST + "\n")
    published.write("docs/deploy.md", "ssh the build host\n")
    published.plant("scrub the host name")
    cp = _check_only(published)
    assert cp.returncode == 0, f"the commit that REMOVES a name was blocked:\n{_out(cp)}"


# ── library probes (one shell each) ─────────────────────────────────────────

def _lib(tmp_path: Path, body: str, env: dict | None = None) -> subprocess.CompletedProcess:
    script = tmp_path / "probe.sh"
    script.write_text(f". '{LIB.as_posix()}'\n{body}\n", encoding="utf-8", newline="\n")
    return subprocess.run([BASH, str(script)], cwd=tmp_path, capture_output=True,
                          timeout=120, env=env)


def test_denylist_loader_refuses_a_pattern_grep_cannot_compile(tmp_path: Path):
    (tmp_path / "sp").write_text(f"{HOST}\nbuild-box-(7\n", encoding="utf-8")
    cp = _lib(tmp_path, 'secret_scan_denylist sp pat; echo "rc=$?"')
    out = _out(cp)
    assert "rc=2" in out, out
    assert "build-box-(7" in out, "the message does not name the broken line"


@pytest.mark.parametrize("raw", [
    b"\xef\xbb\xbf" + f"{HOST}\r\n\r\n  \r\nother\r\n".encode(),   # UTF-8 BOM, CRLF, blanks
    b"\xff\xfe" + f"{HOST}\r\n".encode("utf-16-le"),               # PowerShell 5.1 `>`
], ids=["bom-crlf", "utf16"])
def test_denylist_loader_reads_what_windows_editors_write(tmp_path: Path, raw: bytes):
    """A UTF-8 BOM glued to the first pattern, CRLF endings, blank lines, UTF-16:
    each of them silently disabled one pattern or all of them."""
    (tmp_path / "sp").write_bytes(raw)
    (tmp_path / "text").write_text(f"deploy to {HOST}\n", encoding="utf-8")
    cp = _lib(tmp_path, 'secret_scan_denylist sp pat; echo "load rc=$?"\n'
                        'secret_scan_denylist_text pat < text; echo "scan rc=$?"')
    out = _out(cp)
    assert "load rc=0" in out and "scan rc=1" in out and f"to {HOST}" in out, out


def test_denylist_loader_treats_a_missing_file_as_no_denylist(tmp_path: Path):
    cp = _lib(tmp_path, 'secret_scan_denylist absent pat; echo "rc=$? bytes=$(wc -c < pat | tr -d " ")"')
    assert "rc=0 bytes=0" in _out(cp), _out(cp)


def _blob_id(data: bytes) -> str:
    import hashlib
    return hashlib.sha1(b"blob %d\x00" % len(data) + data).hexdigest()


def test_the_fast_path_names_every_blob_the_precise_scan_would_flag(tmp_path: Path):
    """secret_scan_suspects decides which blobs get the precise scan at all, so
    what it leaves out is published unread. It must name every blob with a hit —
    text, binary, UTF-16 with a non-ASCII entry, the denylist — and must not be
    fooled by a blob holding a line that looks exactly like the next blob's
    `cat-file --batch` header. The clean blob stays out: that is the speed-up."""
    clean = b"nothing to see here\n"
    blobs = {
        "clean": clean,
        "token": f"x = '{TOKEN}'\n".encode(),
        "binary": b"\x00\x01head\x00" + f"k={TOKEN}".encode() + b"\x00\n",
        "utf16": b"\xff\xfe" + f"deploy to {CYRILLIC_HOST}\r\n".encode("utf-16-le"),
        "denylist": f"ssh {HOST}\n".encode(),
        # A fake header naming the clean blob that follows, then a token: a parser
        # that trusted header-shaped lines would pin the token on "clean".
        "spoof": f"{_blob_id(clean)} blob {len(clean)}\n".encode() + clean
                 + f"k={TOKEN}\n".encode(),
    }
    order = ["token", "binary", "utf16", "denylist", "spoof", "clean"]
    for name in order:
        (tmp_path / name).write_bytes(blobs[name])
    (tmp_path / "sp").write_text(f"{HOST}\n{CYRILLIC_HOST}\n", encoding="utf-8")
    env = _git_env(tmp_path)
    cp = _lib(tmp_path, f"""
git init -q repo && cd repo || exit 1
git hash-object -w {' '.join('../' + n for n in order)} > ../list || exit 1
secret_scan_denylist ../sp ../pat || exit 1
mkdir ../scratch
secret_scan_suspects ../list ../pat ../scratch""", env=env)
    got = set(cp.stdout.decode().split())
    want = {_blob_id(blobs[n]) for n in order if n != "clean"}
    assert got == want, (f"expected {sorted(n for n in order if n != 'clean')}, got "
                         f"{sorted(n for n in order if _blob_id(blobs[n]) in got)}\n{_out(cp)}")


@integration
def test_the_range_scan_reads_only_the_blobs_that_could_hit(tmp_path: Path):
    """secret_scan_range ran its precise pass — a dozen process spawns per blob —
    on every blob it was given, so a first publication through github-push.sh
    took minutes on Windows. It must still report the leak, and read one blob to
    do so, not thirty-one."""
    env = _git_env(tmp_path)
    cp = _lib(tmp_path, f"""
git init -q repo && cd repo || exit 1
i=0
while [ "$i" -lt 30 ]; do printf 'clean file %s\\n' "$i" > "f$i.txt"; i=$((i + 1)); done
printf "x = '%s'\\n" '{TOKEN}' > leak.py
git add -A && git commit -qm init || exit 1
git() {{
    if [ "$1" = cat-file ] && [ "$2" = blob ]; then echo read >> ../blob-reads; fi
    command git "$@"
}}
: > ../blob-reads
secret_scan_range HEAD; echo "rc=$?"
echo "reads=$(wc -l < ../blob-reads | tr -d ' ')"
""", env=env)
    out = _out(cp)
    assert "rc=1" in out and "leak.py" in out, f"the leak was not reported:\n{out}"
    assert "reads=1" in out, f"the precise pass read more than the one suspect blob:\n{out}"


def test_binary_content_is_scanned_not_skipped(tmp_path: Path):
    (tmp_path / "blob").write_bytes(b"\x00\x01head\x00" + f"k={TOKEN}".encode() + b"\x00\n")
    cp = _lib(tmp_path, 'secret_scan_text < blob; echo "rc=$?"')
    out = _out(cp)
    assert "rc=1" in out and "binary" in out, out


def test_a_line_that_is_not_valid_utf8_is_still_scanned(tmp_path: Path):
    """GNU grep in a UTF-8 locale suppresses a matching line that carries an
    encoding error — a CP1251 comment next to a key was reported clean. (Git
    Bash's grep maps such bytes instead, so this can only fail on Linux/macOS.)"""
    line = "пароль ".encode("cp1251") + f"{TOKEN}\n".encode()
    (tmp_path / "cp1251.txt").write_bytes(line)
    (tmp_path / "cp1251.diff").write_bytes(b"+++ b/app.py\n+" + line)
    env = dict(os.environ, LC_ALL="C.UTF-8")
    cp = _lib(tmp_path, 'secret_scan_text < cp1251.txt; echo "text rc=$?"\n'
                        'secret_scan_diff < cp1251.diff; echo "diff rc=$?"', env=env)
    out = _out(cp)
    assert "text rc=1" in out and "diff rc=1" in out, out
