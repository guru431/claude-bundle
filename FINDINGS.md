# Findings
Побочные находки. Ревизия: MonthlyStratReview 1-го числа. Stale >90 дней → alert.

## 2026-07-08 · `ClaudeGitPushAll` включен по умолчанию [P1]
**Контекст:** adversarial audit, линза installation/public-release; `home-claude/cron/registry.yaml:140`.
**Что:** задача `ClaudeGitPushAll` без `enabled: false` авто-коммитит и пушит все git-репозитории под `PROJECTS_ROOT`. Для публичного bundle это слишком опасный default: новый пользователь может случайно отправить WIP, приватные заметки или секреты, которые не поймали локальные denylist-правила.
**Предложение:** сделать задачу opt-in (`enabled: false`), включать через явное согласие в installer/docs, добавить dry-run/report перед первым запуском.
**Статус:** open

## 2026-07-08 · `pre-compact` и `session-end` перетирают один pending-файл [P1]
**Контекст:** adversarial audit, линза cron/wiki reliability; `home-claude/cron/hooks/utils.py:302`, `pre-compact.py:62`, `session-end.py:23`.
**Что:** оба hook'а пишут в `wiki/daily/.pending/<session_id>.md`; второй запуск перезаписывает первый. После compact можно потерять именно tail, ради которого существует precompact hook.
**Предложение:** писать `session_id + event/timestamp` или атомарно append/merge existing pending file.
**Статус:** open

## 2026-07-08 · Rejected LLM paths помечают session compile как обработанный [P1]
**Контекст:** adversarial audit, линза data loss; `home-claude/cron/wiki/wiki-compile-sessions.py:365-384`.
**Что:** если LLM вернул changes, но `normalize_wiki_path()` отверг все пути, код пишет error, но при `complete=True` все равно ставит pair marker. Данные больше не будут переобработаны.
**Предложение:** считать `changes != [] && applied == []` hard failure: не ставить marker, сохранять raw LLM output в quarantine/log, возвращать non-zero.
**Статус:** open

## 2026-07-08 · Rejected LLM paths помечают KB source как processed [P1]
**Контекст:** adversarial audit, линза data loss; `home-claude/cron/wiki/wiki-compile-kb.py:240-258`.
**Что:** KB article добавляется в state до проверки, что хотя бы одна change реально применена. При `applied == []` лог говорит "content dropped", но source уже исключен из будущих запусков.
**Предложение:** вызывать `state_add()` только после `applied > 0`; rejected paths делать retryable/quarantined и завершать задачу с ошибкой.
**Статус:** open

## 2026-07-08 · Hard errors в wiki pipeline завершаются exit 0 [P2]
**Контекст:** adversarial audit, линза observability; `wiki-compile-sessions.py:399`, `wiki-compile-kb.py:259`, `wiki-lint.py:281`.
**Что:** provider outage, parse failure, rejected output и lint errors могут попасть только в лог, а Task Scheduler увидит `LastResult=0`. `ClaudeTaskMonitor` тогда не отличит успешный запуск от тихого провала.
**Предложение:** аккумулировать hard failures и завершать процесс `sys.exit(1/2)`; обновить тесты под ожидаемый non-zero.
**Статус:** open

