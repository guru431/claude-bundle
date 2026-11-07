#!/usr/bin/env python3
"""Local search of inherited knowledge. No LLM, network, or state writes.

python cron/knowledge-search.py --project myapp "parser decision"
python cron/knowledge-search.py --project myapp --history "retry ceiling"
python cron/knowledge-search.py --inventory --json
"""
from __future__ import annotations

import argparse
import importlib.util
import json
import re
import sys
from datetime import datetime, timezone
from pathlib import Path

# bundle-io: offbox=nothing money=no writes=nothing

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'cron' / 'hooks'))
sys.path.insert(0, str(ROOT / 'cron' / 'lib'))
from utils import (PROJECTS_BASE, WIKI_ROOT, SKIP_DIRS, codex_sessions, codex_session_allowed,  # noqa: E402
                   dir_to_project, is_subagent_jsonl, masked, normalize_project_name,
                   project_allowed, session_projects)
from session_sources import iter_messages, attribution_for_cwd  # noqa: E402

PROJECTS_DIR = PROJECTS_BASE
GLOBAL_MEMORY = PROJECTS_DIR.parent / 'memory'


def project_key(value: str) -> str:
    return re.sub(r'[-_\s]+', '', value.lower())


def resolve_project(value: str) -> str:
    if not re.fullmatch(r'[A-Za-z0-9_-]+', value):
        raise ValueError('project must be a name, not a path')
    project = normalize_project_name(value)
    session_projects()  # fail closed before reading any reference text
    if not project_allowed(value):
        raise ValueError('project denied by bundle.local.yaml')
    return project


def memory_directories(project: str) -> list[Path]:
    return [folder / 'memory' for folder in sorted(PROJECTS_DIR.glob('*'))
            if folder.name not in SKIP_DIRS
            and project_key(dir_to_project(folder.name)) == project_key(project)
            and (folder / 'memory').is_dir()]


def document_sources(project: str):
    spec = importlib.util.spec_from_file_location('knowledge_wiki_grep', ROOT / 'cron/wiki/wiki-grep.py')
    wiki = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(wiki)
    wiki.WIKI_ROOT = WIKI_ROOT
    for path, _ in wiki.iter_pages(project):
        yield path, 'wiki'
    for folder in memory_directories(project):
        for path in sorted(folder.glob('*.md')):
            yield path, 'claude-project-memory'
    for path in sorted(GLOBAL_MEMORY.glob('*.md')):
        yield path, 'claude-global-memory'


def reference_sources(project: str) -> list[dict]:
    """Small navigation map; do not dump thousands of wiki pages at session start."""
    project = resolve_project(project)
    result = []
    for path in [WIKI_ROOT / 'index.md', WIKI_ROOT / 'projects' / project / '_log.md']:
        if path.is_file():
            result.append({'path': str(path), 'source': 'wiki-index'})
    for folder in memory_directories(project):
        result.append({'path': str(folder), 'source': 'claude-project-memory-directory',
                       'files': sum(1 for _ in folder.glob('*.md'))})
        for name in ['MEMORY.md', 'incidents.md', 'sessions.md']:
            path = folder / name
            if path.is_file():
                result.append({'path': str(path), 'source': 'claude-project-memory'})
    for name in ['MEMORY.md', 'USER.md', 'cross-project-notes.md', 'incidents.md']:
        path = GLOBAL_MEMORY / name
        if path.is_file():
            result.append({'path': str(path), 'source': 'claude-global-memory'})
    return result


def _sections(text: str):
    start, lines = 1, []
    for number, line in enumerate(text.splitlines(), 1):
        if line.startswith(('## ', '### ')) and lines:
            yield start, lines
            start, lines = number, []
        lines.append(line)
    if lines:
        yield start, lines


def document_results(project: str, terms: list[str]):
    for path, source in document_sources(project):
        try:
            text = path.read_text(encoding='utf-8-sig', errors='replace')
            updated = datetime.fromtimestamp(path.stat().st_mtime, timezone.utc).isoformat()
        except OSError:
            continue
        for start, lines in _sections(text):
            section = '\n'.join(lines)
            if not all(term in section.lower() for term in terms):
                continue
            matching = [(i, line) for i, line in enumerate(lines)
                        if any(term in line.lower() for term in terms)]
            # Mask whole lines before clipping; a clipped key is harder to recognize.
            excerpt = '\n'.join(masked(line)[:240] for _, line in matching[:3])
            title = lines[0] if lines and lines[0].startswith('#') else path.stem
            score = sum(40 if term in title.lower() else 1 for term in terms)
            yield {'path': str(path), 'line': start + matching[0][0], 'source': source,
                   'updated': updated, 'score': score, 'excerpt': excerpt}


