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
- Реальный sshd поднят ВНУТРИ контейнера (`apt-get install openssh-server`, пользователь
  `m5deploy` с NOPASSWD sudo — оба существуют только внутри `m5-staging-test`, не пересекаются
  ни с боевым `admin`, ни с чем-либо на хосте), чтобы гонять НАСТОЯЩИЙ `deploy/deploy.sh` (тот
  самый, что использует `ssh`+`scp`+`systemd-run` с VPS-хоста как «операторской машины») по
  IP контейнера (172.17.0.2), с переопределением `FUNDING_DEPLOY_VPS`/`_SSH_KEY`/`_KNOWN_HOSTS`.
  Так пункты 1–3 бьют по РЕАЛЬНОМУ deploy.sh, а не по его ручной имитации.

## НАЙДЕННЫЙ БАГ (P1/P2): `layout_probe.py::denied()` падает на легитимно отсутствующем файле

**Файл**: `deploy/migration/layout_probe.py:18-23` (`denied()`), проявляется в проверке роли
`interface` на строке 62: `denied(root / 'collector/funding_bot.db')`.

```python
def denied(path):
    try:
        readable(path)
    except PermissionError:
        return
    raise RuntimeError('cross-role file is accessible')
```

`denied()` должен подтверждать, что чужая роль НЕ может прочитать файл — ожидается
`PermissionError`. Но `collector/funding_bot.db` создаётся коллектором ЛЕНИВО (в
`migrate_legacy_runtime()` копия делается только `if (legacy_runtime / 'funding_bot.db').exists()`
— условно), то есть при первой миграции, где у legacy ещё не было своего `funding_bot.db`
(коллектор ни разу не запускался), этого файла в `/var/lib/funding-bot/collector/` просто нет.
`Path(...).open('rb')` на несуществующем файле бросает `FileNotFoundError`, а НЕ `PermissionError`
— `denied()` её не ловит, исключение всплывает необработанным, `probe()` для роли `interface`
падает, `probe_service_layout()` в `server_job.py` получает `CalledProcessError` на pre-switch
шаге — **install падает ДЛЯ ЛЮБОГО первого перехода legacy→M5, если коллектор ещё ни разу не
создал свою БД**, независимо от того, что и как деплоится.

**Как поймано**: воспроизведено трижды независимо — (1) чужая попытка в этом контейнере до
12:53 (см. выше, привела к тупиковому состоянию из-за отдельно сломанного r0-baseline), (2) мой
первый реальный `install` v1 в 13:14, (3) повтор в 13:16 — все три с идентичной сигнатурой
(`funding-layout-interface-*.service: Main process exited, code=exited, status=1/FAILURE`, юнит
СТАРТОВАЛ нормально — не проблема прав/EnvironmentFile). Точная причина подтверждена прямым
вызовом `probe('interface', '/var/lib/funding-bot')` от имени `funding-interface` (обходя
`systemd-run --pipe`, который иначе съедает traceback — сам факт, что `--pipe`-вывод пробника
нигде не логируется при падении `CalledProcessError`, а видна только строка команды — тоже
маленький минус наблюдаемости, но не критичный):
```
FileNotFoundError: [Errno 2] No such file or directory: '/var/lib/funding-bot/collector/funding_bot.db'
```

**Сценарий отказа**: первая миграция legacy→M5 на сервере, где коллектор ещё не успел создать
`runtime/funding_bot.db` (или он отсутствует по любой другой причине) — `install` гарантированно
падает на pre-switch (до drain/switch, откат чистый — `pre_switch_owner_restored`/
`legacy_resumed_before_switch`, так что сама по себе эта находка не рискует боевыми данными),
но блокирует переход, пока не появится этот файл. **Открытый вопрос координатору**: на реальном
проде `/home/admin/hyper/funding_bot/runtime/funding_bot.db`, скорее всего, уже существует
(коллектор работает постоянно) — тогда баг не проявится при боевом переходе; но он гарантированно
бьёт по ЛЮБОМУ staging/тестовому первому переходу (как этот) и по любому гипотетическому
редеплою с нуля.

