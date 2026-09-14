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
