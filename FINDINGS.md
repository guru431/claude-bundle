# Findings — claude-bundle
Побочные находки. Ревизия: MonthlyStratReview 1-го числа. Stale >90 дней → alert.

## 2026-07-11 · code-review GLM-5.2: Утечка приватных данных: send_telegram_alert отправляет спис... [P1]
**Контекст:** auto-cron `ClaudeCodeReviewWeekly` (provider=ocg), файл claude-bundle/home-claude/cron/wiki/wiki-lint.py:191-197, category=security
**Что:** Утечка приватных данных: send_telegram_alert отправляет список всех ошибок (включая пути к файлам и имена страниц) во внешний сервис (Telegram). Это может привести к утечке конфиденциальной информации о структуре проекта и содержании wiki.
**Предложение:** Отправлять только сводку (количество ошибок/предупреждений) без детальных путей и имен файлов. Детали должны оставаться в локальных логах.
**Статус:** open

## 2026-07-11 · code-review GLM-5.2: Небезопасная обработка путей в Build-TaskXml: escArgs исполь... [P2]
**Контекст:** auto-cron `ClaudeCodeReviewWeekly` (provider=ocg), файл claude-bundle/home-claude/cron/admin/sync-tasks.ps1:282-294, category=security
**Что:** Небезопасная обработка путей в Build-TaskXml: escArgs использует SecurityElement::Escape, но это не защищает от внедрения дополнительных аргументов через пробелы в пути к скрипту. Если путь содержит пробелы, Task Scheduler может интерпретировать его как несколько аргументов.
**Предложение:** Использовать Quote-Arg для всех аргументов, включая пути к скриптам, или явно оборачивать пути в кавычки в XML.
**Статус:** open

## 2026-07-11 · code-review GLM-5.2: В _llm_deepseek при получении 402 (insufficient_balance) про... [P2]
**Контекст:** auto-cron `ClaudeCodeReviewWeekly` (provider=ocg), файл claude-bundle/home-claude/cron/hooks/utils.py:580-585, category=bug
**Что:** В _llm_deepseek при получении 402 (insufficient_balance) провайдер помечается как depleted, но при этом не происходит fallback на следующий провайдер (opencode). Функция просто возвращает None, что может привести к пропуску важных данных без попытки использования резервного провайдера.
**Предложение:** При 402 ошибке следует возвращать специальный код/исключение, чтобы llm_call мог инициировать fallback на opencode, а не просто возвращать None.
**Статус:** open

## 2026-07-11 · code-review GLM-5.2: В compile_project_data при bodies_withheld=True (когда стран... [P2]
**Контекст:** auto-cron `ClaudeCodeReviewWeekly` (provider=ocg), файл claude-bundle/home-claude/cron/wiki/wiki-compile-sessions.py:155-170, category=optimization
**Что:** В compile_project_data при bodies_withheld=True (когда страниц слишком много) LLM получает только имена страниц, но при этом пытается 'обновить' их. Это приводит к тому, что LLM генерирует контент вслепую, что может привести к потере существующих данных, даже с использованием blind_update.
**Предложение:** При bodies_withheld=True следует полностью пропускать update для существующих страниц и предлагать только создание новых, либо явно указывать LLM не переписывать существующие страницы.
**Статус:** open

## 2026-07-11 · code-review GLM-5.2: В collect_today_user_messages используется last_n=200, но пр... [P2]
**Контекст:** auto-cron `ClaudeCodeReviewWeekly` (provider=ocg), файл claude-bundle/home-claude/cron/memory-update.py:120-125, category=correctness
**Что:** В collect_today_user_messages используется last_n=200, но при этом нет проверки на размер сообщений. Если сообщения очень длинные, общий размер может превысить лимит контекста LLM, что приведет к тихому усечению через build_summary, но без предупреждения.
**Предложение:** Добавить проверку общего размера сообщений и логировать предупреждение, если размер превышает PROMPT_TOTAL_CAP, чтобы пользователь знал о возможной потере данных.
**Статус:** open

## 2026-07-11 · code-review GLM-5.2: В цикле push для wiki директории, если guard_secrets возвращ... [P2]
**Контекст:** auto-cron `ClaudeCodeReviewWeekly` (provider=ocg), файл claude-bundle/home-claude/cron/git-push-all.sh:150-160, category=bug
**Что:** В цикле push для wiki директории, если guard_secrets возвращает ошибку, счетчик skipped увеличивается, но при этом не происходит continue, что может привести к попытке push с некорректным состоянием.
**Предложение:** Добавить continue после увеличения skipped при ошибке guard_secrets для wiki директории.
**Статус:** open

## 2026-07-11 · code-review GLM-5.2: При первом пуше репозитория (когда `RANGE` пустой) скрипт де... [P2]
**Контекст:** auto-cron `ClaudeCodeReviewWeekly` (provider=ocg), файл claude-bundle/home-claude/cron/github-push.sh:85-87, category=optimization
**Что:** При первом пуше репозитория (когда `RANGE` пустой) скрипт делает `git ls-files -z | xargs -0 cat`. Это загружает содержимое ВСЕХ файлов репозитория в одну переменную `diff_content` в памяти. Для объемных репозиториев это может привести к исчерпанию памяти (OOM) или усечению данных по `ARG_MAX`.
**Предложение:** Вместо `cat` всех файлов сразу, стоит передавать список файлов построчно в `grep` или использовать `git grep --cached -f <file>` для потокового сканирования, избегая загрузки всего контента в bash-переменную.
**Статус:** open

