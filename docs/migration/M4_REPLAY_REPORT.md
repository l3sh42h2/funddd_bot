# M4 accounting replay report

Дата среза: 2026-09-14. Реализация replay: ветка `codex/m4-replay-agent`, база `aaf5737`.
Независимый accounting oracle зафиксирован относительно `95354c4`; это не заявление о полной приёмке M4 или AC-15..20.

## Что проверяет replay

`tools/migration/replay_m4.py` читает только синтетический JSON fixture, создаёт новую временную SQLite DB и
сравнивает два независимо построенных снимка:

1. oracle непосредственно из фактов fixture по формулам legacy EVM/SOL на `95354c4`;
2. текущие `deal_book`, строгие quote projections, `marks.journal` или `sol_ledger.ledger` из выбранного через
   `PYTHONPATH` checkout.

Сравнение рекурсивное и сообщает точный путь поля, expected и actual. Oracle дополнительно закреплён
`expected_oracle_sha256` в каждом fixture, поэтому изменение самого oracle не превращает новый ответ в ожидаемый
молча. В отчёт входят target Git SHA; для незакоммиченного production source добавляется hash `+dirty:<12 hex>`.

Снимок включает:

- позиции спота/перпа, multiplier и delta в базовых единицах;
- quote debit/credit/net, entry cost basis и средние цены входа;
- фактические/оценочные комиссии, funding, EVM gas или Solana network/rent/external fees;
- итоговый PnL на зафиксированном срезе;
- точный `inst_json`, его SHA-256, `inst_hash`, token/mint case и operation hashes;
- состояния deal/intent/clip, активную операцию, confirmed/reserved/unreserved остаток;
- unresolved EVM/Solana/HL identity в native scope и кратность одинаковых notification payload.

В known-fixtures все денежные входы доказаны и требуется точное равенство. В malformed/unknown-fixtures строгие
`spot_net`, `perp_net` и PnL обязаны быть `null`. Числовой legacy fallback показывается отдельно как
`legacy_*`, классифицируется `unsafe_legacy_fallback` и по умолчанию даёт ненулевой exit code. Это не допустимая
эквивалентность.

## Синтетические fixtures

| Fixture | SHA-256 | Покрытие | Результат на `aaf5737` |
|---|---|---|---|
| `m4_evm_known.json` | `6fdcc971b04289ac6b0a479057a7de170ee1011aaec753214a5c3060e92f4ff4` | EVM multiplier=1000, final partial order state, fills/funding overlap, same IDs in another venue, gas, exact PnL | PASS exact; PnL `22.108` |
| `m4_solana_known.json` | `415623268b296533ea5c7d38051b203e5d44df0691862116ca81d802ebcb43a5` | Solana mint case, multiplier=1000, HL pages/account namespace, fee/rent/external cost, funding | PASS exact; PnL `0.83935` |
| `m4_evm_unknown_recovery.json` | `9b3926511e5b19fb67a3c38d383b894f14537d26863680840ee9903a9df386f7` | crash после send, DEX/perp unknown, missing amounts, held reserve, duplicate notification | UNSAFE expected; strict PnL `null` |
| `m4_solana_unknown_recovery.json` | `2fd4fbbb19fb3efff313b044eb72db60c4a3cdd9f3aeccfe119c9ca6d086b45d` | Solana signature unknown, HL action unknown after ACK, held reserve, manual resume, duplicate notification | UNSAFE expected; strict PnL `null` |

Идентификаторы, адреса, account names, hashes, суммы и времена в fixtures синтетические. Реальных ключей,
подписанных payload, live DB или пользовательских строк нет.

## AC-15..20 evidence и границы

| AC | Evidence | Вывод |
|---|---|---|
| AC-15 | оба known-fixtures, `test_*_known_facts_match*` | Точное равенство на двух синтетических закрытых сделках и одном срезе. Этого недостаточно для заявления о replay всех существующих live-сделок. |
| AC-16 | оба unknown recovery fixtures, `test_unknown_outcome_*` | Unknown остаётся unknown, PnL не считается; reserve удержан. Сеть не опрашивается, фактическое recovery после finality этим harness не доказывается. |
| AC-17 | overlap batches и одинаковые IDs другого venue/account, collision tests | Точные повторы дедуплицируются, разные native scopes сохраняются, противоречащий дубль даёт ошибку. Legacy EVM не содержит account scope. |
| AC-18 | точные raw `inst_json`, SHA/hash, EVM lower-case token и case-sensitive Solana mint, multiplier=1000 | Историческая identity сверяется byte/hash exact. Невосстановимые отсутствующие scope не синтезируются. |
| AC-19 | `PAUSED_UNKNOWN`, active operation, confirmed/reserved/remaining, повторный replay | Нет второго operation в fixture; reserve не высвобождается; manual resume остаётся обязательным. Реальная миграция всех незавершённых live operations не проверена. |
| AC-20 | notification multiplicity и idempotent ACK test | Повтор ACK меняет только delivery state временной outbox; operation/clip/SOL/HL attempts остаются byte-equivalent. Полный rebuild production read model не проверен. |

## Запуск

Known-only acceptance profile (exit 0 только при точном равенстве):

```bash
PYTHONPATH=src /Users/admin/Documents/vps/funding_bot/.venv/bin/python \
  tools/migration/replay_m4.py \
  tests/replay_fixtures/m4_evm_known.json \
  tests/replay_fixtures/m4_solana_known.json
```

Полная regression matrix с ожидаемыми unsafe fixtures:

```bash
PYTHONPATH=src /Users/admin/Documents/vps/funding_bot/.venv/bin/python \
  tools/migration/replay_m4.py --allow-unsafe
PYTHONPATH=src /Users/admin/Documents/vps/funding_bot/.venv/bin/python -m pytest -q \
  tests/test_m4_replay_accounting.py tests/test_m4_replay_recovery.py
```

Для проверки другого checkout передать его `src` первым в `PYTHONPATH`; script остаётся в этой ветке. На текущем
незакоммиченном checkout основного исполнителя `1a37ea8+dirty:e8a5b964fa0d` known/unsafe классификация и все поля
совпали; это временный fingerprint, а не commit или release receipt.

## Оставшиеся gaps

- Нет sanitized fixture реального согласованного среза legacy EVM/SOL сделок; синтетика не закрывает полноту AC-15.
- Legacy EVM `perp_fills` и `funding_income` имеют venue namespace без account/subaccount. Replay явно сохраняет эту
  границу и не может доказать разделение двух аккаунтов с совпадающими ID.
- Unknown fixtures доказывают сохранение неизвестности/резерва, но не опрашивают сеть и не доказывают весь resolver
  path для nonce/receipt/blockhash/finality/cancel.
- Cost basis в replay — воспроизводимая проекция доказанных entry facts; отдельной сохранённой cost-basis сущности в
  legacy DB нет.
- Read model rebuild проверен только через неизменность trading state при повторном notification ACK. Полный rebuild
  из production facts и повторная доставка через реальный interface остаются вне этого offline scope.
- Harness не отправляет ордера, не открывает production DB, не запускает сервисы и не выполняет VPS load test.
