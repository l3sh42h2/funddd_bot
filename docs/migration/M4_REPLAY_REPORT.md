# M4 accounting replay report

Дата среза: 2026-09-14. Реализация replay: ветка `codex/m4-replay-agent`, база `aaf5737`.
Независимый accounting oracle зафиксирован относительно `95354c4`; это не заявление о полной приёмке M4 или AC-15..20.

## Что проверяет replay

`tools/migration/replay_m4.py` читает только синтетический JSON fixture, создаёт новую временную SQLite DB и
сравнивает два независимо построенных снимка:

1. oracle непосредственно из фактов fixture по формулам legacy EVM/SOL на `95354c4`;
2. текущие `deal_book`, строгие quote projections, фактический `marks.final_mark` или `sol_ledger.ledger` из выбранного через
   `PYTHONPATH` checkout.

Сравнение рекурсивное и сообщает точный путь поля, expected и actual. Oracle дополнительно закреплён
`expected_oracle_sha256` в каждом fixture, поэтому изменение самого oracle не превращает новый ответ в ожидаемый
молча. В отчёт входят target Git SHA; для незакоммиченного production source добавляется hash `+dirty:<12 hex>`.

Снимок включает:

- позиции спота/перпа, multiplier и delta в базовых единицах;
- quote debit/credit/net; cost basis отдельно не заявлен как проверенный, потому что в target нет независимой
  production-проекции, с которой его можно сравнить;
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
| `m4_evm_known.json` | `ec72d85be9025e43ac317fef402e33979b2b35fb19771e844af9d1e4ddd578fd` | EVM multiplier=1000, final partial order state, fills/funding overlap, same IDs in another venue, gas, exact PnL | PASS exact; PnL `22.108` |
| `m4_solana_known.json` | `2cfa5cf100c14b83cabf986e6c25449be631706e1a493d8d060207de9704a338` | Solana mint case, multiplier=1000, HL pages/account namespace, fee/rent/external cost, funding | PASS exact; PnL `0.83935` |
| `m4_evm_unknown_recovery.json` | `1bfec285895bbd536dbff2076dfad4761ec5f0688563ca1e317c8dfc30d6799b` | crash после send, DEX/perp unknown, missing amounts, held reserve, duplicate notification | UNSAFE expected; strict PnL `null` |
| `m4_solana_unknown_recovery.json` | `db8bf7982313663e9ff603c548476ab721da6dcc142e101337c86ddb36bc4c29` | Solana signature unknown, HL action unknown after ACK, held reserve, manual resume, duplicate notification | UNSAFE expected; strict PnL `null` |

Идентификаторы, адреса, account names, hashes, суммы и времена в fixtures синтетические. Реальных ключей,
подписанных payload, live DB или пользовательских строк нет.

## AC-15..20 evidence и границы

| AC | Evidence | Вывод |
|---|---|---|
| AC-15 | оба known-fixtures, `test_*_known_facts_match*`, отдельные reverted-gas/fee-tolerance/open-funding tests | Точное равенство на двух синтетических закрытых сделках и краевых правилах frozen `95354c4`: receipt gas платится и при revert, fee coverage допускает `0.01`, funding активной сделки ограничен `as_of`. Этого недостаточно для заявления о replay всех существующих live-сделок. |
| AC-16 | оба unknown recovery fixtures, `test_unknown_outcome_*` | Unknown остаётся unknown, PnL не считается; reserve удержан. Сеть не опрашивается, фактическое recovery после finality этим harness не доказывается. |
| AC-17 | overlap batches и одинаковые IDs другого venue/account, collision tests | Точные повторы дедуплицируются, разные native scopes сохраняются, противоречащий дубль даёт ошибку. Legacy EVM не содержит account scope. |
| AC-18 | точные raw `inst_json`, SHA/hash, EVM lower-case token и case-sensitive Solana mint, multiplier=1000 | Историческая identity сверяется byte/hash exact. Невосстановимые отсутствующие scope не синтезируются. |
| AC-19 | `PAUSED_UNKNOWN`, target store read of active operation, confirmed/reserved/remaining | Нет второго operation в fixture; reserve не высвобождается; manual resume остаётся обязательным. Это чтение подготовленного состояния, а не доказательство production lifecycle или миграции незавершённых live operations. |
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
- Cost basis исключён из equality projection: отдельной сохранённой сущности и независимого production read API в
  legacy/target DB нет. Считать одну и ту же helper-функцию по fixture и материализованным тем же строкам было бы
  ложным доказательством; нужны execution/read-model tests либо новый официальный projection.
- Read model rebuild проверен только через неизменность trading state при повторном notification ACK. Полный rebuild
  из production facts и повторная доставка через реальный interface остаются вне этого offline scope.
- Harness не отправляет ордера, не открывает production DB, не запускает сервисы и не выполняет VPS load test.
