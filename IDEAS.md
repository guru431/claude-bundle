# Ideas — claude-bundle
Forward-looking improvement proposals (enhancements, not bug reports). Bugs/defects
live in `FINDINGS.md`. Status: `proposed` → `accepted` / `done` / `wontfix`.

> Ревизия 2026-07-13. Осознанно не повторены ранее отклонённые review-inbox,
> fact-revocation/provenance, money-budget и memory-lifecycle proposals; ниже только
> новые возможности либо существенно иной scope.

## 2026-07-13 · I-01 — Единый privacy/DLP gateway [proposed]
**Ценность:** Одна обязательная граница для всех внешних LLM-вызовов устранит расхождение privacy semantics между JSONL, pending, plans, healthcheck, memory и handoff.
**Эскиз:** Schema-validated fail-closed policy; source project/collector/provider для каждого item; secret/PII/path redaction; preview с exact filenames, chars и blocked categories; unattributed data запрещено без explicit opt-in.

## 2026-07-13 · I-02 — Transactional wiki compiler [proposed]
**Ценность:** Исключит last-write-wins, cross-project writes и частичную фиксацию multipart output.
**Эскиз:** Сначала собрать все proposals, валидировать schema/namespace/link targets, coalesce по destination path на одном evolving state, затем atomically commit batch; audit journal хранит before/after hash и позволяет rollback технической транзакции без ручного review queue.

## 2026-07-13 · I-03 — Incremental transcript cursor [proposed]
**Ценность:** Long-lived sessions будут обрабатываться без пропуска дописанного хвоста и без повторной отправки всего файла, снижая стоимость.
**Эскиз:** Хранить file identity + byte offset + trailing partial-line hash; принимать только завершённые JSONL lines; безопасно распознавать truncate/rotate/session completion.

## 2026-07-13 · I-04 — Versioned state schema и `bundle state doctor` [proposed]
**Ценность:** Повреждённое/устаревшее состояние станет диагностируемым и мигрируемым, а status останется действительно read-only.
**Эскиз:** JSON Schema/Pydantic model, explicit version migrations, quarantine invalid state, integrity/reconciliation report и отдельные pure snapshot vs mutating repair modes.

## 2026-07-13 · I-05 — Schema-constrained provider layer [proposed]
**Ценность:** Транспортный HTTP success больше не будет маскировать malformed/семантически непригодный ответ.
**Эскиз:** Общие JSON schemas/structured output там, где provider поддерживает; одинаковый validator для всех adapters; раздельные статусы transport, parse, semantic validate и apply; provider error enum для 402/429/529.

## 2026-07-13 · I-06 — Namespace-aware wiki graph [proposed]
**Ценность:** Одинаковые названия тем смогут безопасно существовать в разных проектах, появятся настоящие backlinks и полезный orphan signal.
**Эскиз:** Canonical full-path IDs, scoped aliases только при уникальности, qualified links в indexes, backlink index и grace queue для явно anticipated targets.

## 2026-07-13 · I-07 — Deterministic health engine и dashboard v2 [proposed]
**Ценность:** Operational health будет зависеть от измеримых thresholds и semantic success, а не только от process rc или текста LLM.
**Эскиз:** Disk/memory/load/services thresholds, scheduler LastResult, heartbeat/backlog/rejected age, cap hits и provider depletion; LLM только объясняет verdict; CLI human/`--json` modes и корректные exit codes.

## 2026-07-13 · I-08 — Transactional installer/upgrader/uninstaller [proposed]
**Ценность:** Одинаковая безопасная модель для Windows, POSIX, manual и agent flow предотвратит потерю local config и half-installed state.
**Эскиз:** `plan → backup → staged copy → deployed self-test → atomic promote`; manifest изменённых/сохранённых файлов, rollback/uninstall и отдельные roots `$ClaudeHome`/`$PipelineRoot`.

## 2026-07-13 · I-09 — First-class local-only pipeline [proposed]
**Ценность:** Sensitive transcripts смогут обрабатываться без off-box egress, а privacy policy станет технически проверяемой.
**Эскиз:** Поддерживаемый local provider (например, OpenAI-compatible localhost), `offbox_fallback: false`, capability/preflight check и заметная маркировка local-only tasks/status.

