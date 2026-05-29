# Findings — claude-bundle
Побочные находки. Ревизия: MonthlyStratReview 1-го числа. Stale >90 дней → alert.

## 2026-05-29 · sync.cmd не self-elevate'ится (коммент врёт) [P2]
**Контекст:** Code review Opus 4.8 (CODE_REVIEW_opus48_2026-05-29.md), home-claude/cron/admin/sync.cmd:2
**Что:** Коммент утверждает, что sync-tasks.ps1 «handles its own elevation», но sync-tasks.ps1:46-53 НЕ self-elevate (нет -Verb RunAs), только IsInRole-проверка с exit 1. Двойной клик sync.cmd non-elevated → сразу падает.
**Предложение:** Добавить self-elevation в .cmd (Start-Process -Verb RunAs) или исправить коммент на «run from elevated shell».
**Статус:** open

## 2026-05-29 · tasklist.exe в кросс-платформенном bundle [P2]
**Контекст:** Code review Opus 4.8 (CODE_REVIEW_opus48_2026-05-29.md), home-claude/cron/git-push-all.sh:26
**Что:** WAIT_FOR_PATTERN использует tasklist.exe (Windows-only); на Linux/macOS фича молча no-op.
**Предложение:** Документировать как Windows-only или гард command -v tasklist.exe.
**Статус:** open

## 2026-05-29 · Широкие Bash-permissions в дефолтном settings.json [P3]
**Контекст:** Code review Opus 4.8 (CODE_REVIEW_opus48_2026-05-29.md), home-claude/settings.json:4-8
**Что:** Дефолт даёт bare "Bash" + Bash(for *)/Bash(do *)/Bash(powershell.exe *)/Bash(cmd.exe *) с пустым deny; copy-paste = «allow most shell». claude-switch.ps1 \$STANDARD_PERMISSIONS ещё шире (Bash(*)).
**Предложение:** Тише дефолтный allowlist + populated deny или коммент-предупреждение.
**Статус:** open

## 2026-05-29 · Стрей плейсхолдер permission [P3]
**Контекст:** Code review Opus 4.8 (CODE_REVIEW_opus48_2026-05-29.md), home-claude/settings.json:22
**Что:** "Bash(__NEW_LINE_*)" похоже на утёкший маркер санитайзера, инертен но сбивает с толку в публичном шаблоне.
**Предложение:** Удалить.
**Статус:** open

## 2026-05-29 · parse_llm_json рекурсия без guard [P3]
**Контекст:** Code review Opus 4.8 (CODE_REVIEW_opus48_2026-05-29.md), home-claude/cron/hooks/utils.py:735
**Что:** parse_llm_json (стр.718) вызывает llm_call для реформата плохого JSON; нет recursion-guard, но 50-iter cap + единичный реформат ограничивают.
**Предложение:** Приемлемо, отмечено для полноты.
**Статус:** open
