# M5 staging fault-injection — 16.09.2026 (черновик, пишется по ходу прогона)

Автор: Claude (Sonnet 5, ступень sonnet-max). Задача координатора: инъекция отказов для
деплой-механизма M4/M5 (`deploy/deploy.sh` + `deploy/migration/server_job.py`) в изолированном
контейнере, без касания боевого бота/VPS.

## Методология

- Контейнер: `m5-staging-test` (образ `m5-staging-test:debian12-systemd`, systemd как PID1),
  поднят на VPS ireland (`admin@34.65.234.12`) командой `sudo docker run --privileged
  --cgroupns=host -v /sys/fs/cgroup:/sys/fs/cgroup:rw -d --name m5-staging-test
  m5-staging-test:debian12-systemd`. Все команды `systemctl`/`server_job.py`/`chown` — только
  через `sudo docker exec m5-staging-test ...` с VPS-хоста, никогда напрямую на хосте.
- Репозиторий: `https://github.com/l3sh42h2/funddd_bot`, ветка `codex/migration-m4-m5`,
  HEAD `db050cdd51cabadbcdb26130c7a4daa875c5ab3d` (клонирован внутри контейнера в `/root/repo`).
- Синтетическое legacy-окружение (НЕ боевые данные): `/home/admin/hyper/funding_bot` внутри
  контейнера = копия `/root/repo` (без `.git`) + `runtime/trade.db` (создана реальным
  `funding_bot.trade.store.connect()`, schema_version=5, min_reader=2 — совпадает с
  `compatibility.json`) + синтетический `runtime/owner.toml` (на основе
  `deploy/owner.toml.example`, все адреса и токены — заглушки) + синтетический `.env`
  (TG_BOT_TOKEN и биржевые ключи — заглушки вида `fake-...`). Легальность подтверждена
  напрямую функциями самого `server_job.py` (`owner.load`, `database_info`,
  `legacy_configuration`, `legacy_identity`) до начала fault-injection.
- Учётные записи `funding-core`(997)/`funding-collector`(996)/`funding-interface`(995) и группы
  `funding-ipc`(994)/`funding-market`(993)/`funding-pace`(992) — подготовлены до начала (см.
  раздел «Окружение» ниже), права проверены отдельно в пункте 6.
- **Обнаружение — контейнер использовался параллельно кем-то ещё (важно для координатора)**:
  в 12:38–12:42 UTC в этом же контейнере работал ЧУЖОЙ процесс (`build_verified.py --output
  /root/artifact-r1.tar`, PID 2940/2946, свои синтетические secrets/keypair
  (`/root/fake_sol_keypair.json`, `/root/secret_core.env`, `/root/secret_cabinet.env`, все
  датированы 12:08 — то есть почти сразу после создания контейнера), свой «зауженный» тестовый
  профиль `test-profile-m5-focus.json`). Сборка их успешно завершилась (получен валидный
  `artifact-r1.tar.receipt.json`). Это НЕ пересекалось с моими путями (`/root/build/v1/...`,
  `/root/repo-v2..v4`) — но дальше выяснилось худшее: тот же процесс (или что-то от его имени)
  уже реально прогнал `install` через ОБЩЕЕ состояние `/opt/funding-bot` + `/var/lib/funding-bot`
  + `/run/lock/funding-bot-deploy.lock` — а эти пути общие на весь контейнер, не мои личные.
  К моменту как я в 12:48 впервые вызвал `inspect-base`, там уже было: синтетический
  «r0-baseline» (source_revision — все нули, явно заглушка) как якобы уже установленный и
  ЗДОРОВЫЙ предыдущий релиз, и незавершённый переход (`.deploy-transition.json`, phase=`fenced`)
  на релиз `db050cdd51ca-69b60af93b35` (это реальный HEAD `db050cdd...` + мой же
  `source_sha256` синтетической legacy-конфигурации — то есть кто-то шёл почти тем же путём,
  что и я). `funding_bot-core.service` и `funding_bot-collector.service` крутились в
  live crash-loop (core: `226/NAMESPACE`, `restart counter=19+`; collector: `203/EXEC`).
  Причина по `journalctl`/отчёту `db050cdd51ca-69b60af93b35-failed.json`: pre-switch проверка
  нового релиза упала на `probe_service_layout` для роли `interface`
  (`layout_probe.py` через `systemd-run --wait` вернул ненулевой код), после чего
  `Job.install()` попытался штатно откатиться к «предыдущему» (r0-baseline) —
  но у r0-baseline `/opt/funding-bot/releases/r0-baseline-000000000000` НЕТ каталога `.venv`
  вообще (в отличие от `db050cdd51ca-69b60af93b35`, где `.venv` есть) — то есть r0-baseline
  сам по себе никогда не мог реально стартовать. Итог в отчёте: `"rollback":
  "pre_switch_restore_failed: DeployFailure: core exited before readiness: core IPC
  unavailable/incompatible"` — откат отката тоже не удался, отсюда живой crash-loop и висящий
  transition. Я прошёл по коду `recover_transition()` — она бы тоже неизбежно упала на том же
  «у r0-baseline нет venv», то есть штатное самолечение здесь принципиально не могло сработать:
  тупиковое состояние, не чья-то ценная незавершённая работа, а мёртвый застрявший эксперимент.
  В 12:53 я остановил/отключил три юнита и снёс `/opt/funding-bot`, `/var/lib/funding-bot`,
  `/run/funding-bot`, `/run/lock/funding-bot-deploy.lock`, юниты из `/etc/systemd/system/`
  (только внутри контейнера) — получил чистую стартовую точку. **Координатору: до 12:53 UTC
  16.09 в `m5-staging-test` кто-то ещё явно работал над тем же/смежным M5-стейджинг тестом
  (возможно Codex или другая сессия Claude) — стоит выяснить, не осталась ли эта работа
  незафиксированной где-то ещё, и не дублируем ли мы сейчас усилия.**
