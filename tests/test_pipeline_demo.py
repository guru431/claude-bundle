"""`wiki-pipeline.py --demo`: the pipeline, run on the shipped example, touching nothing.

A newcomer could see what a night produces only by spending tokens on their own
sessions, or by taking docs/examples/ on trust. The demo runs compile and index on
the example daily with the offline mock provider. Whatever the install it is
started from holds, three things must stay true:

* nothing under that install is written — vault, state ledger, logs, run ledger;
* no provider and no alert channel is reached, with keys, a chain, Telegram and a
  proxy all configured;
* what it prints IS docs/examples/, so the committed sample cannot drift from what
  the code produces.
"""
from __future__ import annotations

import contextlib
import os
import re
import shutil
import socket
import subprocess
import sys
import threading
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
CRON_SRC = ROOT / "home-claude" / "cron"
EXAMPLES = ROOT / "docs" / "examples"
_RUNTIME = shutil.ignore_patterns("__pycache__", "logs", "state")


def _snapshot(root: Path) -> dict[str, bytes]:
    """Every file under `root` and its bytes — byte-code aside, which any import writes."""
    return {p.relative_to(root).as_posix(): p.read_bytes() for p in root.rglob("*")
            if p.is_file() and "__pycache__" not in p.parts}


@contextlib.contextmanager
def _tripwire():
    """A loopback port that counts every connection made to it."""
    server = socket.socket()
    server.bind(("127.0.0.1", 0))
    server.listen(8)
    server.settimeout(0.2)
    hits: list[int] = []
    stop = threading.Event()

    def serve() -> None:
        while not stop.is_set():
            try:
                conn, _ = server.accept()
            except OSError:          # the accept timeout, or the socket closing
                continue
            hits.append(1)
            conn.close()

    thread = threading.Thread(target=serve, daemon=True)
    thread.start()
    try:
        yield server.getsockname()[1], hits
    finally:
        stop.set()
        thread.join(2)
        server.close()


def _run(script: Path, *args: str, env: dict) -> subprocess.CompletedProcess:
    return subprocess.run([sys.executable, str(script), *args], env=env, cwd=script.parent,
                          capture_output=True, text=True, encoding="utf-8", errors="replace",
                          timeout=300)


def _env(tmp_path: Path, **extra) -> dict:
    temp = tmp_path / "temp"
    temp.mkdir(exist_ok=True)
    env = dict(os.environ, TEMP=str(temp), TMP=str(temp), TMPDIR=str(temp), **extra)
    for name in ("NO_PROXY", "no_proxy"):
        env.pop(name, None)
    return env


def test_the_demo_prints_the_example_and_leaves_its_install_alone(tmp_path: Path):
    # A checkout in miniature whose home-claude/ is a LIVE install: settings with
    # keys and a chain, a vault, a state ledger, logs, a breaker state.
    repo = tmp_path / "repo"
    install = repo / "home-claude"
    shutil.copytree(CRON_SRC, install / "cron", ignore=_RUNTIME)
    shutil.copytree(EXAMPLES, repo / "docs" / "examples")
    (install / "wiki" / "daily").mkdir(parents=True)
    (install / "wiki" / "daily" / "2026-01-01.md").write_text(
        "# Daily 2026-01-01\n\n## mine\nMy own notes.\n", encoding="utf-8")
    (install / "wiki" / ".processed.json").write_text('{"compile_sessions": {}}', encoding="utf-8")
    (install / "cron" / "logs").mkdir()
    (install / "cron" / "logs" / "wiki-pipeline_2026-01-01.log").write_text("mine\n", encoding="utf-8")
    (install / "cron" / "state").mkdir()
    (install / "cron" / "state" / "depleted.json").write_text("{}", encoding="utf-8")
    ledger = Path(os.environ["CLAUDE_BUNDLE_RUNS_DIR"])     # conftest: this test's own

    with _tripwire() as (port, hits):
        (install / ".env").write_text(
            "WIKI_LLM_PROVIDER=chain\n"
            "DEEPSEEK_KEY=not-a-real-key\n"
            f"DEEPSEEK_BASE_URL=http://127.0.0.1:{port}/v1\n"
            "TELEGRAM_BOT_TOKEN=not-a-real-token\n"
            "TELEGRAM_CHAT_ID=1\n", encoding="utf-8")
        before = _snapshot(install)
        # Any request that honours a proxy — requests, curl — lands on the tripwire.
        proxy = f"http://127.0.0.1:{port}"
        r = _run(install / "cron" / "wiki" / "wiki-pipeline.py", "--demo",
                 env=_env(tmp_path, HTTP_PROXY=proxy, HTTPS_PROXY=proxy))

    assert r.returncode == 0, r.stdout + r.stderr
    assert hits == [], f"the demo opened {len(hits)} connection(s)"
    assert _snapshot(install) == before, "the demo wrote into the install it was started from"
    assert not list(ledger.glob("runs-*.jsonl")), "the demo wrote the run ledger"
    sandbox = Path(re.search(r"^Sandbox: (.+)$", r.stdout, flags=re.M).group(1).strip())
    assert not sandbox.exists() and not list((tmp_path / "temp").iterdir()), \
        "the sandbox was left behind"

    # The committed example IS what the pipeline makes of it, dates aside.
    page = (EXAMPLES / "projects" / "demo" / "incident-empty-export-2026-03-14.md") \
        .read_text(encoding="utf-8").split("---\n", 2)[2]
    assert page.rstrip() in r.stdout, "docs/examples/ page differs from what compile writes"
    index = (EXAMPLES / "projects" / "index.md").read_text(encoding="utf-8").rstrip()
    run_date = re.escape("· 2026-03-15")
    assert re.search(re.escape(index).replace(run_date, r"· \d{4}-\d{2}-\d{2}"), r.stdout), \
        "docs/examples/projects/index.md differs from what build-index writes"


def test_the_demo_outside_a_checkout_says_where_to_run_it_and_writes_nothing(tmp_path: Path):
    deploy = tmp_path / ".claude"            # what an installer places: no docs/
    shutil.copytree(CRON_SRC, deploy / "cron", ignore=_RUNTIME)
    (deploy / "wiki").mkdir()
    before = _snapshot(deploy)
    script = deploy / "cron" / "wiki" / "wiki-pipeline.py"

    r = _run(script, "--demo", env=_env(tmp_path))
    assert r.returncode == 2, r.stdout + r.stderr
    assert "bundle CHECKOUT" in r.stderr and "do not deploy docs/" in r.stderr, r.stderr

    # A preview flag would leave the demo nothing to show, and it has nothing to hold back.
    r = _run(script, "--demo", "--dry-run", env=_env(tmp_path))
    assert r.returncode == 2 and "--dry-run" in r.stderr, r.stdout + r.stderr

    assert _snapshot(deploy) == before
    assert not list((tmp_path / "temp").iterdir())
