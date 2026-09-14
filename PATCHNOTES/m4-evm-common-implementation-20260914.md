# EVM execution/recovery, затем общий координатор

Автор и исполнитель: Codex. Назначенный владельцем независимый ревьюер: Astra xhigh.
База: 6cdb4e8; ветка codex/migration-m4-m5. Обновлено: 2026-09-14.
Статус: in_progress. Поручение владельца: выполнить оба ТЗ последовательно.

Порядок: E1–E4 по docs/migration/M4_EVM_EXECUTION_TZ.md; после приёмки этого
пакета C1–C5 по docs/migration/M4_COMMON_COORDINATOR_TZ.md.
Область: trade engine/reconcile, adapters, account bindings, operations и их тесты;
затем assembly, SOL lifecycle, leg-aware проекции и examples.

Начальная сверка: origin обновлён; production release-state указывает
aa446f216047-a82af1f56cf0, source aa446f2160479e037d4c99a956cc8a83c35dbe54.
Это версия сервера, не подтверждение неизменности открытых позиций.

Инварианты: frozen IDs/hash и финансовые факты не переписывать; UNKNOWN не
повторять; один native journal и execution owner; прежние approved bounds;
совместимость recovery/exit активных позиций доказать до выпуска.
Проверки и результаты будут добавлены по фактическому выполнению. Код пока не выкачен.
Resize и SL остаются отдельным последующим патчем. Откат не восстанавливает старую БД.

## Первый пакет: кандидат EVM execution/recovery

Реализованы общий submit/resolve EVM spot и Aster/Gate, execution-only legacy
account proofs, per-connection signing views с единой DB authority, reader4,
атомарная активация всех accounts на startup, неизменные native nonce/order journals.
Доказательства и call sites: docs/migration/M4_EVM_EXECUTION_INVENTORY.md.
Профиль 726 passed/14.32 s, отдельные actual-core startup и reserve regressions.
Astra xhigh закрыл выявленные денежные дефекты в ограниченном ревью; дополнительный
назначенный помощник проверяет startup. Полной M4 приёмки пока нет.

Совместимость: floor4 после активации/common attempt; reader2/3 обратно не допускается.
Genuine unverified legacy — activation blocker; текущий проверенный срез содержит
только DQA9Q OPEN с verified units. Перед установкой срез проверяется заново.
Ни scoped accounting attribution, ни frozen IDs/JSON, ни исторический PnL не менялись.
Второй пакет общего координатора пока не реализован; production остаётся aa446f2.

Дополнительная проверка запуска: captured startup time, health читает actual reader floor,
installer schema4 сверяет его с БД перед undrain; conflicting scoped account блокирует
legacy adoption. Actual core startup → full/partial DQA9Q exit: passed без реальной сети.
Профили: 83 passed/1 skipped (scope, notifications, installer), локальные IPC/process
20 passed/4.88 s вне socket sandbox; ранее внутри sandbox были два запрещённых bind.

## Полный Linux-прогон e0065dc и исправления

Полный artifact profile: 2653 passed, 36 failed, 12 skipped за 1317.47 s; кандидат
не принят и не установлен. Отдельно JavaScriptCore: 8 passed/0.28 s; Linux DAC
с фиктивными файлами: 2 passed/0.93 s.

Найдена регрессия execution view: `super()` в наследуемом native IOC получал
объект чужого класса, из-за чего хедж не отправлялся. View теперь имеет subtype
native класса, не создаёт новый transport, вложенные views разворачиваются к
original native; counters/cache общие, signing fence остаётся у своего соединения.
Actual Aster/Gate subclass tests проверяют super, nested no-send recovery, отсутствие
повторной инициализации transport и ровно одну/ноль отправок.

Фикстуры partial DEX route передают approved_min_receive и доказывают его выполнение;
недополучение относительно minOut не выдаётся за валидный refund. Legacy DQA9Q тесты
активируются через настоящий core startup до исполнения, а тесты порчи уже bound
inst_json ожидают отказ обеих ног. Разрешённый authenticated legacy exit проверяется
отдельно; frozen identity ради прохождения теста не переопределяется.

Тест независимости первого collector tick от history заменяет wall-clock порог
(наблюдалось 0.500072 s при лимите 0.5 s) на явную блокировку fake history: tick обязан
вернуть таблицу до снятия блокировки. Производственный collector этим не изменён.
Профиль phase0/phase1/signing/collector gate: 223 passed/6.07 s.
Повторный Linux artifact build и окончательная приёмка остаются обязательными.

Read-only проверка текущих production аккаунтов и активации на приватной серверной
копии БД приостановлена: auto-review требует отдельного разрешения, запрос отправлен
владельцу. Она не выполнена и не считается заменённой synthetic тестами.
