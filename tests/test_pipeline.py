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
import types
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


def test_compile_rejects_path_escape(bundle: Path):
    """A page path that escapes projects/ ('projects/../CLAUDE.md') must be
    rejected: no file written outside the tree, the payload quarantined, and the
    run flagged as a hard failure (wave-1 contract: exit != 0)."""
    wiki = bundle / "wiki"
    resp = bundle / "escape_response.json"
    resp.write_text(json.dumps(
        [{"path": "projects/../CLAUDE.md", "action": "create",
          "content": "# Escaped\n\nThis must never be written outside projects/.\n"}]),
        encoding="utf-8")

    r = _run(bundle / "cron" / "wiki" / "wiki-compile-sessions.py",
             {"WIKI_LLM_MOCK_RESPONSE": str(resp)}, cwd=bundle)
    assert r.returncode != 0, \
        f"expected non-zero exit on rejected payload:\n{r.stdout}\n{r.stderr}"
    # No page escaped the projects/ tree.
    assert not (wiki / "CLAUDE.md").exists(), "traversal wrote a page under wiki/"
    assert not (bundle / "CLAUDE.md").exists(), "traversal wrote a page in the bundle"
    # The dropped payload was quarantined for later inspection.
    rejected = list((bundle / "cron" / "logs" / "rejected").glob("*"))
    assert rejected, f"no quarantine file under cron/logs/rejected/:\n{r.stdout}"


def test_a_source_that_always_fails_is_quarantined_once(bundle: Path):
    """The retry ceiling, end to end: three runs, then it stops — once.

    The pieces were each unit-tested (the counter bumps, a finding dedupes) and
    the BEHAVIOUR they exist for was not: a source the provider deterministically
    refuses must stop being re-sent after WIKI_RETRY_LIMIT nights, leave its
    payload where a human can read it, and file exactly ONE finding rather than
    one per night. Getting this wrong is expensive in the direction that does not
    announce itself — every night, forever, for the same rejected payload.
    """
    # Deterministically unusable: a prose refusal, not JSON. Retrying reproduces
    # it exactly, which is what makes it count against the ceiling (a provider
    # outage is `transient` and deliberately does not).
    resp = bundle / "unusable_response.txt"
    resp.write_text("I'm sorry, I can't help with that.\n", encoding="utf-8")
    script = bundle / "cron" / "wiki" / "wiki-compile-sessions.py"

    for _ in range(3):
        r = _run(script, {"WIKI_LLM_MOCK_RESPONSE": str(resp)}, cwd=bundle)
    assert "QUARANTINED after 3" in r.stdout, \
        f"the ceiling did not stop the source on the third run:\n{r.stdout}"

    rejected = list((bundle / "cron" / "logs" / "rejected").glob("*"))
    assert rejected, f"the payload was not kept for inspection:\n{r.stdout}"

    findings = bundle / "FINDINGS.md"
    assert findings.is_file(), f"no finding was filed:\n{r.stdout}"
    body = findings.read_text(encoding="utf-8")
    entries = [ln for ln in body.splitlines() if ln.startswith("## ")]
    assert len(entries) == 1, f"expected exactly one finding, got {entries}"

    # A fourth run must not re-send the source: the ceiling is reached, so the
    # night is a no-op rather than another call and another finding.
    r4 = _run(script, {"WIKI_LLM_MOCK_RESPONSE": str(resp)}, cwd=bundle)
    after = findings.read_text(encoding="utf-8")
    assert after.count("\n## ") == body.count("\n## "), \
        f"a second finding was filed for an already-quarantined source:\n{r4.stdout}"


# A date safely in the past on any machine, so nothing here depends on the
# clock (the bundle's own test policy, rule 3): flush clamps a session date to
# today, and a fixture dated "tomorrow" would be green some days and red others.
SESSION_DAY = "2020-05-04"


def _seed_session_jsonl(path: Path, count: int, day: str, marker: str = "m",
                        append: bool = False) -> None:
    """A session transcript whose messages carry `day` as their timestamp."""
    filler = "x" * 600
    lines = []
    for i in range(count):
        for role in ("user", "assistant"):
            lines.append(json.dumps({
                "type": role, "timestamp": f"{day}T21:10:0{i % 10}.000Z",
                "message": {"role": role,
                            "content": f"{marker} {role} {i} {filler}"}}))
    body = "\n".join(lines) + "\n"
    with open(path, "a" if append else "w", encoding="utf-8") as f:
        f.write(body)


