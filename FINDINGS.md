# Findings — claude-bundle
Побочные находки. Ревизия: MonthlyStratReview 1-го числа. Stale >90 дней → alert.

> Глубокий аудит 2026-07-13: Python/wiki pipeline, hooks, PowerShell/Bash, installer,
> scheduler generators, Git guards и все Markdown-документы. Базовый набор проверок
> зелёный (`pytest`: 6 passed; self-test: 14 passed, 1 warning), поэтому ниже отдельно
> отмечены сценарии, которых текущие тесты не покрывают.

## 2026-07-13 · F-01 — Privacy manifest работает fail-open [P1]
**Context:** `home-claude/cron/hooks/utils.py:36-79`, `scripts/self-test.ps1`; воспроизведено с невалидным YAML и Python без PyYAML.
**What:** Ошибка разбора, отсутствие PyYAML или неверный тип поля превращают manifest в пустой policy, а пустой `ALLOW_PROJECTS` означает «разрешить всё»; строковый `skip_projects` дополнительно превращается в набор символов. Self-test проверяет пример из source tree, а не фактически развёрнутый manifest, поэтому опасная конфигурация остаётся незамеченной.
**Proposal:** Если manifest существует, разбирать его fail-closed; ввести версионированную schema с проверкой типов/slug и отдельной явной опцией `allow_all: true`; self-test должен валидировать deployed manifest.
**Status:** open

## 2026-07-13 · F-02 — Единый project privacy gate обходится отдельными источниками [P1]
**Context:** `wiki-flush-sessions.py:171-218,245-256,490-514`, `precompact-handoff.py:37-56`; противоречит `CHANGELOG.md` v0.3.0 и `docs/cron-architecture.md` («EVERY source»).
**What:** `.pending` попадает в LLM без `project_allowed()`, глобальные plans теряют provenance и целиком относятся к `main`, а detached precompact handoff отправляет хвост transcript внешнему provider без policy gate. Исключённый проект поэтому всё равно может покинуть машину через три независимых пути.
**Proposal:** Привязать каждый outbound item к проверяемому source project; неизвестную provenance отклонять по умолчанию; применять один policy-enforcement слой непосредственно перед каждым LLM-вызовом.
**Status:** open

## 2026-07-13 · F-03 — Глобальный memory context повторно экспортирует исключённые данные [P1]
**Context:** `home-claude/cron/hooks/memory-update.py:58-65,156-181`, prompts для wiki/memory.
**What:** При memory update в prompt передаётся текущий глобальный `USER.md` (до 40 KB) вместе с allowed messages; он может уже содержать сведения из excluded projects. Prompts запрашивают точные пути, identifiers, server/IP/port и credentials-related факты, но перед внешним provider нет secret/path redaction, поэтому project allowlist не является DLP-границей.
**Proposal:** Разделить memory по scope/provenance, не подмешивать global memory в scoped extraction и добавить обязательный redaction/DLP pass с отчётом о вырезанных категориях.
**Status:** open

## 2026-07-13 · F-04 — «Безопасный первый запуск» фактически означает capture-all [P1]
**Context:** `config/bundle.local.example.yaml`, `INSTALL.md:289-303`, installer open-items check, `CHANGELOG.md` v0.3.0.
**What:** `allow_projects: []` означает ALL, а backlog по умолчанию равен 50; installer разрешает зарегистрировать задачи без явного подтверждения scope. Это расходится с выводом changelog о безопасном onboarding и исправленном capture-all.
**Proposal:** Сделать пустой allowlist равным NONE, требовать `allow_all: true` для глобального сбора, установить backlog default 0 и блокировать scheduler registration до явного privacy acknowledgement.
**Status:** open

## 2026-07-13 · F-05 — Canonical slug может объединить разные проекты и расширить allowlist [P1]
**Context:** `utils.py:282-306`, `memory-update.py:93-138`, `INSTALL.md:289-293`.
**What:** Fallback берёт последний `-`-segment encoded directory, поэтому разные cwd с одинаковым leaf получают один slug. Это может одновременно разрешить лишний проект через allowlist и привести к last-wins потере сообщений в `proj_messages`; документация, напротив, обещает distinct folders без `project_map`.
**Proposal:** Использовать устойчивый slug из полного encoded id либо требовать явный `project_map` при коллизии; doctor/dry-run должен строить many-to-one report и блокировать неоднозначный policy.
**Status:** open

## 2026-07-13 · F-06 — Частичный multipart flush финализирует весь источник [P1]
**Context:** `wiki-flush-sessions.py:290-340,535-555,595-614`; воспроизведён ответ `[failure, success]`.
**What:** Если успешна хотя бы одна часть, результат truthy и проект считается обработанным: все JSONL отмечаются processed, а pending удаляется, хотя содержание неуспешных частей потеряно. Один oversized input block также не режется жёстко и может породить prompt существенно выше лимита.
**Proposal:** Возвращать отдельно `{content, complete, failed_parts}`; отмечать только полностью подтверждённые ranges и hard-split каждый исходный блок до формирования prompt.
**Status:** open

## 2026-07-13 · F-07 — Дописанный хвост активного JSONL теряется навсегда [P1]
**Context:** `wiki-flush-sessions.py:107-151,611-614`; воспроизведено: flush → append durable fact → второй flush сообщает `Nothing to process`.
**What:** Dedup key содержит только `project/name`, без offset, size, hash или признака завершения; после первого прохода все последующие строки того же активного session file игнорируются. Это опровергает прежнее обоснование в `CHANGELOG.md`, что append-only JSONL само по себе исключает потерю pending-данных.
**Proposal:** Хранить file identity, byte cursor и trailing partial-line hash; читать только подтверждённые новые строки и корректно обрабатывать truncate/rotate/session completion.
**Status:** open