## 2026-07-13 · I-10 — Object-based publication firewall [proposed]
**Ценность:** Один scanner защитит pre-commit, auto-push и GitHub mirror от истории, rename, pre-staged files и разных Git workflows.
**Эскиз:** Анализ destination paths и всех новых commits/blobs через Git plumbing; first push сканирует всю reachable history; optional gitleaks adapter; machine-readable evidence и explicit false-positive allowlist.

## 2026-07-13 · I-11 — Collision-free project identity [proposed]
**Ценность:** Privacy, state и memory перестанут зависеть от неоднозначного display slug.
**Эскиз:** Immutable raw/hashed cwd identity как policy key, отдельный человекочитаемый alias, collision report и migration tool для existing project folders/state.

## 2026-07-13 · I-12 — End-to-end fault-injection suite [proposed]
**Ценность:** Тесты начнут ловить реальные failure modes, которые прошли текущие 6 happy-path tests.
**Эскиз:** Snapshot/hash vault+state для каждого `--dry-run`; fixtures partial multipart, valid `[]`, wrong schema, 402, invalid UTF-8, growing JSONL, colliding slugs, cross-namespace path, failing commit/converter и concurrent heartbeat writers.

## 2026-07-13 · I-13 — Generated documentation contracts [proposed]
**Ценность:** Task counts, env variables, prerequisites и universal rules не будут расходиться между README, INSTALL, AGENTS и runtime.
**Эскиз:** Генерировать task/data/cost tables из registry/provider metadata, env reference из schema, AGENTS mirrors из canonical fragments; CI сравнивает generated output и executable mode bits.

## 2026-07-13 · I-14 — Scheduler compiler и `sync -Audit` [proposed]
**Ценность:** Cross-platform artifacts можно будет проверить без регистрации, а Windows name/SID drift станет видимым до mutation.
**Эскиз:** Registry schema validator; systemd/launchd/Task XML compilers; `systemd-analyze verify`, `plutil -lint`/XML parse; audit-only report unmanaged name collisions, principal/trigger/action drift и stale generated units.

## 2026-07-13 · I-15 — Session-scoped handoff protocol [proposed]
**Ценность:** Handoff перестанет зависеть от race и общего файла, а лишний внешний LLM-вызов можно будет убрать.
**Эскиз:** Artifact keyed by session id, atomic ready marker, bounded wait/expiry и использование уже сформированного compact summary; fallback никогда не читает handoff другой session.

## 2026-07-13 · I-16 — Budget-fair memory sampler [proposed]
**Ценность:** Каждый allowed project получит предсказуемую долю context, а silent truncation станет наблюдаемой.
**Эскиз:** Deterministic per-project quota, recent + representative message selection, truncation только на message boundaries и telemetry `omitted chars/messages/projects`.

## 2026-07-13 · I-17 — Optional local full-text retrieval index [proposed]
**Ценность:** Сохранит Markdown как source of truth, но закроет реальные misses grep по формам слов/синонимам без обязательной vector database.
**Эскиз:** Read-only SQLite FTS/BM25 index, rebuildable из vault, scoped search по project/KB и fallback на обычный grep; никакой write authority или скрытого canonical state.

## 2026-07-13 · I-18 — Retention tiers для чувствительных artifacts [proposed]
**Ценность:** Raw rejected payloads, handoffs и session-derived logs не будут жить дольше, чем нужно для диагностики.
**Эскиз:** Короткий TTL/encryption/redaction для raw payload, более долгий metadata-only hash ledger, per-class retention в manifest и dry-run cleanup report.

## 2026-07-13 · I-19 — Credential broker для backend switcher [proposed]
**Ценность:** Переключение backend перестанет помещать plaintext keys в project JSON и зависеть от Git ignore hygiene.
**Эскиз:** Windows Credential Manager/OS keychain или `apiKeyHelper`; settings хранит только provider metadata/reference, doctor проверяет доступность credential без печати значения.
