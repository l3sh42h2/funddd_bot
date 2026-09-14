# M4/M5 — промежуточное состояние, НЕ акт приёмки

Исполнитель: Codex. Ветка `codex/migration-m4-m5`, исходная база `95354c4`.
Независимый ревьюер: GPT-6 Astra xhigh, назначен владельцем. Дата: 2026-09-14.
Ниже сохранена история checkpoint; актуальное дополнение — в конце документа.

## Реализовано и подключено

- `trade/exposure.py`: единая модель экспозиции/хеджа для EVM и SOL.
  Политики округления изменения и целевой позиции сохранены отдельно, включая off-grid историю,
  множитель, предел одобренной ёмкости, reduceOnly и запрет неявного SELL на выходе.
- `trade/operations.py`: общие начало спотового действия и применение доказанного исхода.
  Их вызывают EVM Engine, SolEngine и EVM recovery. Используются прежние clips и operations,
  нет второго журнала резервов. Переход клипа и reserve/settle одной транзакцией;
  повторное доказательство не списывает бюджет повторно, противоречащее — отказ.
  SOL fee_events также применяются в этой транзакции.
- `trade/ledger_flows.py`: общие проекции quote-потоков из исполненных клипов/заявок.
  Используются EVM итогом, оценкой шорта и SOL ledger. Валюты не объединяются.
  Исторический fallback отсутствующей суммы в ноль явно отделён от строгой проекции,
  в которой сумма при пропуске неизвестна. Миграция не меняет историческую формулу PnL.

## Подготовлено для M5, не активирует выкат

- `core/preflight.py`: read-only единый снимок незавершённых журналов; отсутствие БД/таблиц — отказ.
  Инвентарь включён в deploy-only IPC drain status, но `safe_to_switch` остаётся false.
  «Журналы тихие» не утверждает, что внешний баланс сверён или миграция закончена.
- `deploy/migration/artifacts.py`: точная идентичность артефакта/исходников/зависимостей/runtime/profile,
  проверка receipt, атомарный JSON, стабильный flock, STALE_BASE и SQLite backup с WAL.
  Это primitives, а не законченный supervised deploy job. Старый deploy не заменён.
- Проверки типов исходов, противоречивых повторов, rollback транзакции между клипом/резервом,
  partial/refund, разных политик округления, missing amounts, namespace префикса сделки,
  отсутствующей БД, backup WAL, повторного lock и изменения fingerprint.

## Проверки

- Полный локальный набор: 2219 passed, 3 skipped, 148.03 s.
  Лог: `M4_CHECKPOINT_TESTS.txt`. Это checkpoint, не receipt готового M5-артефакта.
- После сбора полного набора дополнены preflight/artifact guards и тест неизвестного статуса;
  профиль M5 повторён: 8 passed, 0.07 s. Не утверждается, что полный прогон был на неизменном артефакте.
- IPC/HTTP ранее повторены вне sandbox: 4 passed, 2.18 s;
  исходный отказ был запретом bind, не отказом торгового кода.
- Exact-value secret scan пройден; исходники при сканировании на VPS не передаются.
- Время всей подготовки отдельно не измерялось. Счётчики токенов/списания подписки недоступны.
  Дополнительные агенты/модели не запускались. Боевой выкат не проводился.

## Что НЕ выполнено — обязательно для завершения

M4 не завершён. Ещё нужны:
1. Полный единый lifecycle entry/exit/recovery, а не два маршрутизируемых исполнителя.
2. Встраивание общего adapter API M3 в этот lifecycle и завершение его связей с native journals.
3. Детерминированное отображение legacy EVM корневых целей/resume в общие операции без двойного резерва.
4. Полный replay на едином срезе: количества, cash flows, fees, funding, cost basis, PnL, состояния;
   нынешние regression tests не выдаются за такой replay.
5. Удаление зависимостей core от HTML presenters/Telegram compatibility форматов.

M5 не завершён. Ещё нужны законченный supervised server deploy, verified artifact runner,
схемные/reader/rollback ворота, настоящий drain/readiness, перенос runtime/прав/служб,
полная Linux-приёмка, назначенное владельцем независимое ревью и боевой release receipt.
Матрица AC-01..29 не объявляется закрытой. Resize/SL и новые live-площадки не включены.

