#!/usr/bin/env python3
"""The JSON half of scripts/install.sh and scripts/uninstall.sh.

The POSIX installer is bash, so that a lite install needs nothing else on the
machine. What it cannot do well in bash — read and write JSON, hash files, add
seven days to a date — lives here, and install.sh calls it only when a usable
Python exists (the full tier requires one anyway).

The install manifest is the SAME file install.ps1 writes, so either installer's
`-Diff` / `--diff` and uninstaller can read the other's:

    {"bundle_version", "installed_at", "tier", "claude_home", "pipeline_root",
     "written":   [{"root": "claude_home" | "pipeline_root", "path", "sha256"}],
     "preserved": ["settings.json", ".env", ...],
     "registry_template_sha256": "...",
     "scheduler": {"target", "units_dir", "units": [{"path", "sha256"}]}}

`scheduler` is the POSIX addition — the units `install.sh --install-units`
placed, so uninstall.sh disables and removes exactly those. uninstall.ps1 never
reads it. `registry_template_sha256` (full tier, both installers) is the shipped
cron/registry.yaml the install came from: an edited registry is kept and carries
no hash, so the next install needs it to tell whether the task definitions
changed. Hashes are upper-case hex, as Get-FileHash writes them.

Subcommands (paths absolute):
  merge-settings SRC DST BACKUP  add the template keys DST lacks (yours win);
                                 back DST up to BACKUP first. Prints the added
                                 keys. Exit 3: DST is not a JSON object, untouched.
  was-ours CLAUDE_HOME BASE ROOT REL
                                 exit 0 when the previous install (the manifest
                                 in CLAUDE_HOME) wrote ROOT/REL, found at
                                 BASE/REL, and it is unchanged since — so
                                 replacing it loses nothing of the user's.
  tier CLAUDE_HOME               the deployed tier from the manifest, or nothing.
  units CLAUDE_HOME              the units a previous install placed that still
                                 exist: "target<TAB>units_dir<TAB>name" per line.
  units-dir TARGET               where the init system loads per-user units from.
  set-env-if-empty FILE KEY VAL  fill an empty or absent KEY= line. Exit 1: the
                                 user set a value, left alone.
  yaml-get YAML KEY              a key of bundle.local.yaml: strings as is, the
                                 rest as JSON; nothing when the file won't parse.
  open-dry-run-window YAML DAYS  set an EMPTY `dry_run_until:` to today + DAYS.
  write-manifest ...             see --help of the subcommand.
  upgrade-notes ...              what a re-install left for the user to do, one
                                 note per line; run BEFORE write-manifest.
  diff ...                       the per-file preview behind install.sh --diff.
  uninstall ...                  the file half of uninstall.sh.
"""
from __future__ import annotations

import argparse
import hashlib
import importlib.util
import json
import os
import re
import shutil
import sys
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

MANIFEST = ".bundle-manifest.json"
SCRIPTS = Path(__file__).resolve().parent.parent


def read_text(path) -> str:
    """Line endings as they are on disk: a CRLF .env must stay CRLF after an edit."""
    with open(path, encoding="utf-8-sig", newline="") as fh:
        return fh.read()


def write_text(path, text: str) -> None:
    with open(path, "w", encoding="utf-8", newline="") as fh:
        fh.write(text)


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 16), b""):
            digest.update(chunk)
    return digest.hexdigest().upper()


def read_manifest(claude_home: Path):
    """The manifest as a dict, None when there is none. Unreadable -> ValueError."""
    path = claude_home / MANIFEST
    if not path.is_file():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, ValueError) as exc:
        raise ValueError(f"{path} is not valid JSON ({exc})")
    if not isinstance(data, dict):
        raise ValueError(f"{path} is not a JSON object")
    return data


def lines_of(path: str | None) -> list[str]:
    if not path or not os.path.isfile(path):
        return []
    with open(path, encoding="utf-8") as fh:
        return [line.rstrip("\r\n") for line in fh if line.strip()]


