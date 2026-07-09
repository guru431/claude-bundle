# Ideas — claude-bundle
Forward-looking improvement proposals (enhancements, not bug reports). Bugs/defects
live in `FINDINGS.md`. Status: `proposed` → `accepted` / `done` / `wontfix`.

<!-- 2026-07-04: empty. All 7 proposals from the 2026-07-04 audit were
     implemented and removed; see CHANGELOG.md "[0.1.0] - 2026-07-04".
     Add new ideas below. -->
## 2026-07-08 · Preflight scripts for full/lite readiness
**Контекст:** adversarial audit, install/deployment UX.
**Идея:** добавить `scripts/preflight.ps1` / `scripts/preflight.sh`: Python real vs Store stub, Git Bash, `requests`/PyYAML, `.env`, mapped drive, existing `~/.claude`, Task Scheduler readiness.
**Статус:** proposed

## 2026-07-08 · Installer `-WhatIf` / `-DryRun`
**Контекст:** adversarial audit, install safety.
**Идея:** режим, который печатает что будет скопировано, что будет перезаписано, какие tasks будут registered/enabled и какие секреты/ключи отсутствуют.
**Статус:** proposed

## 2026-07-08 · Explicit automation consent matrix
**Контекст:** adversarial audit, public bundle defaults.
**Идея:** отдельная секция согласий для wiki compile, healthcheck to LLM, git auto-push, Telegram alerts, warm-window billing и любого фонового cron, который тратит деньги или публикует данные.
**Статус:** proposed

## 2026-07-08 · POSIX full end-to-end quickstart
**Контекст:** adversarial audit, macOS/Linux full tier.
**Идея:** описать полный путь: copy `wiki/cron`, create `.env`, generate units, install units, inspect logs; рядом явно перечислить unsupported Windows-only tasks.
**Статус:** proposed

## 2026-07-08 · Post-install open-items report
**Контекст:** adversarial audit, installer UX.
**Идея:** после install показывать точные unresolved items: `.env missing key`, `PROJECTS_ROOT unset`, `PROJECT_MAP empty`, `sync not run`, `N enabled tasks`, hook status.
**Статус:** proposed

## 2026-07-08 · Registry platform support guard in CI
**Контекст:** adversarial audit, POSIX generator drift.
**Идея:** добавить CI guard, который проверяет, что Windows-only scripts не попадают в POSIX generator без явного `platform`/allow flag.
**Статус:** proposed

## 2026-07-08 · Broaden `check-doc-counts.py`
**Контекст:** adversarial audit, docs consistency.
**Идея:** расширить guard: поддержать number words для total count и/или сверять имена задач из `registry.yaml` против task tables, а не только числа.
**Статус:** proposed

## 2026-07-08 · Run `self-test.ps1` in Windows CI
**Контекст:** adversarial audit, CI/local parity.
**Идея:** Windows job сейчас parse-check'ит `.ps1`; добавить `powershell -File scripts/self-test.ps1`, чтобы CI и локальная проверка не расходились.
**Статус:** proposed

## 2026-07-08 · Install contract matrix
**Контекст:** adversarial audit, README/INSTALL clarity.
**Идея:** одна компактная матрица: lite automated, lite manual, full automated, POSIX lite/full; что копируется, какие prerequisites нужны, какие проверки запускаются.
**Статус:** proposed

## 2026-07-08 · Universal-rules mirror sync-check
**Контекст:** adversarial audit, `home-claude/CLAUDE.md` ↔ `codex/AGENTS.md` drift.
**Идея:** добавить `scripts/check-agents-sync.py` или похожий guard по именованным universal blocks и включить его в self-test/CI.
**Статус:** proposed

## 2026-07-08 · End-to-end pipeline tests from raw sessions
**Контекст:** adversarial audit, wiki pipeline coverage.
**Идея:** добавить тест на `flush → compile → index → lint`, а не только готовый daily fixture, чтобы ловить дубли pending/JSONL и state transitions.
**Статус:** proposed

## 2026-07-08 · Malformed LLM output tests
**Контекст:** adversarial audit, LLM safety.
**Идея:** покрыть malformed JSON/rejected path: rejected path должен быть failure/quarantine, а не processed marker.
**Статус:** proposed

## 2026-07-08 · `cron/state/last_success.json`
**Контекст:** adversarial audit, observability.
**Идея:** писать last-success по фазам `flush`, `compile`, `build`, `lint`, чтобы monitor ловил stale pipeline даже при exit-code drift.
**Статус:** proposed

## 2026-07-08 · Rejected LLM raw-response quarantine
**Контекст:** adversarial audit, debugging data loss.
**Идея:** сохранять raw LLM responses для rejected/parse-failed случаев в `cron/logs/rejected/` с source id и причиной отказа.
**Статус:** proposed

## 2026-07-08 · Atomic `write_page()`
**Контекст:** adversarial audit, wiki write reliability.
**Идея:** сделать запись wiki pages через temp+replace, как `save_state()`, чтобы crash не оставлял полузаписанную страницу.
**Статус:** proposed
