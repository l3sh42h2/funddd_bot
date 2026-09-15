# R0 — handoff независимому reviewer

## Что проверять

- **Кандидат кода:** `b6832d7de2f50bc9f22b4ed7583d839dd5f215dc`
  (`Close R0 recovery and quantity regressions`).
- **Ветка:** `codex/migration-m4-m5`.
- **Принятая production-база:**
  `0f9037cf72de88131dcbcf8129bfd9455973dcba`.
- **Текущий production:** прежний EVM-пакет
  `0f9037cf72de-f6e1e96f711e`; этот common-кандидат **не установлен**.
- **Артефакт Linux:** SHA-256
  `0f15b1168bd8e1b3bf32850dcc35ece75ccb7473103abc3de45e69aec338a1ab`.

Проверять кодовый diff следует от production-базы до candidate:

```bash
git fetch origin
git diff --stat 0f9037c..b6832d7
git diff 0f9037c..b6832d7 -- src/funding_bot/trade/generic_operations.py \
  src/funding_bot/trade/generic_recovery.py \
  src/funding_bot/trade/quantity_units.py \
  src/funding_bot/trade/adapters/obligations.py \
  src/funding_bot/trade/engine.py
```

`ee85b15` был промежуточным кандидатом и не имеет успешного Linux receipt.
Его первый full Linux run закончилcя 7 падениями. Не использовать его как
доказательство готовности.

## Что исправлено в R0

### C01: recovery одноногого rehedge

Ранее общий recovery-путь фактически классифицировал rehedge как двухногую
операцию. После native fill, потери ACK и reopen он мог оставить уже выполненную
коррекцию в `PAUSED_RISK`, потому что второго результата по замыслу нет.

Теперь `generic_operations._recover_resolved()` для `plan.kind == "rehedge"`:

- принимает только result ровно одной planned leading leg;
- записывает и доказывает только этот terminal result;
- проверяет результат по состоянию **родительской** позиции в base units в
  допустимом остатке одного native step;
- завершает reserve через существующий `OperationController` один раз.

Проверяющий сценарий:
`test_single_leg_rehedge_ack_loss_recovers_from_parent_book_once`.
Он имитирует отправку, потерю ACK, reopen SQLite и повторное recovery; ожидает
один native send корректирующей ноги и идемпотентный terminal state.

### C02: contracts против base exposure

В `generic_operations._run_rehedge()` native contracts сравнивались с base
exposure из leg-aware projection. Это неверно при multiplier не равном единице.

Новый чистый модуль `quantity_units.py` определяет:

- `native_to_base(qty_native, multiplier)`;
- `base_to_native_floor(exposure_base, multiplier, step_native)`;
- `reconcile_owned_inventory(...)`.

`base_to_native_floor` использует точные integer ratios `Decimal`; он не зависит
от текущей decimal context precision и не округляет объём вверх. До проверки
`reduce_only` contracts переводятся обратно в base units. Это не создаёт нового
sender и не изменяет bounds/authority frozen plan.

Проверяющие сценарии:

- `test_rehedge_multiplier_point_one_uses_native_contracts_without_crossing_owned_base`;
- `test_rehedge_native_floor_never_rounds_exposure_up_at_decimal_context_boundary`.

### C03: личный surplus в spot wallet

Admission уже допускал spot balance больше journal-owned quantity, но
`generic_recovery.check()` требовал равенства для обеих ног. Новая общая policy
сохраняет точное равенство для exclusive perpetual scope и требует для spot только
`observed >= owned`. Избыток возвращается как `personal_surplus_base` и показывается
в detail сверки. Он не попадает в accounting и не увеличивает owned exit quantity.

Проверяющие сценарии находятся в
`tests/test_generic_recovery_owned_inventory.py`: допустимый surplus, недостаток
покрытия и сохранение exact-политики perp.

### Дополнительные Linux-регрессии

- Повреждённый/non-object `inst_json` не роняет resume during obligation scan.
  Он проходит дальше к существующей безопасной проверке frozen instrument.
- UNKNOWN-refusal включает ID последнего исполнявшегося intent.
- Старый unit fixture получил обязательные frozen `it` и `op_id`; CAS в
  `OperationController.commit_transition()` не ослаблялся.
- Два исторических schema-gate ожидания обновлены для schema 5, а текст
  Solana runtime — для фактической нормализованной venue name `hyperliquid`.
  Это тестовые ожидания, не ослабление runtime gate.

## Доказательства выполнения

| Проверка | Результат | Среда / предел |
|---|---:|---|
| R0 C01–C03 точечные regressions | 6 passed, 0.23 s | локальный Python; включает ACK-loss/reopen, multiplier 0.1, floor boundary, spot surplus и perp exact |
| Весь generic профиль | 77 passed, 3.20 s | локальный Python |
| Связанный набор (generic, resume, units, schema gate, SOL recovery message) | 110 passed, 2.77 s | локальный Python |
| JavaScriptCore dashboard/cabinet | 8 passed, 0.31 s | macOS JavaScriptCore; R0 не меняет UI-код |
| Полный immutable Linux profile | **2,833 passed, 12 skipped, 1,527.80 s** | Ireland VPS, Git checkout exact `b6832d7`, отдельная temporary environment |
| Exact-value secret scan | pass, 0 matches | source archive against server secret values; значения не выводились |

Linux skipped tests: восемь JavaScriptCore tests, два root/DAC fixtures, один
release import-path fixture и один opt-in network probe. Это ожидаемые ограничения
профиля, а не скрытые pass.

Первый запуск `b6832d7` без Git metadata был остановлен builder **до тестов**;
повтор выполнен на Git bundle checkout exact SHA. Только второй запуск является
валидным receipt выше.

## Что reviewer должен проверить вручную

1. **Единый lifecycle.** Убедиться, что C01 не создаёт второго commit/sender:
   `OperationController.commit_transition()` остаётся единственной terminal
   транзакцией, UNKNOWN не превращается в новый submit.
2. **C01 shape.** Rehedge должен разрешать только единственную frozen action,
   а parent delta должен вычисляться после receipt этой операции. Проверить
   negative path: лишний/чужой result должен остановить recovery.
3. **C02 units.** Проверить знаки, `multiplier < 1`, `multiplier > 1`, precision
   floor и `reduce_only`. Нельзя получить crossing zero или дополнительный риск
   из-за rounding.
4. **C03 ownership.** Perpetual remains exact by account scope; spot surplus
   не становится inventory сделки и не расходуется в exit/rehedge/будущем SL.
   Недостаток spot balance должен по-прежнему быть mismatch.
5. **Resume safety.** Corrupt `inst_json` не должен открыть путь к native send.
   UNKNOWN refusal обязан сохраниться до доказанного resolve.
6. **Совместимость.** Новые helper functions не меняют persisted schema, reader
   floor, live venue allowlist, admission authority или старый EVM/SOL sender.

## Не является частью R0

- исторический fills/funding/fees/cost-basis/PnL audit;
- отделение Telegram/presentation от core и полная AC-01…AC-29 приёмка;
- resize, reduce, ручной SL, минутный watcher и spot-sale после stop fill;
- включение Bitget/Lighter/Extended либо любой новой настоящей площадки;
- реальные ордера, переводы или изменение production services.

## Решение после review

`approved` reviewer verdict нужен до штатной установки. Даже после одобрения перед
deploy заново проверяются фактически установленная база, открытые операции/UNKNOWN,
совместимость reader/schema, fresh read-only positions и штатный drain. Результат
этого документа — готовность source candidate к независимой проверке, не выкат.
