# Пять независимых подключений

Каждый файл содержит один адаптер и `register(registry, key)`. Все используют существующий
`NativeAdapter`: quote → prepare → authorize → claim → submit/resolve. В примерах native transport
возвращает доказанный Result v2; он не записывает trade.db. Ни один пример не содержит ключей или live API.

| Добавляется | Файл diff | Регистрация в матрице |
| --- | --- | --- |
| CEX spot | cex_spot.py | fixture_cex_spot |
| CEX futures | cex_futures.py | fixture_cex_perp |
| EVM DEX spot | evm_spot.py | fixture_evm_spot |
| Solana DEX spot | solana_spot.py | fixture_sol_spot |
| DEX futures | dex_futures.py | fixture_dex_perp |

`tests/common_adapter_fixtures.py` загружает эти файлы без изменения общего исполнителя. Он создаёт
отдельные LegSpec и Bindings для каждой ноги. Тип площадки/сети объявлен capabilities, underlying
подтверждается общим asset_id + identity_evidence, account scopes различаются.

Запуск из корня проекта в его Python-окружении:

```sh
PYTHONPATH=src python -m pytest -q tests/test_generic_adapter_matrix.py tests/test_generic_leg_cash.py tests/test_generic_operations.py
```

Матрица использует production registry/context, общий durable coordinator, очередь Engine, текущие
operations/intents/exec_events. Fake transport заменяет только внешнюю площадку. В тестах нет прямого
выставления итогового book, обхода apply или второго журнала резервов.

Подключение реального API требует вместо fake transport: собственных scoped credentials, нативных
quote/submit/resolve/history/observe, достоверной finality, всех валютных потоков, native preflight,
rate limits и crash tests. Успех synthetic примера не разрешает live. Старые EVM/SOL native bindings
сохраняют свои nonce/signature/blockhash/receipt проверки; эти примеры их не заменяют.

Перенос изменений в новый адаптер должен затрагивать только его файл, регистрацию/config и тесты,
если capability уже поддержана. Новая capability требует отдельной спецификации общего контракта.
