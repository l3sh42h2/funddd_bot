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
