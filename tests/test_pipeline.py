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
    pytest.importorskip("yaml")  # gen-scheduler requires PyYAML
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
    leaked = [n for n in emitted if "ClaudeTaskMonitor" in n]
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
    #    call proceeds unqueued, but the holder keeps the slot).
    u.LLM_LOCK.write_text("999999 holder\n", encoding="utf-8")
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
    u.LLM_LOCK.write_text("999999 other-waiter\n", encoding="utf-8")
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
    assert u.LLM_LOCK.read_text(encoding="utf-8").startswith("999999"), \
        "the competing waiter's lock was overwritten"
    assert not list(u.LLM_LOCK.parent.glob(f"{u.LLM_LOCK.name}.stale.*")), \
        "the undone steal left debris behind"


def test_llm_call_latches_a_403(bundle: Path, monkeypatch):
    """403 = the model is not available to this account (a region opt-in never
    accepted) or a WAF rejected us. Neither clears mid-run, so it must latch the
    provider like 402 does. Falling through to the generic error handler left
    the circuit breaker open, and every remaining call of the batch paid another
    round trip to a door already known to be shut."""
    import requests
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

    # _llm_openai_compat does `import requests` at call time, so patching the
    # module object itself is what reaches it.
    monkeypatch.setattr(requests, "post", fake_post)
    assert u._llm_openai_compat("deepseek", "hi") is None
    assert calls["n"] == 1, "a 403 was retried instead of latched"
    assert "deepseek" in u._DEPLETED_PROVIDERS
    assert u._is_depleted("deepseek"), "the circuit breaker did not open on 403"


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
