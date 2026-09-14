# M4 — общий координатор

- Исполнитель: Codex, назначенный пользователем; финальное независимое ревью пользователь передаёт Claude.
- Статус: review. Обновлено 15.09.2026: реализация подготовлена к независимой приёмке;
  итоговый Linux artifact profile ещё требуется. Ниже сохранена история промежуточных срезов.
- База: принятый и установленный 0f9037cf72de88131dcbcf8129bfd9455973dcba.
- Ветка: codex/migration-m4-m5.
- ТЗ: docs/migration/M4_COMMON_COORDINATOR_TZ.md.
- Область: runtime/assembly/credentials, общий lifecycle, SOL ports, leg-aware
  проекции, synthetic adapter examples, профильные тесты.
- Инварианты: неизменные frozen identity, один operation root/native journal,
  UNKNOWN блокирует замену, approved bounds не расширяются. Новые live-пары,
  resize и SL не включаются.
- C1–C4: реализация и профильная матрица подключены; C5: локальные проверки пройдены,
  immutable Linux profile и независимое ревью ещё не приняты. Выкат не выполнен.
- Актуальная карта и оставшиеся работы: docs/migration/M4_COMMON_COORDINATOR_RESULT.md.
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

Дополнительный шаг: SOL quantity step читается через общий filters(symbol),
без прямого Hyperliquid identity().sz_decimals. SOL C2/lifecycle: 82 passed / 3.80 s.
OperationPlan roundtrip/deep-frozen authorization/shared identity proof,
account tampering, exit/reduce-only/currency bounds: 5 passed / 0.04 s.
Остальная привязка SolDesk к HL этим изменением не устранена.

## Продолжение после согласования Terra/Luna

Владелец согласовал продолжение второго ТЗ с менее дорогими моделями и итоговым ревью Claude.
Фактически запущены два GPT-5.6 Terra high: legacy lifecycle/SOL decoupling и durable generic plan.
Основной Codex интегрирует учёт, очередь, read-model и матрицу. Luna на этом срезе не запускалась.

Реализуются generic планы в существующих intents/operations/exec_events, EventAttemptJournal,
reader floor 5 при первом несовместимом событии. Generic CEX/perp–perp сделки имеют NULL legacy
chain/token, обе LegSpec заморожены; новые live сочетания не включены.
Новый generic отчёт позиций имеет DTO 4 и outbox/rollback fence; прежний DTO 2 не получает нового поля.

Учет: terminal Result атомарно сохраняет количество и currency cash; native receipt не может
зачесться двум операциям. Raw token flows должны подтверждать reported qty. Perp notional не cash debit;
rent/спонсор/embedded fees различаются, фандинг дедуплицируется по scope/native ID. Полнота исторического
фандинга и FX/PnL этим не доказаны, в отчёте остаются неизвестными.

Пять исполнимых synthetic adapters вынесены в docs/migration/examples, матрица импортирует именно их.
Проверяются вход/выход, обе стороны perp/perp, actual Engine queue, duplicate command, ACK loss/reopen,
base fee, generic startup/positions/dashboard и запрет несовместимых полномочий/лимита экспозиции.
Native SOL×Gate/Aster acceptance и дальнейшие crash/continuation сценарии ещё дописываются.

Промежуточные результаты: generic matrix + cash 30 passed / 2.47 s; учет/DTO regression 43 passed / 0.35 s.
Первый расширенный профиль: 609 passed и 1 регрессия HL unifiedAccount; контроль затем восстановлен,
targeted 14 passed / 0.52 s. Два HTTP-теста кабинета повторены вне sandbox после запрета loopback:
2 passed / 1.22 s. Итоговый неизменяемый Linux-кандидат ещё не собран.

Статус остается in_progress. Этот срез не является выполненным C1–C5, принятым ревью или выкатом.
Production этим продолжением не менялся. После ТЗ остаются полный аудит исторического учёта и сквозная
приёмка миграции; затем отдельный патч добора/сокращения и SL. Полные счётчики токенов/этапного времени недоступны.

Сверенный checkpoint 15.09.2026 00:00 МСК: расширенный профиль M4/SOL C2/legacy engine/M3/preflight/
shared lifecycle/generic matrix — **649 passed / 18.98 s**. Native SOL×Gate/Aster через actual Desk/Engine
прошли вход без HL native methods/config; дальнейшие exit/rehedge/restart ещё проверяются.
Generic normal execution использует общий TwoLegProgram, rehedge — HedgeProgram. Частичный остаток
продолжает прежний root с неизменной identity обеих ног; plan/approval fingerprint обновляется отдельно.
После доказанного ACK loss новый вход не повторяется; новая коррекция требует свежего approved плана.


## Кандидат приёмки 15.09.2026

Один OperationController теперь выполняет generic admission и все terminal/recovery
транзакции; settlement проверяется внутри frozen-context транзакции до unresolved gate.
Native attempt ID и venue order ID сохраняются раздельно. SOL CEX entry/exit/rehedge/
UNKNOWN и EVM Gate partial-exit resume/restart проходят actual Desk/Engine.

Профиль M4/SOL C2/C3/legacy engine/M3/preflight/shared/generic/RH Gate:
**703 passed / 18.63 s**. Отдельный Mac JavaScriptCore: **8 passed / 0.29 s**.
Offline differential старого принятого EVM source и нового source на одинаковых
фикстурах: равны qty/books/clips/roots/orders/fees для EVM entry/partial/full exit и
SOL entry/full exit. Скрипт запрещает сетевые соединения и фиксирует source hashes.
Это не полный исторический PnL-аудит. JSON доказательства — M4_COMMON_LOCAL_EVIDENCE.json.

Попутно исправлен доказанный preflight bug: в маржу включается абсолютная комиссия,
а не её ставка. На недостаточной марже возможен новый правильный отказ до отправки.
USDC/USDT не приравниваются; SOL legacy policy с несовместимым collateral отказывает.

Модели: два GPT-5.6 Terra high (native lifecycle/preflight и generic durable lifecycle),
основной Codex — интеграция, leg cash/DTO/recovery, матрица и проверка совместимости.
Точные суммарные затраты токенов и время всей реализации недоступны. Ревью Claude
владельцем назначено, результат не получен; этот патчноут не означает приёмку/выкат.