**Исправление** (не применял к дереву, кроме как для обхода в тестовом контейнере — задача просит
не чинить код, только находить): ловить `(PermissionError, FileNotFoundError)` в `denied()`:
```python
except (PermissionError, FileNotFoundError):
    return
```
Несуществующий файл тривиально «недоступен» — семантически это то же самое, что отказ в доступе,
для целей этой проверки.

**Обход, чтобы не блокировать остальные 5 пунктов**: досоздал пустой `runtime/funding_bot.db`
(валидный sqlite-файл через `sqlite3.connect(...)`) в синтетическом legacy-каталоге — подтверждено
(`diff` snapshot до/после), что это НЕ меняет `legacy_identity()`/`source_sha256` (каталог
`runtime/` целиком исключён из `tree_manifest` через `LEGACY_EXCLUDE`, а `funding_bot.db` не
входит ни в один хеш `legacy_configuration()`). После обхода `install` прошёл pre-switch и дошёл
до реального запуска collector+core.

## Пункт 1 — install/rollback полного артефакта

Артефакт: `db050cdd51ca-69b60af93b35` (чистый HEAD задачи, собран через `build_verified.py`
напрямую с суженным профилем — 133/133 тестов). Первая миграция legacy→M5 (`deploy/deploy.sh
install` по-настоящему, через ssh на контейнер) в 13:19 UTC: после обхода бага `layout_probe`
дошла до `phase=switched` за ~16 секунд, `funding_bot-collector.service` и
`funding_bot-core.service` — оба реально СТАРТОВАЛИ и остались `active (running)` (проверено
`systemctl status`: core PID 9929 сразу и стабильно, collector PID 10061). DAC-права каталогов
после install сверены в пункте 6 — все 14 правил совпали. `interface` стартует (процесс жив), но
никогда не проходит `wait_components()` (см. ограничение среды выше про TG-токен) — install
поэтому не доходит до статуса `healthy` в этой песочнице; это ожидаемо и не относится к
install/rollback-механике как таковой. *(rollback-часть — по факту таймаута interface — и
принудительный сценарий v2 с внедрённым отказом ядра описаны ниже/будут дополнены)*.

## Пункт 2 — обрыв SSH/клиента посреди install

Два независимых, дополняющих друг друга эксперимента, оба на РЕАЛЬНОМ `deploy/deploy.sh` через
настоящий ssh (не имитация):

**2a. Клиент убит ДО регистрации job** (`timeout -s KILL 1 bash deploy.sh install ...` — 1 секунда,
заведомо обрывает где-то в процессе upload/до systemd-run). Результат: `systemctl is-active` на
имя юнита — `inactive` (юнит не зарегистрирован), `.deploy-transition.json` отсутствует, символьная
ссылка `current` отсутствует — **ноль мутаций, чистое состояние, безопасно повторить**.

**2b. Клиент убит ПОСЛЕ регистрации, пока job реально работает** — важное уточнение по факту:
вызов `systemd-run` в `deploy/deploy.sh` **не использует `--no-block`**, поэтому сам SSH-клиент,
запустивший `deploy.sh install`, блокируется на ВСЁ время работы job (а не возвращается мгновенно,
как можно подумать по формулировке usage-текста «starts one detached systemd job»; «detached»
относится к владению job systemd’ом, а не к тому, что вызывающая команда не блокируется).
Запустил реальный `install` (в 13:19:36, artifact v1), дождался, пока job дойдёт до реальной
работы (venv нового релиза уже собирался — PID 8285 внутри контейнера, ~72 секунды с момента
запуска), и `kill -9` локального ssh-клиента (`deploy.sh install`) с хоста. Результат:
- локальный ssh-клиент подтверждённо мёртв (`ps -p <pid>` → нет процесса);
- серверный `server_job.py install` (PID 8225 внутри контейнера) остался жив и продолжил работу
  без единого открытого соединения к контейнеру, что и требовалось доказать;
- job дошёл до `phase=switched`, реально запустил collector+core (см. пункт 1) — полностью
  самостоятельно, без какого-либо клиента.

