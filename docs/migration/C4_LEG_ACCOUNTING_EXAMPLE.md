# C4: учёт двух независимых ног

Реализация подключена к generic execution/recovery и private read model.
Это описание границы учёта, не акт приёмки всего common coordinator и не live-релиз.

## Путь факта

`GenericOperationCoordinator._record_result` → `leg_accounting.record_result` →
одна транзакция с quantity fact и `leg_cash.record_execution_cash` в существующем
`exec_events`. `core.leg_report.build` восстанавливает обе проекции по deal/operation,
а generic `/positions` и карточка сделки показывают отдельные ноги и валюты.

Только terminal non-provisional Result v2 с точными leg/spec/scope/native reference
может добавить исполнение. Partial/cancelled с ненулевым fill сохраняет количество.
Промежуточные cumulative updates остаются в native journal, не суммируются как новые
исполнения. Повтор terminal evidence идемпотентен; противоречащий повтор отклоняется.
Один receipt внутри одной account scope нельзя зачесть двум операциям.

## Количество и деньги

- Spot: BUY добавляет, SELL уменьшает фактический inventory. Доказанная внешняя
  комиссия в base дополнительно уменьшает inventory и cash. Embedded fee не списывается дважды.
- Raw input/output обязаны совпасть с замороженными активами и reported execution quantity.
  При противоречии откатывается вся транзакция, включая quantity и reader floor.
- Perpetual: contract quantity и notional отдельны. Notional не записывается как расход
  cash/margin. Каждая нога сохраняет собственные settlement/margin currencies.
- Rent deposit/refund — движение cash и возвратного депозита, не торговый расход.
  Sponsor/superseded/embedded metadata сохраняется без ложного повторного debit.
- Funding — отдельный подписанный факт в своей валюте, дедупликация по scope/native ID.
  Перенос того же funding ID на другую operation не добавляет второй платёж.
- Валюты не складываются без курса и времени оценки. DEX mint соответствует точному
  frozen asset_id; паритет USDT/USDC/USD и ticker aliases не предполагаются.

## Полнота и совместимость

`fees_complete=false` сохраняется после reopen/DTO; неизвестная комиссия не равна нулю.
Доказанное количество остаётся доступным при неполной стоимости.
Полнота исторического funding не доказана самим наличием отдельных funding records.
Без valuation данные PnL остаются `None` с причиной, а не фиктивным нулём.
Поздние исправления terminal fee требуют отдельного versioned correction event;
конфликтующие terminal записи сейчас намеренно отклоняются.

Quantity facts используют reader 4; generic plan/cash требуют reader 5 атомарно до
несовместимой записи. Generic position notification — DTO 4, legacy wire остаётся DTO 2
без добавления необязательного поля. Old-reader rejection проверяется до записи в БД.
Исторические legacy `inst_json`/ID не переписываются и старый PnL не пересчитывается здесь.

## Исполнимые доказательства

`tests/test_generic_leg_cash.py`: raw identity/quantity rollback, base fee, rent,
unknown fee, scoped dedup, reader fence, две ноги в DTO, совместимость старого wire.

`tests/test_generic_adapter_matrix.py`: пять adapter examples через production
registry/context/journal, execution и rebuild, actual Engine queue, позиции и карточка.

`tests/test_generic_operations.py`: partial continuation, UNKNOWN и crash/reopen
между dispatch/claim/send/ACK/apply; recovery не создаёт новый submit.