- **Ограничения окружения контейнера** (не баг деплой-механизма, а особенность
  systemd-in-docker песочницы):
  - `/tmp` внутри контейнера смонтирован `tmpfs ... noexec` — `deploy/deploy.sh build`
    из коробки падает («Cannot load native module... failed to map segment») на нативных
    расширениях (Crypto, pydantic-core, solders) при сборке venv под `/tmp`. Обход:
    `TMPDIR=/root/tmp` (обычный overlay, exec разрешён) перед `deploy/deploy.sh build`.
  - `interface` требует реальный Telegram: `run_interface()` → `Interface.poll_once()` сначала
    вызывает `self.api.get_me()` — с фиктивным `TG_BOT_TOKEN` падает КАЖДЫЙ цикл опроса,
    `last_poll` никогда не выставляется, `health()['ready']` навсегда `False`. Процесс не
    падает (перехват в `poll()`), но `wait_interface()`/`wait_components()` в `server_job.py`
    (оба требуют `ready is True` через `_health_identity`) поэтому НИКОГДА не проходят без
    реального токена — это относится к ЛЮБОМУ релизу, не только к специально сломанному.
    Следствие: «полностью healthy» состояние (collector+core+interface) в этой песочнице
    недостижимо в принципе; там, где это ограничивает конкретный пункт — отмечено явно.
  - Контейнеру выделено 2 vCPU / 3.8 ГБ RAM (`nproc`, `free -h`) — полный `pytest tests`
    (тысячи тестов всего репо, включая HL/Solana/EVM исполнение) идёт на такой машине из
    `deploy/deploy.sh build` неприемлемо долго (первая попытка: 7% за ~9 минут). Для всех
    `build_verified.py` в этом отчёте использован ОДИН И ТОТ ЖЕ суженный тестовый профиль —
    9 файлов, относящихся именно к деплой-механизму: `tests/test_m5_deploy_{artifact,health,
    server}.py`, `tests/test_migration_linux_permissions.py`, `tests/test_migration_m{1..5}.py`
    (вызывается напрямую через `python3 deploy/migration/build_verified.py --profile
    <narrow>.json ...` с `FUNDING_M5_ENTRY=deploy/deploy.sh`, а не через `deploy/deploy.sh
    build`, у которого профиль зашит константой — сам `build_verified.py` не менялся). Это
    НЕ полный прогон репозитория — явно отмечаю, чтобы не выдавать зауженный профиль за полный.

## Пункт 1 — install/rollback полного артефакта

*(в процессе)*

## Пункт 2 — обрыв SSH/клиента посреди install

*(в процессе)*

## Пункт 3 — параллельный install/inspect-base

*(в процессе)*

## Пункт 4 — rollback после «сделки»

*(в процессе)*

## Пункт 5 — UI-only релиз с несовместимым IPC

*(в процессе)*

## Пункт 6 — DAC-права после install

*(в процессе)*

## Итоговый вердикт

*(в процессе)*