def test_flush_dates_the_daily_by_session_not_by_run(bundle: Path, tmp_path: Path):
    """A session belongs in the daily of the day it HAPPENED.

    Everything used to be written to the daily of the day the run happened, so
    the 02:30 flush filed last night's evening session under this morning's
    date, compile stamped its incidents "the morning after", and a backlog sweep
    piled a month of sessions into one daily log.
    """
    home = tmp_path / "home_dated"
    proj_dir = home / ".claude" / "projects" / "C--Users-test-projects-dated"
    proj_dir.mkdir(parents=True)
    _seed_session_jsonl(proj_dir / "s.jsonl", 12, SESSION_DAY)

    resp = bundle / "dated_response.md"
    resp.write_text("- A durable fact. [[index]]\n", encoding="utf-8")
    r = _run(bundle / "cron" / "wiki" / "wiki-flush-sessions.py",
             {"WIKI_LLM_MOCK_RESPONSE": str(resp),
              "USERPROFILE": str(home), "HOME": str(home)}, cwd=bundle)
    assert r.returncode == 0, f"flush failed:\n{r.stdout}\n{r.stderr}"

    daily = bundle / "wiki" / "daily" / f"{SESSION_DAY}.md"
    assert daily.is_file(), f"no daily for the session's own day:\n{r.stdout}"
    assert "## dated" in daily.read_text(encoding="utf-8")
    stray = [p.name for p in (bundle / "wiki" / "daily").glob("????-??-??.md")
             if p.name != f"{SESSION_DAY}.md"
             and "## dated" in p.read_text(encoding="utf-8")]
    assert not stray, f"the session was also filed under the run date: {stray}"


def test_flush_resends_only_the_tail_of_a_grown_session(bundle: Path, tmp_path: Path):
    """A session that grew must cost only its DELTA.

    The state key used to be the file's SIZE, which made a still-open session
    look new: the whole transcript was re-read and re-sent every night — the
    same conversation billed twice and appended to a second daily. The key is
    now the offset the read stopped at.
    """
    home = tmp_path / "home_delta"
    proj_dir = home / ".claude" / "projects" / "C--Users-test-projects-grown"
    proj_dir.mkdir(parents=True)
    jf = proj_dir / "s.jsonl"
    _seed_session_jsonl(jf, 12, SESSION_DAY)

    resp = bundle / "delta_response.md"
    resp.write_text("- A durable fact. [[index]]\n", encoding="utf-8")
    env = {"WIKI_LLM_MOCK_RESPONSE": str(resp),
           "USERPROFILE": str(home), "HOME": str(home)}
    flush = bundle / "cron" / "wiki" / "wiki-flush-sessions.py"

    r = _run(flush, env, cwd=bundle)
    assert r.returncode == 0, f"flush failed:\n{r.stdout}\n{r.stderr}"
    state = json.loads((bundle / "wiki" / ".processed.json").read_text(encoding="utf-8"))
    processed = state.get("flush", {}).get("processed_jsonls", [])
    assert f"grown/s.jsonl@{jf.stat().st_size}" in processed, \
        f"the read offset was not recorded: {processed}"

    # The session continues: two more exchanges, ~2.5 KB, on the next day.
    _seed_session_jsonl(jf, 2, "2020-05-05", marker="tail", append=True)

    import os
    env2 = os.environ.copy()
    env2.update(env)
    env2["WIKI_LLM_PROVIDER"] = "mock"
    r2 = subprocess.run(
        [sys.executable, str(flush), "--dry-run"], cwd=str(bundle), env=env2,
        capture_output=True, text=True, encoding="utf-8", errors="replace",
        timeout=120)
    assert r2.returncode == 0, f"flush --dry-run failed:\n{r2.stdout}\n{r2.stderr}"
    sizes = [int(m) for m in re.findall(r"LLM call\(s\), (\d+) chars", r2.stdout)]
    assert sizes, f"dry run reported no payload for the grown session:\n{r2.stdout}"
    assert max(sizes) < jf.stat().st_size // 2, (
        f"the whole transcript was re-collected ({max(sizes)} chars of a "
        f"{jf.stat().st_size}-byte file):\n{r2.stdout}")


def test_compile_honours_skip_projects(bundle: Path):
    """The privacy policy is unified across the pipeline — compile included.

    flush gates every SOURCE, but a project added to skip_projects AFTER its
    daily was written still had that section sent to the provider, and a
    wiki/projects/<it>/ folder created for it. A daily already on disk is not
    consent.
    """
    pytest.importorskip("yaml")
    (bundle / "bundle.local.yaml").write_text(
        "skip_projects:\n  - myproject\n", encoding="utf-8")
    resp = bundle / "denied_response.json"
    resp.write_text(json.dumps(
        [{"path": "projects/myproject/leak.md", "action": "create",
          "content": "# Leak\n\nThis must never be written. [[index]]\n"}]),
        encoding="utf-8")

    r = _run(bundle / "cron" / "wiki" / "wiki-compile-sessions.py",
             {"WIKI_LLM_MOCK_RESPONSE": str(resp)}, cwd=bundle)
    assert r.returncode == 0, f"compile-sessions failed:\n{r.stdout}\n{r.stderr}"
    assert "denied by policy" in r.stdout, \
        f"the denied section was not reported:\n{r.stdout}"
    assert not (bundle / "wiki" / "projects" / "myproject").exists(), \
        "a denied project got a wiki namespace"


