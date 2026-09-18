"""What the installers say a re-install left for the user to do.

A re-install replaces the bundle's files and, on purpose, nothing of the user's.
So a kept registry that no longer matched the shipped one, tasks registered by
an older syncer, units generated from an older registry and files the bundle
stopped shipping all passed without a word — `commands/wiki.md` stayed on every
lite install that had it, and a Windows registry kept across 0.16.0 never learned
that ClaudeWikiPipeline had become the default. Both installers read the previous
manifest before replacing it and say so; these tests pin what they say and when.
"""
from __future__ import annotations

import hashlib
import importlib.util
import json
from pathlib import Path

import pytest

from ps_helpers import ROOT, define_functions, ps_quote, requires_powershell, run_ps


@pytest.fixture()
def bi():
    spec = importlib.util.spec_from_file_location("bundle_install",
                                                  ROOT / "scripts" / "lib" / "bundle_install.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest().upper()


def _diag(r, got: dict) -> str:
    """Everything the PowerShell run said, for an assertion that failed.

    These tests read their result out of a JSON file, so a cmdlet that wrote an
    error and carried on left the run's exit code at 0 and the failure said only
    that a value was wrong. The two Windows-only failures this made unreadable
    passed on every machine they could be reproduced on; without the run's own
    output there is nothing else to go on.
    """
    return ("\n-- powershell stdout --\n" + (r.stdout or "").strip() +
            "\n-- powershell stderr --\n" + (r.stderr or "").strip() +
            "\n-- results --\n" + json.dumps(got, indent=2, ensure_ascii=False))


def _layout(tmp_path: Path) -> dict:
    """A source bundle, a config root and a pipeline root with some files in each."""
    src = tmp_path / "src"
    for rel, text in (("home-claude/cron/registry.yaml", "tasks: []\n"),
                      ("home-claude/cron/admin/sync-tasks.ps1", "# syncer\n"),
                      ("home-claude/cron/admin/lib/registry-parse.ps1", "# parser\n")):
        (src / rel).parent.mkdir(parents=True, exist_ok=True)
        (src / rel).write_text(text, encoding="utf-8")
    home, pipe = tmp_path / "home", tmp_path / "pipe"
    for path in (home / "CLAUDE.md", home / "commands" / "wiki.md", pipe / "cron" / "retired.py"):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("x", encoding="utf-8")
    return {"src": src, "home": home, "pipe": pipe}


def _previous(home: Path, pipe: Path, **fields) -> dict:
    manifest = {"bundle_version": "0.17.0", "tier": "full",
                "claude_home": str(home), "pipeline_root": str(pipe),
                "written": [{"root": "claude_home", "path": "CLAUDE.md", "sha256": "AA"},
                            {"root": "claude_home", "path": "commands/wiki.md", "sha256": "BB"},
                            {"root": "pipeline_root", "path": "cron/retired.py", "sha256": "CC"},
                            {"root": "pipeline_root", "path": "cron/gone-already.py", "sha256": "DD"}],
                "preserved": [".env"], **fields}
    (home / ".bundle-manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
    return manifest


# ── install.sh: scripts/lib/bundle_install.py upgrade-notes ─────────────────

def _notes(bi, capsys, lay: dict, tier: str, version: str, preserved=(), units_drift=False) -> list[str]:
    written = lay["src"].parent / "written.tsv"
    written.write_text("claude_home\tCLAUDE.md\n", encoding="utf-8")
    kept = lay["src"].parent / "preserved.txt"
    kept.write_text("".join(f"{p}\n" for p in preserved), encoding="utf-8")
    argv = ["upgrade-notes", "--claude-home", str(lay["home"]), "--pipeline-root", str(lay["pipe"]),
            "--source", str(lay["src"]), "--tier", tier, "--version", version,
            "--written", str(written), "--preserved", str(kept)]
    capsys.readouterr()
    assert bi.main(argv + (["--units-drift"] if units_drift else [])) == 0
    return capsys.readouterr().out.splitlines()


def test_a_first_install_has_nothing_to_say(bi, capsys, tmp_path):
    assert _notes(bi, capsys, _layout(tmp_path), "full", "0.18.0") == []


def test_an_upgrade_names_the_version_the_leftovers_and_a_kept_registry(bi, capsys, tmp_path):
    lay = _layout(tmp_path)
    _previous(lay["home"], lay["pipe"])      # written before the template hash existed
    out = _notes(bi, capsys, lay, "full", "0.18.0", preserved=["cron/registry.yaml", ".env"])
    text = "\n".join(out)
    assert out[0].startswith("upgraded 0.17.0 -> 0.18.0: UPGRADING.md")
    # Left on disk: placed last time, not this time, still there. Not what this run
    # wrote, not what is gone already, not what is the user's.
    assert "2 file(s) an earlier install placed" in text
    assert f"    {lay['home'] / 'commands' / 'wiki.md'}" in out
    assert f"    {lay['pipe'] / 'cron' / 'retired.py'}" in out
    assert "CLAUDE.md" not in text and "gone-already" not in text
    assert "yours was kept because you edited it" in text and "--install-units" in text


def test_the_template_hash_decides_and_units_drift_is_reported_only_for_installed_units(
        bi, capsys, tmp_path):
    lay = _layout(tmp_path)
    same = _sha(lay["src"] / "home-claude" / "cron" / "registry.yaml")
    _previous(lay["home"], lay["pipe"], registry_template_sha256=same, bundle_version="0.18.0",
              written=[])
    assert _notes(bi, capsys, lay, "full", "0.18.0", preserved=["cron/registry.yaml"]) == [], \
        "the same template: nothing changed, nothing to say"

    # No hash recorded means an older installer, whatever VERSION says: a checkout
    # between releases carries the old number with a new registry.
    _previous(lay["home"], lay["pipe"], bundle_version="0.18.0", written=[])
    out = _notes(bi, capsys, lay, "full", "0.18.0", preserved=["cron/registry.yaml"])
    assert len(out) == 1 and "yours was kept because you edited it" in out[0], out

    _previous(lay["home"], lay["pipe"], registry_template_sha256=same, bundle_version="0.18.0",
              written=[])
    assert _notes(bi, capsys, lay, "full", "0.18.0", units_drift=True) == [], \
        "no units were ever installed, so a 'drift' preview is just the first one"

    _previous(lay["home"], lay["pipe"], registry_template_sha256=same, bundle_version="0.18.0",
              written=[], scheduler={"target": "systemd", "units_dir": "/x/systemd/user", "units": []})
    assert _notes(bi, capsys, lay, "full", "0.18.0", units_drift=True) == [
        "the installed units no longer match the registry (the preview above lists the "
        "difference) - re-run with --install-units"]


def test_a_lite_run_over_a_full_install_says_the_pipeline_was_not_updated(bi, capsys, tmp_path):
    lay = _layout(tmp_path)
    _previous(lay["home"], lay["pipe"], written=[])
    out = _notes(bi, capsys, lay, "lite", "0.17.0", preserved=["cron/registry.yaml"])
    assert out == ["the last install was FULL and this one is lite - cron/, wiki/, bin/ and "
                   "hooks/ were NOT updated. Re-run with --profile full"]


def test_the_full_manifest_records_the_registry_template(bi, tmp_path):
    lay = _layout(tmp_path)
    written = tmp_path / "written.tsv"
    written.write_text("claude_home\tCLAUDE.md\n", encoding="utf-8")
    for tier, expected in (("full", _sha(lay["src"] / "home-claude" / "cron" / "registry.yaml")),
                           ("lite", None)):
        assert bi.main(["write-manifest", "--claude-home", str(lay["home"]), "--pipeline-root",
                        str(lay["pipe"]), "--tier", tier, "--version", "0.18.0", "--written",
                        str(written), "--source", str(lay["src"])]) == 0
        manifest = json.loads((lay["home"] / ".bundle-manifest.json").read_text(encoding="utf-8"))
        assert manifest.get("registry_template_sha256") == expected, tier


# ── install.ps1: Get-UpgradeNotes ────────────────────────────────────────────

@requires_powershell   # one PowerShell process: 0.6 s measured, so the fast suite
def test_install_ps1_says_what_an_upgrade_left_to_do(tmp_path):
    lay = _layout(tmp_path)
    prev = _previous(lay["home"], lay["pipe"])
    syncer = lay["src"] / "home-claude" / "cron" / "admin" / "sync-tasks.ps1"
    unchanged_syncer = {"root": "pipeline_root", "path": "cron/admin/sync-tasks.ps1", "sha256": _sha(syncer)}
    code = define_functions(ROOT / "scripts" / "install.ps1",
                            ["Get-UpgradeNotes", "Get-Sha256"]) + f"""
$srcHome = {ps_quote(lay['src'] / 'home-claude')}
$ClaudeHome = {ps_quote(lay['home'])}
$PipelineRoot = {ps_quote(lay['pipe'])}
$bundleVer = '0.18.0'
$script:written = @(@{{ root = 'claude_home'; path = 'CLAUDE.md' }})
$script:preserved = @('cron/registry.yaml', '.env')
$results = [ordered]@{{}}

# An upgrade across versions, registry kept, a manifest from before the template hash.
$script:registryKept = $true; $syncStatus = 'yes'
$results.kept = @(Get-UpgradeNotes ({ps_quote(json.dumps(prev))} | ConvertFrom-Json) 'full')
# No hash and the SAME version: still an older installer, so still a kept registry to check.
$results.keptSameVersion = @(Get-UpgradeNotes ({ps_quote(json.dumps(dict(prev, bundle_version="0.18.0", written=[])))} | ConvertFrom-Json) 'full')

# Same version and template, a registry that was NOT kept, the syncer's parser new
# since the last install: sync.cmd — unless this run already synced.
$prev2 = {ps_quote(json.dumps(dict(prev, bundle_version="0.18.0", written=[unchanged_syncer],
                                     registry_template_sha256=_sha(lay["src"] / "home-claude" / "cron" / "registry.yaml"))))} | ConvertFrom-Json
$script:registryKept = $false; $syncStatus = 'no'
$results.syncer = @(Get-UpgradeNotes $prev2 'full')
$syncStatus = 'yes'
$results.synced = @(Get-UpgradeNotes $prev2 'full')
$results.lite = @(Get-UpgradeNotes $prev2 'lite')
$results.first = @(Get-UpgradeNotes $null 'full')
# To a UTF-8 file: the console code page would mangle the notes' dashes.
[System.IO.File]::WriteAllText({ps_quote(tmp_path / 'notes.json')}, ($results | ConvertTo-Json -Depth 4),
                               (New-Object System.Text.UTF8Encoding($false)))
"""
    r = run_ps(code, tmp_path, cwd=tmp_path)
    assert r.returncode == 0, r.stdout + r.stderr
    raw = json.loads((tmp_path / "notes.json").read_text(encoding="utf-8"))
    got = {k: [v] if isinstance(v, str) else (v or []) for k, v in raw.items()}

    kept = got["kept"]
    assert kept[0].startswith("upgraded 0.17.0 -> 0.18.0: UPGRADING.md"), kept
    left = next(n for n in kept if "earlier install placed" in n)
    assert str(lay["home"] / "commands" / "wiki.md") in left
    assert str(lay["pipe"] / "cron" / "retired.py") in left
    assert "CLAUDE.md" not in left and "gone-already" not in left
    assert any("yours was kept" in n and "sync.cmd" in n for n in kept), \
        "a kept registry needs the notice even when this run synced — it synced the OLD one"
    assert len(got["keptSameVersion"]) == 1 and "yours was kept" in got["keptSameVersion"][0], \
        got["keptSameVersion"]

    assert got["syncer"] == [
        f"the task definitions changed since your last install — run "
        f"{lay['pipe'] / 'cron' / 'admin' / 'sync.cmd'} so Task Scheduler matches "
        f"(it lists each task it changes as updated)"], got["syncer"]
    assert got["synced"] == [], "this run's own sync already applied the new syncer"
    assert got["lite"] == [
        "the last install was FULL and this one is lite — cron/, wiki/, bin/ and hooks/ were "
        "NOT updated. Re-run with -Profile full"], got["lite"]
    assert got["first"] == []


@requires_powershell   # one PowerShell process, like the test above
def test_install_ps1_replaces_only_a_registry_it_bootstrapped_and_nobody_edited(tmp_path):
    """install.ps1 kept EVERY bootstrapped registry, so no upgrade's new tasks or
    defaults reached a Windows install, while install.sh replaced an unedited one.
    The manifest now records the registry the installer bootstrapped, and a
    re-install replaces a file that still matches it — never an edited one, and
    never one an older installer left without that record."""
    lay = _layout(tmp_path)
    (lay["src"] / "home-claude" / "cron" / "registry.yaml").write_text(
        "tasks:\n  - name: X\n    script: <bundle-install-path>\\cron\\x.py\n    user: <user>\n",
        encoding="utf-8")
    reg = lay["pipe"] / "cron" / "registry.yaml"
    reg.write_text("tasks:\n  - name: X\n    script: C:\\b\\cron\\x.py\n    user: me\n", encoding="utf-8")
    edited = tmp_path / "edited.yaml"
    edited.write_text(reg.read_text(encoding="utf-8") + "    enabled: false\n", encoding="utf-8")
    code = define_functions(ROOT / "scripts" / "install.ps1",
                            ["Test-KeepRegistry", "Write-Manifest", "Get-Sha256", "Good", "Info"]) + f"""
$srcHome = {ps_quote(lay['src'] / 'home-claude')}
$ClaudeHome = {ps_quote(lay['home'])}; $homeFull = $ClaudeHome
$PipelineRoot = {ps_quote(lay['pipe'])}; $pipeFull = $PipelineRoot
$DryRun = $false; $bundleVer = '0.18.0'
$script:written = New-Object System.Collections.Generic.List[object]
$script:preserved = New-Object System.Collections.Generic.List[string]
$results = [ordered]@{{}}

$script:previousManifest = $null
$results.template = Test-KeepRegistry (Join-Path $srcHome 'cron/registry.yaml')
$results.noManifest = Test-KeepRegistry {ps_quote(reg)}
$results.missing = Test-KeepRegistry (Join-Path $PipelineRoot 'cron/none.yaml')

# Each conjunct Write-Manifest's registry_bootstrapped_sha256 hangs on, recorded
# separately: the note is absent whichever one is false, and the manifest alone
# cannot say which — which is how a missing Get-FileHash read as "nothing to
# record" for a whole release.
$probe = Join-Path $PipelineRoot 'cron/registry.yaml'
$results.psVersion = "$($PSVersionTable.PSVersion)"
$results.probePath = "$probe"
$results.probeExists = [bool](Test-Path $probe)
$results.probePlaceholder = [bool](Select-String -Path $probe -Pattern '<(bundle-install-path|user)>' -Quiet)
$results.probeHash = "$(Get-Sha256 $probe)"

# What this run bootstrapped is recorded — and a kept registry is not.
$script:registryKept = $false
Write-Manifest 'full'
$results.manifestExists = [bool](Test-Path -LiteralPath (Join-Path $ClaudeHome '.bundle-manifest.json'))
$script:previousManifest = Get-Content (Join-Path $ClaudeHome '.bundle-manifest.json') -Raw | ConvertFrom-Json
$results.recorded = "$($script:previousManifest.registry_bootstrapped_sha256)"
$results.unedited = Test-KeepRegistry {ps_quote(reg)}
$results.edited = Test-KeepRegistry {ps_quote(edited)}
$script:registryKept = $true
Write-Manifest 'full'
$results.recordedWhenKept = "$((Get-Content (Join-Path $ClaudeHome '.bundle-manifest.json') -Raw | ConvertFrom-Json).registry_bootstrapped_sha256)"
[System.IO.File]::WriteAllText({ps_quote(tmp_path / 'keep.json')}, ($results | ConvertTo-Json),
                               (New-Object System.Text.UTF8Encoding($false)))
"""
    r = run_ps(code, tmp_path, cwd=tmp_path)
    assert r.returncode == 0, r.stdout + r.stderr
    got = json.loads((tmp_path / "keep.json").read_text(encoding="utf-8"))
    assert got["template"] is False, "a template with placeholders is not the user's" + _diag(r, got)
    assert got["noManifest"] is True, "no record of what was bootstrapped: kept" + _diag(r, got)
    assert got["missing"] is False, _diag(r, got)
    assert got["recorded"] == _sha(reg), _diag(r, got)
    assert got["unedited"] is False, "the registry the last install bootstrapped was kept" + _diag(r, got)
    assert got["edited"] is True, "an edited registry would be replaced" + _diag(r, got)
    assert got["recordedWhenKept"] == "", "a kept registry is not recorded as bootstrapped" + _diag(r, got)


@requires_powershell   # one PowerShell process, like the tests above
def test_install_ps1_replaces_only_a_wiki_index_nobody_changed(tmp_path):
    """install.ps1 kept ANY existing wiki/index.md, while install.sh replaces one
    that is still what the last install wrote. Now both keep it only once it has
    changed — by hand, or by the nightly build-index refreshing its Stats table —
    and a record-less one, as before."""
    lay = _layout(tmp_path)
    shipped = lay["src"] / "home-claude" / "wiki" / "index.md"
    shipped.parent.mkdir(parents=True)
    shipped.write_text("# Wiki\n\nThe page this bundle ships now.\n", encoding="utf-8")
    installed = lay["pipe"] / "wiki" / "index.md"
    installed.parent.mkdir(parents=True)
    installed.write_text("# Wiki\n\nThe page the last install wrote.\n", encoding="utf-8")
    refreshed = tmp_path / "refreshed.md"
    refreshed.write_text(installed.read_text(encoding="utf-8") + "\n## Stats\n\n| projects/ | 3 |\n",
                         encoding="utf-8")
    record = {"written": [{"root": "pipeline_root", "path": "wiki/index.md", "sha256": _sha(installed)}]}
    code = define_functions(ROOT / "scripts" / "install.ps1",
                            ["Test-KeepWikiIndex", "Get-Sha256"]) + f"""
$srcHome = {ps_quote(lay['src'] / 'home-claude')}
$results = [ordered]@{{}}
$script:previousManifest = $null
# The shipped page Test-KeepWikiIndex compares against, reached the way it
# reaches it: a page that does not resolve reads exactly like a changed one.
$probe = Join-Path $srcHome 'wiki/index.md'
$results.psVersion = "$($PSVersionTable.PSVersion)"
$results.probePath = "$probe"
$results.probeExists = [bool](Test-Path $probe)
$results.probeHash = "$(Get-Sha256 $probe)"
$results.missing = Test-KeepWikiIndex {ps_quote(lay['pipe'] / 'wiki' / 'none.md')}
$results.shipped = Test-KeepWikiIndex {ps_quote(shipped)}
$results.noRecord = Test-KeepWikiIndex {ps_quote(installed)}
$script:previousManifest = {ps_quote(json.dumps(record))} | ConvertFrom-Json
$results.unchanged = Test-KeepWikiIndex {ps_quote(installed)}
$results.refreshed = Test-KeepWikiIndex {ps_quote(refreshed)}
[System.IO.File]::WriteAllText({ps_quote(tmp_path / 'index.json')}, ($results | ConvertTo-Json),
                               (New-Object System.Text.UTF8Encoding($false)))
"""
    r = run_ps(code, tmp_path, cwd=tmp_path)
    assert r.returncode == 0, r.stdout + r.stderr
    got = json.loads((tmp_path / "index.json").read_text(encoding="utf-8"))
    assert got["missing"] is False, _diag(r, got)
    assert got["shipped"] is False, "the shipped page holds nothing of the user's" + _diag(r, got)
    assert got["noRecord"] is True, "no record of what the last install wrote: kept" + _diag(r, got)
    assert got["unchanged"] is False, "the page the last install wrote, untouched, was kept" + _diag(r, got)
    assert got["refreshed"] is True, "a page build-index had refreshed would be replaced" + _diag(r, got)
