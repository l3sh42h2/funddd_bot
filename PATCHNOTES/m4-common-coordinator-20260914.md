# M4 — общий координатор

- Исполнитель: Codex, назначенный пользователем; финальное независимое ревью пользователь передаёт Claude.
- Статус: in_progress. Обновлено 14.09.2026 после EVM release 19:25:10 UTC.
- База: принятый и установленный 0f9037cf72de88131dcbcf8129bfd9455973dcba.
- Ветка: codex/migration-m4-m5.
- ТЗ: docs/migration/M4_COMMON_COORDINATOR_TZ.md.
- Область: runtime/assembly/credentials, общий lifecycle, SOL ports, leg-aware
  проекции, synthetic adapter examples, профильные тесты.
- Инварианты: неизменные frozen identity, один operation root/native journal,
  UNKNOWN блокирует замену, approved bounds не расширяются. Новые live-пары,
  resize и SL не включаются.
- C1: независимые фабрики и scoped credentials в работе.
- C2–C5: не завершены. Выкат общего координатора не выполнен.
- Перед выпуском: независимое ревью, immutable Linux profile, fresh production
  compatibility audit, штатный deploy с reader gates.

## Промежуточный C2/C3 срез

Общий OperationController владеет admission intent/root и atomic terminal
root/intent/deal. Engine и SOL policy вызывают один run_operation; отдельный
SolEngine.execute удалён. Native submit loops и детали политики ещё требуют
оставшегося обобщения и C4 acceptance, поэтому C3 полностью не закрыт.

Durable obligations reader учитывает UNKNOWN perp при reserve=0, pending EVM
approve до clip и HL updateLeverage без deal_id через frozen account scope.
Proposal/approval/resume не разрешаются поверх неизвестного исполнения.
Ошибки отчёта после terminal commit не возвращают финансовую операцию на паузу.

Профиль M4 + SOL C2 + trade engine: 535 passed / 13.55 s (до дальнейших
изменений помощников). Новые targeted tests: 6 passed / 0.30 s. Подключение
SOL port и independent runtime factories ещё в работе, final build не проводился.

По решению владельца реализацию выполняют GPT-5.6 Luna medium helpers,
основной Codex интегрирует изменения. Предыдущие Astra/helpers исчерпали
доступный лимит; новое финальное ревью Astra не запрашивается.