def history_sources(project: str):
    for folder in sorted(PROJECTS_DIR.glob('*')):
        if folder.name in SKIP_DIRS or project_key(dir_to_project(folder.name)) != project_key(project):
            continue
        for path in sorted(folder.glob('*.jsonl')):
            if not is_subagent_jsonl(str(path)):
                try:
                    yield path, 'claude-transcript', path.stem, path.stat().st_size
                except OSError:
                    continue
    for session in codex_sessions(PROJECTS_DIR):
        if codex_session_allowed(session) and project_key(session.project) == project_key(project):
            yield session.path, 'codex-transcript', session.session_id, session.size


def search(project: str, query: str, limit: int = 10, history: bool = False) -> list[dict]:
    project = resolve_project(project)
    terms = query.lower().split()
    if not terms:
        return []
    results = list(document_results(project, terms))
    if history:
        for path, source, sid, size in history_sources(project):
            for msg in iter_messages(path, max_bytes=size):
                if all(term in msg['text'].lower() for term in terms):
                    safe = masked(msg['text'])
                    first = min(safe.lower().find(term) for term in terms if term in safe.lower()) if any(
                        term in safe.lower() for term in terms) else 0
                    start = max(0, first - 100)
                    results.append({'path': str(path), 'line': msg['line'], 'source': source,
                                    'session_id': sid, 'updated': msg['timestamp'], 'score': 1,
                                    'excerpt': f"[{msg['role']}] " + safe[start:start + 600]})
    results.sort(key=lambda hit: (-hit['score'], hit['path'], hit['line']))
    return results[:max(1, min(limit, 50))]


def inventory() -> list[dict]:
    """Dry run for ingestion: only counts/attribution, never conversation text."""
    result = []
    for session in codex_sessions(PROJECTS_DIR, audit=result):
        allowed = codex_session_allowed(session)
        result.append({'project': session.project, 'session_id': session.session_id,
                       'path': str(session.path), 'allowed': allowed,
                       'attribution': session.attribution,
                       'reason': None if allowed else 'privacy-or-skip',
                       'messages': sum(1 for _ in iter_messages(session.path, max_bytes=session.size)) if allowed else None})
    return result


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument('query', nargs='*')
    ap.add_argument('--project')
    ap.add_argument('--history', action='store_true', help='also search raw Claude/Codex conversations')
    ap.add_argument('--sources', action='store_true', help='list relevant reference files without reading text')
    ap.add_argument('--inventory', action='store_true', help='dry-run Codex ingestion without LLM or writes')
    ap.add_argument('--limit', type=int, default=10)
    ap.add_argument('--json', action='store_true')
    args = ap.parse_args(argv)
    try:
        if args.inventory:
            output = inventory()
        else:
            name = args.project
            if not name:
                name, _attribution = attribution_for_cwd(str(Path.cwd()), session_projects())
            if not name:
                ap.error('specify --project outside a registered project')
            project = resolve_project(name)
            if args.sources:
                output = reference_sources(project)
            else:
                if not args.query:
                    ap.error('provide search terms, --sources, or --inventory')
                output = search(project, ' '.join(args.query), args.limit, args.history)
    except (OSError, ValueError, RuntimeError, KeyError) as exc:
        print(f'knowledge-search: {exc}', file=sys.stderr)
        return 2
    if args.json:
        print(json.dumps(output, ensure_ascii=False, indent=2))
    else:
        for item in output:
            print(f"{item['path']}" + (f":{item['line']}" if 'line' in item else '') + f" [{item.get('source', 'codex-inventory')}]")
            if 'excerpt' in item:
                print(item['excerpt'])
            if args.inventory:
                print(f"  project={item['project']} allowed={item['allowed']} messages={item['messages']}")
        if not output:
            print('No matches. Use --history to include raw conversations, or --sources to inspect available notes.')
    return 0


if __name__ == '__main__':
    if hasattr(sys.stdout, 'reconfigure'):
        sys.stdout.reconfigure(encoding='utf-8')
    raise SystemExit(main())
