# Findings — claude-bundle
Side observations collected during work. Review monthly. Stale >90 days → alert.

<!-- 2026-06-11: all 27 findings from the 2026-06-10 project analysis were
     fixed and removed; see CHANGELOG.md entry "2026-06-10 — Fable 5
     project-analysis: full fix batch" for the resolution details. -->

## 2026-06-14 · CLAUDE.md/AGENTS.md sync drift — claude-bundle [P3]
**Контекст:** еженедельный sync-check (`cron/agents-md-sync-check.py`)
**Что:** обнаружены расхождения между CLAUDE.md и AGENTS.md.
**Предложение:** свести руками либо принять расхождение как намеренное.
**Статус:** open

<details>
<summary>Диагностика от DeepSeek</summary>

### CRITICAL_MISSING_IN_AGENTS
- Отсутствует правило зеркалирования универсальных правил: при добавлении нового правила в `home-claude/CLAUDE.md`, если оно универсальное (file-ops, encoding, error recovery, findings, secrets, Task Scheduler), его необходимо также добавить в `codex/AGENTS.md`. В `CLAUDE.md` это явно указано в cross‑link таблице, но `AGENTS.md` не содержит даже ссылки на это требование — агент, изменяющий глобальные правила, может забыть обновить зеркало, что нарушит консистентность для Codex CLI.
</details>

