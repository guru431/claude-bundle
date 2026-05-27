# Changelog

## 2026-05-27 — initial public extract

Extracted as a standalone project from an internal bundle that had been
used to deploy Claude Code onto secondary machines.

### What was already there (from the internal bundle)
- Sanitized `home-claude/CLAUDE.md` — Karpathy rules, tool selection, Bash
  sandbox limits, file-encoding BOM rules, Superpowers workflow
- Sanitized `home-claude/settings.json` — permission allow-list, enabled
  plugins, language
- `INSTALL.md` (for a human) and `AGENT-INSTRUCTIONS.md` (for Claude Code
  to self-deploy)

### Added in this extract
- `home-claude/hooks/`
  - `block-iptables-save-to-rules.py` — PreToolUse, blocks a specific
    dangerous iptables pattern (incident-driven; sanitized of internal
    references)
  - `md2pdf-on-edit.py` — PostToolUse, keeps `<name>.pdf` in sync with
    `<name>.md` when a paired PDF exists
  - `README.md`
- `home-claude/skills/`
  - `code-review-external/SKILL.md` — template for running a
    second-opinion review through a different LLM
  - `personal-voice/SKILL.md` — template for writing text in the user's
    voice per register
  - `README.md`
- `home-claude/commands/`
  - `code-review-ext.md` — slash wrapper for the `code-review-external`
    skill
  - `README.md`
- `home-claude/settings.example-with-hooks.json` — reference settings.json
  with the two hooks wired in (for copy-paste)
- `LICENSE` (MIT)
- `.gitignore`
- Top-level `README.md` rewritten for a public audience

### Sanitization changes vs. internal version
- All internal hostnames, IPs, domain names, drive paths removed
- `env` block in `settings.json` cleared of real `ZABBIX_TOKEN`,
  `MIKROTIK_PASSWORD`, `ROUTEROS_PASSWORD`
- All `mcp__<internal-service>__*` permissions removed
- Hooks pointing at the internal meta-repo's `cron/hooks/` removed from
  the default `settings.json` (kept as a separate
  `settings.example-with-hooks.json` reference, with placeholders)
- Skills' references to internal voice corpora, an internal code-review
  script, and a specific LLM provider subscription replaced with
  `<placeholder>` paths and a "Setup" / "Adapt" section
- Incident references in `block-iptables-save-to-rules.py` REASON string
  generalized; specific dates / file paths removed
