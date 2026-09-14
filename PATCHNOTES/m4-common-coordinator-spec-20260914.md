# ТЗ общего координатора и независимых ног

- Автор: Codex. Обновлено: 2026-09-14. Статус: ready (документ).
- Задача владельца: ТЗ следующего после EVM execution/recovery этапа.
- База: 1274dc9; ветка codex/migration-m4-m5; production code ранее aa446f2.
- Файл: docs/migration/M4_COMMON_COORDINATOR_TZ.md.
- Ревью будущего денежного кода: Astra xhigh по назначению M4/M5. Для документа агент не запускался.
- Результат: C1…C5, production call sites, SOL port, общий lifecycle, пять типов adapters,
  leg-aware projections, тесты расширяемости и условия приёмки/отката.
- Проверки: сверка текущих contracts/guide/call sites, diff --check и secret scan перед push.
- Изменены только документы. Реализация, миграция БД и выкат не выполнялись.