## 2026-07-13 · F-08 — Compile-sessions теряет изменения ранних chunks по схеме last-write-wins [P1]
**Context:** `wiki-compile-sessions.py:172-290`; воспроизведены два parts, меняющие один path.
**What:** Все chunks получают одно исходное состояние страницы, после чего их full-body changes применяются последовательно; последний chunk стирает факты предыдущего. Комментарий/changelog о сохранении успешных частей для этого сценария неверен.
**Proposal:** Coalesce changes по canonical destination и применять их к последовательно обновляемому in-memory state; коммитить страницу один раз после валидации всех parts.
**Status:** open

## 2026-07-13 · F-09 — Compile-KB может уничтожить существующую страницу через `action:create` [P1]
**Context:** `wiki-compile-kb.py:110-117,164-176`; воспроизведено исчезновение существующего durable fact.
**What:** Модель видит имена, но не body существующих страниц, а любое действие кроме `append` переписывает body. Ошибочный `create` на уже существующий path поэтому является разрушительным replace.
**Proposal:** Для существующего KB target принудительно использовать append/merge; replace разрешать только отдельным валидированным действием с current-body context и rollback copy.
**Status:** open

## 2026-07-13 · F-10 — Session compiler может писать в чужой project и глобальный KB [P1]
**Context:** `wiki-compile-sessions.py:251-296`, `utils.py:1043-1078`; воспроизведены записи из проекта `allowed` в `projects/other/...` и `kb/...`.
**What:** Общий path normalizer ограничивает лишь корнями `projects|kb`, но compiler не проверяет prefix текущего проекта. Ошибка модели или prompt injection может перезаписать чужую/глобальную страницу, а activity log при этом приписывает действие исходному проекту.
**Proposal:** Вводить caller-specific allowed root (`projects/{current_project}/`) и quarantine любого mismatch до файловой записи; для KB compiler аналогично разрешать только `kb/`.
**Status:** open

## 2026-07-13 · F-11 — Secret guard публичного репозитория имеет три обхода [P1]
**Context:** `.githooks/pre-commit:14-18`, `github-push.sh:60-91`, Git mode bits; подтверждены temp-repo сценарии add→remove в двух commits и `git mv safe.txt .env`.
**What:** Push guard проверяет только net diff `base..HEAD`, поэтому секрет, добавленный и удалённый разными outgoing commits, остаётся в публикуемой истории; sensitive filenames проверяются только с `--diff-filter=A`, поэтому rename/copy обходят gate. Сам pre-commit хранится mode `100644` и игнорируется Git на POSIX как non-executable, хотя документация объявляет его активным после `core.hooksPath`.
**Proposal:** Сделать hook и запускаемые `.sh` executable; сканировать destination paths для ACMR и каждый outgoing commit/blob/history object, а не endpoint diff; добавить executable integration tests обоих обходов.
**Status:** open

## 2026-07-13 · F-42 — Manual/POSIX installers перезаписывают пользовательское состояние [P1]
**Context:** `README.md:186-194`, `INSTALL.md:75-86,198-203,439-444`, `AGENT-INSTRUCTIONS.md:131-139`, `scripts/install-lite.sh:21-26`.
**What:** Несколько manual snippets и POSIX lite installer используют force-copy без backup/confirm; существующие `CLAUDE.md`, `settings.json` и `.env` могут быть уничтожены при повторном запуске. Автоматический Windows installer уже умеет backup/preserve, поэтому документация создаёт две несовместимые модели upgrade safety.
**Proposal:** Объявить такие snippets строго fresh-only либо переиспользовать transactional backup/merge поведение основного installer; POSIX path должен иметь те же safety guarantees.
**Status:** open

## 2026-07-13 · F-43 — Backend switcher не гарантирует, что файл с API keys исключён из Git [P1]
**Context:** `scripts/claude-switch.ps1:232-235,328-474`, `docs/llm-routing.md:122`, project `.claude/settings.local.json`.
**What:** Switcher пишет реальные keys в project-local JSON, но не проверяет `git check-ignore` и не обнаруживает уже tracked file; ссылка docs только на безопасность `.env` к этому файлу не относится. Даже если Claude Code обычно настраивает ignore для local settings, tracked/вручную созданный файл остаётся реальным путём утечки.
**Proposal:** До записи проверять `git ls-files`/`git check-ignore`, hard-fail на tracked file и добавлять exact path в `.git/info/exclude`; предпочтительно хранить secrets через OS credential store.
**Status:** open

## 2026-07-13 · F-44 — `sync.cmd` допускает command injection до PowerShell/UAC [P1]
**Context:** `home-claude/cron/admin/sync.cmd:12-31`; воспроизведён аргумент с `&`, опровергающий rationale `CHANGELOG.md` про безопасность `PowerShell -File`.
**What:** `%*` сначала разворачивается в batch `echo`, затем `%PASSARGS%` вставляется в elevated command line; metacharacters интерпретирует `cmd.exe` до запуска PowerShell. Predictable `%TEMP%\sync-tasks-args.txt` дополнительно создаёт TOCTOU окно перед elevation.
**Proposal:** Выполнять self-elevation из PowerShell со структурированным argument array и без повторного cmd parsing; если нужен transport file — unique ACL-restricted file плюс строгая deserialize/allowlist validation.
**Status:** open

