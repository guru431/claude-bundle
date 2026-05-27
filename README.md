# claude-bundle

A portable, sanitized starter pack for [Claude Code](https://docs.claude.com/claude-code)
that you can drop onto a new machine in a few minutes.

It is **not** a plugin and **not** a framework. It's the bare files that
sit in `~/.claude/` — global rules, sane permissions defaults, a couple of
optional hooks, and two example skill/command templates — extracted from a
real working setup, with all personal data, hosts, tokens, and project
paths stripped out.

## What you get

```
home-claude/
├── CLAUDE.md                              global rules (Karpathy + tool
│                                          selection + encoding + workflow)
├── settings.json                          permissions, plugins, language
├── settings.example-with-hooks.json       same, with the two example hooks
│                                          wired in (copy-paste reference)
├── hooks/
│   ├── block-iptables-save-to-rules.py    PreToolUse — forbid a specific
│   │                                      dangerous iptables pattern
│   ├── md2pdf-on-edit.py                  PostToolUse — keep <name>.pdf in
│   │                                      sync with <name>.md
│   └── README.md
├── skills/
│   ├── code-review-external/SKILL.md      template — second-opinion review
│   │                                      via a different LLM
│   ├── personal-voice/SKILL.md            template — write text as you, by
│   │                                      register (email/chat/technical)
│   └── README.md
└── commands/
    ├── code-review-ext.md                 template slash wrapper for the
    │                                      code-review-external skill
    └── README.md
```

Plus install docs:

- [`INSTALL.md`](INSTALL.md) — step-by-step for a human
- [`AGENT-INSTRUCTIONS.md`](AGENT-INSTRUCTIONS.md) — same, but written for
  Claude Code to follow if you let it self-deploy on a new machine
- [`CHANGELOG.md`](CHANGELOG.md)

## What's actually in `CLAUDE.md`

A compact rule-set the assistant should follow in every project on this
machine. Highlights:

- **Karpathy coding discipline** — think before coding, simplicity first,
  surgical changes, goal-driven execution with explicit verify steps
- **Tool selection** — dedicated tools (Glob, Grep, Read, Edit, Write) over
  Bash when one fits; explicit list of cases where Bash is appropriate
- **Bash sandbox quirks on Windows + VS Code** — which commands silently
  fail and what to use instead
- **File encoding** — BOM rules for `.ps1` / `.sh` / `.cmd` on Windows
  (a real source of bugs when an LLM writes Cyrillic into a PowerShell
  script without BOM)
- **Findings pattern** — capture side observations during work without
  derailing the current task
- **Working methodology** — `/brainstorm` → `/writing-plans` →
  `/subagent-driven-development` via the `superpowers` plugin, plus
  `/systematic-debugging` for bugs, `/verification-before-completion`
  before declaring done
- **Codex CLI coexistence** — how to keep `~/.codex/AGENTS.md` in sync if
  you also run Codex CLI

## What's deliberately NOT here

| Not included | Why |
|---|---|
| `.credentials.json` | personal — `claude /login` creates its own |
| `.openclaude-profile.json` | personal API keys |
| `memory/` | personal facts, infra notes, incident history |
| `projects/` | session history, possibly sensitive |
| Hooks pinned to a specific project path | they won't resolve on your machine |
| MCP permissions for internal infrastructure | each entry is a private host |
| `Bash(<absolute drive path>/*)` | path of the source machine |
| Plugins binaries / cache | re-installed via `/plugin install` |

## Quick start

1. Install [Claude Code](https://docs.claude.com/claude-code/quickstart),
   sign in.
2. Copy the contents of `home-claude/` into `~/.claude/` (Windows:
   `C:\Users\<you>\.claude\`).
3. In a Claude chat:
   ```
   /plugin marketplace add anthropics/claude-plugins-official
   /plugin install superpowers
   /plugin install context7
   ```
4. Reload the window.

Full step-by-step (with Windows path examples and verification checks) in
[`INSTALL.md`](INSTALL.md).

## Customizing

- **Change the language.** Edit `settings.json` → `"language": "ru"` to
  whatever you prefer (or remove the key for English default).
- **Wire the hooks.** Copy the `hooks` block from
  `settings.example-with-hooks.json` into `settings.json`, then update the
  Python path and `<user>` placeholder for your machine.
- **Use the skill templates.** Both `code-review-external` and
  `personal-voice` need a couple of paths filled in before they're useful —
  see their `SKILL.md` files. They're written as patterns, not as drop-in
  utilities.

## Provenance

This bundle was extracted from a real working `~/.claude/` and an internal
meta-repo on one developer machine. The sanitization rules:

- No hostnames, IPs, domain names, or absolute drive paths from the source
- No real tokens or passwords (the source `env` block had several values
  — all stripped)
- No project-specific MCP servers — each entry pointed at a private host
- No hooks pointing at the source meta-repo's automation scripts (those
  are workflow plumbing for one specific setup, not general-purpose)
- Skills and the slash command come with personal paths replaced by
  `<placeholders>` and a "Setup" section explaining what you provide

If something still looks personal, please [open an issue](../../issues).

## License

[MIT](LICENSE).
