"""Read-only adapters for Claude and Codex transcripts. No LLM or state writes."""
from __future__ import annotations

import json
import ntpath
import posixpath
import re
from dataclasses import dataclass
from pathlib import Path


def validate_session_projects(raw, project_map: dict) -> dict[str, dict]:
    """Portable cwd ownership schema, shared by runtime and offline self-test."""
    result = {}
    if isinstance(raw, dict):
        for name, root in raw.items():
            if (not isinstance(name, str) or not re.fullmatch(r'[a-z0-9][a-z0-9_-]*', name)
                    or not isinstance(root, str) or not root.strip()):
                break
            path = Path(root).expanduser()
            if not path.is_absolute():
                break
            encoded = re.sub(r'[^A-Za-z0-9]', '-', str(path).rstrip('/\\'))
            folded = bool(ntpath.splitdrive(str(path))[0])
            aliases = {slug for key, slug in project_map.items()
                       if (key.lower() == encoded.lower() if folded else key == encoded)}
            if aliases and aliases != {name}:
                break
            if any(normalized_path(meta['path']) == normalized_path(str(path))
                   for meta in result.values()):
                break
            result[name] = {'path': str(path)}
        else:
            return result
    raise ValueError('session_projects must map unique project slugs to absolute paths, agreeing with project_map')


def normalized_path(value: str) -> str:
    # Windows paths are case-insensitive; POSIX paths are not.
    value = value.removeprefix('\\\\?\\')
    if ntpath.splitdrive(value)[0] or '\\' in value:
        return ntpath.normpath(value).replace('\\', '/').rstrip('/').lower()
    return posixpath.normpath(value).rstrip('/')


def project_for_cwd(cwd: str, projects: dict) -> str | None:
    """Longest registered root wins; unknown paths never become 'boss'."""
    current = normalized_path(cwd)
    matches = []
    for name, meta in projects.items():
        root = meta.get('path') if isinstance(meta, dict) else None
        if not isinstance(root, str) or not root:
            continue
        root = normalized_path(root)
        if current == root or current.startswith(root + '/'):
            matches.append((len(root), name))
    matches.sort(reverse=True)
    if len(matches) > 1 and matches[0][0] == matches[1][0]:
        return None  # two registered owners for the same root are ambiguous
    return matches[0][1] if matches else None


def attribution_for_cwd(cwd: str, projects: dict) -> tuple[str | None, str]:
    """Attribute an external linked worktree via reciprocal Git administrative files.

    No Git commands/config/hooks execute. A self-declared .git pointer alone is
    insufficient: the registered repository must also point back to this checkout.
    """
    name = project_for_cwd(cwd, projects)
    if name:
        return name, 'registered-root'
    def pointer(path: Path) -> str:
        with path.open('rb') as stream:
            raw = stream.read(4097)
        if len(raw) > 4096:
            raise ValueError('oversized Git administrative pointer')
        return raw.decode('utf-8').strip()

    try:
        current = Path(cwd).resolve()
        for worktree in (current, *current.parents):
            marker = worktree / '.git'
            if not marker.exists():
                continue
            if not marker.is_file() or marker.is_symlink():
                return None, 'unknown-repository'
            value = pointer(marker)
            if not value.startswith('gitdir: '):
                return None, 'invalid-worktree'
            admin = (worktree / value[8:]).resolve()
            common = (admin / pointer(admin / 'commondir')).resolve()
            back = Path(pointer(admin / 'gitdir')).resolve()
            if (back != marker.resolve() or admin.parent != common / 'worktrees'
                    or not admin.is_dir()):
                return None, 'invalid-worktree'
            owners = [key for key, meta in projects.items() if isinstance(meta, dict)
                      and isinstance(meta.get('path'), str)
                      and (Path(meta['path']) / '.git').resolve() == common]
            if len(owners) != 1:
                return None, 'ambiguous-or-unregistered-worktree'
            return owners[0], 'git-worktree'
    except (OSError, ValueError):
        return None, 'invalid-worktree'
    return None, 'unknown-cwd'


@dataclass(frozen=True)
class CodexSession:
    path: Path
    project: str
    session_id: str
    project_root: str
    size: int
    attribution: str = 'registered-root'


def codex_home(projects_dir: Path) -> Path:
    # ~/.claude/projects and ~/.codex are siblings under the same profile.
    return projects_dir.parent.parent / '.codex'


def _lines(path: Path, max_bytes: int | None = None):
    """A validated snapshot must not grow while an extractor reads it."""
    with path.open('rb') as stream:
        number = 0
        remaining = max_bytes
        while remaining is None or remaining > 0:
            raw = stream.readline() if remaining is None else stream.readline(remaining)
            if not raw:
                break
            number += 1
            if remaining is not None:
                remaining -= len(raw)
            yield number, raw.decode('utf-8', errors='replace')


