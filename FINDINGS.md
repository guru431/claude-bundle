# Findings
Побочные находки. Ревизия: MonthlyStratReview 1-го числа. Stale >90 дней → alert.

## 2026-06-07 · CLAUDE.md/AGENTS.md sync drift — claude-bundle [P3]
**Контекст:** еженедельный sync-check (`cron/agents-md-sync-check.py`)
**Что:** обнаружены расхождения между CLAUDE.md и AGENTS.md.
**Предложение:** свести руками либо принять расхождение как намеренное.
**Статус:** open

<details>
<summary>Диагностика от DeepSeek</summary>

### CRITICAL_MISSING_IN_AGENTS
- Нет упоминания `.githooks/pre-commit` — в CLAUDE.md это единственный файл, реализующий автоматизацию sanitization (denylist grep + generic key/token scan). AGENTS.md говорит «run the grep sanity check from `CLAUDE.md` § Sanitization checklist», но не упоминает, что hook уже существует и нужно лишь выполнить `git config core.hooksPath .githooks`. Агент может не найти hook и переписывать grep вручную каждый раз.

### OUTDATED_IN_AGENTS
*(нет)*

### CONTRADICTIONS
*(нет)*
</details>