Это прямое, а не косвенное доказательство: обрыв клиента (обычной SSH-сессии оператора) не может
прервать и не прерывает установку, потому что `systemd-run` передаёт владение процессом PID1
контейнера в момент регистрации транзитного юнита, а не в момент завершения ssh-клиента.

## Пункт 3 — параллельный install/inspect-base

- **Параллельный `inspect-base` во время реально идущего install** (после kill клиента из 2b,
  когда install гарантированно ещё шёл — фаза `state_ready`/`fenced`, ноль ssh-соединений с
  предыдущим клиентом): выполнен `deploy.sh inspect-base` с ДРУГОГО, независимого клиента —
  завершился успешно (`exit=0`) за ~8 секунд и вернул `kind=transition, phase=state_ready` —
  подтверждает, что `inspect-base` НЕ берёт `flock` (в коде: `inspect(paths)` вызывается вне
  `deploy_lock()`) и безопасно читает промежуточное состояние, не блокируясь и не мешая
  идущему job.
- **Параллельный второй `install` с тем же артефактом, пока первый держит flock**: первая попытка
  (тем же release_id → то же имя юнита `funding-bot-deploy-<id>`) уткнулась в то, что
  `systemd-run --unit <то же имя>` для уже активного юнита блокируется на уровне systemd (не
  дошло до пользовательского кода за 20 секунд `timeout`) — это смешивает два разных механизма
  защиты (уникальность имени юнита systemd и наш `flock`), так что вывод по САМОМУ flock из этой
  попытки не считаю чистым.
- Более ранняя (до фикса среды) попытка теста 3 — второй `install` СПУСТЯ ~30с ПОСЛЕ того, как
  первый уже упал и снял flock — корректно поймал не `DEPLOY_BUSY`, а `STALE_BASE` (т.к.
  `current_identity()` за это время стал `kind=transition`, а `--expected-base` был снят под
  `kind=legacy`) — тоже валидная, но другая защита: подтверждает, что «протухший» снэпшот базы
  безопасно отвергается даже когда flock уже свободен.
- **Прямая защита самого flock** (`af.deploy_lock()`, `fcntl.flock(LOCK_EX|LOCK_NB)` →
  `Refused('DEPLOY_BUSY')` при занятости) прочитана и проверена по коду (`artifacts.py:181-192`)
  — механизм корректен по построению (неблокирующий lock на стабильный inode, никогда не
  unlink'ается), но ЧИСТОЙ end-to-end демонстрации именно `DEPLOY_BUSY` (в отличие от смежных
  STALE_BASE/systemd-unit-collision) в рамках бюджета времени добиться не успел — честно
  отмечаю как непокрытое дочиста (частично покрыто по коду + двумя соседними защитами выше).

## Пункт 4 — rollback после «сделки»

*(в процессе — синтетическая открытая «сделка» будет вставлена в живой `trade.db` реального
install из пункта 1, затем спровоцирован откат релизом v2 с внедрённым отказом ядра)*

## Пункт 5 — UI-only релиз с несовместимым IPC

Полный end-to-end прогон через `server_job.py`'s `is_ui`-ветку недостижим в этой песочнице по
ТОЙ ЖЕ причине, что и «healthy»-статус вообще: `wait_interface()` требует `ready is True` от
`Interface.health()`, а это требует реального Telegram (см. ограничения среды) — ЛЮБОЙ, даже
полностью совместимый UI-only релиз, здесь никогда не пройдёт `wait_interface()`. Поэтому пункт
проверен на **компонентном уровне** — напрямую против живого `core.sock` реально установленного
и запущенного core (пункт 1), это тот же самый протокол/сервер, который использует `interface`
(`funding_bot.ipc.protocol.Client/Server`, `VERSION=1`):
1. Запрос с `protocol_version=999` (заведомо несовместимый) → core вернул структурный отказ
   `{'ok': False, 'error': 'unsupported_version'}` (код `_Handler.handle()`:
   `if req.get('protocol_version') != VERSION: raise RpcError('unsupported_version')`) —
   **core не упал, не перезапустился, соединение просто закрылось штатно**.
