# M4 accounting replay на реальных данных — отчёт

Дата среза: 2026-09-16. Автор: Claude (Sonnet 5), по прямому поручению владельца. База: коммит
`571d86fd7455caf933b844dcf8a2b381e0dcf671` ветки `codex/migration-m4-m5` (на момент работы — HEAD ветки;
совпадает с тем, что было выкачено на VPS 16.09 22:51 согласно `funding-deploy-571d86f-20260916`). Этот
отчёт — продолжение `docs/migration/M4_REPLAY_REPORT.md` (синтетический replay от 14.09) и закрывает пробел,
явно названный в его разделе «Оставшиеся gaps»: *«Нет sanitized fixture реального согласованного среза legacy
EVM/SOL сделок; синтетика не закрывает полноту AC-15»*.

Денежный код НЕ менялся. Sim-гейт в `operation_roots.py` не трогался. Все выводы ниже — только чтение.

## Резюме

- В проде существует **всего 2 сделки за всю историю проекта**: `DQA9Q` (единственная живая, OPEN, реальная
  прибыль/убыток) и `D9W8M` (ABORTED до какого-либо реального движения денег). Ни одной ЗАКРЫТОЙ сделки нет.
  Solana/HL — ни одной сделки, ни одной строки `hl_fills`/`hl_funding` вообще. Это подтверждает гипотезу
  задания: объём реальных данных для AC-15 крошечный, но не нулевой.
- Нашёлся и заработал **второй, ранее не описанный в `M4_REPLAY_REPORT.md` инструмент** —
  `tools/migration/replay_snapshot.py` — уже готовый харнесс именно для «redacted trade journal snapshot»
  (его собственная докстрока), т.е. для реальных данных, а не для синтетики. До этого отчёта он был
  проверен только на тех же 4 синтетических fixtures, что и `replay_m4.py`.
- Построил из реального (снятого safe-снимком, см. ниже) `trade.db` два снимка в формате этого харнесса —
  для `DQA9Q` (на срез последней доступной метки, 2026-09-16 08:02:55 UTC) и `D9W8M` — и прогнал их через
  `replay_snapshot.replay()`: **baseline (коммит `95354c4`, до появления `accounting.py`/`scoped_accounting.py`
  вообще) и target (текущий `571d86f`) дали побайтово идентичный результат** по позициям, потокам, комиссиям,
  фандингу, газу, cash-basis и PnL на обеих реальных сделках. `mismatches: []`.
- Это подтвердил независимо: пересчитанные значения PnL/funding/fees **совпали, до полной decimal-точности, с
  теми же числами, которые само боевое ядро (`core`, тот же `571d86f`) уже независимо записало в `deal_marks`**
  в тот же момент времени. Совпадение не тавтологично: замер брал только уже случившиеся факты (квитанции газа,
  исполнения, funding_income) и пересчитывал их отдельным офлайн-процессом на двух разных ревизиях кода.
- НО: это равенство проверяет только **legacy-путь** учёта (`marks.journal()`'s прямой запрос по `deal_fills`/
  `funding_income`). Более новый «scoped»-движок (`accounting.sources()`) и полностью generic
  leg/operations-модель (`leg_accounting.py`, `operations` root) **ни разу не активировались на реальных
  данных** — не из-за sim-гейта на исполнение, а потому что: (а) схема `scoped_accounting` никогда не
  мигрирована на боевую БД (ни одной из её таблиц там нет), и (б) таблица `operations` пуста. Подробности и
  почему — ниже, раздел 4.
- **cost basis** (в смысле AC-15 — отдельная от cash-flow, capital-gains-style величина) не проверяем в принципе:
  такой отдельной проекции нет НИ в legacy, НИ в generic коде — только в планах/доках. Исходные данные для неё
  (историческая цена входа, ряд `deal_marks.px_dex/px_perp`) в БД есть.
- Вывод по AC-15 — **частично закрыт**, конкретика в разделе 8.

## 1. Проверка ветки и коммита

```
$ git log --oneline -1 codex/migration-m4-m5
571d86f docs: stop duplicating the drifting test count in the audit doc
$ git merge-base 571d86fd7455caf933b844dcf8a2b381e0dcf671 HEAD
571d86fd7455caf933b844dcf8a2b381e0dcf671
```