Linux checkpoint выполнен после последних code-правок: 158 passed за 82.12 s;
отдельная DAC-проверка — 1 passed за 0.05 s. Лог и fingerprint архива:
`M4_CHECKPOINT_LINUX_TESTS.txt`. Временная папка удалена скриптом; production не менялся.
Это профильная Linux-проверка, не полный Linux acceptance M5.

### Continuation: common clip lifecycle and durable drain (in progress)

- EVM and Solana entry/exit now share `OperationController.run_clips`; native signing/recovery ports remain.
- Strict cash-flow projections propagate missing amounts into unknown PnL; legacy numeric fallback is not evidence.
- Core has a durable deploy epoch, independent execution fence, verified release identity and asynchronous native recovery gate. First transition can start fenced before executor/IPC startup.
- Targeted monetary/ledger/drain tests: 124 passed; local IPC/process tests: 17 passed (Unix sockets require sandbox exemption).
- Helpers: GPT-5.6 Sol high for replay and deploy, isolated worktrees. Independent reviewer GPT-6 Astra xhigh is reviewing this checkpoint.
- This is an integration checkpoint, NOT M4/M5 acceptance: replay, generic adapter wiring, presenter boundary, integrated deploy/health verification and VPS switch remain outstanding.

### Общие корневые операции и повторное ревью M5

- EVM подключён к operations: fresh approval, запуск, reserve/settle, пауза и ручной resume той же цели.
- operation_roots детерминированно отображает незавершённые legacy intents, не меняя исторические JSON/hash.
- Новые интеграционные проверки подтверждают fresh approval только остатка и отсутствие лишних отправок.
- M5 supervised runner реализован в ветке, но повторное Astra review обнаружило незакрытые crash/retry/rollback случаи.
- Последний целевой профиль EVM/root/SOL callbacks: 57 passed. Полная приёмка и выкат ещё впереди.

### Реальный срез и Linux DAC (не полная приёмка)

- По отдельному разрешению владельца снят read-only whitelist финансовых записей двух EVM сделок в приватные
  локальные `/tmp` файлы. Ни ключи/подписанные payload, ни raw snapshots в Git не передавались.
- Offline subprocess replay frozen `95354c4` против `3cdfaee` не обнаружил различий проекций на обеих записях.
  Это НЕ verified-known-complete: у legacy EVM нет durable evidence полноты fills/funding;
  при отсутствующей цене native gas итог cash basis/PnL остаётся неизвестным в обеих версиях.
- Срезы SHA256: `0b445c3ec31fd0f8f3a157d7668fe310fd2dae3a4cb622adcadc3f704861512f`,
  `28f519d6cc275170f1d50ba1273706d089ab58908e546451baa0f6f5c516bdc1`.
- После отдельного разрешения владельца код `3cdfaee` загружен во временную папку Ireland VPS для одного Linux
  root DAC fixture: `test_shared_pace_dac_allows_two_uids_but_denies_private_controls` — **1 passed, 0.20s**.
  Использованы синтетические UID/файлы; source archive и временные каталоги удалены. Production services/data не менялись.
- Это отдельный тест прав, не полная проверка Linux-артефакта и не боевой выкат. M4/M5 остаются in_progress.

### Result v2 и scoped ingest: промежуточный исходный код

- Replay false acceptance исправлены и закрыты Astra: отсутствующие fills/network_total и записи после cut не
  дают verified-known-complete. Result v2 отделяет execution/finality от completeness fees и сохраняет raw amounts.
- PerpJournal/futures bindings проверены локально через production_registry на Aster/Gate/HL fake native callbacks:
  один native submit, подпись записана до внешнего callback, повторный claim не отправляет снова, hedge/link flags сохранены.
- Scoped accounting защищает namespace account/venue/symbol, атомарные страницы и cursors, неизменяемую historical
  attribution. Пока это отдельный модуль: существующие store/reconcile/read-model ещё не переключены на него.