## 2026-07-08 · `normalize_wiki_path()` допускает `.`/`..` segments [P2]
**Контекст:** adversarial audit, линза path safety; `home-claude/cron/hooks/utils.py:911-986`.
**Что:** путь вида `projects/../log.md` проходит базовые проверки как трехсегментный project path и может выйти из intended folder при записи через `WIKI_ROOT / path`.
**Предложение:** нормализовать `\` → `/`, reject any part in `{".", ".."}`, валидировать slug/filename allowlist'ом до записи.
**Статус:** open

## 2026-07-08 · `sync.cmd` разворачивает user args в elevated cmd [P2]
**Контекст:** adversarial audit, линза Windows elevation; `home-claude/cron/admin/sync.cmd:17-31`.
**Что:** старый PowerShell string injection закрыт, но args сохраняются в predictable temp file и затем исполняются как `%PASSARGS%` в elevated `cmd`. Crafted аргументы с metacharacters могут превратиться в command injection уже после UAC elevation.
**Предложение:** не replay'ить аргументы через cmd expansion; передавать whitelist параметров через PowerShell `Start-Process -ArgumentList @(...)`, encoded JSON/base64 или другой формат без интерпретации shell.
**Статус:** open

## 2026-07-08 · Installer по умолчанию выбирает `full`, хотя docs для "just setup" ведут в Lite [P2]
**Контекст:** adversarial audit, линза install UX; `scripts/install.ps1:42`, `AGENT-INSTRUCTIONS.md:14-17`.
**Что:** запуск Windows installer без параметров тянет full-профиль: cron, `.env`, password/sync UX и prereqs. Agent instructions для неопределенного "set up Claude Code" говорят делать Lite.
**Предложение:** сделать default `lite`, либо требовать явный выбор без default; перед `full` запускать preflight и показывать последствия.
**Статус:** open

## 2026-07-08 · Installer перезаписывает существующие `CLAUDE.md`/`settings.json` без merge/confirm [P2]
**Контекст:** adversarial audit, линза deployment safety; `scripts/install.ps1:54-63`, `AGENT-INSTRUCTIONS.md:42-46`.
**Что:** installer делает `Copy-Item -Force`, хотя agent instructions требуют остановиться при существующем `~/.claude`. Это может потереть локальные настройки пользователя.
**Предложение:** detect existing non-empty config, предложить backup/merge/overwrite; в `-NonInteractive` падать без явного `-Force`.
**Статус:** open

## 2026-07-08 · Installer запускает self-test по source repo, а не по deployment [P2]
**Контекст:** adversarial audit, линза false-green checks; `scripts/install.ps1:100,127`, `scripts/self-test.ps1:24-25`.
**Что:** post-install self-test проверяет `home-claude` внутри repo. При `-InstallPath` он может не заметить ошибку в установленном `~/.claude`, а warning про placeholders относится к source template.
**Предложение:** добавить `self-test.ps1 -InstallPath <path>` или отдельный deployed-mode; installer должен проверять фактический `$InstallPath`.
**Статус:** open

## 2026-07-08 · POSIX generator может создать unit для Windows-only `ClaudeTaskMonitor` [P2]
**Контекст:** adversarial audit, линза POSIX full; `scripts/gen-scheduler.py`, `home-claude/cron/registry.yaml:164-174`, `INSTALL.md:393-409`.
**Что:** docs говорят, что Windows-only kinds будут skipped, но `ClaudeTaskMonitor` объявлен как `kind: bash`, поэтому generator может выпустить systemd/launchd unit для скрипта, который проверяет Windows Task Scheduler.
**Предложение:** добавить `platform: windows|posix|all` в registry и фильтр в generator; `ClaudeTaskMonitor` пометить Windows-only.
**Статус:** open

## 2026-07-08 · POSIX full flow в документации расходится [P2]
**Контекст:** adversarial audit, линза docs/deployment; `AGENT-INSTRUCTIONS.md:277-282`, `INSTALL.md:393-405`, `README.md:230-235`.
**Что:** README/INSTALL предлагают `gen-scheduler.py`, а AGENT-INSTRUCTIONS все еще говорят вручную переводить `registry.yaml` в crontab. Агент может выбрать устаревший путь.
**Предложение:** обновить AGENT-INSTRUCTIONS на `gen-scheduler.py` flow и явно перечислить unsupported POSIX tasks.
**Статус:** open

## 2026-07-08 · Lite verification требует Python, хотя Lite заявлен как no-extra-software [P2]
**Контекст:** adversarial audit, линза Lite contract; `AGENT-INSTRUCTIONS.md:88-94`.
**Что:** Tier 1 Lite обещает отсутствие Python/Git/Node, но verification использует `python -c` для JSON. На чистой Lite-машине проверка не сработает.
**Предложение:** для Windows использовать PowerShell `ConvertFrom-Json`; для POSIX делать python check только if-present или ограничиться проверкой наличия файлов.
**Статус:** open

## 2026-07-08 · `_run-hidden.vbs` не читает `.env` overrides для interpreter paths [P2]
**Контекст:** adversarial audit, линза session 0 reliability; `home-claude/bin/_run-hidden.vbs:36-47`, `config/llm-providers.example.env:110-111`, `INSTALL.md:189`.
**Что:** docs/templates предлагают `PYTHON_EXE`/`BASH_EXE`, но VBS launcher читает только process env. В Password-mode/session 0 `.env` еще не загружен, поэтому нестандартный Python/Git Bash может упасть до появления логов.
**Предложение:** либо парсить `.env` в VBS рядом с bundle root, либо подставлять explicit interpreter path в registered action при `sync-tasks.ps1`.
**Статус:** open

## 2026-07-08 · Flush может задублировать JSONL transcript и hook pending для одной сессии [P2]
**Контекст:** adversarial audit, линза wiki pipeline cost/correctness; `home-claude/cron/wiki/wiki-flush-sessions.py:390-418`.
**Что:** flush одновременно берет recent/backlog JSONL и `.pending` drafts. Для одной session это может продублировать tail в daily log, увеличить LLM cost и шанс повторных wiki pages.
**Предложение:** pending должен хранить transcript/session id; flush должен skip'ать matching JSONL или объединять источники с dedup.
**Статус:** open

## 2026-07-08 · State lock timeout продолжает unlocked write [P2]
**Контекст:** adversarial audit, линза race conditions; `home-claude/cron/hooks/utils.py:88-95`.
**Что:** lock нужен против `load → modify → save` race, но при timeout callers продолжают без lock. При overlap cron-фаз это снова может потерять state keys.
**Предложение:** для cron fail loud/retry later при lock timeout; минимум вернуть non-zero вместо unlocked state write.
**Статус:** open

## 2026-07-08 · CI secret scan исключает весь `.github/` [P2]
**Контекст:** локальная security-линза после сбоя одного subagent; `.github/workflows/ci.yml:63-78`.
**Что:** CI generic token scan исключает `.github/`, хотя workflow files тоже публичные tracked files. Если локальный pre-commit не активен или bypass'нут, секретоподобная строка в workflow не будет поймана CI.
**Предложение:** исключать только конкретные self-matching строки/файлы или сканировать `.github/` отдельным allowlist-aware шагом.
**Статус:** open

## 2026-07-08 · Cron LLM default дрейфует относительно текущей глобальной политики [P2]
**Контекст:** локальная strategic/rules линза; `home-claude/cron/hooks/utils.py:526-534`, `docs/llm-routing.md:59-77`, `README.md:143-145`, `config/llm-providers.example.env:73-79`.
**Что:** bundle делает DeepSeek primary/default и OpenCode Go fallback. Текущие глобальные инструкции для ночных cron-скриптов в этой сессии говорят, что с 2026-06-07 primary должен быть OpenCode Go (`deepseek-v4-flash` через OCG proxy), а DeepSeek direct fallback.
**Предложение:** сверить source of truth и либо поменять default chain в коде/docs/template, либо явно зафиксировать, что публичный bundle намеренно отличается от приватной рабочей политики.
**Статус:** open

## 2026-07-08 · README manual Lite snippet не копирует skills/commands [P2]
**Контекст:** adversarial audit, линза install contract; `README.md:6-8`, `README.md:168-175`.
**Что:** Lite profile обещает skill templates и slash command, installer'ы их копируют, но manual fallback snippet копирует только `CLAUDE.md` и `settings.json`.
**Предложение:** добавить `Copy-Item -Recurse "$src\skills"` и `"$src\commands"` либо явно назвать snippet core-only without skills/commands.
**Статус:** open

## 2026-07-08 · CLAUDE.md verification section устарел относительно CI/tests [P2]
**Контекст:** adversarial audit, линза docs/CI consistency; `CLAUDE.md:191-208`, `.github/workflows/ci.yml:93-96`.
**Что:** раздел говорит "this repo has no automated tests", но CI запускает pytest smoke test. Там же утверждается, что CI запускает hook smoke tests, хотя workflow такого шага не содержит.
**Предложение:** переименовать раздел в "Verification" / "Local verification", добавить `python -m pytest tests/ -q`, добавить hook smoke step в CI или убрать обещание.
**Статус:** open

## 2026-07-08 · Lite install создает `.env` и запускает full-ish self-test [P3]
**Контекст:** adversarial audit, линза Lite UX; `scripts/install.ps1:82-101`.
**Что:** даже `-Profile lite` создает `.env` из LLM template и запускает общий source self-test. Это расходится с "config only / no extra software" и оставляет лишний файл в `~/.claude`.
**Предложение:** `.env` создавать только для `full`; для Lite сделать минимальную проверку скопированных файлов.
**Статус:** open

## 2026-07-08 · README обещает self-test для `install-lite.sh`, но скрипт его не запускает [P3]
**Контекст:** adversarial audit, линза docs/installer consistency; `README.md:161-163`, `scripts/install-lite.sh:21-42`.
**Что:** README говорит, что оба automated installer'а stamp version и run self-test; POSIX lite script только копирует файлы и stamp'ит версию.
**Предложение:** добавить lightweight POSIX/offline self-test или уточнить README.
**Статус:** open

## 2026-07-08 · Bootstrap предупреждает о любом non-C drive как maybe mapped [P3]
**Контекст:** adversarial audit, линза Windows UX; `scripts/bootstrap-registry.ps1:55-68`, `home-claude/cron/admin/sync-tasks.ps1:391-417`.
**Что:** bootstrap пугает локальные `D:\`/`E:\`, хотя syncer уже умеет точно отличать mapped drives через CIM `DriveType=4`.
**Предложение:** переиспользовать actual mapped-drive detection в bootstrap.
**Статус:** open

## 2026-07-08 · Placeholder `<bundle-install-path>` размыт между repo root и deploy path [P3]
**Контекст:** adversarial audit, линза deployment docs; `INSTALL.md:138-143`, `INSTALL.md:220-242`, `home-claude/cron/registry.yaml`.
**Что:** термин сначала читается как место, где живет bundle repo, затем bootstrap получает `$dst` (`~/.claude`). Это легко приводит к registry, указывающему не туда.
**Предложение:** развести `RepoRoot` и `DeployPath`; в registry template переименовать placeholder в `<deployed-claude-path>`.
**Статус:** open

## 2026-07-08 · `docs/wiki-method.md` описывает старый flush/compile contract [P3]
**Контекст:** adversarial audit, линза docs/code drift; `docs/wiki-method.md:47`, `docs/wiki-method.md:56`, `docs/wiki-method.md:63`.
**Что:** docs описывают flush как JSONL → `.pending`, compile-sessions как `.pending` → pages и hash/mtime dedup in frontmatter. Текущий код: hooks пишут `.pending`, flush пишет `daily/YYYY-MM-DD.md`, compile-sessions читает daily, state хранит path/processed.
**Предложение:** синхронизировать docs с текущей архитектурой или вернуть hash-based dedup в код.
**Статус:** open

## 2026-07-08 · Cron architecture обещает найти renamed managed tasks, код ищет по имени [P3]
**Контекст:** adversarial audit, линза docs/code drift; `docs/cron-architecture.md:94-97`, `home-claude/cron/admin/sync-tasks.ps1:347-348`.
**Что:** docs говорят, что syncer найдет managed tasks even if renamed, но implementation вызывает `Get-ScheduledTask -TaskName $name`. Rename создаст дубль и оставит старую managed task жить отдельно.
**Предложение:** убрать обещание из docs или реализовать discovery по marker со stable id.
**Статус:** open

## 2026-07-08 · Troubleshooting про `projects/main` устарел [P3]
**Контекст:** adversarial audit, линза docs/code drift; `README.md:245`, `INSTALL.md:344`, `home-claude/cron/hooks/utils.py:899-908`.
**Что:** docs говорят, что при пустом `KNOWN_PROJECTS` все страницы попадут в `projects/main`; текущий `normalize_project_name()` уже выводит ASCII slug и падает в `main` только если имя не извлечь.
**Предложение:** обновить troubleshooting symptom/cause.
**Статус:** open

## 2026-07-08 · Layout docs пропускают `requirements.txt` и занижают CI surface [P3]
**Контекст:** adversarial audit, линза docs/CI drift; `README.md:95-98`, `CLAUDE.md:76-78`, `.github/workflows/ci.yml:23`.
**Что:** layout перечисляет `requirements-dev.txt`, но не `requirements.txt`, хотя CI и Tier-2 deps используют именно `requirements.txt`. README CI summary также не отражает doc-count/pytest/PowerShell parse полностью.
**Предложение:** писать `requirements.txt, requirements-dev.txt` и расширить CI summary.
**Статус:** open

## 2026-07-08 · GitHub publication wording может быть stale относительно локального remote [P3]
**Контекст:** adversarial audit, линза release docs; `AGENTS.md:10`, `CLAUDE.md:23`, `CLAUDE.md:219`, локальный `git remote -v`.
**Что:** docs говорят "Public on GitHub (planned)" / remote `gh`, а локальная конфигурация уже содержит remote `github`. Это может быть просто pre-release state, но release docs выглядят неоднозначно.
**Предложение:** зафиксировать текущий статус: "published", "release pending" или "GitHub remote may exist locally"; синхронизировать имя remote.
**Статус:** open
<!-- 2026-07-04: empty. All 11 findings from the 2026-07-04 adversarial
     multi-lens audit were resolved; see CHANGELOG.md "[0.1.0] - 2026-07-04".
     Add new findings above this line (newest first). -->