HEAD ветки `codex/migration-m4-m5` на момент работы **точно равен** `571d86f` — коммиту, который сейчас
выкачен на VPS. Расхождения нет, отдельно оценивать конфликт не потребовалось.

## 2. Что уже проверял синтетический harness (`replay_m4.py`) — коротко

`docs/migration/M4_REPLAY_REPORT.md` (14.09, база `aaf5737`) сравнивает независимо посчитанный по формулам
legacy EVM/SOL «oracle» с текущими `deal_book`/`journal`-проекциями на 4 РУЧНЫХ fixtures (`tests/replay_fixtures/
m4_evm_known.json`, `m4_solana_known.json`, `m4_evm_unknown_recovery.json`, `m4_solana_unknown_recovery.json`).
Он же остаётся источником истины по AC-16..20 (recovery, дубли, identity, notification-idempotency) — этот
отчёт их не переоценивает. Единственное, что этот отчёт добавляет к AC-15, — реальные данные вместо
синтетических; методология сравнения (recursive diff, `expected_oracle_sha256`, strict evidence) не менялась.

## 3. Второй инструмент: `tools/migration/replay_snapshot.py`

Файл существовал в репозитории (`tests/test_m4_snapshot_replay.py`, 15 тестов, все на синтетике), но
`M4_REPLAY_REPORT.md` о нём не упоминает. Его докстрока: *«Replay a redacted trade journal snapshot against
two funding_bot revisions... Each worker creates a disposable SQLite database from a strict column whitelist,
then calls the revision's read-only accounting projections»* — то есть он изначально спроектирован для
реальных, а не синтетических данных.

Механизм (`tools/migration/replay_snapshot.py:566-617`):

1. Снимок — JSON с ровно одной сделкой: `deals`/`intents`/`clips`/`perp_orders`/`dex_txs`/`perp_fills`/
   `funding_income` (+ HL-таблицы для Solana), урезанные до строгого списка колонок
   `COMMON_COLUMNS`/`SOL_COLUMNS` (`replay_snapshot.py:33-58`) — кошелёк, `raw_tx`, `owner_json` и любые
   не перечисленные поля **отклоняются** `load_snapshot()` (проверено тестом
   `test_real_snapshot_contract_rejects_secret_or_unwhitelisted_columns`).
2. `marks` (spot/perp/native quote) — входные данные снимка, не пересчитываются; обе ревизии видят одну и
   ту же цену.
3. Снимок материализуется в одноразовую SQLite БД **дважды** — под интерпретатором из экспортированного
   `baseline_ref` (по умолчанию `95354c4`, есть закреплённый sha256-архив `tests/replay_fixtures/
   baseline-95354c4.tar.gz`, работает без сети/git) и под `target_root` (проверяемый checkout). Оба процесса
   изолированы (`_block_network()` рвёт сокеты) и вызывают одни и те же читающие функции:
   `engine.deal_book`, `marks.journal` (EVM) / `sol_ledger.ledger` (SOL).
4. Результаты сравниваются рекурсивным diff'ом; классификация учитывает, доказана ли полнота источника
   (`_strict_evidence`), и никогда не выдаёт «эквивалентно», если чего-то не хватает.

Это и есть механизм для AC-15 на реальных данных — не нужно было ничего писать заново, не хватало только
самого снимка из реальной БД.

## 4. Реальные данные: как взяты и что в них

### 4.1 Снятие снимка (без записи и без сети в боевую БД)

На VPS (`admin@34.65.234.12`, сейчас процессы `funding-collector`/`funding-core`/`funding-interface` из
`/opt/funding-bot/current`, БД `/var/lib/funding-bot/core/trade.db`, владелец `funding-core:funding-core`,
режим 600):

```
sudo -u funding-core python3 -c "
import sqlite3
src = sqlite3.connect('/var/lib/funding-bot/core/trade.db')
dst = sqlite3.connect('/var/tmp/claude-m4-replay-20260916/trade-snapshot.db')
with dst:
    src.backup(dst)
"
```

