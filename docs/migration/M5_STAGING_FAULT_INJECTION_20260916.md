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
- **Обнаружение**: в этом же контейнере в 12:38–12:42 UTC работал ЧУЖОЙ процесс
  (`build_verified.py --output /root/artifact-r1.tar`, PID 2940/2946, свои синтетические
  secrets/keypair, свой «зауженный» тестовый профиль `test-profile-m5-focus.json`) — похоже,
  контейнер использовался параллельно ещё одной сессией/агентом. Процесс не трогал, дождался
  естественного завершения (получен `artifact-r1.tar.receipt.json`). Мои файлы — отдельные
  пути (`/root/build/v1/...`, `/root/repo-v2..v4`), коллизий по именам не было. Координатору:
  стоит уточнить, кто ещё работал в `m5-staging-test` 16.09 в это окно — на всякий случай.
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
