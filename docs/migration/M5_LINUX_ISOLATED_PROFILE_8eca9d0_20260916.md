# M5: isolated Linux profile — `8eca9d0` (2026-09-16)

Собрано Claude через `deploy/deploy.sh build` (реальный `deploy/migration/build_verified.py`, не ручной
прогон) на VPS ireland, изолированно от боевого рантайма — отдельный клон в `/var/tmp`, отдельный python
(интерпретатор из уже установленного релиза `571d86fd7455-a19b18bf6ce3/.venv`, только чтобы иметь pip;
никакие боевые файлы/процессы/БД не читались и не менялись этим прогоном).

## Кандидат

- source commit: `8eca9d00d60081f70edf1ac444a14a28ea225f6b` (`codex/migration-m4-m5`, merge «Merge Claude
  progress edit-in-place» — включает AC-07 буквальное закрытие (`trade/formatters.py`), progress/sol_progress
  edit-in-place, и более ранние M4-исправления Codex — cost basis fail-closed фикс, reader fence v5/v6);
- source_sha256: `b69776732b14aab667a38a975cbeddf3bfb19c691f234f99888d7236b7b8aaef`;
- artifact_sha256: `ab86ee2c00f2efbfb3d79b418d178f0b8b4560fd0678a3d8a9c20e6bebde42e2`;
- release_id (сгенерированный build'ом): `8eca9d00d600-b69776732b14`;
- patchnote, привязанный к сборке: `PATCHNOTES/m4-progress-edit-in-place-20260916.md`.

## Зависимости — точная фиксация из `deploy/requirements.lock`, включая Solana

Полный резолв через `pip download --only-binary=:all: --no-deps -r deploy/requirements.lock` (детерминированно,
без резолвера pip) — `solders==0.29.0` установлен как часть штатной сборки, не отдельным шагом. Это закрывает
отдельно запрошенный «Linux-прогон с Solana-зависимостями» — он не отдельная задача, а естественная часть
этой же сборки.

## Результат

```
2999 passed, 12 skipped in 1211.55s (0:20:11)
```
`exit_code=0`, `failed=0` (из receipt). Полное время сборки+тестов (включая resolve/install зависимостей):
`duration_s=1347.47` (~22.5 мин).

12 skip — все объяснимые и не относятся к патчу:
- 8× JavaScriptCore-тесты дашборда/кабинета (только на Маке, не Linux-ограничение);
- 2× root DAC/setuid-фикстуры (`test_m5_deploy_server.py`, `test_migration_linux_permissions.py`) — сборка
  шла не под root;
- 1× `test_sol_c4_release.py` self-check «тесты идут не venv этого дерева» — ожидаемо: это проверка того,
  что тестовый прогон использует именно venv, собранный build_verified.py под этот SHA, а не сторонний;
  штатно на своём месте под настоящим `deploy.sh build`, не венв-дрейф, как было в моих локальных прогонах
  на Маке ранее сегодня;
- 1× `test_sol_doctor.py` сетевой probe (только по явному `FUNDING_BOT_NET_PROBE=1`).

`test_sol_c2_engine.py` (Solana-движок, тесты которого раньше на Маке скипались из-за отсутствия `solders`) —
выполнился в основном теле прогона (0 упоминаний в списке медленных/упавших тестов receipt'а, при
`failed=0`) — подтверждает вживую вчерашний (в рамках этой же сессии) фикс `tests/sol_c2_world.py: RecHooks.
notice()`, который раньше нельзя было проверить локально.

## Что это доказывает и что нет

Доказывает: точная сборка (зависимости строго по lock, без резолвера), полный тестовый набор проекта зелёный
на Linux под официальным build-инструментом, включая ранее непроверяемую здесь Solana-ветку. НЕ доказывает:
настоящую установку (`deploy/deploy.sh install`) этого SHA на прод или staging — только сборка и тесты, ничего
не переключено, боевые `funding_bot-*` службы и `trade.db` не тронуты, VPS работает на прежнем `571d86fd7455-
a19b18bf6ce3` без изменений.

## Read-only preflight на живом VPS (тот же заход, отдельно от сборки)

Снят непосредственно перед сборкой: `deals` — ровно 2 записи (`DQA9Q` OPEN, `D9W8M` ABORTED, без изменений
с предыдущей проверки), `operations` — 0 строк (generic-путь по-прежнему не активирован), `intents` — `done:1,
failed:1`, UNKNOWN нигде, все три службы `active`. Текущий релиз на проде не менялся.

## Что дальше (не входит в эту задачу)

Независимое финальное ревью точного SHA `8eca9d0` целиком (не по кускам) и явное решение владельца о
production switch — с указанием конкретного SHA, отдельно от общего разрешения на работу.