`sqlite3.Connection.backup()` — это read-consistent снимок через SQLite backup API (учитывает WAL), боевой
файл не открывался на запись и не блокировался. Снимок скачан на Mac через `sudo -u funding-core cat ... | ssh`
(без промежуточного chmod), sha256 сверен до и после передачи
(`4a05a464590098213a5039a41212150d54271e6a1cd96000846f1f3c14965629` — совпал), временный файл и директория на
VPS удалены (`sudo rm -rf /var/tmp/claude-m4-replay-20260916`, проверено, что не осталось). Дальше вся работа —
только с локальной копией в scratchpad; в боевую `trade.db` не писал ничего и не открывал её напрямую вторым
клиентом.

### 4.2 Инвентаризация

30 таблиц, из них с данными: `deals`(2), `intents`(2), `clips`(1), `perp_orders`(1), `dex_txs`(2),
`perp_fills`(2), `funding_income`(86), `deal_marks`(990), `exec_events`(31), `tg_updates`(19),
`core_notifications`(3), `flags`(1). Всё остальное, включая `operations`, `operation_intents`,
`route_candidates`, `hl_*`, `sol_*`, `fee_events`, `ingest_cursors` — **пусто**.

| deal_id | state | sim | что произошло |
|---|---|---|---|
| `D9W8M` | ABORTED | 0 (реальный, не sim) | intent `entry` провалился на настройке margin-mode на Aster (`HTTP 400 code -4168 Unable to adjust to isolated-margin mode under the Multi-Assets mode`) ДО создания клипа/перп-заявки/dex-транзакции. Денежных потоков нет вообще. |
| `DQA9Q` | OPEN | 0 | Единственная живая позиция бота (AIW3/aster, BSC). 1 intent, 1 clip BALANCED, 1 perp-заявка FILLED (SELL 4902 @ avg 0.04093), 2 dex-транзакции (approve + swap, обе MINED_OK), 2 perp-филла (сумма qty=4902, quote=200.63886, комиссия=0.08025553 USDT), 86 строк funding_income (сумма 6.96720279 USDT), 990 строк deal_marks с 2026-09-12T18:58Z по 2026-09-16T08:03Z. |

Других сделок нет. Задание верно предположило: «полный replay истории» здесь по объёму — тривиальная задача
(1 содержательная сделка), но не по строгости методологии.

## 5. Три слоя учёта в кодовой базе — и что из них реально работает на проде

Это ключевая находка, определяющая, что именно доказывает равенство baseline/target ниже.

1. **Legacy** — `marks.journal()` (`src/funding_bot/trade/marks.py:117-182`) без обвязки: прямая сумма
   `deal_fills`/`funding_income`/`perp_orders`. Существовал до M4, существует и сейчас как fallback.
2. **`accounting.sources()`** (`src/funding_bot/trade/accounting.py:200-260`) — «account-scoped» движок:
   отдельные таблицы `scoped_perp_fills`/`scoped_funding_income`/`scoped_deal_accounts`/
   `scoped_account_proofs` и т.д. (DDL в `scoped_accounting.py:103-168`), включается только если
   `scoped_accounting.deal_scope(con, deal_id)` вернул не-`None`. `journal()` вызывает
   `accounting.sources()` и, если получил результат, **подменяет им** legacy-числа
   (`marks.py:120-182`, ветка `if scoped is not None`).
3. **Полностью generic leg/operations-модель** — `leg_accounting.py`/`leg_cash.py`
   (`exec_events` вида `leg_execution_fact_v1`/`leg_execution_cash_v1`/`leg_funding_fact_v1`), завязанная на
   таблицу `operations` (`operation_roots.py`) — это и есть путь, который использовал бы generic-координатор
   при живом исполнении (то, что блокирует sim-гейт).

Проверка по реальной БД показывает: **слой 2 неактивен, слой 3 пуст, значит выполняется только слой 1**:

- `scoped_accounting.deal_scope()` (`scoped_accounting.py:432-450`) возвращает `None`, если не существует
  ни таблицы `scoped_accounting_schema`, ни `scoped_deal_accounts` (строка 434-435) — обе отсутствуют в
  боевой БД (полный список таблиц в 4.2). Значит `accounting.sources()` → `None` для ЛЮБОЙ сделки на проде,
  на любой ревизии. `scoped_accounting.migrate()` на бою никогда не вызывался.
