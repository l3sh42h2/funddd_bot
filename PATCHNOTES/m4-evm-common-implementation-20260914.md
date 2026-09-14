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
