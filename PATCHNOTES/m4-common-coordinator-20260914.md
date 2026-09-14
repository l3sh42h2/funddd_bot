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


## Интеграция независимых адаптеров и terminal evidence

Luna medium реализовали independent component factories, scoped credentials и
SOL boundary. Основной Codex исправил интеграцию exact per-clip bindings:
реальные EVM/SOL spot calls теперь compose обе ноги, а последующие IOC children
используют тот же perpetual adapter/capture/native journal. Сборка не должна
терять native результат и превращать успешный fill в UNKNOWN. Actual Engine
queue tests проверяют compose на каждый клип и отсутствие повторной операции.
Perp-only/recovery paths пока требуют завершения общей composition интеграции.

Исправлены Aster key-view gate, EVM signature-before-send callbacks, dynamic
mode ceiling/drain и выбор wallet только своей сети. C1 production factories
проверены профильными тестами, но шесть комбинаций C1–C5 ещё не приняты.

Новый leg_accounting остаётся неподключённым форматом/проекцией, не готовым
торговым путём CEX/perp–perp. Только terminal Result v2 даёт факт; повтор native
reference не добавляет qty/fees. Unknown fee и fees_complete сохраняются после
reopen/DTO, embedded fee не списывается второй раз, SELL base fee уменьшает
инвентарь. Поздние коррекции комиссий пока требуют отдельного формата.

Проверки этого среза: 590 passed / 14.33 s (M4, SOL C2, trade engine, M3).
Отдельно 110 passed / 8.50 s перед расширением compose-spy assertions.
Это локальные профильные результаты, не immutable Linux release receipt.
Общий пакет не выкачен; production остаётся EVM release 0f9037cf72de-f6e1e96f711e.
Осталось: venue-neutral preflight/план, устранение двух policy submit loops,
perp-only/recovery, пять synthetic adapters через core, generic schema/reader
compatibility, C5 и review packet для Claude. C1–C5 завершёнными не объявлены.


Последний профиль после pre-submit отказов и новых форматов: 597 passed / 14.35 s.
Отрицательная actual Engine проверка несовместимой пары подтверждает ноль swap/IOC
и освобождение резерва только до common claim либо с ABANDONED_UNSIGNED proof.
Исправлена сигнатура tamper fixture: теперь она действительно меняет route evidence,
а не падает раньше с TypeError. Нет native attempt/claim; ошибка не выдаётся за UNKNOWN.

operation_plan.py и perp_preflight.py — новые пока неподключённые форматы/bridge.
Они не означают завершение generic core: план ещё не записывается, reader floor
для generic activation ещё не введён; SolDesk по-прежнему требует снятия HL-привязок.