- В `deals.perp_scope` (колонка существует специально под это) у `DQA9Q` — `NULL`.
- `operations`/`operation_intents` — 0 строк. Среди 31 `exec_events` сделки `DQA9Q` нет ни одного вида
  `leg_execution_fact_v1`/`leg_execution_cash_v1`/`leg_funding_fact_v1` — слой 3 не писал вообще ничего.

Значит: любое равенство baseline/target на реальных данных ниже — это доказательство того, что **legacy-путь
не сломан M4-рефакторингом**, а НЕ доказательство того, что новый scoped/generic движок даёт тот же ответ —
тот код на реальных сделках попросту ни разу не запускался.

### 5.1 Важная деталь — идентичность аккаунта для DQA9Q уже проверена, но другим модулем

Среди `exec_events` сделки `DQA9Q` есть запись `execution_account_binding_v1` (ts 2026-09-15, т.е. это
писало уже боевое ядро `core`, а не я):

```
account: "acct:v1:aster:a91c9c125e6ff90d748984d7e7b3e81b2dd17f38c9e10a44494afa21cfe43dc4"
provenance: "authenticated_legacy_orders"
venue: "aster", symbol: "AIW3USDT", sim: false
```

Это пишет `src/funding_bot/trade/adapters/execution_scope.py` (`KIND` на строке 16,
`_install_legacy`/`_legacy_candidate` строки 149-201) — модуль **идентичности для исполнения**
(защита от отправки продолжающего ордера не на тот аккаунт), который РЕАЛЬНО опрашивал биржу
(`prove_order`/`native_account`) и подтвердил, чей это аккаунт. Это **не то же самое**, что
`scoped_accounting.bind_deal()` — разные таблицы, разный словарь `proof_kind`
(`frozen_leg`/`public_config`/`public_api_identity`/`migration_manifest` в `scoped_accounting.py:33`, тогда
как здесь `provenance="authenticated_legacy_orders"`), и `account_for()` (`execution_scope.py:52-74`) лишь
СВЕРЯЕТ оба источника, если оба есть — не конвертирует один в другой.

Значит инфраструктура для подтверждения «чей это аккаунт» на реальных данных уже есть и уже отработала — но
готового конвертера/адаптера, который взял бы эту уже доказанную идентичность и завёл её в
`scoped_accounting` (что включило бы слой 2 на реальных данных), в коде нет. Это именно тот «недостающий
шаг», о котором просило задание. **Я НЕ стал сам собирать такую привязку** (не мог независимо повторить
биржевой прувинг без сетевого похода на биржу, а сопоставление словарей `proof_kind` — решение архитектуры,
не моё) — фиксирую как открытый вопрос в разделе 9, не как сделанный шаг.

## 6. Реальные снимки и прогон

Снимки собраны скриптом на основе whitelist самого харнесса (`replay_snapshot.COMMON_COLUMNS`, не
задублированного вручную), из ЛОКАЛЬНОЙ копии `trade.db`. Итог — `tests/replay_fixtures/real_data/
m4_real_dqa9q_20260916.json` и `.../m4_real_d9w8m_aborted_20260916.json`. Проверено: ни кошелька
(`0xe4ebf0815d0980e5a03f7d675f86dc5079fb8919`), ни `raw_tx`, ни `owner_json` в файлах нет (whitelist
отсекает сам).

### 6.1 Марки для `DQA9Q` — как получены, без выдумывания цены

Срез — последняя доступная строка `deal_marks` (2026-09-16T08:02:55.816Z, `ts=1789545775.8165255`; это
фактически «сейчас», сделка всё ещё открыта). `spot_quote`/`perp_quote` взяты напрямую из этой строки
(`px_dex`/`px_perp` — те же числа, что видел бы кабинет владельца).

`native_quote` (цена BNB) я **не изобретал** и не запрашивал во внешних источниках: вывел из двух уже
реальных чисел, которые само боевое ядро уже записало для этой же сделки — `deal_marks.gas` (доллары) этой
строки и точный расход газа по двум квитанциям майнинга (`dex_txs`: approve `gas_used=46506` + swap
`gas_used=220530`, обе `eff_gas_price=71625000` wei):

