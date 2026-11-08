"""Portable quarantine records and an exclusive lock shared with wiki recovery."""
from __future__ import annotations

import contextlib
import hashlib
import json
from pathlib import Path

import utils


def fingerprint(path: Path) -> str | None:
    return hashlib.sha256(path.read_bytes()).hexdigest() if path.exists() else None


def source_fingerprint(path: Path) -> str | None:
    """Match the UTF-8 text snapshot read by the compilers, including CRLF inputs."""
    if not path.exists():
        return None
    text = path.read_text(encoding="utf-8", errors="replace")
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


@contextlib.contextmanager
def page_lock():
    with utils._file_lock(utils.BUNDLE_ROOT / "cron/state/.wiki-pages.lock", wait=0,
                          stale=600, fail_open=False, label="wiki pages", mode="os") as held:
        if not held:
            raise OSError("wiki compiler/recovery is busy; retry after it finishes")
        yield


def checked_target(rel: str, project: str, kind: str) -> Path:
    """Require an exact canonical namespace, including the resolved destination."""
    if utils.manifest_broken() or not rel or utils.normalize_wiki_path(rel) != rel:
        raise ValueError("invalid target or manifest")
    parts = Path(rel).parts
    if len(parts) != 3 or any(part in (".", "..") or ":" in part for part in parts):
        raise ValueError("invalid target")
    if kind == "sessions":
        if not project or not utils.project_allowed(project) or parts[:2] != ("projects", project):
            raise ValueError("source/target project denied")
    elif kind != "kb" or parts[0] != "kb":
        raise ValueError("invalid target namespace")
    path = utils.WIKI_ROOT / rel
    try:
        resolved = path.resolve().relative_to(utils.WIKI_ROOT.resolve())
    except ValueError:
        raise ValueError("target escapes the wiki vault") from None
    # Directory links may not change the compiler's namespace or target project.
    if resolved.parts[:2] != parts[:2]:
        raise ValueError("target redirects to another namespace")
    return path


def quarantine_change(change, reason: str, *, kind: str, source: Path,
                      source_rel: str, project: str = "", target_rel: str = "",
                      blind: bool = False, source_sha256: str | None = None) -> None:
    """Keep a masked record; old unstructured records remain inspection-only."""
    target_verified, target_sha = False, None
    try:
        target = checked_target(target_rel, project, kind)
        target_sha = fingerprint(target)
        target_verified = True
    except (OSError, ValueError):
        pass
    try:
        source_sha = source_sha256 or source_fingerprint(source)
    except OSError:
        source_sha = None
    record = {"schema": "wiki-change-v1", "kind": kind, "reason": reason,
              "project": project, "source": source_rel, "source_sha256": source_sha,
              "target_path": target_rel, "target_verified": target_verified,
              "target_sha256": target_sha, "blind_update": blind, "change": change}
    source_id = f"{Path(source_rel).stem}#{project}" if kind == "sessions" else source_rel
    utils.quarantine_raw(source_id, reason, json.dumps(record, ensure_ascii=False))