def test_flush_dedup(bundle: Path, tmp_path: Path):
    """flush turns a JSONL session into a daily log and records it processed;
    a second run must NOT reprocess it (dedup via .processed.json)."""
    wiki = bundle / "wiki"
    # Sandbox HOME/USERPROFILE so utils.PROJECTS_BASE (Path.home()/.claude/
    # projects) resolves into tmp, where we seed one session fixture.
    home = tmp_path / "home"
    proj_dir = home / ".claude" / "projects" / "C--Users-test-projects-myproj"
    proj_dir.mkdir(parents=True)

    # >3 user messages and >10 KB (flush's size floor); not a subagent session.
    filler = "x" * 600
    lines = []
    for i in range(12):
        lines.append(json.dumps({"type": "user",
            "message": {"role": "user", "content": f"user message {i} {filler}"}}))
        lines.append(json.dumps({"type": "assistant",
            "message": {"role": "assistant", "content": f"assistant reply {i} {filler}"}}))
    (proj_dir / "sess1.jsonl").write_text("\n".join(lines) + "\n", encoding="utf-8")

    # flush appends the LLM text verbatim under a `## project` heading.
    resp = bundle / "flush_response.md"
    resp.write_text("- A durable fact about the widget parser. [[index]]\n",
                    encoding="utf-8")

    env = {"WIKI_LLM_MOCK_RESPONSE": str(resp),
           "USERPROFILE": str(home), "HOME": str(home)}
    flush = bundle / "cron" / "wiki" / "wiki-flush-sessions.py"

    r = _run(flush, env, cwd=bundle)
    assert r.returncode == 0, f"flush failed:\n{r.stdout}\n{r.stderr}"
    dailies = list((wiki / "daily").glob("????-??-??.md"))
    assert dailies, f"no daily log produced:\n{r.stdout}"
    state = json.loads((wiki / ".processed.json").read_text(encoding="utf-8"))
    processed = state.get("flush", {}).get("processed_jsonls", [])
    assert any("sess1.jsonl" in k for k in processed), \
        f"session not recorded processed: {processed}"

    before = {p.name: p.read_text(encoding="utf-8")
              for p in (wiki / "daily").glob("????-??-??.md")}

    # Second run: the session is already processed → nothing to reprocess.
    r2 = _run(flush, env, cwd=bundle)
    assert r2.returncode == 0, f"flush rerun failed:\n{r2.stdout}\n{r2.stderr}"
    assert "Nothing to process" in r2.stdout, \
        f"expected dedup skip on rerun:\n{r2.stdout}"
    after = {p.name: p.read_text(encoding="utf-8")
             for p in (wiki / "daily").glob("????-??-??.md")}
    assert after == before, "rerun changed the daily logs (duplicate processing)"


def test_flush_respects_allowlist(bundle: Path, tmp_path: Path):
    """allow_projects in bundle.local.yaml makes flush read ONLY the listed
    projects — an off-list project's sessions are never collected/processed
    (unified privacy policy)."""
    wiki = bundle / "wiki"
    # Manifest lives at BUNDLE_ROOT (= the copied bundle tree); utils loads it
    # at import, and each _run() is a fresh process, so it takes effect.
    (bundle / "bundle.local.yaml").write_text(
        "allow_projects:\n  - keepme\n", encoding="utf-8")

    home = tmp_path / "home_allow"
    projects = home / ".claude" / "projects"
    filler = "x" * 600

    def seed(dirname: str):
        d = projects / dirname
        d.mkdir(parents=True)
        lines = []
        for i in range(12):
            lines.append(json.dumps({"type": "user",
                "message": {"role": "user", "content": f"msg {i} {filler}"}}))
            lines.append(json.dumps({"type": "assistant",
                "message": {"role": "assistant", "content": f"reply {i} {filler}"}}))
        (d / "s.jsonl").write_text("\n".join(lines) + "\n", encoding="utf-8")

    seed("C--Users-test-projects-keepme")   # on the allowlist
    seed("C--Users-test-projects-secret")   # off the allowlist → must be skipped

    resp = bundle / "allow_response.md"
    resp.write_text("- A durable fact. [[index]]\n", encoding="utf-8")
    env = {"WIKI_LLM_MOCK_RESPONSE": str(resp),
           "USERPROFILE": str(home), "HOME": str(home)}

    r = _run(bundle / "cron" / "wiki" / "wiki-flush-sessions.py", env, cwd=bundle)
    assert r.returncode == 0, f"flush failed:\n{r.stdout}\n{r.stderr}"
    state = json.loads((wiki / ".processed.json").read_text(encoding="utf-8"))
    processed = state.get("flush", {}).get("processed_jsonls", [])
    assert any(k.startswith("keepme/") for k in processed), \
        f"allowed project not processed: {processed}"
    assert not any(k.startswith("secret/") for k in processed), \
        f"off-list project leaked past the allowlist: {processed}"