```
gas_native = (46506 + 220530) × 71625000 / 1e18 = 0.0000191264535 BNB
native_quote = deal_marks.gas / gas_native = 0.013554415750281708084725250 / 0.0000191264535
             = 708.67376172387150 USDT/BNB
```

Проверка на подлог/ошибку формулы: та же формула, применённая к 11 разным строкам `deal_marks` за
~4 суток, даёт плавный ряд 726.5 → 708.7 (естественный дрейф цены, без скачков) — согласуется с тем, что
это действительно подразумеваемая рыночная цена BNB в проде, а не артефакт.

### 6.2 Результат — `DQA9Q`

```
classification: exact_observed_slice_incomplete_sources
verified_equivalent: false
mismatches: []
unsafe_projection_differences: []
strict_evidence.missing_monetary_evidence: []
strict_evidence.source_complete: {fills: null, funding: null}
```

baseline (`95354c4`) и target (`571d86f`) — **идентичны по каждому полю**:

| Поле | Значение (baseline = target) |
|---|---|
| position.tokens_raw | 4902151005294831441679 |
| position.short_contracts | 4902 |
| accounting.spot_net | −200 |
| accounting.perp_net | 200.63886 |
| accounting.fees | 0.08025553 |
| accounting.funding | 6.96720279 |
| accounting.gas_native | 0.0000191264535 |
| accounting.cash_basis_quote | 7.51225284424971829191527475 |
| same_cut_pnl_quote | 6.510014232597412055756261 |

**Независимая сверка.** Ровно та же строка `deal_marks`, записанная боевым `core` (той же ревизией
`571d86f`) в момент замера, а не этим harness'ом: `funding=6.96720279`, `fees=0.08025553`,
`pnl_now=6.5100142325974120557562610`. Совпадение с harness'ом — до полной decimal-точности. Это не тавтология:
harness пересчитывает офлайн, отдельным процессом, из тех же сырых фактов (квитанций, филлов,
funding_income) — совпадение подтверждает, что и методология сбора снимка, и обе ревизии кода считают верно,
а не что сравнение вырождено.

Почему не `verified_known_exact`: `_strict_evidence()` для EVM (`replay_snapshot.py:357`) жёстко возвращает
`source_complete = {fills: None, funding: None}` — для legacy EVM `perp_fills`/`funding_income` нет
курсора/watermark-доказательства полноты истории (в отличие от Solana/HL, где есть `ingest_cursors`). Это
**тот же потолок**, что получает и синтетический `m4_evm_known.json`, прогнанный через этот же
`replay_snapshot.py` (тест `test_same_cut_excludes_later_funding_and_reports_evm_coverage_unknown` в
`tests/test_m4_snapshot_replay.py:185-194` — тоже `exact_observed_slice_incomplete_sources`). Значит это не
дефект реальных данных и не то, что я упустил — это существующее структурное ограничение харнесса для всей
EVM-семьи, зафиксированное до этой работы.

### 6.3 Результат — `D9W8M`

Тривиальное нулевое равенство (`mismatches: []`, все денежные поля `0`, `same_cut_pnl_quote: "0"`), тот же
потолок классификации (`exact_observed_slice_incomplete_sources`) по той же причине. Ценность этого случая —
подтвердить, что harness корректно обрабатывает реальную сделку без единого клипа/ордера, а не падает.

### 6.4 Негативный контроль

`replay_snapshot.replay()` по конструкции кормит ОДИН И ТОТ ЖЕ снимок в обе ревизии — порча значения в самом
снимке меняет обе стороны одинаково и ничего не докажет (проверил это явно: испортил `commission_abs` в
одном филле — расхождения не возникло, как и ожидалось). Значит настоящая проверка «диф вообще что-то ловит»
— это уже существующие синтетические тесты харнесса (`test_known_value_difference_remains_actionable_mismatch`,
`test_missing_execution_quote_is_unknown_not_accepted_as_equivalence` и т.д., все проходят, см. раздел 10). А
для моей методологии сбора снимка настоящий негативный/позитивный контроль — именно совпадение с независимо
посчитанным `deal_marks` из 6.2: если бы скрипт сборки снимка терял или искажал факты, offline-пересчёт разошёлся
бы с тем, что независимо насчитал живой процесс. Разошёлся бы — я бы это увидел.