def _codex_metadata(path: Path, projects: dict, size: int,
                    audit: list[dict] | None = None) -> CodexSession | None:
    def reject(reason):
        if audit is not None:
            audit.append({'path': str(path), 'project': None, 'session_id': None,
                          'allowed': False, 'messages': None, 'reason': reason})
        return None

    metadata = None
    contexts = set()
    for _, line in _lines(path, size):
        # Most lines are large tool outputs; only metadata needs decoding here.
        if '"session_meta"' not in line and '"turn_context"' not in line:
            continue
        try:
            event = json.loads(line)
        except ValueError:
            if line.endswith('\n'):
                return reject('invalid-metadata')
            continue  # an incomplete trailing line will be read next time
        if not isinstance(event, dict):
            continue
        payload = event.get('payload')
        if not isinstance(payload, dict):
            continue
        if event.get('type') == 'session_meta':
            if metadata is not None and metadata != payload:
                return reject('conflicting-metadata')
            metadata = payload
        elif event.get('type') == 'turn_context':
            if not isinstance(payload.get('cwd'), str):
                return reject('unknown-turn-cwd')
            contexts.add(payload['cwd'])
    if not metadata or not isinstance(metadata.get('cwd'), str):
        return reject('missing-metadata')
    source = metadata.get('source')
    if (isinstance(source, dict) and 'subagent' in source) or source == 'subagent':
        return reject('subagent')
    name, attribution = attribution_for_cwd(metadata['cwd'], projects)
    sid = metadata.get('id')
    if not name or not isinstance(sid, str) or not re.fullmatch(r'[A-Za-z0-9_-]{1,128}', sid):
        return reject(attribution if not name else 'invalid-session-id')
    # A resumed thread may move into a private/different project. Never attribute
    # the whole transcript to its original, possibly internal, project.
    if any(attribution_for_cwd(cwd, projects)[0] != name for cwd in contexts):
        return reject('cwd-changed-or-unknown')
    return CodexSession(path, name, 'codex-' + sid, projects[name]['path'], size, attribution)


def find_codex_sessions(projects_dir: Path, projects: dict,
                        since: float | None = None, *, audit: list[dict] | None = None) -> list[CodexSession]:
    if not projects:
        return []  # no configured roots means no Codex collection
    home = codex_home(projects_dir)
    roots = [home / 'sessions', home / 'archived_sessions']
    if not any(root.is_dir() for root in roots):
        return []
    found = {}
    for root in roots:
        for path in sorted(root.rglob('*.jsonl')):
            try:
                stat = path.stat()
                mtime = stat.st_mtime
                if since is not None and mtime < since:
                    continue
                session = _codex_metadata(path, projects, stat.st_size, audit)
            except (OSError, UnicodeError):
                continue
            if session and (session.session_id not in found or mtime > found[session.session_id][0]):
                found[session.session_id] = (mtime, session)
    return [session for _, session in sorted(found.values(), key=lambda item: (item[0], str(item[1].path)))]


def _text(content, block_types: set[str]) -> str:
    if isinstance(content, str):
        return content
    if not isinstance(content, list):
        return ''
    return '\n'.join(block if isinstance(block, str) else str(block.get('text', ''))
                     for block in content
                     if isinstance(block, str) or (isinstance(block, dict) and block.get('type') in block_types
                                                   and isinstance(block.get('text'), str)))


def message_from_line(line: str) -> dict | None:
    """Shared Claude/Codex message filter for whole-file and delta readers."""
    try:
        event = json.loads(line)
    except ValueError:
        return None
    if not isinstance(event, dict):
        return None
    kind = event.get('type')
    if kind in ('user', 'assistant'):
        msg = event.get('message', event)
        if not isinstance(msg, dict):
            return None
        role = msg.get('role', kind)
        text = _text(msg.get('content'), {'text'})
    elif kind == 'response_item':
        msg = event.get('payload')
        if not isinstance(msg, dict) or msg.get('type') != 'message':
            return None
        role = msg.get('role')
        if msg.get('channel') == 'analysis':
            return None
        text = _text(msg.get('content'), {'input_text', 'output_text'})
        if role == 'user':
            if text.lstrip().startswith(('# AGENTS.md instructions for ', '<permissions instructions>')):
                return None
            text = re.sub(r'<environment_context>[\s\S]*?</environment_context>', '', text)
    else:
        return None
    text = text.strip()
    if role not in ('user', 'assistant') or not text:
        return None
    stamp = event.get('timestamp')
    return {'role': role, 'text': text, 'ts': stamp if isinstance(stamp, str) else ''}


def iter_messages(path: Path, max_bytes: int | None = None):
    """Real conversation text with source line numbers, without event duplicates."""
    try:
        for number, line in _lines(path, max_bytes):
            msg = message_from_line(line)
            if msg:
                yield {**msg, 'line': number, 'timestamp': msg['ts']}
    except (OSError, UnicodeError):
        return