- На этом checkpoint адаптеры/фундамент: 60 passed; scoped ingest: 16 passed. Это профильные проверки, не полный
  неизменяемый Linux artifact profile. M4/M5 остаются in_progress; generic production wiring, presenter boundary,
  scope integration, окончательная replay/приёмка и VPS switch ещё требуют работы.

### Проверенные барьеры отправки и перенос решений команд

- PerpJournal отказывает при внешней незавершённой транзакции: rollback вызывающего кода не может отменить
  claim уже отправленного ордера и разрешить повторную отправку. Native Aster/Gate сохраняют доказанный
  terminal partial, Hyperliquid предоставляет instrument для общего адаптера. Astra закрыл bounded review;
  профиль native/adapter/M3: 67 passed.
- Одобрение, nonce/TTL/CAS и проверка полномочий перенесены в core.approvals/core.authority; грамматика команд —
  в operator_commands. Telegram compatibility facades сохранены. Inbox bytes/hash/key/offset не изменены.
- Независимое Astra сравнение authority: 47 639 совпавших сценариев; 4 876 malformed-входов вместо прежнего
  исключения дают IGNORE, иных различий не найдено. Профиль reviewer: 232 passed. Локальный IPC/approval: 26 passed.
- Это промежуточный перенос AC-07: presenters/outbox ещё требуют отделения. Generic execution и scoped accounting
  пока не подключены сквозным образом. M4/M5 остаются in_progress, боевого выката не было.

### AC-07: уведомления и отчёты перенесены в interface (промежуточно)

- core.commands больше не импортирует Telegram views/sender. Коды ответов approval, публичные шапки планов,
  кнопки, короткие operator notices, status/positions/restart snapshots обрабатывает interface.presenter.
- Точные Decimal передаются явными значениями, None сохраняет неизвестность. Snapshot не сериализует
  произвольные Python-объекты. Шапка плана включает только публичные поля, исключает owner/wallet/inst config.
- Plan guard вызывается до постановки плана в outbox. Durable plan_id/nonce/ACK binding сохранены;
  ACK связывает сообщение с планом и не одобряет сделку. Ошибка UI после approval не отменяет единственный submit.
- Старые send/edit/answer продолжают доставляться. Первый DTO2 event атомарно поднимает core_meta.schema_version
  до 2. Новый deploy проверяет DTO reader повторно после остановки writer; регрессии late-event проверены
  для rollback и установки downgrade. Старый verified runner может попытаться выбрать код, но предыдущий
  Journal откажется запускаться по уже существовавшим schema gates; не утверждается, что старый runner знает DTO2.
- Найдено и исправлено падение команды «статус» из-за self.sender.fails в процессе core (Outbox такого счётчика
  не имеет). Свои Telegram delivery/poll health добавляет interface при отображении отчёта.
- Профиль уведомления/approval/IPC/M5/TG: 222 passed, 1 Linux-only skipped, 6.47s. Дополнительный install test
  со свежим DTO2 событием во время drain: 2 passed (включая прежний сценарий). Отдельный полный Linux прогон не делался.
- Astra закрыл два reader-gate замечания и проверил пакет кнопок/ACK; расширение reports проходит отдельное bounded review.
- AC-07 ещё НЕ закрыт: p.html/Refused.html и execution progress/report hooks сохраняют legacy представление;
  core read-model и общий денежный lifecycle требуют дальнейшего переноса. M4/M5 in_progress, production не менялся.

- Дополнение: Astra нашёл утечку HTML-escaped зарегистрированного секрета через резервный лог say/alarm при
  отсутствующем owner. Эти ветки больше не логируют тело сообщения; synthetic secret regression проверяет обе.
  Последний целевой notification profile: 22 passed. Canonical report dataclasses перенесены в ipc/reports,
  Telegram re-exports сохранены для старых callers; core.commands больше напрямую не зависит от tg package.

