# Подключение торговой ноги (M3, контракт v1)

M3 предоставляет адаптерный слой. Подключение OperationController к нему и связывание попыток с общим Ledger — M4.
Production startup уже использует общий assembly/CredentialProvider; исторические engine/SolEngine пока ведут
старые операции. Новая пара в реестре не означает разрешение live: нужны профиль/лимиты, проверенный instrument,
полномочия каждой ноги, журнал и авторизация операции. Реальные новые биржи этим этапом не подключаются.

## Файлы

- `trade/adapters/contracts.py`: LegSpec, Action, Quote, Prepared, Result, Observation, ExecutionPage, capabilities.
- `registry.py`, `context.py`: регистрация фабрики, независимая сборка двух ног и передача ресурсов точной LegSpec.
- `native.py`: общая оболочка вокруг существующего исполнителя; до отправки — durable prepare, authorize, claim.
- `futures_bindings.py`: методы существующего PerpLeg для Aster/Gate/Hyperliquid, независимые от спота.
- `spot_bindings.py`: EVM OKX и Solana SpotRouter/SolanaExecutor; raw units, routing/finality остаются нативными.
- `outcomes.py`, `hl_preflight.py`: перевод результатов/отказов и venue-specific проверки.
- `credentials.py`: один process-scoped provider; отдельные EVM/Aster/Gate/Solana/HL credentials.
- `mapping.py`: read-only преобразование исторического InstrumentSpec; исходные JSON и hash не переписываются.
- `attempts.py`: минимальный durable submit barrier. Нативные журналы остаются источником подписи и исполнения.
- `trade/assembly.py`: один вход сборки для core и legacy trader; aliases старых enabled-профилей сохранены.
- `trade/runtime.py`: отдельные build_sol_component / build_hl_component; исторический build_sol_legs — оболочка.

## Как добавить площадку

1. Реализовать её нативный API и binding либо прямой контракт Adapter. Адаптер получает только свою LegSpec,
   scoped credential, соединение/DAO ядра и clock. Передавать ему вторую ногу или Telegram не нужно.
2. Declare capabilities: cex/dex, spot/perpetual, evm/solana там, где сеть относится к этому исполнителю.
   Spot short без модели займа запрещён. Не поддерживаемые cancel/reduce-only/trigger имеют явный отказ.
3. `describe` сверяет полный native instrument, account/subaccount/network, multiplier, шаги, валюты и identity
   proof. Не подставлять multiplier=1 при неизвестном значении. EVM-адреса сверяются в адресных полях; mint и
   fullcoin Solana/HL сохраняют регистр. Исторические LegSpec создаются через versioned mapping.
4. `quote` возвращает границы и expiry. Для ExactIn DEX BUY `bounds.spend` — фиксированная сумма входа, quantity —
   минимум получаемых токенов. Для SELL quantity — расход токенов, `bounds.min_receive` — минимум котировки.
   EVM дополнительно сравнивает свежий on-chain min_receive с одобренным перед отправкой. Solana использует
   effective on-chain minimum, проверенный unsigned candidate и прежний presign_check.
5. Подготовка не считается исполнением. Submit требует journal + core authorize; попытка CLAIMED не отправляется
   снова даже после падения. Для того же action_id нельзя слепо создать другой attempt. M4 добавляет разрешённые
   продолжения после доказанного результата. Разрешение неизвестного результата читает нативный журнал.
6. `Result` сохраняет actual quantity, evidence, finality/provisional, terminal и fees_complete. Пустой список
   комиссий при fees_complete=false означает неизвестность, не бесплатную сделку. CANCELLED может содержать fills.
   Solana confirmed не превращается в SETTLED; только finalized-результат проходит эту границу.
7. `read_executions` возвращает scoped dedup keys и явную полноту. Для HL — overlapping time cursor + tid;
   для Aster/Gate — native trade_id. Не склеивать валюты PnL/комиссий без курса.
8. Зарегистрировать фабрику в `production_registry()` и конфигурацию новой ноги. Pair строится вызовом
   `registry.compose(first_spec, second_spec, context)`. Изменять OperationController/PnL/Telegram не требуется
   для capability, уже описанной контрактом. Новая capability — отдельное расширение и тесты.
9. Добавить contract/recovery tests по образцу `tests/test_migration_m3.py`: обе стороны, multiplier, partial,
   UNKNOWN, restart, stale quote, changed identity, readonly, неподдержанные действия, pagination.

## Связывание с core при M4

Создать AdapterContext и добавить scoped Bindings для каждой LegSpec. Для текущих площадок использовать
futures_bindings.bind / spot_bindings.evm / spot_bindings.solana. Передать реальные core DAO для
attempt_lookup/resolve_row/resolve_ref, clip_ref, on_signed, read_executions и atomic receipt apply.
Затем собрать Pair через `RuntimeRegistry.adapters`. Это один механизм для всех совместимых сочетаний.

DAO — не фиктивные callbacks: M4 должен доказать mapping к старым perp_orders/dex_txs/sol_tx_attempts и единое
Operation/Action ownership. До этого общие submit-методы не активируются в боевом coordinator. EVM allowance
остаётся отдельным journaled действием; новый swap binding не прячет approve в той же попытке.
Приватные unsigned кандидаты Solana хранятся только до expiry; после рестарта нужен новый план, не повтор подписи.

## Результат подключения на фейках

В test_five_new_adapters_only_registration_changes добавлены пять классов через register() и LegSpec:
CEX spot, CEX perpetual, DEX EVM spot, DEX Solana spot, DEX perpetual. Проверяются spot/perp и perp/perp,
BUY/SELL, отказ spot-short/reduce-only и отсутствие обязательного wallet/gas у CEX. Никаких правок бизнес-координатора,
учёта или интерфейса для этих регистраций нет. Это проверка расширяемости, не live-сертификация новых площадок.

Шаблон: `examples/new_cex_spot_adapter.py`. Он намеренно отказывает до реализации bindings и тестов;
его наличие не объявляет новую площадку поддержанной.
