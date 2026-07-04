"""End-to-end wiki-pipeline smoke test with the offline `mock` LLM provider.

The bundle's core value — turning a daily log into atomic project pages, then
rebuilding the index and linting the vault — was never exercised by CI, yet
every past review found real bugs in exactly this pipeline. This test drives
compile-sessions → build-index → lint against a fixture, using
`WIKI_LLM_PROVIDER=mock` (utils._llm_mock) so it needs no network and no key.

It copies cron/ into a tmpdir so every path the scripts derive from __file__
(BUNDLE_ROOT, WIKI_ROOT, state, logs) lands in the tmpdir — the repo's own
wiki/ is never touched.

Run: pytest tests/ -q
"""
from __future__ import annotations

import json
import re
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
CRON_SRC = ROOT / "home-claude" / "cron"


def _run(script: Path, env_extra: dict, cwd: Path) -> subprocess.CompletedProcess:
    import os
    env = os.environ.copy()
    # Neutralise any real provider config from the developer's shell.
    for k in ("DEEPSEEK_KEY", "OPENCODE_GO_API_KEY", "OPENCODE_GO_KEY"):
        env.pop(k, None)
    env.update(env_extra)
    env["WIKI_LLM_PROVIDER"] = "mock"
    return subprocess.run(
        [sys.executable, str(script)],
        cwd=str(cwd), env=env,
        capture_output=True, text=True, encoding="utf-8", errors="replace",
        timeout=120,
    )


@pytest.fixture()
def bundle(tmp_path: Path) -> Path:
    """A throwaway bundle tree: cron/ copied in, an empty wiki/ with one daily."""
    shutil.copytree(CRON_SRC, tmp_path / "cron")
    wiki = tmp_path / "wiki"
    (wiki / "daily").mkdir(parents=True)
    # Replicate the shipped vault skeleton the index builder expects to exist.
    for sub in ("kb/concepts", "kb/tools", "kb/people", "projects"):
        (wiki / sub).mkdir(parents=True, exist_ok=True)
    # Minimal main index so build-index's stats-table update has a target.
    (wiki / "index.md").write_text(
        "# Wiki\n\n## Stats\n\n| Section | Pages | Updated |\n"
        "|---------|-------|---------|\n| projects/ | 0 | - |\n",
        encoding="utf-8",
    )
    # One daily log with a single, cleanly-named project section.
    (wiki / "daily" / "2026-01-01.md").write_text(
        "# Daily 2026-01-01\n\n## myproject\n"
        "Investigated the widget parser dropping trailing tokens; the boundary\n"
        "check was off by one. Fixed and verified against the sample corpus.\n",
        encoding="utf-8",
    )
    return tmp_path


def test_compile_build_lint(bundle: Path):
    wiki = bundle / "wiki"

    # Canned compile-sessions response: one page under projects/myproject/.
    page_body = (
        "# Widget parser fix\n\n"
        "The parser dropped trailing tokens because the boundary check was "
        "off by one. Adjusting it fixed the loss. Verified against the sample "
        "corpus. Back to [[index]].\n"
    )
    resp = bundle / "compile_response.json"
    resp.write_text(json.dumps(
        [{"path": "projects/myproject/widget-parser-fix.md",
          "action": "create", "content": page_body}]), encoding="utf-8")

    # 1) compile-sessions: daily → project page (right slug, not "main").
    r = _run(bundle / "cron" / "wiki" / "wiki-compile-sessions.py",
             {"WIKI_LLM_MOCK_RESPONSE": str(resp)}, cwd=bundle)
    assert r.returncode == 0, f"compile-sessions failed:\n{r.stdout}\n{r.stderr}"

    proj_pages = list((wiki / "projects" / "myproject").glob("*.md"))
    proj_pages = [p for p in proj_pages if p.name != "_log.md"]
    assert proj_pages, f"no page created under projects/myproject; stdout:\n{r.stdout}"
    assert not (wiki / "projects" / "main").exists(), \
        "section collapsed into projects/main instead of its own slug"

    # 2) build-index: projects/index.md regenerated and names the project.
    r = _run(bundle / "cron" / "wiki" / "wiki-build-index.py", {}, cwd=bundle)
    assert r.returncode == 0, f"build-index failed:\n{r.stdout}\n{r.stderr}"
    idx = wiki / "projects" / "index.md"
    assert idx.is_file(), "projects/index.md not written"
    assert "myproject" in idx.read_text(encoding="utf-8")

    # 3) lint: no ERRORs (broken links / ambiguous names / index desync).
    r = _run(bundle / "cron" / "wiki" / "wiki-lint.py", {}, cwd=bundle)
    assert r.returncode == 0, f"lint crashed:\n{r.stdout}\n{r.stderr}"
    m = re.search(r"(\d+)\s+errors", r.stdout)
    assert m, f"lint printed no stats line:\n{r.stdout}"
    assert int(m.group(1)) == 0, f"lint reported errors:\n{r.stdout}"