def test_wiki_pipeline_runs_phases_in_order(bundle: Path, tmp_path: Path):
    """wiki-pipeline.py runs flush -> compile -> index in one process, in that
    order, and exits 0 (F6 orchestrator)."""
    home = tmp_path / "home_pipe"
    (home / ".claude" / "projects").mkdir(parents=True)  # empty → flush no-ops
    resp = bundle / "pipe_compile.json"
    resp.write_text(json.dumps(
        [{"path": "projects/myproject/note.md", "action": "create",
          "content": "# Note\n\nA durable fact. Back to [[index]].\n"}]),
        encoding="utf-8")
    r = _run(bundle / "cron" / "wiki" / "wiki-pipeline.py",
             {"WIKI_LLM_MOCK_RESPONSE": str(resp),
              "USERPROFILE": str(home), "HOME": str(home)}, cwd=bundle)
    assert r.returncode == 0, f"pipeline failed:\n{r.stdout}\n{r.stderr}"
    order = [r.stdout.find(f"[{p}]") for p in ("flush", "compile", "index")]
    assert all(i != -1 for i in order), f"a phase did not run:\n{r.stdout}"
    assert order == sorted(order), f"phases ran out of order:\n{r.stdout}"
    assert "all phases OK" in r.stdout


def test_gen_scheduler_skips_windows_only_task(tmp_path: Path):
    """gen-scheduler must not emit a POSIX unit for a `platform: windows` task
    (ClaudeTaskMonitor) — guards against a Windows-only task leaking into
    systemd/launchd units."""
    yaml = pytest.importorskip("yaml")  # gen-scheduler requires PyYAML

    # Read the registry FIRST and assert the fixture this test needs actually
    # exists. Without it the test was vacuous: delete every `platform: windows`
    # task and it still passes, green and checking nothing.
    reg = yaml.safe_load((ROOT / "home-claude" / "cron" / "registry.yaml")
                         .read_text(encoding="utf-8"))
    windows_only = [t["name"] for t in reg["tasks"]
                    if str(t.get("platform", "")).lower() == "windows"]
    assert windows_only, ("registry.yaml has no `platform: windows` task, so this "
                          "test can no longer prove anything — point it at one")

    out_dir = tmp_path / "units"
    r = subprocess.run(
        [sys.executable, str(ROOT / "scripts" / "gen-scheduler.py"),
         "--target", "both", "--out-dir", str(out_dir)],
        capture_output=True, text=True, encoding="utf-8", errors="replace",
        timeout=60,
    )
    assert r.returncode == 0, f"gen-scheduler failed:\n{r.stdout}\n{r.stderr}"
    emitted = [p.name for p in out_dir.rglob("*") if p.is_file()]
    assert emitted, f"gen-scheduler wrote no unit files:\n{r.stdout}"
    leaked = [n for n in emitted for w in windows_only if w in n]
    assert not leaked, f"platform: windows task leaked into POSIX units: {leaked}"


def test_compile_empty_response_is_a_noop(bundle: Path):
    """A valid-but-empty LLM answer ('[]') means "nothing worth extracting".

    It must no-op cleanly: exit 0, write no page, and leave an existing page
    untouched. An empty array is indistinguishable from a parse give-up
    (parse_llm_json returns [] on failure), so the danger is treating "no
    changes" as "wipe what's there".
    """
    wiki = bundle / "wiki"
    keep = wiki / "projects" / "myproject" / "keeper.md"
    keep.parent.mkdir(parents=True, exist_ok=True)
    keep.write_text("# Keeper\n\nPre-existing content.\n", encoding="utf-8")
    before = keep.read_text(encoding="utf-8")

    resp = bundle / "empty_response.json"
    resp.write_text("[]", encoding="utf-8")

    r = _run(bundle / "cron" / "wiki" / "wiki-compile-sessions.py",
             {"WIKI_LLM_MOCK_RESPONSE": str(resp)}, cwd=bundle)
    assert r.returncode == 0, f"empty [] should be a clean no-op:\n{r.stdout}\n{r.stderr}"
    assert keep.read_text(encoding="utf-8") == before, "empty response destroyed an existing page"
    assert not list((bundle / "cron" / "logs" / "rejected").glob("*")), \
        "empty [] was quarantined as if it were a failure"