## 2026-07-13 · F-45 — Persistent prompt injection не отделена от trusted instructions [P1]
**Context:** raw transcripts/external KB/existing pages в `wiki-*.md` prompts, `session-start.py:72-99`, permissive `home-claude/settings.json:5-22`.
**What:** Недоверенный текст вставляется в LLM prompts без явной instruction boundary, сохраняется как wiki, а затем снова инъецируется в agent context. В сочетании с широкими default allow rules для PowerShell/cmd/python/curl это создаёт persistent путь от содержимого источника к высокопривилегированной следующей сессии.
**Proposal:** Обрамлять untrusted data отдельными typed delimiters, явно запрещать исполнение embedded instructions, сканировать/quarantine подозрительный output и сузить default permissions до least privilege.
**Status:** open

## 2026-07-13 · F-46 — Уже staged sensitive file переживает auto-commit exclusion [P1]
**Context:** `git-push-all.sh:182-200`; изолированно воспроизведено для заранее staged `.env`.
**What:** Отрицательный pathspec в последующем `git add --all` не снимает файл, который пользователь уже добавил в index. Если value не совпадает с token regex, auto-commit способен включить sensitive path, несмотря на объявленное исключение.
**Proposal:** Перед commit hard-fail при любом cached sensitive path (включая ACMR destination), не пытаясь молча unstage; покрыть pre-staged fixtures.
**Status:** open

## 2026-07-13 · F-12 — Частично rejected LLM batch считается полностью обработанным [P2]
**Context:** `wiki-compile-sessions.py` и `wiki-compile-kb.py` main/apply paths.
**What:** Если один change применён, а соседний отвергнут validator-ом, source/pair всё равно финализируется; отвергнутая часть не quarantined и не retry. Это отдельный случай от ранее осознанно принятого поведения для all-rejected batch.
**Proposal:** Возвращать `{applied, rejected}` и не финализировать source при `rejected > 0`, либо вести детерминированный rejection ledger с явным terminal status.
**Status:** open

## 2026-07-13 · F-13 — Flush сигнализирует успех при полном/частичном LLM failure [P2]
**Context:** `wiki-flush-sessions.py:519-630`; воспроизведено без доступного provider.
**What:** Даже при провале всех projects процесс возвращает 0 и обновляет `last_success`, тогда как чистый no-op выходит до heartbeat. Scheduler и `wiki-pipeline` получают ложный green status именно на сбое и stale status при здоровом отсутствии работы.
**Proposal:** Возвращать non-zero при любом незавершённом project; heartbeat определять как «фаза успешно запущена», обновляя его на clean no-op, но не на semantic failure.
**Status:** open

## 2026-07-13 · F-14 — Валидный ответ `[]` компилятора превращается в бесконечную ошибку [P2]
**Context:** `prompts/wiki-compile-sessions.md:52-53`, `wiki-compile-sessions.py:239-242,399-404`.
**What:** Prompt разрешает `[]` как корректный no-change, но `not result` смешивает его с parse/transport failure; ветка успешных `0 changes` оказывается недостижимой, source повторяется и run возвращает 1.
**Proposal:** Использовать `None` только для failure, а пустой list считать complete semantic success.
**Status:** open

## 2026-07-13 · F-15 — Исправление corrupt UTF-8 неполное [P2]
**Context:** `wiki-compile-sessions.py:128-135`, `wiki-compile-kb.py:95`, `utils.py:827-832`; подтверждён `UnicodeDecodeError` на apply.
**What:** Preview sessions читает с `errors=replace`, но apply повторно вызывает strict `read_page`; KB падает ещё на initial read. Вывод `CHANGELOG.md`, что corrupt page больше не может прервать run, не соответствует текущей реализации.
**Proposal:** Централизовать tolerant read с явным warning/quarantine и использовать одинаковую стратегию на preview и apply; добавить invalid-byte fixture.
**Status:** open

## 2026-07-13 · F-16 — OpenCode HTTP 402 не включает circuit breaker [P2]
**Context:** `utils.py:1437-1449`; воспроизведены два последовательных HTTP-запроса после первого 402.
**What:** Depletion отмечается для 429/529, но generic OpenCode 402 возвращает `None` без `depleted=True`; дорогой/бесполезный provider вызывается снова. Общий вывод changelog «402 помечает provider depleted» верен не для всех adapters.
**Proposal:** Нормализовать provider errors в единый enum и обрабатывать 402 одинаково во всех adapters; проверить circuit-breaker contract parameterized tests.
**Status:** open

## 2026-07-13 · F-17 — Объявленные non-mutating режимы меняют vault/state [P2]
**Context:** `wiki-pipeline.py:78-93`, `wiki-build-index.py:216-225`, `bundle-status.py:5-8,82`, `utils.py:123-127`; оба сценария воспроизведены.
**What:** `wiki-pipeline.py --dry-run` всё равно переписывает indexes/heartbeat, потому что build-index игнорирует flag; read-only `bundle-status.py` может создать `.processed.json` при legacy migration. Это нарушает два явных пользовательских контракта.
**Proposal:** Протянуть dry-run до каждой mutating функции и добавить pure `peek_state(persist_migration=False)`; regression test должен сравнивать hash всего vault/state до и после.
**Status:** open

## 2026-07-13 · F-18 — State/heartbeat persistence не имеет schema и безопасной конкуренции [P2]
**Context:** `utils.py:113-127,153+`.
**What:** Синтаксически валидные `[]`, string или неверные bucket types проходят loader, а затем ломают `.get`/set operations. `last_success.json` обновляется read-modify-write через общий `.tmp` без lock, поэтому параллельные tasks могут потерять чужую phase entry; ошибки записи проглатываются.
**Proposal:** Ввести versioned state schema/migrations, quarantine invalid state и atomic write через unique temp под общим lock; добавить concurrent writer test.
**Status:** open