## 2026-07-11 · code-review GLM-5.2: В `find_new_files` проверяется `time.time() - f.stat().st_mt... [P2]
**Контекст:** auto-cron `ClaudeCodeReviewWeekly` (provider=ocg), файл claude-bundle/home-claude/cron/wiki/wiki-compile-kb.py:63-68, category=bug
**Что:** В `find_new_files` проверяется `time.time() - f.stat().st_mtime < 300`, чтобы пропустить недавно измененные файлы. Однако `f.stat()` может вызвать `OSError`, если файл был удален между `glob` и `stat()`. Это обрабатывается, но если файл удалится после `stat()`, но до `read_text()` в `compile_article`, скрипт упадет с необработанным исключением, прервав весь цикл обработки.
**Предложение:** Обернуть вызов `article_path.read_text(encoding="utf-8")` внутри `compile_article` в try/except OSError и логировать ошибку, продолжая цикл обработки следующих файлов.
**Статус:** open

## 2026-07-11 · code-review GLM-5.2: В регулярном выражении для поиска секретов используется `\b`... [P2]
**Контекст:** auto-cron `ClaudeCodeReviewWeekly` (provider=ocg), файл claude-bundle/home-claude/cron/github-push.sh:96-97, category=security
**Что:** В регулярном выражении для поиска секретов используется `\b` (word boundary) перед токенами: `\b(ghp_[A-Za-z0-9]{20,}|...)`. Если секрет находится в начале строки или после символа, не являющегося словом (например, после `=` или `:` без пробела), `\b` может не сработать, и секрет будет пропущен при сканировании через `grep -nE`.
**Предложение:** Убрать `\b` перед группой токенов или заменить на `(?:^|[^A-Za-z0-9_])`, чтобы гарантированно ловить токены, даже если они прилегают к спецсимволам.
**Статус:** open

## 2026-07-11 · code-review GLM-5.2: В parse_llm_json используется цикл с 50 итерациями для попыт... [P3]
**Контекст:** auto-cron `ClaudeCodeReviewWeekly` (provider=ocg), файл claude-bundle/home-claude/cron/hooks/utils.py:680-690, category=optimization
**Что:** В parse_llm_json используется цикл с 50 итерациями для попытки исправления JSON. Это может привести к значительным задержкам, если LLM вернет сильно поврежденный JSON, особенно учитывая вызов llm_call внутри цикла для повторного форматирования.
**Предложение:** Уменьшить лимит итераций до 10-15 и добавить таймаут на общее время попыток исправления.
**Статус:** open

## 2026-07-11 · code-review GLM-5.2: В _retarget_subproject_headers используется DEFAULT_PROJECT ... [P3]
**Контекст:** auto-cron `ClaudeCodeReviewWeekly` (provider=ocg), файл claude-bundle/home-claude/cron/wiki/wiki-flush-sessions.py:310-315, category=correctness
**Что:** В _retarget_subproject_headers используется DEFAULT_PROJECT = 'main' для проверки, но это значение определено в wiki-flush-sessions.py, а не в utils.py. Если DEFAULT_PROJECT изменится в одном месте, но не в другом, это приведет к некорректной маршрутизации.
**Предложение:** Вынести DEFAULT_PROJECT в общие константы в utils.py и импортировать его во всех скриптах.
**Статус:** open

## 2026-07-11 · code-review GLM-5.2: Команда `wmic logicaldisk get size,freespace,caption` исполь... [P3]
**Контекст:** auto-cron `ClaudeCodeReviewWeekly` (provider=ocg), файл claude-bundle/home-claude/cron/claude-healthcheck.sh:55-65, category=portability
**Что:** Команда `wmic logicaldisk get size,freespace,caption` используется для получения информации о дисках в Windows. Утилита `wmic` объявлена устаревшей (deprecated) и удалена по умолчанию в Windows 11 22H2+ и Windows Server 2025.
**Предложение:** Заменить вызов `wmic` на PowerShell команду, например: `powershell.exe -Command "Get-CimInstance Win32_LogicalDisk | Select-Object Caption,FreeSpace,Size | Format-Table -AutoSize"`.
**Статус:** open

## 2026-07-11 · code-review GLM-5.2: Если `curl` вернет пустой ответ (например, при таймауте сети... [P3]
**Контекст:** auto-cron `ClaudeCodeReviewWeekly` (provider=ocg), файл claude-bundle/home-claude/cron/telegram-send.sh:52-54, category=correctness
**Что:** Если `curl` вернет пустой ответ (например, при таймауте сети или ошибке DNS), `RESPONSE` будет пустым. Тогда `HTTP_CODE` будет пустым, а `BODY` из-за `sed '$d'` тоже станет пустым. Проверка `[ "$HTTP_CODE" != "200" ]` сработает, но вывод ошибки будет неинформативным: `HTTP : `.
**Предложение:** Добавить явную проверку на пустой `RESPONSE` перед парсингом, чтобы выдавать понятное сообщение вроде 'telegram-send: curl failed (empty response / network error)'.
**Статус:** open

# Findings

Побочные находки. Ревизия: MonthlyStratReview 1-го числа. Stale >90 дней → alert.

<!-- 2026-07-11: empty. The 2026-07-10 audit (9 findings) was resolved in full;
     see CHANGELOG.md "[0.3.0] - 2026-07-11". Add new findings above this line
     (newest first). -->