## 7. Cost basis и funding buckets

Полнотекстовый grep по `src/` на `cost.basis`/`cost_basis` — **ноль совпадений**. Термин существует только в
документах планирования (`AFTER_COMMON_COORDINATOR_PLAN.md`, `MIGRATION_ACCEPTANCE.md`,
`M4_COMMON_COORDINATOR_RESULT.md`) и как явно зафиксированный пробел в самом `replay_m4.py:672`: *«cost basis
has no independent target projection in this harness; dedicated execution tests remain required»*. То есть
cost basis (в смысле AC-15 — отдельная от `cash_basis_quote` величина, что-то вроде цены приобретения для
расчёта реализованного/нереализованного P&L для целей учёта, а не кэш-флоу) **не реализован НИ в legacy, НИ в
generic коде** — сравнивать нечего ни на синтетике, ни на реальных данных, потому что нет второй проекции.

Что есть в реальной БД и могло бы стать входом для такой проекции при её появлении: `deal_marks.px_dex`/
`px_perp` — ряд из 990 точек за всю жизнь сделки; `perp_orders.avg_price` (0.04093) и цена спот-входа,
выводимая из клипа (`dex_out/dex_in` = 4902.151005294831441679/200 ≈ 0.040793). Сырых данных достаточно —
но пока нет самой проекции, тестировать AC-15 в части cost basis не на чем ни на одной ревизии.

