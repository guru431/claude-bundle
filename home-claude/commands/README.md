# User-level slash commands

One example: `code-review-ext.md` — a thin wrapper that calls the
`code-review-external` skill and runs your reviewer script.

It's a **template**. Before first use, edit the path in step 2 to point at
your local Python and your local reviewer script.

User-level slash commands live in `~/.claude/commands/<name>.md`. The
front-matter `description` and `argument-hint` show up in the `/` picker.
`allowed-tools` is the safety boundary — keep it minimal.