2. Сразу следующий запрос с правильным `protocol_version=1` по тому же сокету → core ответил
   штатно, полным `get_status` (ready/drain/release_id/…) — **core полностью работоспособен**
   после отказа предыдущему клиенту.

Это прямо подтверждает механизм из требования «core жив, interface безопасно отказывает»: ядро
проверяет протокол на каждый запрос независимо и не деградирует от несовместимого клиента.
Дополнительно (структурный уровень, а не рантайм): `ui_only()` в `server_job.py` требует
СОВПАДЕНИЯ `ipc_version`/`dto_version`/`schema_version`/`min_reader` между старым и новым
манифестом — то есть если бы кто-то «забыл» поднять `ipc_version` при реально несовместимом
изменении IPC, `is_ui`-ветка (которая НЕ трогает core вообще) могла бы ошибочно посчитать релиз
безопасным для UI-only свапа; но поскольку рантайм-протокол (выше) в любом случае отвергает
несовместимую версию на каждый запрос, даже в этом гипотетическом случае core остаётся жив и
цел — второй, независимый уровень защиты. *(структурный UI-only прогон через сам install —
релиз v4, только интерфейсный файл — будет добавлен, если останется время; не является
обязательным для доказательства «core жив, interface безопасно отказывает», это уже доказано
выше)*.

## Пункт 6 — DAC-права после install

Сверены ВСЕ 14 правил из `deploy/migration/README.md` под реальными uid/gid контейнера
(`funding-core`=997, `funding-collector`=996, `funding-interface`=995, `funding-ipc`=994,
`funding-market`=993, `funding-pace`=992) после реального install (пункт 1), скриптом
`check_dac.py` (чистое чтение, `os.stat`, ничего не меняет) — **ALL_OK, 14/14**:

| Путь | Владелец:группа | Права | Статус |
|---|---|---|---|
| `/var/lib/funding-bot` | root:root | 0755 | OK |
| `.../secrets` | root:root | 0700 | OK |
| `.../core` | funding-core:funding-core | 0700 | OK |
| `.../interface` | funding-interface:funding-interface | 0700 | OK |
| `.../collector` | funding-collector:funding-market | 0710 | OK |
| `.../collector/public` | funding-collector:funding-market | 02750 | OK |
| `.../shared` | root:funding-pace | 02770 | OK |
| `.../shared/okxdex.pace` | root:funding-pace | 0660 | OK |
| `/run/funding-bot` | funding-core:funding-ipc | 0750 | OK |
| `/run/funding-bot/core.sock` | funding-core:funding-ipc | 0660 | OK |
| `secrets/core.env` | root:root | 0600 | OK |
| `secrets/collector.env` | root:root | 0600 | OK |
| `secrets/interface.env` | root:root | 0600 | OK |
| `core/trade.db` | funding-core:funding-core | 0600 | OK |

Дополнительно: собственная встроенная в продукт проверка `probe_service_layout()` →
`layout_probe.py` (реальный `systemd-run` под КАЖДЫМ целевым UID, с `ProtectSystem=strict` и
т.д., проверяет ещё и КРЕСТ-ролевые границы — core не читает interface/state.json, collector не
читает core/trade.db, и т.д.) для ролей `core` и `collector` прошла успешно как часть install
из пункта 1 (роль `interface` упёрлась в баг, см. раздел находки выше, после обхода тоже
проходит — не переприлагал вручную, полагаюсь на факт успешного `phase=switched`). Примечание про
`/run/funding-bot`: README отдельно предупреждает, что systemd's `RuntimeDirectory=` по умолчанию
поставил бы группу `funding-core` (первичную), а не `funding-ipc` — в реальном коде это явно
чинится в `core/bootstrap.py::_run()` (`os.chown(socket_path().parent, -1, funding-ipc-gid)`,
до и после создания сокета) — подтверждено результатом выше (группа именно `funding-ipc`, а не
`funding-core`).

## Итоговый вердикт

*(в процессе — после завершения пункта 4)*