«Funding buckets» — предположил, что это отсылка к «fee/funding buckets» из
`PATCHNOTES/m4-accounting-audit-20260915.md` (цепочка `Result → quantity/cash facts → fee/funding buckets →
read model»), т.е. к слою 3 (`leg_cash.py`) — он же подтверждённо пуст на реальных данных (раздел 5). Если
имелось в виду что-то другое — прошу уточнить, не додумывал.

## 8. Вывод по AC-15

| Часть AC-15 | Статус на реальных данных |
|---|---|
| Количества (position/deal_book) | Проверено, точное совпадение baseline/target на единственной реальной сделке с движением денег |
| Cash flows (spot/perp net) | Проверено, точное совпадение, сверено с независимым `deal_marks` |
| Fees | Проверено, точное совпадение, сверено с независимым `deal_marks` |
| Funding | Проверено, точное совпадение, сверено с независимым `deal_marks` |
| PnL (mark-to-market, не exit) | Проверено, точное совпадение, сверено с независимым `deal_marks.pnl_now` |
| Cost basis | Не проверяемо — проекции нет ни в одном коде |
| Статусы (OPEN/ABORTED) | Проверено, совпадают |
| EVM-сторона в целом | Проверено НА LEGACY-ПУТИ; слой 2 (`scoped_accounting`) и слой 3 (`leg_accounting`/generic) не задействованы на реальных данных вообще |
| SOL/HL-сторона | Непроверяемо — реальных Solana/HL-сделок в БД нет ни одной |

**AC-15 на реальных данных закрыт ЧАСТИЧНО**, а именно: доказано, что M4-рефакторинг не сломал legacy-путь
учёта на единственной реальной сделке, где вообще было какое-то движение денег, с независимой сверкой против
боевых чисел. Это настоящий, не вырожденный положительный результат, но это НЕ доказательство эквивалентности
нового generic/scoped читающего движка — тот движок на реальных данных ни разу не запускался, потому что: (а)
на бою не мигрирована схема `scoped_accounting` и ни одна сделка не привязана (`bind_deal` не вызывался), и
(б) `operations`/generic-leg слой пуст (ожидаемо — это то, что блокирует sim-гейт).

**Прежде чем кто-либо примет решение снимать sim-гейт**, по-прежнему нужно, как минимум одно из:
1. Прогнать generic/scoped движок на реальных данных — что требует реального шага миграции
  (`scoped_accounting.migrate()` + `bind_deal()` на проде или на его копии) — решение владельца, не моё;
  первый ingredient (доказанная биржей идентичность аккаунта для `DQA9Q`) уже есть в `execution_scope.py`,
  но конвертера в `scoped_accounting.ProvenScope` нет.
2. Дождаться, пока накопится больше одной реальной сделки — текущая выборка (1 сделка с деньгами, 1 пустая)
  статистически ничтожна, даже если методологически строга.
3. Отдельно реализовать и сравнить cost basis-проекцию — её пока нет ни на одной стороне.

## 9. Открытые вопросы владельцу (не решал сам)

- Стоит ли реально мигрировать `scoped_accounting` на бою и привязать `DQA9Q` (через уже готовую
  идентичность `acct:v1:aster:a91c9c125e...` из `execution_account_binding_v1`), чтобы включить слой 2 хотя бы
  на одной реальной сделке? Это изменение боевой БД — не моя зона по прямому ограничению задания.
- Нужен ли/как должен выглядеть конвертер `execution_scope.py`'s `authenticated_legacy_orders` →
  `scoped_accounting.ProvenScope.proof_kind`? Архитектурное решение, не техническая доработка «просто
  прогнать».
- Репозиторий сейчас публичный (`funding-repo-public-exposure.md`, решение ещё не принято владельцем). Этот
  коммит добавляет реальные (хоть и небольшие, $200 notional) суммы/времена сделки `DQA9Q` в fixture-файлы и
  в этот отчёт. Ключей/секретов там нет (проверено built-in whitelist'ом харнесса и вручную), но сами цифры —
  реальная торговая активность. Не пушу сам (по инструкции задания), но владельцу стоит взвесить это именно
  в контексте вопроса о публичности репозитория, прежде чем пушить эту ветку.

## 10. Тесты

Добавлен `tests/test_m4_real_data_replay.py` (2 теста, используют `tests/replay_fixtures/real_data/*.json` —
намеренно ВНЕ `tests/replay_fixtures/*.json` верхнего уровня, чтобы не попасть под
`test_m4_replay_accounting.py::test_all_replay_inputs_are_explicitly_synthetic_and_secret_free`, которая
проверяет, что там лежит только синтетика; реальные данные там жить не должны, это не костыль, а соблюдение
чужого инварианта). Тесты фиксируют: побайтовое равенство baseline/target, потолок классификации для EVM,
и конкретные реальные числа (funding/fees/cash_basis/PnL) как регрессию.

```
PYTHONPATH=src .venv/bin/python -m pytest -q tests/test_m4_real_data_replay.py
2 passed

PYTHONPATH=src .venv/bin/python -m pytest -q tests/test_m4_*.py tests/test_generic_*.py tests/test_migration_m*.py
701 passed, 1 skipped
```

Полный набор (2935 тестов), окружение `pip install -e ".[dev,trade,sol]"` (не закреплено к
`deploy/requirements.lock`, только для локального прогона): `1 failed, 2931 passed, 3 skipped`. Единственный
провал — `tests/test_sol_c4_release.py::test_lock_is_closed_and_matches_this_environment`, который сверяет
версии транзитивных зависимостей (`rlp`/`hexbytes`/`eth-rlp`/`eth-keys`) с `deploy/requirements.lock`; он не
про учёт и не про эту ветку. Проверил гипотезу отдельно: тот же тест на venv, поставленном строго из
`deploy/requirements.lock` (`pip install -r deploy/requirements.lock && pip install --no-deps -e .`) —
**проходит**. Значит это артефакт того, как я собрал одноразовое окружение для прогона, а не регрессия от
этой ветки; изменений в `pyproject.toml`/`deploy/requirements.lock` в этой ветке нет. Полный прогон всего
набора на venv, поставленном строго из `deploy/requirements.lock`: **`2932 passed, 3 skipped, 0 failed`**
(те же 3 skip, что и в первом прогоне — не относятся к этой ветке).

## 11. Что не делал (осознанно)

- Не трогал `operation_roots.py`/sim-гейт.
- Не запускал `scoped_accounting.migrate()`/`bind_deal()` ни на реальной, ни на локальной копии — не стал
  сам придумывать привязку аккаунта в обход того, что это архитектурное решение (раздел 5.1, 9).
- Не ходил в сеть/на биржу Aster ни разу.
- Не писал и не менял ничего в `src/funding_bot/`.
- Не пушил ветку.
