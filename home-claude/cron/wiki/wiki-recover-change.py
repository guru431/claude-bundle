#!/usr/bin/env python3
"""Preview/apply one quarantined wiki change locally, without an LLM or network."""
from __future__ import annotations

import argparse
import difflib
import hashlib
import importlib.util
import json
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "hooks"))
import utils  # noqa: E402
from wiki_recovery import checked_target, fingerprint, page_lock, source_fingerprint  # noqa: E402


def _compiler(kind):
    if kind not in ("sessions", "kb"):
        raise ValueError("invalid compiler kind")
    spec = importlib.util.spec_from_file_location(
        "wiki_recovery_compiler", Path(__file__).with_name(f"wiki-compile-{kind}.py"))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def recover(path: Path, *, apply: bool = False, target_rel: str | None = None) -> str:
    if path.is_symlink():
        raise ValueError("quarantine links cannot be replayed")
    path = path.resolve()
    try:
        path.relative_to(utils.REJECTED_DIR.resolve())
    except ValueError:
        raise ValueError("record is outside the quarantine directory") from None
    if path.suffix != ".txt" or path.stat().st_size > 2 * 1024 * 1024:
        raise ValueError("select one quarantine .txt record")
    raw = path.read_text(encoding="utf-8")
    record = json.loads(raw)
    if not isinstance(record, dict) or record.get("schema") != "wiki-change-v1":
        raise ValueError("legacy quarantine has no source/target fingerprints; inspect it manually")
    compiler = _compiler(record.get("kind"))
    kind, project = record["kind"], record.get("project")
    change, source_rel = record.get("change"), record.get("source")
    if (not isinstance(project, str) or not isinstance(change, dict)
            or not isinstance(change.get("path"), str) or not isinstance(source_rel, str)
            or type(record.get("blind_update")) is not bool):
        raise ValueError("invalid quarantine metadata")
    if utils.manifest_broken() or (kind == "sessions" and not utils.project_allowed(project)):
        raise ValueError("source project or manifest denied")
    if kind == "sessions":
        if not re.fullmatch(r"\d{4}-\d{2}-\d{2}\.md", source_rel):
            raise ValueError("invalid source daily")
        source_root = utils.DAILY_DIR
    else:
        if (not source_rel.startswith(("articles/", "plans/", "proposals/"))
                or "\\" in source_rel or ":" in source_rel
                or any(part in (".", "..") for part in source_rel.split("/"))):
            raise ValueError("invalid KB source")
        source_root = compiler.KBNEWS_DIR
    source = source_root / source_rel
    try:
        source.resolve().relative_to(source_root.resolve())
    except ValueError:
        raise ValueError("source escapes the configured source directory") from None
    if not record.get("source_sha256") or source_fingerprint(source) != record["source_sha256"]:
        raise ValueError("source changed or its fingerprint is unavailable")

    rel = target_rel or record.get("target_path")
    if not isinstance(rel, str):
        raise ValueError("select a canonical --target within the source namespace")
    target = checked_target(rel, project, kind)
    if not target_rel and record.get("target_verified") is not True:
        raise ValueError("original target is unsafe; select an allowed --target explicitly")
    original_target = record.get("target_verified") is True and rel == record.get("target_path")
    raw_sha = hashlib.sha256(raw.encode("utf-8")).hexdigest()
    receipt_path = path.with_suffix(".review.json")
    if receipt_path.is_symlink():
        raise ValueError("review receipt is a link")
    receipt = json.loads(receipt_path.read_text(encoding="utf-8")) if receipt_path.exists() else {}
    if not isinstance(receipt, dict):
        raise ValueError("invalid review receipt")
    target_sha = fingerprint(target)
    if (receipt.get("raw_sha256") == raw_sha and receipt.get("target_path") == rel
            and receipt.get("applying") and target_sha == receipt.get("after_sha256")):
        return f"Already applied: {rel}"
    if original_target and target_sha != record.get("target_sha256"):
        raise ValueError("target changed since quarantine; inspect/merge it manually")
    change = {**change, "path": rel, "_bodies_withheld": record["blind_update"]}
    previews = []
    callback = lambda name, text: previews.append((name, text))
    if kind == "sessions":
        _, rejected = compiler.apply_changes([change], source_rel, project,
                                             record["blind_update"], dry_run=True, on_preview=callback)
    else:
        _, rejected = compiler.apply_changes([change], {}, f"{source_root.name}/{source_rel}",
                                             dry_run=True, on_preview=callback)
    if rejected or len(previews) != 1 or previews[0][0] != rel:
        raise ValueError("compiler refused the selected change; inspect/repair its content and path")
    after = previews[0][1]
    review = {"raw_sha256": raw_sha, "source_sha256": record["source_sha256"],
              "target_path": rel, "before_sha256": target_sha,
              "after_sha256": hashlib.sha256(after.encode("utf-8")).hexdigest()}
    if apply:
        if receipt != review:
            raise ValueError("preview this exact record and unchanged target before --apply")
        # Recheck inputs after rendering. The lock also excludes the nightly compilers.
        if fingerprint(target) != target_sha or source_fingerprint(source) != record["source_sha256"]:
            raise ValueError("source or target changed during review")
        checked_target(rel, project, kind)
        utils.atomic_write_text(receipt_path, json.dumps({**review, "applying": True}))
        utils.atomic_write_text(target, after)
        return f"Applied: {rel}"
    before = target.read_text(encoding="utf-8", errors="replace") if target.exists() else ""
    diff = "\n".join(difflib.unified_diff(before.splitlines(), after.splitlines(),
                                        fromfile=rel, tofile=rel + " (proposed)", lineterm=""))
    utils.atomic_write_text(receipt_path, json.dumps(review))
    return utils.masked(f"PREVIEW: {rel}\n{diff}")


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("record", type=Path, help="one structured .txt in cron/logs/rejected")
    parser.add_argument("--target", help="explicit corrected canonical path in the same namespace")
    parser.add_argument("--apply", action="store_true", help="apply the exact previously previewed change")
    args = parser.parse_args(argv)
    try:
        with page_lock():
            print(recover(args.record, apply=args.apply, target_rel=args.target))
    except ValueError as exc:
        print(f"Recovery refused: {utils.masked(str(exc))}", file=sys.stderr)
        return 1
    except (OSError, TypeError, KeyError) as exc:
        print(f"Recovery refused: local {type(exc).__name__}; check the record, file access and compiler lock",
              file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    raise SystemExit(main())