## 2026-07-13 · F-19 — Часть Python tasks игнорирует bundle `.env` [P2]
**Context:** `_run-hidden.vbs:49-79`, `md2pdf-sync.py:42-50`, `log-retention.py:18-20`, общий dotenv loader в `utils.py`.
**What:** Runner читает из `.env` только executable paths, а эти два scripts обращаются прямо к process environment. В Password-mode настройки `PROJECTS_ROOT` и `WIKI_LOG_RETENTION_DAYS`, которые INSTALL/template предлагают хранить в bundle `.env`, фактически не действуют.
**Proposal:** Загружать `.env` единым bootstrap до объявления module constants во всех scheduled Python entrypoints; покрыть Password-mode subprocess test.
**Status:** open

## 2026-07-13 · F-20 — md2pdf скрывает ошибки converter-а [P2]
**Context:** `md2pdf-sync.py:97-141`; воспроизведён converter exit 7.
**What:** Script считает failed conversions и пытается alert, но безусловно возвращает 0. Task Scheduler и monitor поэтому не отличают частичный/полный провал от успеха.
**Proposal:** После обработки всех файлов возвращать 1, если `failed > 0`, сохранив best-effort продолжение и alert.
**Status:** open

## 2026-07-13 · F-21 — Full preflight предлагает неверный dependency file и разрешает нерабочую установку [P2]
**Context:** `install.ps1:88-90`, `requirements-dev.txt`, `requirements.txt`, `utils.py` imports `requests`.
**What:** Installer проверяет `requests,yaml`, но советует `pip install -r requirements-dev.txt`, где находится только pytest; затем Full install продолжает регистрацию tasks после warning. Без `requests` provider code может упасть ещё до graceful fallback.
**Proposal:** Ссылаться на `requirements.txt`, сделать runtime dependencies blocking preflight для Full profile и добавить clean-venv installation test.
**Status:** open

## 2026-07-13 · F-22 — Ручной Full deployment использует не тот registry и не создаёт manifest [P2]
**Context:** `INSTALL.md:198-203,247-255,289-310`, `AGENT-INSTRUCTIONS.md:131-214`.
**What:** Инструкции предлагают редактировать deployed `$dst\cron\registry.yaml`, но запускают `sync.cmd` из source checkout; deployed `bundle.local.yaml` объявлен «скопированным», хотя шага копирования нет. Manual path также безусловно перезаписывает `.env`, создавая риск потери local configuration при повторном запуске.
**Proposal:** Запускать `$dst\cron\admin\sync.cmd`, создавать manifest/.env только если отсутствуют, а upgrade выполнять через backup + merge; проверить документацию copy-paste integration test.
**Status:** open

## 2026-07-13 · F-23 — self-test проверяет не тот deployment при custom path [P2]
**Context:** `scripts/self-test.ps1:72-77,212`, installer `-InstallPath` flow.
**What:** Version marker всегда читается из `$USERPROFILE\.claude`, а `PROJECTS_ROOT` проверяется относительно source `$root`, не deployed `$home_claude`; actual deployed manifest также не проходит schema validation. Custom install может получить зелёный отчёт для другой копии.
**Proposal:** Все deployment checks выводить из одного resolved `InstallPath`, печатать проверяемые absolute paths и тестировать default/custom layouts.
**Status:** open

## 2026-07-13 · F-24 — POSIX scheduler generator ломает пути и теряет arguments [P2]
**Context:** `scripts/gen-scheduler.py:82-89,124-129,171-195`; воспроизведён path `/opt/Claude & Team`.
**What:** systemd `ExecStart` строится простым join без корректного escaping, launchd XML генерируется строками без XML escaping и становится invalid на `&`; registry `script_args` вообще игнорируются. Пути с spaces/special chars дают неработающие units/plists.
**Proposal:** Использовать systemd-specific argument escaping (включая `%`), строить plist через `plistlib`, добавлять parsed `script_args` и валидировать artifacts парсером в tests.
**Status:** open

## 2026-07-13 · F-25 — Scheduler sync может присвоить чужую task и не замечает смену user [P2]
**Context:** `sync-tasks.ps1:10-11,362,474-581`, `docs/cron-architecture.md` ownership contract.
**What:** Наличие registry marker в текущей task не проверяется: `Register-ScheduledTask -Force` заменит unmanaged task с тем же именем вопреки обещанию «outside registry left alone». Desired-state comparison сохраняет `user`, но не включает его в change decision, поэтому смена principal может остаться неприменённой.
**Proposal:** На name collision без ownership marker выдавать error/skip до explicit adopt/force; добавить user principal в diff и integration tests ownership transitions.
**Status:** open

## 2026-07-13 · F-26 — Git sweep игнорирует ошибку commit и может завершиться зелёным [P2]
**Context:** `git-push-all.sh:198-199`, wiki twin `:243-244`.
**What:** Return code `git_commit` не проверяется и `set -e` нет; rejected hook, missing identity или другая commit error всё равно логируется как `auto-committed changes`, после чего sweep продолжает fetch/push старого HEAD и способен вернуть 0.
**Proposal:** Обернуть commit в явный `if`, увеличить failure counter, отправить alert и не выполнять push этого repo; добавить harness с намеренно падающим hook.
**Status:** open

