# Аудит полноты учёта generic operations

Дата: 15.09.2026.  Область: новый common/generic путь M4 для двух независимых
ног. Это не аудит всей исторической базы и не разрешение на выкат.

## Что проверено

| Область | Механизм | Вывод в пределах generic path |
| --- | --- | --- |
| Исполнение | `GenericOperationCoordinator._record_result` принимает только terminal, non-provisional `Result`; факт привязан к operation, leg, frozen spec и native reference. | Отправленная, UNKNOWN или provisional попытка не становится учтённым исполнением. |
| Количество | `leg_accounting.fact_from_result` и `rebuild` используют Decimal/raw и multiplier; SELL меняет знак, external base fee уменьшает инвентарь. | Количество восстанавливается по execution facts, не по запрошенному объёму. |
| Идемпотентность | Одинаковый native reference в том же scope повторно не добавляется; тот же reference другой операции/ноги конфликтует. | Повторный resolve/restart не должен задвоить qty или fee. |
| Денежные потоки | `record_result` записывает quantity fact и `leg_execution_cash_v1` в одной SQLite-транзакции. Spot input/output, fee, refundable rent и sponsor различаются. | Невозможно сохранить quantity без соответствующего cash event при нормальном пути записи. |
| Комиссии | Известные комиссии собираются по валютам; embedded/superseded/refundable и комиссия чужого payer не списываются второй раз. Unknown fee сохраняет quantity, но делает completeness ложной. | Неизвестная комиссия не подменяется нулём. |
| Funding | `leg_funding_fact_v1` требует perpetual leg, frozen currency, scope и native ID; ключ дедупликации включает scope. | Одноимённый funding ID разных аккаунтов не смешивается, а повтор в том же scope не зачисляется повторно. |
| Валюты и PnL | `core.leg_report` передаёт quantities, cash, notional и funding отдельными currency buckets. | USDT, USDC и USD не суммируются без отдельного курса; generic report честно возвращает `pnl=None`. |
| Recovery/scope | Для OPEN после recovery retained `partial/interrupted` допустим только при точном terminal proof, полном target и нулевом reserve. | Повреждённое proof не открывает scope для новой даже disjoint операции. |

## Закреплённые проверки

На рабочем кандидате выполнены:

- `tests/test_generic_operations.py`: 93 passed. Включает обычный и recovered OPEN scope, а также восемь повреждений terminal proof: нет события, чужой root/intent, другой retained status, невалидный JSON, ненулевой reserve, неполный target и изменённый frozen parent.
- `tests/test_m4_leg_accounting.py`, `tests/test_generic_leg_cash.py`, `tests/test_m4_execution_scope.py`: 55 passed. Включает reopen/rebuild, base fee, multiplier, rent/refund/sponsor, unknown fee, native ID conflict, funding currency/scope, reader floor и DTO boundary.

Негативная proof-матрица намеренно создаёт повреждённую историю только в
тестовой SQLite-базе после удаления её append-only triggers. В production эти
triggers остаются включены; цель теста — проверить fail-closed reader на уже
повреждённых или импортированных исторических данных.

## Что не доказано этим аудитом

1. Полнота *исторических* EVM/SOL/CEX fills, gas и funding для старых сделок.
   Старые журналы имеют отдельные readers и требуют сверки на одном реальном
   временном срезе до установки.
2. Пересчёт итогового PnL между валютами: в generic path намеренно нет FX
   conversion или fabricated USD PnL.
3. Поздняя корректировка комиссии после terminal Result: текущий immutable
   формат честно оставляет fee unknown, но отдельный correction event ещё не
   определён.
4. Фактическая полнота страниц venue history, rate limits и network recovery.
   Это требует отдельной offline копии production DB и read-only venue
   reconciliation, а не fixture tests.
5. Финальный Linux artifact receipt, production drain, schema/reader gate,
   compatibility активных позиций и post-deploy check.

## Итог

В статически прослеженном и профильными тестами покрытом generic path новые
execution facts, cash, fees и funding не дают оснований для нового блокирующего
дефекта. Граница этого вывода намеренно узкая: он не заменяет историческую
сверку, независимое ревью следующего кандидата или операционную приёмку.