def contained(base: Path, rel) -> Path | None:
    """BASE/REL when REL is a plain relative path that stays inside BASE.

    Absolute paths, drive letters and `..` are refused outright, and the result
    is checked after resolving symlinks: without this one edited line in a JSON
    file turns an uninstaller into an arbitrary-file deleter.
    """
    if not isinstance(rel, str) or not rel.strip():
        return None
    if rel.startswith(("/", "\\")) or re.match(r"[A-Za-z]:", rel) or ".." in re.split(r"[\\/]", rel):
        return None
    full = Path(os.path.realpath(base / rel))
    try:
        full.relative_to(os.path.realpath(base))
    except ValueError:
        return None
    return full


def within(path: Path, base: Path) -> bool:
    try:
        Path(os.path.realpath(path)).relative_to(os.path.realpath(base))
        return True
    except ValueError:
        return False


def gen_scheduler():
    spec = importlib.util.spec_from_file_location("gen_scheduler", SCRIPTS / "gen-scheduler.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


UNIT_NAME = {
    "systemd": re.compile(r"[A-Za-z0-9_.@-]+\.(service|timer)"),
    "launchd": re.compile(r"com\.claude-bundle\.[A-Za-z0-9_.-]+\.plist"),
}
UNIT_DIR_TAIL = {"systemd": ("systemd", "user"), "launchd": ("Library", "LaunchAgents")}


def scheduler_units(manifest: dict) -> tuple[list[tuple[str, Path, str, str]], int]:
    """([(target, units_dir, name, sha256)], rejected) for the recorded units.

    The directory and every name must look like what install.sh places — a
    per-user unit directory and the bundle's own unit names — so the manifest
    cannot point the uninstaller at anything else.
    """
    section = manifest.get("scheduler")
    if not isinstance(section, dict):
        return [], 0
    target = section.get("target")
    units = section.get("units") if isinstance(section.get("units"), list) else []
    units_dir = str(section.get("units_dir") or "")
    if target not in UNIT_NAME or not os.path.isabs(units_dir) \
            or Path(units_dir).parts[-2:] != UNIT_DIR_TAIL[target]:
        return [], len(units)
    found, rejected = [], 0
    for unit in units:
        name = unit.get("path") if isinstance(unit, dict) else None
        if not isinstance(name, str) or not UNIT_NAME[target].fullmatch(name):
            rejected += 1
            continue
        found.append((target, Path(units_dir), name, str(unit.get("sha256", ""))))
    return found, rejected


# ── subcommands ──────────────────────────────────────────────────────────────

def merge_settings(src: str, dst: str, backup: str) -> int:
    template = json.loads(read_text(src))
    try:
        user = json.loads(read_text(dst))
        if not isinstance(user, dict):
            raise ValueError("not a JSON object")
    except (OSError, ValueError) as exc:
        print(f"{dst} could not be parsed ({exc})")
        return 3
    added = [key for key in template if key not in user]
    if added:
        shutil.copy2(dst, backup)
        user.update({key: template[key] for key in added})
        # No BOM: Claude Code reads settings.json as plain UTF-8.
        write_text(dst, json.dumps(user, indent=2, ensure_ascii=False) + "\n")
    print(", ".join(added))
    return 0


def was_ours(claude_home: str, base: str, root: str, rel: str) -> int:
    try:
        manifest = read_manifest(Path(claude_home))
    except ValueError:
        return 1
    target = Path(base) / rel
    if not manifest or not target.is_file():
        return 1
    for entry in manifest.get("written") or []:
        if isinstance(entry, dict) and entry.get("root", "claude_home") == root \
                and entry.get("path") == rel:
            return 0 if str(entry.get("sha256", "")).upper() == sha256(target) else 1
    return 1


def tier(claude_home: str) -> int:
    try:
        manifest = read_manifest(Path(claude_home)) or {}
    except ValueError:
        manifest = {}
    if manifest.get("tier") in ("lite", "full"):
        print(manifest["tier"])
    return 0


def units(claude_home: str) -> int:
    try:
        manifest = read_manifest(Path(claude_home)) or {}
    except ValueError:
        return 0
    for target, units_dir, name, _ in scheduler_units(manifest)[0]:
        if (units_dir / name).is_file():
            print(f"{target}\t{units_dir}\t{name}")
    return 0


def set_env_if_empty(path: str, key: str, value: str) -> int:
    """Only an EMPTY or absent line is filled; a value the user set is never touched."""
    lines = read_text(path).splitlines(keepends=True)
    empty = re.compile(rf"[ \t]*(export[ \t]+)?{re.escape(key)}[ \t]*=[ \t]*\r?\n?$")
    filled = re.compile(rf"[ \t]*(export[ \t]+)?{re.escape(key)}[ \t]*=[ \t]*\S")
    for i, line in enumerate(lines):
        if filled.match(line):
            return 1
        if empty.match(line):
            ending = line[len(line.rstrip("\r\n")):] or "\n"
            lines[i] = f"{key}={value}{ending}"
            break
    else:
        if lines and not lines[-1].endswith("\n"):
            lines[-1] += "\n"
        lines.append(f"{key}={value}\n")
    write_text(path, "".join(lines))
    return 0


def yaml_get(path: str, key: str) -> int:
    """A string value as is, anything else as JSON (`null` when absent)."""
    try:
        import yaml
        data = yaml.safe_load(Path(path).read_text(encoding="utf-8-sig"))
    except Exception:  # no PyYAML or broken YAML: nothing to read, and not fatal
        return 0
    value = data.get(key) if isinstance(data, dict) else None
    print(value.strip() if isinstance(value, str) else json.dumps(value))
    return 0


def open_dry_run_window(path: str, days: str) -> int:
    """A FRESH manifest only: install.sh calls this right after copying the template."""
    until = (date.today() + timedelta(days=int(days))).isoformat()
    new, count = re.subn(r"(?m)^dry_run_until:[ \t]*(?=\r?$)", f"dry_run_until: {until}",
                         read_text(path), count=1)
    if count:
        write_text(path, new)
        print(until)
    return 0


def write_manifest(args) -> int:
    bases = {"claude_home": Path(args.claude_home), "pipeline_root": Path(args.pipeline_root)}
    preserved = list(dict.fromkeys(lines_of(args.preserved)))
    try:
        previous = read_manifest(bases["claude_home"]) or {}
    except ValueError:
        previous = {}
    written, seen = [], set()
    for line in lines_of(args.written):
        root, _, rel = line.partition("\t")
        if root not in bases or (root, rel) in seen or rel in preserved:
            continue
        seen.add((root, rel))
        path = bases[root] / rel
        if path.is_file():
            written.append({"root": root, "path": rel, "sha256": sha256(path)})
    manifest = {
        "bundle_version": args.version,
        "installed_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "tier": args.tier,
        "claude_home": str(bases["claude_home"]),
        "pipeline_root": str(bases["pipeline_root"]),
        "written": written,
        "preserved": preserved,
    }
    template = Path(args.source) / "home-claude" / "cron" / "registry.yaml" if args.source else None
    if args.tier == "full" and template and template.is_file():
        manifest["registry_template_sha256"] = sha256(template)
    if args.units_dir:
        units_dir = Path(args.units_dir)
        manifest["scheduler"] = {
            "target": args.scheduler, "units_dir": str(units_dir),
            "units": [{"path": name, "sha256": sha256(units_dir / name)}
                      for name in dict.fromkeys(lines_of(args.units))
                      if (units_dir / name).is_file()]}
    elif isinstance(previous.get("scheduler"), dict):
        # Units this run did not touch are still installed. Dropping them from
        # the manifest would leave uninstall.sh blind to timers that keep firing.
        manifest["scheduler"] = previous["scheduler"]
    write_text(bases["claude_home"] / MANIFEST, json.dumps(manifest, indent=4, ensure_ascii=False) + "\n")
    print(f"wrote {MANIFEST} ({len(written)} files - uninstall with scripts/uninstall.sh)")
    return 0


def upgrade_notes(args) -> int:
    """What this re-install left for the user to do, one note per line.

    A re-install replaces the bundle's files and, on purpose, nothing of the
    user's — so an edited registry that no longer matches the shipped one, units
    generated from an older registry and files this bundle stopped shipping all
    used to pass without a word. The previous manifest is the only record of what
    the last install was, which is why install.sh calls this BEFORE
    write-manifest replaces it. The same notes as install.ps1::Get-UpgradeNotes;
    UPGRADING.md has the steps behind each. Continuation lines start with spaces.
    """
    claude_home = Path(args.claude_home)
    try:
        prev = read_manifest(claude_home)
    except ValueError:
        prev = None
    if not prev:
        return 0                                    # a first install has no past
    notes = []
    prev_ver, prev_tier = str(prev.get("bundle_version") or ""), prev.get("tier")
    if prev_ver and prev_ver != args.version:
        notes.append(f"upgraded {prev_ver} -> {args.version}: UPGRADING.md lists what a "
                     f"re-install does not do for you - read every section above {prev_ver}")
    if prev_tier == "full" and args.tier != "full":
        notes.append(f"the last install was FULL and this one is {args.tier} - cron/, wiki/, "
                     f"bin/ and hooks/ were NOT updated. Re-run with --profile full")
    # Files the last install wrote that this one neither wrote nor kept: the new
    # manifest does not list them, so uninstall.sh will never see them again.
    bases = {"claude_home": claude_home, "pipeline_root": Path(args.pipeline_root)}
    now = {tuple(line.split("\t", 1)) for line in lines_of(args.written)}
    preserved = set(lines_of(args.preserved))
    left = []
    for entry in prev.get("written") or []:
        if not isinstance(entry, dict):
            continue
        root, rel = entry.get("root") or "claude_home", entry.get("path")
        if (root, rel) in now or rel in preserved or root not in bases:
            continue
        full = contained(bases[root], rel)
        if full is not None and full.is_file():
            left.append(full)
    if left:
        notes.append(f"{len(left)} file(s) an earlier install placed are not part of this one - "
                     f"left on disk and no longer tracked, so uninstall.sh will not remove "
                     f"them. Delete them if nothing of yours uses them:")
        notes.extend(f"    {path}" for path in left[:10])
        if len(left) > 10:
            notes.append(f"    ... and {len(left) - 10} more")
    if prev_tier == "full" and args.tier == "full":
        template = Path(args.source) / "home-claude" / "cron" / "registry.yaml"
        recorded = prev.get("registry_template_sha256")
        # No field: an installer older than this one wrote that manifest, so the
        # template it came with is older too. Not the version string — a checkout
        # between releases carries the same VERSION with a different registry.
        changed = (not recorded or not template.is_file()
                   or str(recorded).upper() != sha256(template))
        if changed and "cron/registry.yaml" in preserved:
            notes.append(f"the shipped registry changed since your last install, and yours was "
                         f"kept because you edited it: carry the changes over from {template}, "
                         f"then re-run with --install-units")
        elif args.units_drift and isinstance(prev.get("scheduler"), dict):
            notes.append("the installed units no longer match the registry (the preview above "
                         "lists the difference) - re-run with --install-units")
    for note in notes:
        print(note)
    return 0


def diff(args) -> int:
    claude_home, source = Path(args.claude_home), Path(args.source)
    bases = {"claude_home": claude_home, "pipeline_root": Path(args.pipeline_root)}
    print("\n=== claude-bundle install diff (nothing will be written) ===")
    print(f"Profile:      {args.profile}")
    print(f"ClaudeHome:   {claude_home}")
    print(f"PipelineRoot: {bases['pipeline_root']}")
    print(f"Source:       {source / 'home-claude'}")
    try:
        manifest = read_manifest(claude_home)
    except ValueError as exc:
        print(f"[warn] {exc} - diffing against the source only")
        manifest = None
    if manifest:
        print(f"Manifest:     {claude_home / MANIFEST} - tier {manifest.get('tier')}, "
              f"{len(manifest.get('written') or [])} file(s), installed {manifest.get('installed_at')}")
    elif manifest is None:
        print(f"[warn] no readable {MANIFEST} under {claude_home} - every file reads as a fresh install")
    print()
    counts = {"new": 0, "modified": 0, "unchanged": 0, "removed-from-bundle": 0}
    planned = set()
    for line in lines_of(args.plan):
        root, rel, src = line.split("\t")
        if (root, rel) in planned:
            continue
        planned.add((root, rel))
        dst = bases[root] / rel
        if not dst.is_file():
            status = "new"
        elif sha256(dst) == sha256(source / src):
            status = "unchanged"
        else:
            status = "modified"
        counts[status] += 1
        if status != "unchanged":
            print(f"  {status:<20} {dst}")
    # Only the manifest knows these: files an older bundle installed that this
    # one no longer ships. An upgrade leaves them on disk.
    for entry in (manifest or {}).get("written") or []:
        if not isinstance(entry, dict):
            continue
        key = (entry.get("root", "claude_home"), entry.get("path"))
        if key in planned:
            continue
        counts["removed-from-bundle"] += 1
        print(f"  {'removed-from-bundle':<20} {bases.get(key[0], claude_home) / str(key[1])}")
    print()
    for status, count in counts.items():
        print(f"  {status:<20} {count}")
    print("\nCaveats, so the list is not read as a plain overwrite plan:")
    print("  settings.json is MERGED (your keys win; only missing template keys are added).")
    print("  An edited cron/registry.yaml or wiki/index.md is preserved, not replaced.")
    print("  Anything 'modified' is backed up to .bundle-backup-<stamp>/ by a real install.")
    print("Nothing was written. Drop --diff to install.")
    return 0


def uninstall(args) -> int:
    claude_home = Path(os.path.abspath(args.claude_home))
    try:
        manifest = read_manifest(claude_home)
    except ValueError as exc:
        print(f"ERROR: install manifest is unreadable: {exc}")
        print("       Fix or delete it, then remove the files by hand.")
        return 1
    if manifest is None:
        print(f"ERROR: no install manifest at {claude_home / MANIFEST}")
        print("       Without it this script cannot tell your files from the bundle's,")
        print("       so it removes nothing. Installed elsewhere? Pass --claude-home.")
        return 1
    if not isinstance(manifest.get("written"), list):
        print(f"ERROR: install manifest has no 'written' list: {claude_home / MANIFEST}")
        print("       It is corrupt or from a newer bundle - removing nothing.")
        return 1
    apply = (args.confirm or args.force) and not args.dry_run

    # ClaudeHome is where the manifest IS, never what it claims; the pipeline
    # root can only come from the file, so it must at least be absolute.
    recorded = manifest.get("claude_home")
    if recorded and os.path.abspath(str(recorded)) != str(claude_home):
        print(f"[warn] manifest records claude_home = {recorded}, but it was found in "
              f"{claude_home} - using the latter")
    pipeline_root = claude_home
    if manifest.get("pipeline_root"):
        if not os.path.isabs(str(manifest["pipeline_root"])):
            print(f"ERROR: manifest pipeline_root is not an absolute path: {manifest['pipeline_root']}")
            return 1
        pipeline_root = Path(os.path.abspath(str(manifest["pipeline_root"])))
    bases = {"claude_home": claude_home, "pipeline_root": pipeline_root}

    print("\n=== claude-bundle uninstaller ===")
    print(f"ClaudeHome:   {claude_home}")
    if pipeline_root != claude_home:
        print(f"PipelineRoot: {pipeline_root}")
    print(f"Installed:    {manifest.get('installed_at')} (bundle {manifest.get('bundle_version')}, "
          f"{manifest.get('tier')} tier)")
    print(f"Files:        {len(manifest['written'])} written by the installer")
    print(f"Mode:         {'DELETE' if apply else 'dry run - re-run with --confirm to delete'}\n")

    removed = gone = skipped = 0
    touched: set[Path] = set()

    def remove(path: Path, label: str, recorded_hash: str) -> None:
        nonlocal removed, gone, skipped
        if not path.is_file():
            gone += 1
            return
        if recorded_hash and recorded_hash.upper() != sha256(path) and not args.force:
            print(f"[warn] changed since install - keeping {label} (use --force to delete it anyway)")
            skipped += 1
            return
        removed += 1
        if apply:
            path.unlink()
            touched.add(path.parent)
        else:
            print(f"[dry-run] would remove {label}")

    units, rejected = scheduler_units(manifest)
    if rejected:
        print(f"[warn] {rejected} scheduler unit entr(y/ies) do not look like the bundle's - ignored")
    for _target, units_dir, name, recorded_hash in units:
        remove(units_dir / name, str(units_dir / name), recorded_hash)
    for entry in manifest["written"]:
        root = entry.get("root", "claude_home") if isinstance(entry, dict) else None
        full = contained(bases[root], entry.get("path")) if root in bases else None
        if full is None:
            print(f"[warn] manifest entry escapes its root - ignored: {entry}")
            rejected += 1
            continue
        remove(full, str(entry.get("path")), str(entry.get("sha256", "")))

    # Prune only directories a removal emptied, and their parents up to (never
    # including) a root — the trees also hold the user's own files.
    pruned = 0
    if apply:
        roots = {os.path.realpath(p) for p in bases.values()}
        candidates: set[Path] = set()
        for directory in touched:
            current = directory
            while os.path.realpath(current) not in roots and current != current.parent \
                    and any(within(current, base) for base in bases.values()):
                candidates.add(current)
                current = current.parent
        for directory in sorted(candidates, key=lambda p: len(str(p)), reverse=True):
            if not directory.is_dir():
                continue
            cache = directory / "__pycache__"
            if cache.is_dir() and all(p.is_file() and p.suffix == ".pyc" for p in cache.rglob("*")):
                shutil.rmtree(cache)
                pruned += 1
            if not any(directory.iterdir()):
                directory.rmdir()
                pruned += 1
        if skipped == 0:
            (claude_home / MANIFEST).unlink()
            print(f"[ok]   removed {MANIFEST}")
        else:
            print(f"[warn] kept {MANIFEST} - {skipped} file(s) still listed in it were skipped")

    if manifest.get("preserved"):
        print("\nKept (yours - the installer never claimed these):")
        for rel in manifest["preserved"]:
            print(f"  {rel}")
    print("\n--- Summary -----------------------------------------------------")
    print(f"removed: {removed}   already gone: {gone}   skipped (modified): {skipped}   "
          f"rejected (bad path): {rejected}   empty dirs pruned: {pruned}")
    if not apply:
        print("\nDry run - nothing was deleted. Re-run with --confirm to apply.")
        return 0
    return 2 if skipped else 0


def main(argv: list[str] | None = None) -> int:
    # The shell reads this output with `read`. A Windows Python (Git Bash, the
    # test suite) would end each line with \r\n, and the \r would stick to the
    # last field — a unit name that no longer names a file.
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(newline="\n")
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="command", required=True)
    for name, params in (("merge-settings", ("src", "dst", "backup")),
                         ("was-ours", ("claude_home", "base", "root", "rel")),
                         ("tier", ("claude_home",)), ("units", ("claude_home",)),
                         ("units-dir", ("target",)),
                         ("set-env-if-empty", ("path", "key", "value")),
                         ("yaml-get", ("path", "key")),
                         ("open-dry-run-window", ("path", "days"))):
        cmd = sub.add_parser(name)
        for param in params:
            cmd.add_argument(param)
    cmd = sub.add_parser("write-manifest")
    for opt in ("--claude-home", "--pipeline-root", "--tier", "--version", "--written"):
        cmd.add_argument(opt, required=True)
    for opt in ("--preserved", "--scheduler", "--units-dir", "--units", "--source"):
        cmd.add_argument(opt)
    cmd = sub.add_parser("upgrade-notes")
    for opt in ("--claude-home", "--pipeline-root", "--source", "--tier", "--version", "--written"):
        cmd.add_argument(opt, required=True)
    cmd.add_argument("--preserved")
    cmd.add_argument("--units-drift", action="store_true",
                     help="gen-scheduler.py --check found the installed units out of date")
    cmd = sub.add_parser("diff")
    for opt in ("--claude-home", "--pipeline-root", "--source", "--plan", "--profile"):
        cmd.add_argument(opt, required=True)
    cmd = sub.add_parser("uninstall")
    cmd.add_argument("--claude-home", required=True)
    for flag in ("--confirm", "--force", "--dry-run"):
        cmd.add_argument(flag, action="store_true")
    args = parser.parse_args(argv)

    if args.command == "merge-settings":
        return merge_settings(args.src, args.dst, args.backup)
    if args.command == "was-ours":
        return was_ours(args.claude_home, args.base, args.root, args.rel)
    if args.command == "tier":
        return tier(args.claude_home)
    if args.command == "units":
        return units(args.claude_home)
    if args.command == "units-dir":
        print(gen_scheduler().installed_units_dir(args.target).as_posix())
        return 0
    if args.command == "set-env-if-empty":
        return set_env_if_empty(args.path, args.key, args.value)
    if args.command == "yaml-get":
        return yaml_get(args.path, args.key)
    if args.command == "open-dry-run-window":
        return open_dry_run_window(args.path, args.days)
    if args.command == "write-manifest":
        return write_manifest(args)
    if args.command == "upgrade-notes":
        return upgrade_notes(args)
    if args.command == "diff":
        return diff(args)
    return uninstall(args)


if __name__ == "__main__":
    sys.exit(main())