## 2026-07-13 · F-47 — `git-push-all --dry-run` меняет настоящий staging area [P2]
**Context:** `git-push-all.sh:26-33,182-200`; ранее отклонённый finding повторно проверен по текущему control flow.
**What:** Dry-run выполняет реальный `git add --all`, затем `git reset HEAD`; этим он снимает пользовательский staging, существовавший до запуска. Название режима обещает read-only, но результатом является потеря подготовленного index state.
**Proposal:** Использовать temporary `GIT_INDEX_FILE` либо вычислять candidate diff через read-only plumbing; integration test должен заранее staged разные файлы и подтвердить byte-identical index после run.
**Status:** open

## 2026-07-13 · F-48 — Secret scanner блокирует удаление уже утёкшего секрета [P2]
**Context:** `secret-scan.sh:22-34`, `.githooks/pre-commit:26-60`.
**What:** Проверяется полный unified diff, включая removed lines; commit, который удаляет token, сам распознаётся как новая утечка и блокируется. Это затрудняет remediation и смешивает разные задачи staged-content и outbound-history scanning.
**Proposal:** В pre-commit проверять added lines/new blobs, а outgoing guard — все новые commit blobs; deletion должна проходить при отсутствии секрета в новом состоянии.
**Status:** open

## 2026-07-13 · F-49 — Installer принимает асинхронный scheduler sync за успех [P2]
**Context:** `install.ps1:280-293`, self-elevating `sync.cmd`.
**What:** Installer запускает syncer и сразу выставляет `$syncRan = $true`; elevated process не ожидается, поэтому UAC cancel/registration error не отражаются, а self-test способен начаться параллельно. Completion report не подтверждает зарегистрированное состояние.
**Proposal:** Запускать elevation с `-Wait -PassThru`, передавать реальный exit/result обратно installer-у и только затем запускать deployment self-test.
**Status:** open

## 2026-07-13 · F-50 — Backend switcher удаляет чужие environment settings [P2]
**Context:** `claude-switch.ps1:271-285` (`Set-Env`/`Clear-Env`).
**What:** Switcher заменяет весь `env` object, а clear удаляет его целиком; project-local переменные, которыми script не владеет, теряются при переключении backend. Это нарушает surgical ownership конфигурации.
**Proposal:** Изменять/удалять только документированный allowlist ключей switcher-а, сохраняя остальные properties и исходный order/encoding.
**Status:** open

## 2026-07-13 · F-51 — Healthcheck не собирает и не алертит заявленные anomalies [P2]
**Context:** `healthcheck.md:1-13`, `claude-healthcheck.sh:41-47,106-123`, `registry.yaml:159-162`.
**What:** Default local collector передаёт лишь uname/disk, хотя prompt/registry обещают load, memory, swap, runaway processes и services. Результат LLM только пишется в log; даже `URGENT` analysis не вызывает Telegram alert — уведомляется лишь отказ самого LLM.
**Proposal:** Собирать заявленные metrics и вычислять severity детерминированными thresholds; LLM использовать только для explanation, а alert привязать к machine verdict.
**Status:** open

## 2026-07-13 · F-52 — Claude cron mode игнорирует настроенный executable path [P2]
**Context:** `utils.py:1487-1506`, `CLAUDE_BIN` в env template/routing docs, Password-mode session 0.
**What:** `_llm_claude()` запускает bare `claude`, хотя bundle уже имеет `CLAUDE_BIN`; в scheduler PATH executable может отсутствовать. Вывод changelog о недостижимости этой ветки устарел: текущие инструкции прямо разрешают `WIKI_LLM_PROVIDER=claude`.
**Proposal:** Разрешать executable через `CLAUDE_BIN`, проверять path/версию в installer и doctor, а отсутствие считать явным provider-unavailable status.
**Status:** open

## 2026-07-13 · F-53 — Lite hook example включает Full-only lifecycle hooks [P2]
**Context:** `home-claude/hooks/README.md:1-5`, `INSTALL.md:98-104`, `settings.example-with-hooks.json:55-81`.
**What:** Lite user предлагается скопировать hooks block, в котором кроме двух Tier-1 hooks находятся SessionStart/SessionEnd/PreCompact, зависящие от отсутствующего `cron/`. Reference wiring тем самым смешивает tiers вопреки основному contract репозитория.
**Proposal:** Выпустить отдельные Lite и Full examples либо дать точные минимальные JSON fragments с dependency checks.
**Status:** open

## 2026-07-13 · F-54 — Semantic AGENTS mirror check не проверяет семантику [P2]
**Context:** `scripts/check-agents-sync.py:2-32`, `home-claude/CLAUDE.md`, `codex/AGENTS.md`.
**What:** Check подтверждает лишь наличие пяти headings и пропускает изменение текста/часть universal blocks; уже есть drift в strong success criteria, а Error Recovery/File Encoding не сравниваются как полноценные sections. Название и docs создают ложное ощущение синхронности mirrors.
**Proposal:** Генерировать оба файла из canonical fragments либо сравнивать normalized section hashes для полного списка universal blocks.
**Status:** open

## 2026-07-13 · F-55 — Lock timeout возвращает именно ту race, которую lock должен исключить [P2]
**Context:** `utils.py:182-203`, rationale `CHANGELOG.md:278-280`.
**What:** После timeout mutation намеренно продолжается unlocked; при реально зависшем/долгом writer это снова допускает lost update. Аргумент «лучше не пропустить run» конфликтует с документированной self-healing моделью, где missed phase безопасно подхватывается позже.
**Proposal:** При timeout завершать phase retriable failure/skip, не выполнять state write без ownership; stale-lock recovery отделить от обычного contention.
**Status:** open

