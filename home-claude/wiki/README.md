# wiki/ — empty starter vault

This is a starter wiki vault built around the Karpathy method (file-based,
navigated by `[[wikilinks]]`, no RAG). It ships **empty** — the pages will
fill up as the cron pipeline processes your real Claude Code sessions.

Layout:

```
wiki/
  index.md                   hand-curated top + auto-generated lists below
  projects/
    main/                    the FALLBACK bucket, not a placeholder — see below
  kb/
    concepts/                external concepts (one .md per concept)
    tools/                   external tools / services / libraries
    people/                  external authors / researchers
  daily/                     auto-generated daily logs
    .pending/                staging area for sessions before flush
```

`projects/main/` is not a placeholder to rename: it is where the pipeline puts
anything it cannot attribute to a project — a daily-log section whose heading
yields no usable slug, a pending draft with no `Project:` line,
`projects/unknown/` from a model. `normalize_wiki_path` and
`normalize_project_name` both resolve to it deliberately. Renaming it would just
recreate it on the next run.

To make your own projects land in their own folders, populate `known_projects:`
and `project_map:` in `~/.claude/bundle.local.yaml`. `wiki-lint` reports a
`main/` that has grown large as "project-collapse", which is the signal that the
manifest needs an entry.

The wiki itself can be a separate git repo if you want a separate history
or separate remote — this bundle just gives you the directory skeleton.
If you put `wiki/` under a separate `.git`, add `wiki/` to the bundle's
`.gitignore` to avoid double-tracking. By default it ships nested, with no
`.git` of its own. Only a vault that has one is committed and pushed by
`ClaudeGitPushAll` (off by default), and `wiki/.gitignore` keeps the pipeline's
working files — `.pending/` drafts, the state ledger — out of that history.

For how the pipeline fills this up — see `docs/wiki-method.md` in the
bundle repository (the relative link breaks once `wiki/` is copied to
`~/.claude/`, so it's referenced by name here).