def test_compile_rejects_wrong_schema(bundle: Path):
    """Entries that parse as JSON but don't match the {path, content} contract
    must be rejected and quarantined, not written as empty/garbage pages, and
    the pair must not be finalized (exit != 0 so the retry redoes it)."""
    resp = bundle / "schema_response.json"
    resp.write_text(json.dumps([
        {"path": "projects/myproject/no-content.md", "action": "create"},  # content missing
        {"content": "orphaned content with no path"},                      # path missing
        ["not", "an", "object"],                                           # not a dict
    ]), encoding="utf-8")

    r = _run(bundle / "cron" / "wiki" / "wiki-compile-sessions.py",
             {"WIKI_LLM_MOCK_RESPONSE": str(resp)}, cwd=bundle)
    assert r.returncode != 0, \
        f"a schema-violating payload must be a hard failure:\n{r.stdout}\n{r.stderr}"
    assert not (bundle / "wiki" / "projects" / "myproject" / "no-content.md").exists(), \
        "wrote a page for an entry that carried no content"
    assert list((bundle / "cron" / "logs" / "rejected").glob("*")), \
        f"schema-violating payload was dropped without quarantine:\n{r.stdout}"


def test_flush_merges_colliding_slugs(bundle: Path, tmp_path: Path):
    """Two project dirs can resolve to ONE slug (dir_to_project's trailing-
    segment fallback). Both sessions must be processed and merged — the second
    dir must not silently displace the first, and neither may be skipped."""
    home = tmp_path / "home_collide"
    projects = home / ".claude" / "projects"
    filler = "x" * 600

    def seed(dirname: str, marker: str):
        d = projects / dirname
        d.mkdir(parents=True)
        lines = []
        for i in range(12):
            lines.append(json.dumps({"type": "user",
                "message": {"role": "user", "content": f"{marker} msg {i} {filler}"}}))
            lines.append(json.dumps({"type": "assistant",
                "message": {"role": "assistant", "content": f"reply {i} {filler}"}}))
        (d / f"{marker}.jsonl").write_text("\n".join(lines) + "\n", encoding="utf-8")

    # Different absolute paths, same trailing segment → both become "myapp".
    seed("C--Users-test-work-myapp", "first")
    seed("D--clients-other-myapp", "second")

    resp = bundle / "collide_response.md"
    resp.write_text("- A durable fact. [[index]]\n", encoding="utf-8")
    env = {"WIKI_LLM_MOCK_RESPONSE": str(resp),
           "USERPROFILE": str(home), "HOME": str(home)}

    r = _run(bundle / "cron" / "wiki" / "wiki-flush-sessions.py", env, cwd=bundle)
    assert r.returncode == 0, f"flush failed:\n{r.stdout}\n{r.stderr}"
    state = json.loads((bundle / "wiki" / ".processed.json").read_text(encoding="utf-8"))
    processed = state.get("flush", {}).get("processed_jsonls", [])
    # Both sessions recorded, both under the one shared slug.
    assert any("first.jsonl" in k for k in processed), f"first dir's session lost: {processed}"
    assert any("second.jsonl" in k for k in processed), f"second dir's session lost: {processed}"


def test_compile_coalesces_same_path_changes(bundle: Path):
    """Two changes targeting ONE page must merge, not overwrite.

    apply_changes re-reads the page it just wrote and replaces the body, so a
    model that split one page across two entries lost the first entry with no
    reject and no log line.
    """
    import importlib.util
    sys.path.insert(0, str(bundle / "cron" / "hooks"))
    spec = importlib.util.spec_from_file_location(
        "wcs", bundle / "cron" / "wiki" / "wiki-compile-sessions.py")
    wcs = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(wcs)

    out = wcs.coalesce_changes([
        {"path": "projects/demo/incident-x.md", "content": "## Symptom\nIt broke."},
        {"path": "projects/demo/incident-x.md", "content": "## Fix\nRestart it."},
        {"path": "projects/demo/other.md", "content": "unrelated"},
    ])
    assert len(out) == 2, f"same-path entries not coalesced: {out}"
    merged = next(c for c in out if c["path"].endswith("incident-x.md"))
    assert "It broke." in merged["content"], "first entry's content was lost"
    assert "Restart it." in merged["content"], "second entry's content was lost"
    assert merged["content"].index("Symptom") < merged["content"].index("Fix"), \
        "coalescing reordered the model's output"


