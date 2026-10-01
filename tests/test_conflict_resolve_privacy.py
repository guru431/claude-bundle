"""wiki-conflict-resolve sends whole pages to the provider — never a denied project's.

Every collector upstream honors the privacy policy, but this script read the
vault itself and had no gate: a project added to `skip_projects` after its pages
were compiled still went out, page by page, on the next merge pass.
"""
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