## 2026-07-13 · F-56 — Personal-voice workflow не учитывает privacy третьих лиц [P2]
**Context:** `home-claude/skills/personal-voice/SKILL.md:91-117`.
**What:** Setup предлагает отправить экспорт sent email/chat в long-context LLM, но не требует consent, redaction, retention review или local-only processing; предупреждение относится только к публикации готового profile. Corpus содержит данные собеседников, не только пользователя.
**Proposal:** Добавить consent/redaction checklist, minimization/retention policy и local-processing режим по умолчанию; off-box upload требовать явного подтверждения.
**Status:** open

## 2026-07-13 · F-57 — Документирован неверный Claude MCP config path [P2]
**Context:** `codex/AGENTS.md:168-169`; [Claude MCP scopes](https://code.claude.com/docs/en/mcp), [Codex config reference](https://developers.openai.com/codex/config-reference).
**What:** Файл `~/.claude/.mcp.json` указан как user config и как прямой shared source с Codex. Актуальный Claude user/local scope хранится в `~/.claude.json`, project scope — в `.mcp.json`; Codex использует `~/.codex/config.toml` и собственный TOML schema, поэтому один файл нельзя просто объявить общим для двух clients.
**Proposal:** Документировать отдельные target formats и дать generator/sync command из одного neutral server manifest вместо несуществующего shared path.
**Status:** open

## 2026-07-13 · F-27 — Memory sampling теряет проекты и режет сообщения недетерминированно [P2]
**Context:** `memory-update.py:93-138` (`joined[:8000]`, aggregate `text[:40000]`).
**What:** При slug collision данные одного directory заменяют другой, а caps заполняются в filesystem iteration order; поздние проекты могут исчезнуть целиком, строки режутся посередине сообщения без telemetry. Это не прежний ложный finding о context overflow: лимит работает, но распределение и наблюдаемость неверны.
**Proposal:** Сначала merge sources по slug, затем использовать deterministic per-project quota и message-boundary truncation; логировать omitted chars/projects.
**Status:** open

## 2026-07-13 · F-28 — Malformed memory output считается успехом [P2]
**Context:** `memory-update.py:186-194`; воспроизведено `not-json-at-all → True`.
**What:** Отсутствующий/неразбираемый JSON возвращает success, поэтому retry/alert не происходит; неверные JSON types дополнительно могут упасть на `.get/.strip`. Provider transport success ошибочно приравнен к semantic success.
**Proposal:** Валидировать response schema и типы до apply, quarantine malformed response, возвращать non-zero и различать transport/parse/semantic status.
**Status:** open

## 2026-07-13 · F-29 — Cross-notes opt-in не использует содержимое sentinel [P2]
**Context:** `memory-update.py:209-217` и cross-notes response handling.
**What:** Достаточно существования `scan_<date>.json`; его content/schema не читаются, а extraction всё равно строится из raw messages. Если `links` приходит string вместо list, code способен обработать его посимвольно.
**Proposal:** Либо потреблять и валидировать scan result как единственный input, либо заменить sentinel явным config flag; schema-check `links` до записи.
**Status:** open

## 2026-07-13 · F-30 — Detached handoff имеет race и смешивает параллельные sessions [P2]
**Context:** `pre-compact.py:20-52`, `session-start.py:48-75`, общий `memory/handoff.md`.
**What:** PreCompact запускает LLM detached с timeout до 120 s и сразу возвращает, а следующий SessionStart читает один общий файл; обычно новый handoff ещё не готов, поэтому читается none/предыдущий. Параллельные sessions также перезаписывают и потребляют данные друг друга.
**Proposal:** Коррелировать artifact по session id, писать atomically с ready marker и определённым lifecycle; предпочтительно использовать уже доступный compact summary без второго внешнего LLM.
**Status:** open

## 2026-07-13 · F-31 — Source history/provenance заявлены, но pipeline их не использует [P2]
**Context:** `wiki-flush-sessions.py:625-627`, `wiki-compile-kb.py:64-95,181-186`, `docs/wiki-method.md`.
**What:** `history.jsonl` объявлен Source D для daily, но влияет только на строку count в log; KB dedup хранит лишь path, поэтому изменённый in-place source больше никогда не обрабатывается. Документы обещают source hash/mtime и two-way dedup, тогда как compilers записывают только source path, а hash helpers фактически dead.
**Proposal:** Либо скорректировать спецификацию, либо хранить source revision/hash/mtime и reprocess changed content; history включать только как документированное sanitized metadata.
**Status:** open

## 2026-07-13 · F-32 — Wikilink contract внутренне противоречив и не namespace-aware [P2]
**Context:** `prompts/wiki-flush-sessions.md:36-37`, `prompts/wiki-compile-sessions.md:28-29`, `wiki-lint.py:64-93,289-293`, `wiki-build-index.py:106-119,165-180`.
**What:** Prompts требуют real или anticipated links, но lint делает любой unresolved target hard error. Parser отбрасывает path и сравнивает только stem, поэтому `[[projects/a/foo]]` может ложно пройти благодаря `projects/b/foo.md`; generated indexes сами создают bare-stem links и наращивают ambiguity.
**Proposal:** Сначала разрешать canonical full paths и scoped aliases, bare alias принимать только при уникальности; anticipated targets сделать WARN с grace marker либо запретить их prompt-ом.
**Status:** open

## 2026-07-13 · F-33 — Rejected payloads обходят log retention [P2]
**Context:** `utils.quarantine_raw`, `cron/logs/rejected/*.txt`, `log-retention.py`.
**What:** Quarantine может содержать echoed private session text/LLM response, но retention чистит только top-level `*.log|*.jsonl`; rejected payloads копятся бессрочно. Это нарушает ожидаемую ограниченность хранения чувствительных operational logs.
**Proposal:** Ввести отдельный короткий TTL, recursive cleanup и redaction/encryption для payload; metadata/hash можно хранить дольше без raw content.
**Status:** open

## 2026-07-13 · F-34 — Data matrix healthcheck скрывает передаваемую инфраструктурную телеметрию [P2]
**Context:** `claude-healthcheck.sh:41-109`, `docs/cron-architecture.md` data matrix, `config/llm-providers.example.env`.
**What:** Prompt включает local uname/disk и при настройке remote Linux/Windows — host metrics, тогда как matrix указывает лишь «prompt → provider». Скрипт просит `REMOTE_SSH_HOST`/`WIN_REMOTE_HOST`, но эти переменные отсутствуют в canonical env template.
**Proposal:** Полностью описать egress fields/hosts/provider и consent; добавить env variables в template с пустыми значениями и privacy preview.
**Status:** open

## 2026-07-13 · F-35 — Документы всё ещё советуют редактировать пустые code constants [P2]
**Context:** `utils.py:5`, `docs/wiki-method.md:115-118`, `home-claude/wiki/index.md:35-44`, `README.md:265`, `INSTALL.md:382`, `AGENT-INSTRUCTIONS.md:286-287`, `AGENTS.md:44-46`.
**What:** После миграции на `bundle.local.yaml` ряд актуальных документов продолжает отправлять пользователя в `PROJECT_MAP`/`KNOWN_PROJECTS`, хотя cardinal rule требует держать их пустыми. Такие изменения будут потеряны при reinstall и расходятся с текущей архитектурой config separation.
**Proposal:** Оставить исторические changelog записи, но во всех current docs/code docstrings ссылаться только на deployed manifest; добавить drift check на запрещённые текущие инструкции.
**Status:** open

## 2026-07-13 · F-36 — Wiki lint обещает проверки, которые не дают полезного сигнала [P3]
**Context:** `wiki-lint.py`, generated indexes, описания lint в README/docs.
**What:** Orphan scan учитывает generated indexes, которые уже ссылаются почти на каждую страницу, поэтому после build-index проверка практически всегда clean. Документы также обещают контроль обязательного frontmatter, но соответствующей полной проверки нет.
**Proposal:** Исключить generated indexes из inbound-link evidence, отдельно валидировать обязательные frontmatter fields/types и добавить negative fixtures.
**Status:** open

## 2026-07-13 · F-37 — Абсолютные выводы wiki-method не выдерживают собственной модели [P3]
**Context:** `docs/wiki-method.md` утверждения «No retrieval misses» и «wikilinks stable forever», а также последующий trade-off section.
**What:** Grep не находит синонимы/семантически близкие формулировки, а wikilinks ломаются при rename и неоднозначных одинаковых stems; позже документ сам признаёт часть этих trade-offs. Это не code bug, но выводы создают завышенные гарантии и внутренне противоречат разделу ограничений.
**Proposal:** Переформулировать как осознанный local-first trade-off, описать реальные failure modes и optional full-text/semantic read-only index.
**Status:** open

## 2026-07-13 · F-38 — Path-depth contract расходится с normalizer [P3]
**Context:** `docs/wiki-method.md`, compile prompts, `utils.py:1056-1061`.
**What:** Документы/prompts требуют paths ровно трёх уровней и подразумевают rejection, но implementation молча flatten-ит deeper path. Оба варианта допустимы, однако текущая смесь скрывает model error и делает итоговый path неожиданным.
**Proposal:** Выбрать один контракт: quarantine/reject deeper paths либо явно документировать deterministic normalization и показывать его в activity log.
**Status:** open

## 2026-07-13 · F-39 — `bundle-status` может показать provider green при невозможном routing [P3]
**Context:** `bundle-status.py` provider/key check.
**What:** В режиме `opencode` наличие только DeepSeek key удовлетворяет общей проверке, хотя этот routing не имеет DeepSeek fallback. Пользователь получает ложноположительный status до первого реального вызова.
**Proposal:** Проверять credentials/capability matrix для выбранного provider chain и выполнять optional zero-data synthetic probe.
**Status:** open

## 2026-07-13 · F-40 — Custom InstallPath для lite профиля создаёт неиспользуемую конфигурацию [P3]
**Context:** `install.ps1` usage для `-Profile lite -InstallPath`, `scripts/install-lite.sh:14-19`, `INSTALL.md:428`; [Claude environment reference](https://code.claude.com/docs/en/env-vars).
**What:** Windows CLI рекламирует произвольный InstallPath, но лишь предупреждает и сообщает completion для location, которое клиент не читает. POSIX variant аналогично рекламирует собственный `CLAUDE_HOME`, тогда как поддерживаемый переключатель config root называется `CLAUDE_CONFIG_DIR`.
**Proposal:** Для lite использовать/экспортировать `CLAUDE_CONFIG_DIR` либо запретить custom path; отдельный неактивный staging вынести в явно названный export mode.
**Status:** open

## 2026-07-13 · F-41 — Hooks README содержит невыполнимый bootstrap совет [P3]
**Context:** `home-claude/hooks/README.md`, hook command examples.
**What:** Документ советует оставить `<python-exe>` в command и задать `CLAUDE_HOOK_PYTHON`, но environment может быть прочитан только уже запущенным Python process. Там же missing converter назван silent no-op, хотя implementation возвращает `systemMessage`.
**Proposal:** Показывать реальный launcher/wrapper или абсолютный Python path и синхронизировать описанное observable behavior с кодом.
**Status:** open

## 2026-07-13 · F-58 — Wiki architecture описана как единый pipeline, которого нет [P3]
**Context:** `docs/wiki-method.md:33-89`, compile prompts, `wiki-pipeline.py`.
**What:** KB compiler назван третьей фазой общего four-phase pipeline, хотя он independent/off-by-default и отсутствует в orchestrator; обещанные backlinks не вычисляются, а правило `sources` не применимо к indexes/logs. Примеры «strict JSON» дополнительно содержат невалидные union expressions вроде `"create" | "update"`.
**Proposal:** Нарисовать две реальные tracks (session ingestion и optional KB), отдельно maintenance phases; сузить page invariants и показывать валидные JSON examples.
**Status:** open

## 2026-07-13 · F-59 — Prerequisites и профиль установки описаны противоречиво [P3]
**Context:** `README.md:19`, `INSTALL.md:33-54`, `AGENT-INSTRUCTIONS.md:10-12`, env template.
**What:** Full одновременно объявлен требующим provider key, хотя Claude CLI mode работает без него; Telegram keys попадают в minimal viable, хотя Telegram optional. No-arg installer описан как full flow, но default prompt выбирает Lite, а «Full includes companions» расходится с optional prompts.
**Proposal:** Свести prerequisites в одну capability matrix по profile/provider/task и генерировать одинаковый текст во всех трёх документах.
**Status:** open

## 2026-07-13 · F-60 — Зафиксированная цена DeepSeek уже устарела [P3]
**Context:** `docs/llm-routing.md:25-26,134-135`, `config/llm-providers.example.env`; [официальная страница DeepSeek pricing](https://api-docs.deepseek.com/quick_start/pricing/).
**What:** Документы указывают около `$0.27/M` cache-miss input для V4 Flash, тогда как официальный тариф на дату аудита — `$0.14/M`; сама страница предупреждает, что цены меняются. Долгоживущий hardcode без `as of` неизбежно создаёт drift.
**Proposal:** Давать ссылку на primary pricing, дату проверки и помечать число illustrative; optional docs check может сигнализировать о давно не обновлённой дате.
**Status:** open

## 2026-07-13 · F-61 — В актуальных документах есть неподтверждённые/неверные operational facts [P3]
**Context:** `README.md:122-125,276`, `home-claude/CLAUDE.md:70-72,187-197`.
**What:** README отправляет за sanitization checklist в CHANGELOG вместо `CLAUDE.md` и фиксирует быстро устаревающий count plugin items без источника. Global rules обещают auto-write `memory/incidents.md`, которого implementation не делает, и приводят quota Exa без даты/primary source.
**Proposal:** Исправить navigation, генерировать counts из artifacts, удалить несуществующую automation и снабжать внешние quotas ссылкой/`as of` либо не фиксировать число.
**Status:** open

## 2026-07-13 · F-62 — Windows path/encoding rules внутренне противоречат друг другу [P3]
**Context:** `home-claude/CLAUDE.md:52-56,125-147`, `codex/AGENTS.md:99-115`.
**What:** Сначала запрещён формат `/c/...`, затем он же рекомендован для `Program Files`. CP1251 назван универсальным Windows ANSI для batch files, хотя ANSI/OEM code page зависит от locale; правило непереносимо и способно испортить non-Cyrillic systems.
**Proposal:** Развести PowerShell и Git Bash examples, использовать один непротиворечивый path contract; для batch либо ASCII/UTF-8 с явным code page, либо locale-aware generation.
**Status:** open

## 2026-07-13 · F-63 — Circuit breaker не обосновывает отказ от cost caps [P3]
**Context:** rationale `CHANGELOG.md:150-153`, `_DEPLETED_PROVIDERS` в `utils.py`.
**What:** Breaker останавливает только provider, который уже отвечает failure/depleted; любое количество успешных платных вызовов остаётся без call/token ceiling. Поэтому вывод, что он предотвращает runaway spend и делает budget guard избыточным, логически не следует из реализации.
**Proposal:** Исправить rationale; даже без monetary pricing можно иметь простые per-run/per-day call и token ceilings с explicit override.
**Status:** open

## 2026-07-13 · F-64 — Dry-run/quarantine/lint переоценены как замена semantic review [P3]
**Context:** rationale `CHANGELOG.md:144-146` для отклонённого review workflow.
**What:** Dry-run показывает inputs, но не будущую extraction; quarantine ловит parse/path failures, а lint — структуру, не истинность, hallucinations или destructive semantic merge. Эти механизмы полезны, но приведённое обоснование не покрывает риск, ради которого обсуждался review.
**Proposal:** Переписать rationale как осознанный trade-off и отдельно измерять semantic error rate; не объявлять structural gates эквивалентом проверки смысла.
**Status:** open

## 2026-07-13 · F-65 — External-review skill понижает severity по лексике, а не impact [P3]
**Context:** `home-claude/skills/code-review-external/SKILL.md:112-118`.
**What:** Findings с маркерами `theoretically`/`could potentially` автоматически становятся P3 независимо от доказательств и последствий. Осторожная формулировка reviewer не уменьшает exploitability, а public keys/RFC1918 addresses могут быть privacy violations даже без статуса cryptographic secret.
**Proposal:** Классифицировать по evidence, reachability и impact; lexical cues использовать только как сигнал уверенности, не как hard downgrade.
**Status:** open

## 2026-07-13 · F-66 — Monitor логирует отправку alert даже при ошибке Telegram [P3]
**Context:** `claude-task-monitor.sh:306-307`.
**What:** Return code sender-а игнорируется, после чего пишется `Alert sent`; operational log не позволяет отличить доставленное уведомление от провала канала.
**Proposal:** Проверять exit code, логировать `delivery_failed` и сохранять retryable notification state.
**Status:** open
