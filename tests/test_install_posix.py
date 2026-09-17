"""scripts/install.sh, uninstall.sh and scripts/lib/bundle_install.py.

The POSIX full tier used to be seven hand-typed commands: no manifest, no
dry-run window, no uninstaller, and a lite installer that REPLACED
settings.json while the docs promised a merge. What is pinned here:

* the dangerous operations, fast: an uninstall removes only what the manifest
  lists, only inside its roots, only while unchanged, and never by default;
  a merge never overwrites a key of yours; a pinned .env value is never touched;
* the scripts end to end (`integration`: they fork a few hundred processes,
  which Git Bash on Windows turns into tens of seconds) — every directory is
  under tmp_path, and systemctl / loginctl are stubs that only log their calls.
"""
from __future__ import annotations

import hashlib
import importlib.util
import json
import os
import subprocess
import sys
from datetime import date
from pathlib import Path

import pytest

from conftest import find_bash

ROOT = Path(__file__).resolve().parent.parent
BASH = find_bash()


@pytest.fixture()
def bi():
    spec = importlib.util.spec_from_file_location("bundle_install",
                                                  ROOT / "scripts" / "lib" / "bundle_install.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest().upper()


def _manifest(home: Path, written: list[str], **extra) -> None:
    """A manifest recording each of `written` (claude_home-relative) as it is now."""
    home.mkdir(parents=True, exist_ok=True)
    body = {"bundle_version": "0.0.0", "installed_at": "2026-01-01T00:00:00Z", "tier": "lite",
            "claude_home": str(home), "pipeline_root": str(home),
            "written": [{"root": "claude_home", "path": rel, "sha256": _sha(home / rel)}
                        for rel in written],
            **extra}
    (home / ".bundle-manifest.json").write_text(json.dumps(body), encoding="utf-8")


# ── the uninstaller: what it may delete ──────────────────────────────────────

def test_uninstall_is_a_dry_run_unless_confirmed(bi, tmp_path, capsys):
    home = tmp_path / "home"
    (home / "skills").mkdir(parents=True)
    (home / "skills" / "x.md").write_text("ours", encoding="utf-8")
    _manifest(home, ["skills/x.md"])
    assert bi.main(["uninstall", "--claude-home", str(home)]) == 0
    assert (home / "skills" / "x.md").is_file()
    assert "would remove skills/x.md" in capsys.readouterr().out
    assert bi.main(["uninstall", "--claude-home", str(home), "--confirm", "--dry-run"]) == 0
    assert (home / "skills" / "x.md").is_file(), "--dry-run must win over --confirm"


def test_uninstall_never_leaves_its_roots(bi, tmp_path, capsys):
    """One edited line in a JSON file must not turn the uninstaller into an
    arbitrary-file deleter — neither through `..` or an absolute path, nor
    through a scheduler section pointing somewhere that is not a unit dir."""
    home = tmp_path / "home"
    home.mkdir()
    victim = tmp_path / "victim.txt"
    victim.write_text("not the bundle's", encoding="utf-8")
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    (elsewhere / "victim.service").write_text("[Service]\n", encoding="utf-8")
    (home / "ours.txt").write_text("ours", encoding="utf-8")
    body = {"tier": "lite", "claude_home": str(home), "pipeline_root": str(home), "written": [
        {"root": "claude_home", "path": "../victim.txt", "sha256": _sha(victim)},
        {"root": "claude_home", "path": str(victim), "sha256": _sha(victim)},
        {"root": "claude_home", "path": "ours.txt", "sha256": _sha(home / "ours.txt")}],
        "scheduler": {"target": "systemd", "units_dir": str(elsewhere),
                      "units": [{"path": "victim.service", "sha256": _sha(elsewhere / "victim.service")}]}}
    (home / ".bundle-manifest.json").write_text(json.dumps(body), encoding="utf-8")

    assert bi.main(["uninstall", "--claude-home", str(home), "--confirm"]) == 0
    assert victim.is_file() and (elsewhere / "victim.service").is_file()
    assert not (home / "ours.txt").exists()
    assert "rejected (bad path): 3" in capsys.readouterr().out


def test_uninstall_keeps_a_changed_file_unless_forced(bi, tmp_path, capsys):
    home = tmp_path / "home"
    home.mkdir()
    (home / "CLAUDE.md").write_text("as installed", encoding="utf-8")
    _manifest(home, ["CLAUDE.md"])
    (home / "CLAUDE.md").write_text("edited since", encoding="utf-8")

    assert bi.main(["uninstall", "--claude-home", str(home), "--confirm"]) == 2
    assert (home / "CLAUDE.md").is_file()
    assert (home / ".bundle-manifest.json").is_file(), "the manifest still describes a kept file"
    assert bi.main(["uninstall", "--claude-home", str(home), "--confirm", "--force"]) == 0
    assert not (home / "CLAUDE.md").exists() and not (home / ".bundle-manifest.json").exists()


def test_uninstall_without_a_manifest_removes_nothing(bi, tmp_path):
    home = tmp_path / "home"
    home.mkdir()
    (home / "CLAUDE.md").write_text("yours", encoding="utf-8")
    assert bi.main(["uninstall", "--claude-home", str(home), "--confirm"]) == 1
    (home / ".bundle-manifest.json").write_text("{not json", encoding="utf-8")
    assert bi.main(["uninstall", "--claude-home", str(home), "--confirm"]) == 1
    assert (home / "CLAUDE.md").is_file()


# ── the merges: what it may change ───────────────────────────────────────────

def test_settings_merge_adds_only_missing_keys(bi, tmp_path):
    """install-lite.sh copied the template over settings.json — the hooks,
    permissions and plugins a user wired in by hand were gone after an update."""
    template = tmp_path / "template.json"
    template.write_text(json.dumps({"permissions": {"allow": ["Read"]}, "env": {}}), encoding="utf-8")
    user = tmp_path / "settings.json"
    user.write_text(json.dumps({"permissions": {"allow": ["Bash(ls:*)"]}, "hooks": {"x": 1}}),
                    encoding="utf-8")
    backup = tmp_path / "settings.json.bak"

    assert bi.main(["merge-settings", str(template), str(user), str(backup)]) == 0
    merged = json.loads(user.read_text(encoding="utf-8"))
    assert merged == {"permissions": {"allow": ["Bash(ls:*)"]}, "hooks": {"x": 1}, "env": {}}
    assert json.loads(backup.read_text(encoding="utf-8"))["hooks"] == {"x": 1}

    backup.unlink()
    assert bi.main(["merge-settings", str(template), str(user), str(backup)]) == 0
    assert not backup.exists(), "nothing to add must not rewrite or back up the file"

    user.write_text("{broken", encoding="utf-8")
    assert bi.main(["merge-settings", str(template), str(user), str(backup)]) == 3
    assert user.read_text(encoding="utf-8") == "{broken"


def test_a_pinned_env_value_is_never_overwritten(bi, tmp_path):
    env = tmp_path / ".env"
    env.write_bytes(b"# keys\r\nPYTHON_EXE=\r\nexport BASH_EXE=/opt/bash\r\n")
    assert bi.main(["set-env-if-empty", str(env), "PYTHON_EXE", "/usr/bin/python3"]) == 0
    assert bi.main(["set-env-if-empty", str(env), "BASH_EXE", "/bin/bash"]) == 1
    assert bi.main(["set-env-if-empty", str(env), "PROJECTS_ROOT", "/srv/work"]) == 0
    assert env.read_bytes() == (b"# keys\r\nPYTHON_EXE=/usr/bin/python3\r\n"
                                b"export BASH_EXE=/opt/bash\r\nPROJECTS_ROOT=/srv/work\n")


def test_the_dry_run_window_opens_only_on_an_empty_key(bi, tmp_path, monkeypatch):
    class Today(date):
        @classmethod
        def today(cls):
            return cls(2026, 3, 30)

    monkeypatch.setattr(bi, "date", Today)
    # The template's shape: an empty key, with commented examples that name
    # `confirm` as well as a date. Only the empty key gets the date.
    template = ("projects_root:\r\ndry_run_until:\r\n"
                "#   dry_run_until: 2026-09-05\r\n#   dry_run_until: confirm\r\n")
    fresh = tmp_path / "fresh.yaml"
    fresh.write_bytes(template.encode("utf-8"))
    assert bi.main(["open-dry-run-window", str(fresh), "7"]) == 0
    assert fresh.read_bytes() == template.replace("dry_run_until:\r\n",
                                                  "dry_run_until: 2026-04-06\r\n", 1).encode("utf-8")

    # A value the user chose is never replaced — a later date, or `confirm`,
    # which holds the preview until they write a date themselves.
    for chosen in ("dry_run_until: 2030-01-01\n", "dry_run_until: confirm\n",
                   "dry_run_until: confirm   # until the policy is reviewed\n"):
        set_by_user = tmp_path / "set.yaml"
        set_by_user.write_text(chosen, encoding="utf-8")
        assert bi.main(["open-dry-run-window", str(set_by_user), "7"]) == 0
        assert set_by_user.read_text(encoding="utf-8") == chosen


def test_a_run_that_installs_no_units_keeps_the_ones_already_recorded(bi, tmp_path):
    """Dropping them from the manifest would leave uninstall.sh blind to timers
    that keep firing at scripts it is about to delete."""
    home = tmp_path / "home"
    (home / "CLAUDE.md").parent.mkdir(parents=True)
    (home / "CLAUDE.md").write_text("x", encoding="utf-8")
    recorded = {"target": "systemd", "units_dir": "/home/u/.config/systemd/user",
                "units": [{"path": "ClaudeWikiPipeline.timer", "sha256": "AB"}]}
    _manifest(home, ["CLAUDE.md"], scheduler=recorded)
    written = tmp_path / "written.tsv"
    written.write_text("claude_home\tCLAUDE.md\n", encoding="utf-8")
    assert bi.main(["write-manifest", "--claude-home", str(home), "--pipeline-root", str(home),
                    "--tier", "full", "--version", "1.0.0", "--written", str(written)]) == 0
    manifest = json.loads((home / ".bundle-manifest.json").read_text(encoding="utf-8"))
    assert manifest["scheduler"] == recorded
    assert [e["path"] for e in manifest["written"]] == ["CLAUDE.md"]


def test_diff_lists_new_modified_and_removed_from_bundle(bi, tmp_path, capsys):
    source = tmp_path / "bundle"
    (source / "home-claude" / "skills").mkdir(parents=True)
    for rel, text in (("CLAUDE.md", "rules"), ("skills/a.md", "a"), ("skills/b.md", "b")):
        (source / "home-claude" / rel).write_text(text, encoding="utf-8")
    home = tmp_path / "home"
    (home / "skills").mkdir(parents=True)
    (home / "CLAUDE.md").write_text("rules", encoding="utf-8")
    (home / "skills" / "a.md").write_text("edited", encoding="utf-8")
    (home / "skills" / "gone.md").write_text("old", encoding="utf-8")
    _manifest(home, ["CLAUDE.md", "skills/gone.md"])
    plan = tmp_path / "plan.tsv"
    plan.write_text("".join(f"claude_home\t{rel}\thome-claude/{rel}\n"
                            for rel in ("CLAUDE.md", "skills/a.md", "skills/b.md")), encoding="utf-8")

    assert bi.main(["diff", "--claude-home", str(home), "--pipeline-root", str(home),
                    "--source", str(source), "--plan", str(plan), "--profile", "lite"]) == 0
    out = capsys.readouterr().out
    assert "modified" in out and str(home / "skills" / "a.md") in out
    assert "new " in out and str(home / "skills" / "b.md") in out
    assert "removed-from-bundle" in out and "gone.md" in out
    assert [line.split() for line in out.splitlines() if line.strip().startswith(
        ("new ", "modified ", "unchanged ", "removed-from-bundle "))][-4:] == [
        ["new", "1"], ["modified", "1"], ["unchanged", "1"], ["removed-from-bundle", "1"]]


# ── the scripts themselves ───────────────────────────────────────────────────

def _sh(path) -> str:
    """A path as Git Bash on Windows understands it (C:\\x -> /c/x); as is elsewhere.

    The scripts are POSIX: `C:/x` is not an absolute path to them."""
    if os.name != "nt":
        return str(path)
    posix = Path(path).resolve().as_posix()
    return f"/{posix[0].lower()}{posix[2:]}"


def _run(script: str, *args, env_extra: dict | None = None, path_first: Path | None = None,
         timeout: int = 600) -> subprocess.CompletedProcess:
    env = {k: v for k, v in os.environ.items() if k not in ("CLAUDE_CONFIG_DIR", "PYTHON_EXE")}
    env["PYTHON_EXE"] = _sh(sys.executable)
    if path_first is not None:
        env["PATH"] = str(path_first) + os.pathsep + env["PATH"]
    env.update(env_extra or {})
    res = subprocess.run([BASH, str(ROOT / "scripts" / script), *args], env=env,
                         capture_output=True, timeout=timeout)
    res.out = res.stdout.decode("utf-8", "replace") + res.stderr.decode("utf-8", "replace")
    return res


def _stubs(tmp_path: Path, *names: str, exit_code: int = 0) -> tuple[Path, Path]:
    """Executables that record `name args` in calls.log and do nothing else."""
    stubs = tmp_path / "stubs"
    stubs.mkdir(exist_ok=True)
    log = tmp_path / "calls.log"
    for name in names:
        stub = stubs / name
        stub.write_text(f'#!/bin/sh\necho "{name} $*" >> "{_sh(log)}"\nexit {exit_code}\n',
                        encoding="utf-8", newline="\n")
        stub.chmod(0o755)
    return stubs, log


# The fixture rather than `skipif(BASH is None)`: without a bash these FAIL on
# Windows (tests/conftest.py), and _run() spawns the same find_bash() answer.
needs_bash = pytest.mark.usefixtures("bash")


@needs_bash
def test_help_and_argument_errors():
    """The smoke test the test policy asks of every entry point."""
    helped = _run("install.sh", "--help")
    assert helped.returncode == 0 and "--install-units" in helped.out
    assert _run("install.sh", "--profile", "everything").returncode == 2
    assert _run("install-lite.sh", "--profile", "full").returncode == 2, \
        "install-lite.sh must not turn into a full install"
    assert _run("uninstall.sh", "--help").returncode == 0


@pytest.mark.integration
@needs_bash
def test_lite_merges_settings_leaves_out_wiki_and_uninstalls_cleanly(tmp_path):
    home = tmp_path / "home"
    home.mkdir()
    (home / "settings.json").write_text(json.dumps({"hooks": {"Stop": []}, "language": "ru"}),
                                        encoding="utf-8")
    res = _run("install-lite.sh", "--claude-home", _sh(home))
    assert res.returncode == 0, res.out

    settings = json.loads((home / "settings.json").read_text(encoding="utf-8"))
    assert settings["hooks"] == {"Stop": []} and settings["language"] == "ru"
    assert "permissions" in settings, "the template keys were not merged in"
    assert (home / "commands" / "code-review-ext.md").is_file()
    assert not (home / "commands" / "wiki.md").exists(), "/wiki needs cron/, which lite has none of"
    assert "full tier only" in res.out
    manifest = json.loads((home / ".bundle-manifest.json").read_text(encoding="utf-8"))
    assert manifest["tier"] == "lite" and "settings.json" in manifest["preserved"]

    (home / "skills" / "mine.md").write_text("added by the user", encoding="utf-8")
    diff = _run("install.sh", "--diff", "--claude-home", _sh(home))
    assert diff.returncode == 0 and "unchanged" in diff.out and "Nothing was written" in diff.out

    assert _run("uninstall.sh", "--claude-home", _sh(home), "--confirm").returncode == 0
    left = sorted(p.relative_to(home).as_posix() for p in home.rglob("*")
                  if p.is_file() and not p.name.startswith("settings.json.bak-"))
    assert left == ["settings.json", "skills/mine.md"], "uninstall took something of yours, or left its own"


@pytest.mark.integration
@needs_bash
def test_lite_without_python_replaces_settings_and_says_so(tmp_path):
    """No Python, no JSON merge: the honest fallback is a replacement WITH a
    backup, stated in the summary — not a silent overwrite."""
    stubs, _ = _stubs(tmp_path, "python3", "python", exit_code=1)
    home = tmp_path / "home"
    home.mkdir()
    (home / "settings.json").write_text('{"mine": true}', encoding="utf-8")
    res = _run("install.sh", "--claude-home", _sh(home), path_first=stubs,
               env_extra={"PYTHON_EXE": ""})
    assert res.returncode == 0, res.out
    backups = list(home.glob("settings.json.bak-*"))
    assert backups and json.loads(backups[0].read_text(encoding="utf-8")) == {"mine": True}
    assert "REPLACED" in res.out and "NOT written" in res.out
    assert not (home / ".bundle-manifest.json").exists()


@pytest.mark.integration
@needs_bash
def test_full_install_with_units_reinstall_and_uninstall(tmp_path):
    stubs, calls = _stubs(tmp_path, "systemctl", "loginctl")
    env = {"XDG_CONFIG_HOME": str(tmp_path / "xdg"), "HOME": str(tmp_path / "userhome"),
           "USERPROFILE": str(tmp_path / "userhome")}
    if importlib.util.find_spec("requests") is None:
        # The preflight only asks find_spec; the tasks never run here.
        (tmp_path / "pylib").mkdir()
        (tmp_path / "pylib" / "requests.py").write_text("", encoding="utf-8")
        env["PYTHONPATH"] = str(tmp_path / "pylib")
    home, pipe = tmp_path / "home", tmp_path / "pipe"
    units = tmp_path / "xdg" / "systemd" / "user"
    install = ("install.sh", "--profile", "full", "--scheduler", "systemd", "--install-units",
               "--claude-home", _sh(home), "--pipeline-root", _sh(pipe))

    first = _run(*install, "--enable-linger", env_extra=env, path_first=stubs)
    assert first.returncode == 0, first.out
    assert (home / "hooks").is_dir() and (pipe / "cron" / "registry.yaml").is_file()
    env_file = (pipe / ".env").read_text(encoding="utf-8")
    assert "\nPYTHON_EXE=" in env_file and "\nPYTHON_EXE=\n" not in env_file
    local = (pipe / "bundle.local.yaml")
    until = next(line for line in local.read_text(encoding="utf-8").splitlines()
                 if line.startswith("dry_run_until:")).split(":", 1)[1].strip()
    # A week from the day the installer ran — measured against the file it wrote.
    written_on = date.fromtimestamp(local.stat().st_mtime)
    assert (date.fromisoformat(until) - written_on).days == 7
    assert (units / "ClaudeWikiPipeline.timer").is_file()
    log = calls.read_text(encoding="utf-8")
    assert "systemctl --user enable --now ClaudeWikiPipeline.timer" in log
    assert "loginctl enable-linger" in log

    # An edited registry is the user's: kept on reinstall, and what the units follow.
    registry = pipe / "cron" / "registry.yaml"
    text = registry.read_text(encoding="utf-8")
    start = text.index("  - name: ClaudeLogRetention")
    end = text.index("  - name:", start + 1)
    registry.write_text(text[:end].rstrip("\n") + "\n    enabled: false\n\n" + text[end:],
                        encoding="utf-8")
    edited = registry.read_bytes()
    # `dry_run_until: confirm` holds the preview until the user writes a date
    # themselves; a reinstall must neither reopen the window nor touch the file.
    local.write_text(local.read_text(encoding="utf-8").replace(
        f"dry_run_until: {until}", "dry_run_until: confirm"), encoding="utf-8")
    held = local.read_bytes()
    assert b"dry_run_until: confirm" in held
    second = _run(*install, env_extra=env, path_first=stubs)
    assert second.returncode == 0, second.out
    assert registry.read_bytes() == edited
    assert local.read_bytes() == held, "the reinstall rewrote bundle.local.yaml"
    assert "kept your cron/registry.yaml" in second.out
    assert not (units / "ClaudeLogRetention.timer").exists(), "a disabled task's timer stayed installed"
    assert "systemctl --user disable --now ClaudeLogRetention.timer" in calls.read_text(encoding="utf-8")

    calls.write_text("", encoding="utf-8")
    third = _run("uninstall.sh", "--claude-home", _sh(home), "--confirm",
                 env_extra=env, path_first=stubs)
    assert third.returncode == 0, third.out
    log = calls.read_text(encoding="utf-8").splitlines()
    assert log and log[0].startswith("systemctl --user disable --now "), \
        "the timers must be disabled before anything else happens"
    assert not list(units.glob("Claude*")) and not (home / "hooks").exists()
    kept = sorted(p.relative_to(pipe).as_posix() for p in pipe.rglob("*") if p.is_file())
    assert kept == [".env", "bundle.local.yaml", "cron/registry.yaml"]
