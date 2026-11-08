"""Recovery exercises the same page renderer/guards as nightly compilation."""
import importlib.util
import json
import sys
from datetime import datetime
from pathlib import Path

import pytest


@pytest.fixture
def recovery(cron_copy, monkeypatch):
    monkeypatch.syspath_prepend(str(cron_copy / "cron/hooks"))
    monkeypatch.syspath_prepend(str(cron_copy / "cron/lib"))
    for name in ("utils", "wiki_recovery", "untrusted", "runs"):
        monkeypatch.delitem(sys.modules, name, raising=False)
    spec = importlib.util.spec_from_file_location(
        "recovery_test", cron_copy / "cron/wiki/wiki-recover-change.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    class FixedDatetime(datetime):
        @classmethod
        def now(cls, tz=None):
            return cls(2020, 5, 6, tzinfo=tz)
    monkeypatch.setattr(module.utils, "datetime", FixedDatetime)
    module.utils.DAILY_DIR.mkdir(parents=True)
    source = module.utils.DAILY_DIR / "2020-05-04.md"
    source.write_text("# Daily\n## demo\nNew durable knowledge.\n", encoding="utf-8")
    return module, source


def record(recovery, *, kind="sessions", target="projects/demo/gateway.md", reason="write failed"):
    module, source = recovery
    import wiki_recovery as helpers
    if kind == "kb":
        source = module.utils.BUNDLE_ROOT / "kb_sources/articles/sample.md"
        source.parent.mkdir(parents=True, exist_ok=True)
        source.write_text("External article.\n", encoding="utf-8")
    helpers.quarantine_change({"path": target, "content": "# Gateway\n\nRecovered fact.\n"},
                              reason, kind=kind, source=source,
                              source_rel=source.name if kind == "sessions" else "articles/sample.md",
                              project="demo" if kind == "sessions" else "",
                              target_rel=target, blind=True)
    return next(module.utils.REJECTED_DIR.glob("*.txt"))


@pytest.mark.parametrize("kind,target", [("sessions", "projects/demo/gateway.md"),
                                        ("kb", "kb/tools/gateway.md")])
def test_preview_apply_and_crash_retry_are_exact_and_idempotent(recovery, kind, target):
    module, _ = recovery
    path = record(recovery, kind=kind, target=target)
    with pytest.raises(ValueError, match="preview"):
        module.recover(path, apply=True)
    before_state = module.utils.STATE_PATH.read_bytes() if module.utils.STATE_PATH.exists() else None
    diff = module.recover(path)
    assert "+Recovered fact." in diff
    page = module.utils.WIKI_ROOT / target
    assert not page.exists()
    receipt = json.loads(path.with_suffix(".review.json").read_text(encoding="utf-8"))
    assert module.recover(path, apply=True).startswith("Applied:")
    assert module.fingerprint(page) == receipt["after_sha256"]
    assert module.recover(path, apply=True).startswith("Already applied:")
    assert page.read_text(encoding="utf-8").count("Recovered fact.") == 1
    assert (module.utils.STATE_PATH.read_bytes() if module.utils.STATE_PATH.exists() else None) == before_state


@pytest.mark.parametrize("mutation", ["source", "target", "record", "privacy"])
def test_changed_inputs_or_policy_refuse_apply(recovery, monkeypatch, mutation):
    module, source = recovery
    path = record(recovery)
    module.recover(path)
    if mutation == "source":
        source.write_text("changed", encoding="utf-8")
    elif mutation == "target":
        page = module.utils.WIKI_ROOT / "projects/demo/gateway.md"
        page.parent.mkdir(parents=True)
        page.write_text("Owner's new page", encoding="utf-8")
    elif mutation == "record":
        path.write_text(path.read_text(encoding="utf-8").replace("Recovered fact", "Other fact"), encoding="utf-8")
    else:
        monkeypatch.setattr(module.utils, "SKIP_JSONL_PROJECTS", ["demo"])
    with pytest.raises(ValueError):
        module.recover(path, apply=True)


def test_out_of_scope_record_needs_explicit_safe_retarget(recovery):
    module, _ = recovery
    path = record(recovery, target="kb/tools/gateway.md", reason="path-outside-project")
    with pytest.raises(ValueError):
        module.recover(path)
    target = "projects/demo/gateway.md"
    assert "PREVIEW" in module.recover(path, target_rel=target)
    assert "Applied" in module.recover(path, apply=True, target_rel=target)


def test_legacy_and_escaped_sources_are_inspection_only(recovery):
    module, _ = recovery
    path = record(recovery)
    original = json.loads(path.read_text(encoding="utf-8"))
    for invalid in ([{"path": "projects/demo/a.md"}], {**original, "source": "../2020-05-04.md"}):
        path.write_text(json.dumps(invalid), encoding="utf-8")
        with pytest.raises(ValueError):
            module.recover(path)


def test_compiler_records_rejected_payload_and_preview_leaves_no_artifacts(recovery):
    module, source = recovery
    compiler = module._compiler("sessions")
    applied, rejected = compiler.apply_changes(
        [{"path": "kb/tools/stray.md", "content": "A fact"}], source.name, "demo")
    assert not applied and rejected
    path = next(module.utils.REJECTED_DIR.glob("*.txt"))
    data = json.loads(path.read_text(encoding="utf-8"))
    assert data["schema"] == "wiki-change-v1" and data["source_sha256"]
    before = sorted(module.utils.REJECTED_DIR.iterdir())
    compiler.apply_changes([{"path": "bad", "content": "A fact"}], source.name, "demo", dry_run=True)
    assert sorted(module.utils.REJECTED_DIR.iterdir()) == before


def test_recovery_shares_compiler_lock(recovery):
    module, _ = recovery
    path = record(recovery)
    with module.page_lock():
        assert module.main([str(path)]) == 1
    assert not path.with_suffix(".review.json").exists()