def _load_gen_scheduler():
    import importlib.util
    spec = importlib.util.spec_from_file_location(
        "gen_scheduler", ROOT / "scripts" / "gen-scheduler.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _load_utils(bundle: Path, name: str):
    """Import the COPIED cron/hooks/utils.py so BUNDLE_ROOT lands in tmp_path."""
    import importlib.util
    spec = importlib.util.spec_from_file_location(
        name, bundle / "cron" / "hooks" / "utils.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_llm_queue_never_steals_a_live_lock(bundle: Path):
    """The queue's whole point is one provider call at a time on one account.

    Taking an abandoned lock over used to be an unconditional unlink, which is a
    TOCTOU race: two waiters both see age > STALE, the first removes the lock and
    takes its own, the second removes THAT fresh lock and takes its own — both
    then call the provider in parallel on one key, the self-inflicted 429 the
    queue exists to prevent. A live lock must survive a waiter; an abandoned one
    must be taken over without leaving debris behind.
    """
    import os
    import time
    u = _load_utils(bundle, "utils_lock")
    u.LLM_LOCK.parent.mkdir(parents=True, exist_ok=True)

    # 1. Normal round trip: held inside, released after.
    with u._llm_queue():
        assert u.LLM_LOCK.exists()
    assert not u.LLM_LOCK.exists()

    # 2. A LIVE lock is never removed by a waiter that gave up (fail-open: the
    #    call proceeds unqueued, but the holder keeps the slot). The holder PID
    #    has to be a real live process — the queue now checks it.
    u.LLM_LOCK.write_text(f"{os.getpid()} holder\n", encoding="utf-8")
    u.LLM_LOCK_WAIT = 0
    with u._llm_queue():
        pass
    assert u.LLM_LOCK.exists(), "a waiter deleted a live holder's lock"

    # 3. An abandoned lock IS taken over, and no .stale.* debris is left.
    old = time.time() - 10_000
    os.utime(u.LLM_LOCK, (old, old))
    u.LLM_LOCK_STALE = 1800
    with u._llm_queue():
        assert u.LLM_LOCK.exists()
    assert not u.LLM_LOCK.exists()
    assert not list(u.LLM_LOCK.parent.glob(f"{u.LLM_LOCK.name}.stale.*"))


def test_llm_queue_loses_the_steal_race_gracefully(bundle: Path, monkeypatch):
    """The interleaving the old unconditional unlink got wrong.

    Waiter sees an abandoned lock; before it acts, the holder releases and a
    DIFFERENT waiter takes a fresh one. The lock now on disk is that fresh lock,
    and removing it puts two processes on the provider at once — the exact 429
    the queue prevents. The steal must therefore be undone once the stolen file
    turns out to be fresh.
    """
    import os
    import time
    u = _load_utils(bundle, "utils_race")
    u.LLM_LOCK.parent.mkdir(parents=True, exist_ok=True)
    # A real live PID: the competing waiter must look alive, or the queue is
    # entitled to reclaim its lock and the race this test models never happens.
    u.LLM_LOCK.write_text(f"{os.getpid()} other-waiter\n", encoding="utf-8")
    old = time.time() - 10_000
    os.utime(u.LLM_LOCK, (old, old))
    u.LLM_LOCK_STALE = 1800
    u.LLM_LOCK_WAIT = 0  # give up quickly instead of looping on a live lock

    real_replace = os.replace

    def racing_replace(src, dst):
        # Simulate the window: what we are about to steal is already the fresh
        # lock a competing waiter just took, not the abandoned one we stat'ed.
        if Path(src) == u.LLM_LOCK and Path(src).exists():
            os.utime(src, None)
        return real_replace(src, dst)

    monkeypatch.setattr(os, "replace", racing_replace)
    with u._llm_queue():
        pass

    assert u.LLM_LOCK.exists(), "the fresh lock of a competing waiter was destroyed"
    assert u.LLM_LOCK.read_text(encoding="utf-8").startswith(f"{os.getpid()} other-waiter"), \
        "the competing waiter's lock was overwritten"
    assert not list(u.LLM_LOCK.parent.glob(f"{u.LLM_LOCK.name}.stale.*")), \
        "the undone steal left debris behind"


def test_llm_queue_reclaims_a_dead_owners_lock_at_once(bundle: Path, monkeypatch):
    """A lock whose owner died must not cost the next job the full STALE wait.

    Age alone cannot tell "holder is working" from "holder was killed": a job
    stopped by a timeout or a reboot leaves a lock with a fresh mtime, and every
    later job then waits out LLM_LOCK_STALE — half an hour of silence that reads
    as a hung script, since a waiter logs nothing while it waits.
    """
    import os
    u = _load_utils(bundle, "utils_dead_owner")
    u.LLM_LOCK.parent.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(u.time, "sleep", lambda _s: None)

    # Fresh by mtime, but the owning process is gone.
    u.LLM_LOCK.write_text("4242 killed-by-timeout\n", encoding="utf-8")
    monkeypatch.setattr(u, "_pid_alive", lambda pid: pid != 4242)

    with u._llm_queue():
        assert u._lock_owner_pid(u.LLM_LOCK) == os.getpid(), "the queue was not entered"

    assert not u.LLM_LOCK.exists()
    assert not list(u.LLM_LOCK.parent.glob(f"{u.LLM_LOCK.name}.stale.*")), \
        "the reclaimed lock left debris behind"


def test_llm_queue_keeps_waiting_on_an_unreadable_lock(bundle: Path, monkeypatch):
    """No PID to check → fall back to the age rule, never to "steal it anyway"."""
    u = _load_utils(bundle, "utils_bad_lock")
    u.LLM_LOCK.parent.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(u.time, "sleep", lambda _s: None)
    u.LLM_LOCK.write_text("garbage\n", encoding="utf-8")
    u.LLM_LOCK_WAIT = 0

    with u._llm_queue():
        pass

    assert u.LLM_LOCK.exists(), "an unreadable lock was treated as abandoned"


def test_llm_call_latches_a_403(bundle: Path, monkeypatch):
    """403 = the model is not available to this account (a region opt-in never
    accepted) or a WAF rejected us. Neither clears mid-run, so it must latch the
    provider like 402 does. Falling through to the generic error handler left
    the circuit breaker open, and every remaining call of the batch paid another
    round trip to a door already known to be shut.

    On the SECOND consecutive one. The latch now persists for six hours across
    processes, so latching on a single 403 would let one bad minute at a proxy
    take the provider out for the whole night and hand the payload to the next
    one in the chain — a real bill for someone else's blip."""
    u = _load_utils(bundle, "utils_403")
    # Not token-shaped on purpose: the repo's own secret guard scans this diff.
    monkeypatch.setenv("DEEPSEEK_KEY", "unit-test-placeholder")
    calls = {"n": 0}

    class Resp:
        status_code = 403
        text = "RegionError: requires explicit opt in"

    def fake_post(*a, **kw):
        calls["n"] += 1
        return Resp()

    # A STUB module, not the real `requests`. requirements-dev.txt says the fast
    # suite "needs no provider SDK", and a bare `import requests` here made that
    # false: the suite was red on any machine without it and green on CI only
    # because CI happens to install requirements.txt too. _llm_openai_compat
    # imports requests at CALL time, so a stub in sys.modules is what it gets.
    fake_requests = types.ModuleType("requests")
    fake_requests.post = fake_post
    monkeypatch.setitem(sys.modules, "requests", fake_requests)

    res = u._llm_openai_compat("deepseek", "hi")
    assert res.text is None
    assert res.kind == "config", f"a 403 is not something waiting fixes: {res.kind}"
    assert calls["n"] == 1, "a 403 was retried instead of returned"
    assert not u._is_depleted("deepseek"), \
        "one 403 latched the provider for six hours — two in a row is the evidence"

    res = u._llm_openai_compat("deepseek", "hi")
    assert res.text is None
    assert res.kind == "config"
    assert calls["n"] == 2, "the second 403 was retried instead of latched"
    assert "deepseek" in u._DEPLETED_PROVIDERS
    assert u._is_depleted("deepseek"), \
        "the circuit breaker did not open on the second consecutive 403"


def test_a_dead_chain_is_written_down_for_the_healthcheck(bundle: Path, monkeypatch):
    """Every provider failing must leave a trace — silently, but a trace.

    A dead chain is not one event: a night is a hundred calls meeting the same
    shut door, so this library must not alert (and must not acquire an outbound
    channel of its own — see record_chain_dead). It writes the fact; the daily
    healthcheck, which already owns the Telegram channel, reports it. Without
    that note each task logs its own bad night and nothing says the machine has
    no LLM at all — upstream that lasted two nights.
    """
    u = _load_utils(bundle, "utils_chain_dead")
    monkeypatch.setattr(u, "LLM_PROVIDER", u.PROVIDER_CHAIN_NAME)
    monkeypatch.setattr(u, "OFFBOX_FALLBACK", True)
    monkeypatch.setattr(u, "_llm_openai_compat",
                        lambda *a, **kw: u.LLMResult(None, "transient", "stub"))

    res = u._llm_call_unlocked("hi")
    assert res.text is None

    state = json.loads((bundle / "cron" / "state" / "chain-dead.json")
                       .read_text(encoding="utf-8"))
    assert state["fails"] == 1
    assert state["first_iso"] and state["last_iso"]

    # A second failure counts up but keeps the START of the outage: what a
    # reader needs is how long it has been down, not that it failed a second ago.
    first = state["first_iso"]
    u._llm_call_unlocked("hi again")
    state = json.loads((bundle / "cron" / "state" / "chain-dead.json")
                       .read_text(encoding="utf-8"))
    assert state["fails"] == 2 and state["first_iso"] == first


def test_nothing_to_compile_is_a_failure_when_pending_is_not_empty(bundle: Path):
    """An idle night and a lost night must not report the same way.

    "No uncompiled dailies" is healthy when flush ran and found nothing. It is a
    FAILURE when flush died (no provider, an exception) and wrote no daily while
    the raw material still sits in .pending: there is nothing to compile because
    the night was lost. Both used to exit 0, so a stalled pipeline read as green
    to every health check watching the return code.
    """
    wiki = bundle / "wiki"
    (wiki / "daily" / "2026-01-01.md").unlink()      # no daily for either case

    # Case 1: nothing pending either — an honest idle run.
    r = _run(bundle / "cron" / "wiki" / "wiki-compile-sessions.py", {}, cwd=bundle)
    assert r.returncode == 0, f"an idle run must stay green:\n{r.stdout}\n{r.stderr}"

    # Case 2: raw material stuck in .pending — flush never turned it into a daily.
    pending = wiki / "daily" / ".pending"
    pending.mkdir(parents=True, exist_ok=True)
    (pending / "session-abc.md").write_text("## myproject\nraw notes\n",
                                            encoding="utf-8")

    r = _run(bundle / "cron" / "wiki" / "wiki-compile-sessions.py", {}, cwd=bundle)
    assert r.returncode != 0, \
        f"a lost night reported green:\n{r.stdout}\n{r.stderr}"
    assert ".pending" in r.stdout, \
        f"the reason must name .pending, or nobody can act on it:\n{r.stdout}"


def test_provider_sends_its_required_session_header(bundle: Path, monkeypatch):
    """A gateway that demands a conversation id must actually be handed one.

    OpenCode Go started requiring `x-opencode-session` on 2026-09-12 and answers
    HTTP 400 MissingSessionID without it — to every call, whatever the key or
    the quota says. A provider dead for that reason looks exactly like a provider
    dead for any other, so the header is declared in the PROVIDERS table and
    asserted here rather than trusted to stay in place.

    The id is per PROCESS, not per call: a run's calls share a system prefix the
    gateway can cache only if the id is stable across them.
    """
    u = _load_utils(bundle, "utils_session_header")
    monkeypatch.setenv("OPENCODE_GO_KEY", "unit-test-placeholder")
    seen = []

    class Resp:
        status_code = 200

        @staticmethod
        def json():
            return {"choices": [{"message": {"content": "ok"}}]}

    def fake_post(*a, **kw):
        seen.append(kw.get("headers", {}))
        return Resp()

    fake_requests = types.ModuleType("requests")
    fake_requests.post = fake_post
    monkeypatch.setitem(sys.modules, "requests", fake_requests)

    assert u._llm_openai_compat("opencode", "hi").text == "ok"
    assert u._llm_openai_compat("opencode", "hi again").text == "ok"

    header = u.PROVIDERS["opencode"]["session_header"]
    assert all(header in h for h in seen), \
        f"{header} missing — the gateway answers 400 MissingSessionID to every call"
    assert seen[0][header] == seen[1][header], \
        "a fresh id per call throws away the prompt caching the header exists for"

    # A provider that declares no such header must not grow one.
    assert "session_header" not in u.PROVIDERS["deepseek"]


def test_gen_scheduler_escapes_and_passes_script_args(tmp_path: Path):
    """An install path with a space or '&' must not corrupt the emitted units,
    and registry `script_args` must reach the command line: the launchd plist
    was hand-built XML (invalid on '&') and ExecStart was an unquoted join."""
    import plistlib
    gs = _load_gen_scheduler()
    task = {"name": "T", "kind": "bash", "trigger": "Daily 03:00",
            "script": "<bundle-install-path>/cron/x.sh",
            "script_args": ["--flag", "a b", "x&y"]}
    argv = gs.exec_argv(task, "/opt/Claude & Team")
    assert argv == ["/bin/bash", "/opt/Claude & Team/cron/x.sh",
                    "--flag", "a b", "x&y"], argv

    assert gs.emit_launchd(task, "/opt/Claude & Team", tmp_path) is None
    plist_path = tmp_path / "launchd" / "com.claude-bundle.T.plist"
    with open(plist_path, "rb") as f:
        obj = plistlib.load(f)  # raises on malformed XML
    assert obj["ProgramArguments"] == argv

    assert gs.emit_systemd(task, "/opt/Claude & Team", tmp_path) is None
    exec_line = next(l for l in (tmp_path / "systemd" / "T.service")
                     .read_text(encoding="utf-8").splitlines()
                     if l.startswith("ExecStart="))
    # Each token must survive shell-style splitting as one word.
    import shlex
    assert shlex.split(exec_line[len("ExecStart="):].replace("%%", "%")) == argv
