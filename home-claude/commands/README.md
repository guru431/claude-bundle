# User-level slash commands

Two commands ship here.

`code-review-ext.md` — a thin wrapper that calls the `code-review-external`
skill and runs your reviewer script. It's a **template**: before first use, edit
the path in step 2 to point at your local Python and your local reviewer script.

`wiki.md` (`/wiki <words>`) — searches the vault the nightly pipeline builds,
via `cron/wiki/wiki-grep.py`. Pure Python over local files: no LLM call, no
network, nothing written. Full tier only (a lite install has no `cron/`).

User-level slash commands live in `~/.claude/commands/<name>.md`. The
front-matter `description` and `argument-hint` show up in the `/` picker.
`allowed-tools` is the safety boundary — keep it minimal.