- Независимое расширенное ревью Astra завершено: P2 логирования закрыт, новых blocking findings не подтверждено.
  AST трёх report dataclass идентичен прежнему; 28 differential stop/resume/positions сценариев сохранили
  сообщения, durable pause/event state и параметры reconciliation. Reviewer profile: 175 passed, 2.88s.
  Проверенный code checkpoint: 6e0c50f; это публикация ветки, не выкат и не завершение M4/M5.

### Продолжение: production submit SOL через общий adapter port

- Добавлен `adapters.execution.submit_ioc`: production registry → native futures binding → PerpJournal → Result v2.
  `sol_flow._hl_hedge` использует этот порт вместо прямого IOC. Native object передаётся старому clip accounting
  только после нормализации terminal result; чужой client ID/неполные суммы дают UNKNOWN без повторной отправки.
- Перед native quote проверяются связи persisted deal → intent → clip, исторические inst_json, client ID,
  venue и simulation namespace. map_perpetual не требует противоположную спотовую ногу.
- Astra обнаружил два существенных случая: pre-send отказ оставлял вечный UNKNOWN; неатомарная проверка отсутствия
  native attempt могла гоняться с живой отправкой. Исправления используют common prepared proof account/instrument
  и native HL journal: BEGIN IMMEDIATE, доказанная общая DB, затем NOT_PLACED; обязательный on_signed отказывает
  перед POST. PREPARED/SIGNED и отсутствие только common nonce не считаются доказательством отсутствия отправки.
- Проверены pre-send отказ → manual rehedge, crash между common claim/native prepare → настоящий startup → manual
  rehedge, и конкурентные recovery/native prepare с двумя потоками. Последнее bounded review ещё ожидается.
- Базовый профиль после scope guards: 94 passed, 1.92s; новая двухпоточная regression отдельно: 1 passed, 0.29s.
  Это не полный Linux artifact profile. M4/M5 остаются in_progress: EVM/spot ports, scoped readers, оставшиеся
  presentation dependencies, final replay/acceptance и production cutover ещё не завершены.

- Astra подтвердил закрытие race: 95 tests, 10 дополнительных namespace/DB сценариев и обратный порядок
  native SIGNED перед recovery. Другая DB, чужой account, отсутствующий common proof, PREPARED/SIGNED отказаны.
- Найден ещё один путь: malformed persisted FINAL без filled/avg_px через старый settle становился нулём и мог
  вызвать второй хедж. SOL immediate/startup recovery переведены на metadata-free `adapters.execution.settle_ioc`:
  persisted scope/side + native proof resolver + тот же Result v2 gate. NOT_FOUND не выводится из одного query.
  RecoveryScope хранит только доказанную идентичность, без выдуманных торговых tick/step; новые filters не запрашиваются.
  Проверки malformed FINAL → UNKNOWN → восстановление исходного доказанного row без второго send и legacy
  recovery без нового common proof прошли (2 passed, 0.42s). Последнее bounded review этого расширения ожидается.

- Terminal matrix дополнена: invalid qty для отмены/отказа тоже UNKNOWN; raw compatibility fill допускается
  только при совпадении quantities/quote/avg с нормализованным результатом. Positive REJECTED остаётся UNKNOWN.
- Положительный CANCELLED (включая native EXPIRED/CANCELED/CANCELLED) проецируется в PARTIALLY_FILLED;
  нулевая отмена — в EXPIRED. Native journal сохраняет исходный статус. Иначе старые cash-flow/fee readers
  пропускали бы частично исполненную отменённую заявку при правильно посчитанном количестве позиции.
- Три actual SOL сценария partial 400 + fill 503 проверяют exact позицию, perp quote flow, SOL ledger и суммы
  комиссий. Они прошли (3 passed, 0.55s); изменённый профиль — 118 passed, 2.28s. Final closure pending.

- Final bounded Astra closure получена: новых blocking findings в SOL futures submit/settle пакете нет.
  Независимый профиль 161 passed, 2.25s; дополнительный persisted EXPIRED400 → PARTIALLY_FILLED + FILLED503
  сохранил native EXPIRED и exact cash flow 150.57525000. Локальный финальный профиль: 166 passed, 2.13s.
  Это closure конкретной границы, не полная приёмка M4/M5; production cutover не выполнялся.
