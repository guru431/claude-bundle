"""wiki-conflict-resolve sends whole pages to the provider — never a denied project's.

Every collector upstream honors the privacy policy, but this script read the
vault itself and had no gate: a project added to `skip_projects` after its pages
were compiled still went out, page by page, on the next merge pass.
"""
import re
import sys
from pathlib import Path

PAGE = """# {title}

Some facts about {title}.

## Update (2026-01-02)

# {title} again

More facts about {title}.
"""


def _load(bundle: Path, monkeypatch, name: str):
    """The COPIED script with ITS utils — see test_pipeline._load_wiki_script."""
    import importlib.util
    monkeypatch.syspath_prepend(str(bundle / "cron"))
    monkeypatch.syspath_prepend(str(bundle / "cron" / "hooks"))
    for mod in ("utils", "untrusted", "runs"):
        monkeypatch.delitem(sys.modules, mod, raising=False)
    spec = importlib.util.spec_from_file_location(
        name, bundle / "cron" / "wiki" / "wiki-conflict-resolve.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _collision(bundle: Path, rel: str, title: str) -> None:
    page = bundle / "wiki" / rel
    page.parent.mkdir(parents=True, exist_ok=True)
    page.write_text(PAGE.format(title=title), encoding="utf-8")


def test_a_denied_projects_pages_never_reach_the_provider(cron_copy: Path, monkeypatch):
    (cron_copy / "bundle.local.yaml").write_text("skip_projects: [secretproj]\n",
                                                 encoding="utf-8")
    _collision(cron_copy, "projects/secretproj/setup.md", "Secret setup")
    _collision(cron_copy, "projects/openproj/setup.md", "Open setup")
    _collision(cron_copy, "kb/tools/hugo.md", "Hugo static site generator")
    wcr = _load(cron_copy, monkeypatch, "wcr_privacy")
    sent: list[str] = []
    monkeypatch.setattr(wcr, "llm_call", lambda prompt, **kw: sent.append(prompt) or None)
    monkeypatch.setattr(sys, "argv", ["wiki-conflict-resolve.py", "--limit", "10"])

    assert wcr.main() == 0

    assert len(sent) == 2, f"expected the open project and the kb page, got {len(sent)}"
    assert not [p for p in sent if "Secret setup" in p], "a denied project's page went out"
    assert any("withheld by the privacy policy: 1" in ln for ln in wcr.log_lines)


def _merge(prompt: str, **_kw) -> str:
    """A sane merge of whichever PAGE the prompt carries: one H1, both facts."""
    title = re.search(r"(?m)^# (.+) again$", prompt).group(1)
    return f"# {title}\n\nSome facts about {title}.\n\nMore facts about {title}.\n"


def test_same_named_pages_of_two_projects_get_two_previews(cron_copy: Path, monkeypatch):
    """The preview was named after the stem: `setup.md` of one project overwrote
    the preview of another's, and the log pointed both at one file."""
    _collision(cron_copy, "projects/alpha/setup.md", "Alpha setup")
    _collision(cron_copy, "projects/beta/setup.md", "Beta setup")
    wcr = _load(cron_copy, monkeypatch, "wcr_previews")
    monkeypatch.setattr(wcr, "llm_call", _merge)
    monkeypatch.setattr(sys, "argv", ["wiki-conflict-resolve.py"])

    assert wcr.main() == 0

    previews = sorted(p.name for p in wcr.PREVIEW_DIR.glob("*.merged.md"))
    assert previews == ["projects__alpha__setup.merged.md", "projects__beta__setup.merged.md"]
    assert "Alpha setup" in (wcr.PREVIEW_DIR / previews[0]).read_text(encoding="utf-8")


def test_a_page_that_cannot_be_written_fails_alone(cron_copy: Path, monkeypatch):
    """An OSError on one page (a lock, permissions, a full disk) took the whole
    run down — the rest unmerged, and no log listing the pages already rewritten."""
    _collision(cron_copy, "projects/alpha/setup.md", "Alpha setup")
    _collision(cron_copy, "projects/beta/setup.md", "Beta setup")
    wcr = _load(cron_copy, monkeypatch, "wcr_write_fail")
    monkeypatch.setattr(wcr, "llm_call", _merge)
    real_write = wcr.write_page

    def write_page(path, fm, body):
        if "alpha" in path.as_posix():
            raise PermissionError("locked by another program")
        real_write(path, fm, body)

    monkeypatch.setattr(wcr, "write_page", write_page)
    monkeypatch.setattr(sys, "argv", ["wiki-conflict-resolve.py", "--apply"])

    assert wcr.main() == 0

    assert "again" not in (cron_copy / "wiki/projects/beta/setup.md").read_text(encoding="utf-8")
    assert any("FAIL projects/alpha/setup.md" in ln for ln in wcr.log_lines)
    assert any("ok=1 failed=1" in ln for ln in wcr.log_lines)


def test_a_broken_manifest_withholds_every_project_page(cron_copy: Path, monkeypatch):
    """Fail-closed, like every other stage: no policy means no project data moves."""
    (cron_copy / "bundle.local.yaml").write_text("skip_projects: notalist\n",
                                                 encoding="utf-8")
    _collision(cron_copy, "projects/openproj/setup.md", "Open setup")
    wcr = _load(cron_copy, monkeypatch, "wcr_broken")
    sent: list[str] = []
    monkeypatch.setattr(wcr, "llm_call", lambda prompt, **kw: sent.append(prompt) or None)
    monkeypatch.setattr(sys, "argv", ["wiki-conflict-resolve.py"])

    assert wcr.main() == 0
    assert sent == []
