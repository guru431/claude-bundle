# wiki/ — empty starter vault

This is a starter wiki vault built around the Karpathy method (file-based,
navigated by `[[wikilinks]]`, no RAG). It ships **empty** — the pages will
fill up as the cron pipeline processes your real Claude Code sessions.

Layout:

```
wiki/
  index.md                   hand-curated top + auto-generated lists below
  projects/
    main/                    placeholder — rename or replace with your own
  kb/
    concepts/                external concepts (one .md per concept)
    tools/                   external tools / services / libraries
    people/                  external authors / researchers
  daily/                     auto-generated daily logs
    .pending/                staging area for sessions before flush
```

The wiki itself can be a separate git repo if you want a separate history
or separate remote — this bundle just gives you the directory skeleton.
If you put `wiki/` under a separate `.git`, add `wiki/` to the bundle's
`.gitignore` to avoid double-tracking. By default it ships nested.

For how the pipeline fills this up — see `docs/wiki-method.md` in the
bundle repository (the relative link breaks once `wiki/` is copied to
`~/.claude/`, so it's referenced by name here).
