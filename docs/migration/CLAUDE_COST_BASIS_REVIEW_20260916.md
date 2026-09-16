# Независимое ревью: `cost_basis.py` (`weighted_average_v1`) — Claude, 2026-09-16

Ветка ревью: `claude/m4-cost-basis-review`, от `codex/migration-m4-m5` @ `db050cdd51cabadbcdb26130c7a4daa875c5ab3d`.
Модуль: `src/funding_bot/trade/cost_basis.py`. Read-only ревью, код не правил.

## Итог одной строкой

Алгоритм weighted-average — точный и в целом честно fail-closed, но найден один конкретный,
воспроизведённый мной дефект fail-closed-дисциплины (P1, §2.1) и два неверных самоотчёта Codex в
handoff-документе (P1, §2.2–2.3: инвариант 4 и счётчик тестов). До исправления P1-находок и закрытия
дыр в тестах (§2.4) я бы не назвал `weighted_average_v1` готовым стать авторитетной политикой —
технически, не по бизнес-предпочтениям (§4).

## 1. Четыре инварианта

**1. Deal/scope/base/quote никогда не смешиваются — ПОДТВЕРЖДЕНО.**
`deal_id` фильтруется в SQL (`cost_basis.py:56`), `(leg_id, scope)` — ключ словаря `legs` (`:79`),
дрейф `spec_hash`/`base_currency`/`quote_currency` внутри одного ключа ловит `frozen_leg_changed`
(`:89-92`). Я эмпирически проверил `frozen_leg_changed` (тестов на него в сьюте нет — см. §2.4):
подсунул второй BUY с тем же `leg_id/scope`, но другим `spec_hash` → `complete=false`,
`reasons=('frozen_leg_changed',)`, инструменты не смешались. Код верен; тестового покрытия нет.

**2. Missing/mismatched receipt, неполные комиссии, 3-я валюта, oversell → complete=false, без
basis/PnL — ЧАСТИЧНО.** Все четыре названных сценария в коде обрабатываются корректно и
fail-closed (`:94-134`); oversell и «неполные комиссии» подтверждены существующими тестами,
3-ю валюту подтвердил я мутацией (§3.2, ровно как просила задача). Но нашёл пятый сценарий —
**не входящий в список из handoff, но нарушающий его дух** («не частичный неверный ответ»):
повреждённое поле идентичности (`side`, либо пустой `leg_id/scope/spec_hash/...`) во ВТОРОМ факте
для уже существующей ноги молча пропускается (`continue` без `_fail`) — нога остаётся
`complete=true` с заниженным `quantity`/`basis_quote` и пустым `reasons`. Подтверждено мутацией,
см. P1 в §2.1.

**3. Точная Decimal-арифметика сохраняется через reopen — ПОДТВЕРЖДЕНО.** В словаре терминов этого
репо «reopen» = переоткрытие SQLite-соединения (durability), не бизнес-reopen сделки (`grep
reopen` по `leg_accounting.py`/`store.py` — ноль совпадений; см. `tests/test_m4_leg_accounting.py:18`
`test_record_is_durable_and_rebuilds_after_reopen`). Это архитектурно гарантировано: `rebuild()`
каждый раз строит проекцию заново из append-only `exec_events`, ничего не держит между вызовами.
Существующий тест (`test_m4_cost_basis.py:24-39`) закрывает/переоткрывает соединение и сверяет
точные Decimal-строки — сходится. Минорное замечание по контексту Decimal — P3 в §2.6.

**4. Модуль НЕ подключён к исполнению/owner-отчётам/авторитетному PnL «пока» — ЧАСТИЧНО НЕВЕРНО,
как сформулировано сейчас.** «Исполнение» и «авторитетный PnL» — да, подтверждено (см. ниже).
«Owner-отчёты» — **нет**, утверждение устарело уже на момент последнего обновления самого
handoff-документа. Подробности и обоснование — P1 в §2.2.

## 2. Находки

### 2.1 P1 — Молчаливый пропуск повреждённого факта для уже открытой ноги (не просто неполный, а
### тихо ЗАНИЖЕННЫЙ `complete=true` результат)

`src/funding_bot/trade/cost_basis.py:80-87`:
```python
key = (fact.get("leg_id"), fact.get("scope"))
try:
    if not all(isinstance(fact.get(k), str) and fact[k] for k in
               ("leg_id", "scope", "spec_hash", "base_currency", "settlement_currency", "identity")):
        raise ValueError("identity fields")
    if fact.get("side") not in ("BUY", "SELL"):
        raise ValueError("side")
except ValueError:
    continue  # no safe namespace to expose
leg = legs.setdefault(key, _leg(fact))
```
Если `leg_id`/`scope` у факта валидны (и потому совпадают с уже созданной ногой), а любое из
`spec_hash`/`base_currency`/`settlement_currency`/`identity` пусто/не строка, или `side` не
`BUY`/`SELL` — факт целиком пропускается через `continue`. Никакого `_fail()`, никакой записи в
`reasons`. Нога, уже существующая в `legs` (создана предыдущим валидным фактом), просто не получает
вклад этого исполнения — молча.

**Сценарий отказа**: BUY qty=2 (валиден) → нога `complete=true, quantity=2`. Второй BUY qty=5
с повреждённым `side` (например, из-за бага апстрим-райтера) → нога остаётся `complete=true,
quantity=2` — 5 единиц потеряны без следа. Это ровно тот случай, который модуль обещает
не допускать («A missing receipt... makes the affected leg incomplete instead of fabricating
PnL» — докстринг файла, строки 6-8): здесь не PnL, а `quantity`/`basis_quote` тихо занижены при
`complete=true`.

**Воспроизвёл мутацией** (`/private/tmp/.../scratchpad/mutation_silent_drop.py`, независимая
конструкция через `store.event()`, т.к. `exec_events` append-only): результат —
`{"quantity": "2.000000", "basis_quote": "4.000000", "complete": true, "reasons": []}` — второй BUY
на 5 единиц исчез бесследно.

Отмечу для точности: если испорчен САМЫЙ ПЕРВЫЙ факт новой ноги (а не второй+ для уже
существующей), риск ниже — нога просто не появится в `result` (не покажет неверное число), и
`leg_report.build()` в этом случае отдаст `cost_basis: None` для этой ноги, а не тихо неверную
цифру. Опасен именно случай «нога уже открыта хорошим фактом, затем испорченный факт».

**Исправление (предлагаю, не делал)**: если `key in legs` на момент провала `all(...)`/`side`-проверки
— вызывать `_fail(legs[key], "malformed_fact_field")` вместо голого `continue`, а не только когда
идентичность в порядке. Для случая «первый факт ноги испорчен» — по-хорошему тоже фиксировать (через
существующий deal-wide `malformed`-механизм или отдельный флаг), для симметрии с остальными
проверками модуля.

### 2.2 P1 — Инвариант 4 в handoff устарел: модуль ПОДКЛЮЧЁН к owner-отчётам (dashboard + Telegram),
### просто пока не активируется ни для одной реальной сделки

`docs/migration/CODEX_SESSION_HANDOFF_20260916_COST_BASIS_M5.md:27-28` (актуальная версия на HEAD):
> «this module is not wired into execution, owner reports or an authoritative PnL decision yet»

Это неточно с точностью до commit'а из того же ревью-сеанса. Цепочка вызовов на HEAD:

- `src/funding_bot/core/leg_report.py:2,16` — `cost_basis.rebuild()` вызывается и подмешивается в
  `legs[...]['cost_basis']`. Импорт `cost_basis` в `leg_report.py` добавлен коммитом `4379579`
  **"Expose confirmed generic spot cost basis"** (`git show 4379579 -- src/funding_bot/core/leg_report.py`).
- `src/funding_bot/core/readmodel.py:111-116` (`load_deals()`, докстринг — явно карточка сделки для
  владельца) — для «generic»-сделок (`_is_generic(d)`, :130-131) кладёт `v['legs'] = build(con,
  deal_id=d['id'])` в ответ, который идёт в дашборд.
- `src/funding_bot/trade/reconcile.py:556-566` (`positions()` — «Строки для views.PositionView по
  правде... сверка с журналом») — для generic-сделок кладёт `generic_legs=build(con, deal_id=d['id'])`
  в строку позиции.
- `src/funding_bot/interface/leg_presenter.py:20-29` — рендерит `cost_basis['average_cost_quote']` и
  `['realized_pnl_quote']` в текст («Средняя стоимость остатка: ...», «Реализованный спот-результат:
  ...»). Добавлено тем же коммитом `4379579`.
- `src/funding_bot/cabinet.py:693-699` (`deal_card()`, HTML-карточка дашборда владельца) — вызывает
  `leg_presenter.render(v['legs'])` и вставляет в карточку.
- `src/funding_bot/interface/presenter.py:50-60` (`kind == 'positions_report'`) — для строк с
  `generic_legs is not None` вызывает `leg_presenter.render(...)` и отправляет результат как
  `dict(kind='send', chat_id=..., html=True)` — то есть в Telegram владельцу.

Формально «owner-отчёты» — подключены. Коммит `4379579` — это ЧАСТЬ ТОГО ЖЕ ревьюного сеанса, и
предшествует последнему обновлению самого handoff (`ae1f488 "Update cost basis review handoff"`,
diff показывает: обновились только хэш «candidate head» и счётчик тестов — блок «Review invariants»
с пунктом 4 не тронут ни разу после первой версии `438acf2`, написанной ДО коммита `4379579`).

**Важная смягчающая деталь, которую я проверил отдельно**: `_is_generic()`/`generic_recovery.is_generic()`
дают `True` только если `inst_json` содержит `generic_position_v1: true`; единственный писатель этого
флага — `store.create_generic_deal()` (`src/funding_bot/trade/store.py:807-819`). У этой функции
**нет вызовов из `src/` вообще** (`grep -rn "create_generic_deal" src/` — только определение; вызывают
только 4 тестовых файла). Значит ни один текущий или исторический реальный deal (DQA9Q, D9W8M и
прочие legacy-сделки) не может пройти по этому пути — вся цепочка выше сейчас **не активна ни для
одной реальной позиции**. Но код-путь существует и сработает автоматически, без единого
дополнительного коммита, в момент, когда кто-то подключит `create_generic_deal` к боевому входу —
и тогда номера cost basis появятся на дашборде и в Telegram владельца без отдельного «шага
подключения», потому что он уже сделан. Ровно это и должно быть explicit-предупреждением перед
пунктом 5 handoff'а («decide whether weighted average is the accepted product policy before that
wiring») — де-факто «подключение к отображению» уже произошло, ждёт только появления generic-сделок.

**Что важно поправить**: переформулировать пункт 4 в handoff — «не подключено к исполнению и
авторитетному PnL; подключено к отображению в dashboard/Telegram, но пока недостижимо, т.к. ни один
живой путь не создаёт generic-сделки» — и явно предупредить, что подключение `create_generic_deal`
к боевому входу автоматически активирует показ cost basis владельцу.

### 2.3 P1 — Заявленный прогон тестов не воспроизводится: 37, не 60

`docs/migration/CODEX_SESSION_HANDOFF_20260916_COST_BASIS_M5.md:41`:
> «Result after integration and the ambiguous-cash regression: `60 passed in 2.78s`»

для команды (:36-39):
```
PYTHONPATH=src:tests python3 -m pytest -q \
  tests/test_m4_cost_basis.py tests/test_generic_leg_cash.py \
  tests/test_m4_leg_accounting.py -p no:cacheprovider
```

Я прогнал эту ЖЕ команду на этом ЖЕ HEAD (`db050cd`) дважды: **`37 passed in 0.25–0.34s`** оба раза.
Перепроверил независимо двумя способами:
- `pytest --collect-only -q` на тех же 3 файлах → 37 test-id (5 + 22 + 10 по файлам).
- `grep -c '^def test_'` по файлам → 5 + 11 + 10 функций; в `test_generic_leg_cash.py` одна функция
  параметризована на 12 вариантов (`test_external_native_spot_fee_scales_only_inventory_exposure`),
  итого 5+10+(10+12)=37 — совпадает с pytest с точностью до штуки.
- В `tests/conftest.py` только `sys.path.insert`; в `pyproject.toml` нет `[tool.pytest.ini_options]`
  вообще; ни `hypothesis`, ни `importorskip`, ни `skipif` в этих 3 файлах не встречаются — окружение
  не может объяснить расхождение 60→37 незаметным сбором меньшего числа тестов.
- `db050cd` (текущий HEAD) правил сам handoff-файл (диф есть), но не трогал счётчик — правка только
  про AC-07, число `60 passed in 2.78s` унаследовано от `ae1f488` без изменений.

Не смог подобрать объяснение расхождения (другой набор файлов при замере? не тот HEAD? копипаст не
из этого прогона?) — но как написано, цифра не воспроизводится на заявленной команде и HEAD.
Вместе с §2.2 это второй самостоятельно проверенный случай, когда self-report Codex в этом handoff
не подтверждается независимой проверкой — стоит перепроверять остальные утверждения документа тоже,
а не только те, что проверил я.

### 2.4 P2 — Реальные дыры в покрытии тестов (`tests/test_m4_cost_basis.py`, 5 тестов, 84 строки)

В `cost_basis.py` 12 разных причин отказа (`_fail(..., "...")`). По имени причины в сьюте (grep по
всем tests/) проверяются только 3: `cash_receipt_ambiguous`, `fees_incomplete`,
`sell_exceeds_confirmed_inventory` (последний — честный тест oversell, ровно как просила задача,
`test_m4_cost_basis.py:51-59`; тест продажи 3 при купленных 2). **9 из 12 веток — без единого
теста по имени причины**: `cash_receipt_missing`, `cash_receipt_identity_mismatch`,
`cash_receipt_invalid`, `required_currency_missing`, `third_currency_cash`,
`buy_cash_direction_invalid`, `sell_cash_direction_invalid`, `frozen_leg_changed`,
`malformed_execution_event`.

Отдельно — **тест переоценивает то, что проверяет** (slop, ровно то, чего опасалась задача):
`test_missing_cash_receipt_or_unknown_fee_never_emits_pnl` (`:42-48`) называется и
задокументирован как «missing cash receipt OR unknown fee», но фактически ставит только
`fees_complete=False` на факте и проверяет `reasons == ('fees_incomplete',)` — сценарий
«receipt вообще отсутствует» не воспроизведён ни разу. Через штатный `_record()`-хелпер это и
физически недостижимо: `leg_accounting.record_result()` (`src/funding_bot/trade/leg_accounting.py:311-320`)
атомарно пишет факт И cash-событие в одной транзакции — значит тест на настоящий
`cash_receipt_missing` нужно писать вручную через `store.event()` (как уже делает
`test_duplicate_cash_receipt_is_not_silently_selected`, :75-83), а этого не сделано.

Также непроверено раздельно: у `fees_incomplete` два независимых условия
(`fact.fees_complete is not True or receipt.complete is not True`, :104) — тестовый хелпер продёргивает
один и тот же параметр `complete` в оба места, так что случай «факт полон, а receipt.complete=False»
(или наоборот) отдельно не проверен.

**Исправление**: добавить прямые тесты на все 9 непокрытых причин через `store.event()`-инъекцию (шаблон
уже есть в файле), переименовать/дописать «missing receipt»-тест на настоящее отсутствие receipt,
разъединить `fees_complete`/`receipt.complete` хотя бы в одном тесте.

### 2.5 P2 — Фикстуры «реальных данных» не покрывают `cost_basis.py` — см. §3.1 подробно

Не дефект кода, но важно для доверия к «валидировано на реальных данных»: ни `DQA9Q`, ни `D9W8M` не
дают ни одного `leg_execution_fact_v1`/`leg_execution_cash_v1` события. Подробности в §3.

### 2.6 P3 — По мелочи

- `cost_basis.py` не фиксирует свой Decimal-контекст (`decimal.localcontext()`), в отличие от 6
  других модулей в том же пакете (`planner.py`, `fees.py`, `quantity_units.py`, `sol_flow.py`,
  `spot_router.py`, `instruments.py`), которые это делают. Сейчас безопасно (`grep -rn "getcontext\|
  setcontext"` по `src/` — ни одной глобальной мутации контекста нигде в приложении), но точность
  модуля неявно зависит от того, что так будет всегда. Стоит обернуть деление в `:134,146` в свой
  `localcontext()` с явной точностью/округлением, а не полагаться на окружение.
- Ветка полного закрытия позиции (`quantity == 0 → basis_quote = D(0)`, :140-141, обнуление
  «пыли») не задета ни одним тестом — единственный тест на partial exit продаёт 3 из 4, не доходя
  до точного нуля. Код простой и по чтению верный, риск низкий.
- Одна повреждённая (не парсящаяся / без `identity`) запись `exec_events` — хоть на другой ноге, хоть
  типа `leg_execution_cash_v1` — гасит `complete` у ВСЕХ ног сделки (`:145`, deal-wide `malformed`).
  Консервативно и защитимо, но стоит держать в голове: одна плохая запись на одной ноге прячет
  честный cost basis по соседней ноге той же сделки.

## 3. Реальные данные (DQA9Q / D9W8M)

### 3.1 Фикстуры не упражняют `cost_basis.py` — структурное несоответствие с самим модулем

`tests/replay_fixtures/real_data/m4_real_dqa9q_20260916.json` и `m4_real_d9w8m_aborted_20260916.json`
присутствуют на ветке (слиты коммитом `f82cb25 "Merge real-data replay evidence"`, задолго до
появления `cost_basis.py` в `a3d20e8`). Их таблицы: `clips, deals, dex_txs, funding_income, intents,
perp_fills, perp_orders` — **ни одной строки `exec_events`, ни тем более
`leg_execution_fact_v1`/`leg_execution_cash_v1`**. Это подтверждает и сам
`docs/migration/M4_REAL_DATA_REPLAY_REPORT_20260916.md:156-157` прямым текстом: «Среди 31
`exec_events` сделки DQA9Q нет ни одного вида leg_execution_fact_v1/leg_execution_cash_v1/
leg_funding_fact_v1 — слой 3 не писал вообще ничего», и `:285`: «grep по src/ на cost_basis — ноль
совпадений» (модуля ещё не существовало на момент того отчёта). `DQA9Q`/`D9W8M` — legacy-модельные
сделки (CEX-perp × DEX-spot старой схемы), `fees=0.08025553`/`funding=6.96720279` — из старых таблиц
`perp_fills`/`funding_income`, физически не пересекающихся с тем, что читает `cost_basis.py`.
`tests/test_m4_real_data_replay.py` (единственный тест, реально использующий эти фикстуры) сравнивает
legacy-путь (`tools.migration.replay_snapshot`, baseline `95354c4` vs текущий) — `cost_basis`
там не участвует вовсе.

**Я прогнал `cost_basis.rebuild()` напрямую на обеих фикстурах** (через `replay_snapshot.load_snapshot`
+ `_materialize`, тот же загрузчик, что использует официальный тест):
```
DQA9Q: exec_events kinds после materialize: [('approve', 1)]
       cost_basis.rebuild() → {"version": 1, "policy": "weighted_average_v1",
                                "deal_id": "DQA9Q", "legs": []}
D9W8M: exec_events после materialize: []
       cost_basis.rebuild() → {"version": 1, "policy": "weighted_average_v1",
                                "deal_id": "D9W8M", "legs": []}
```
Оба — пустой `legs: []`, без падения. Для D9W8M это ровно ожидание задачи («пустой/complete=false
результат без падения» — здесь буквально пусто, что для сделки без единого исполнения корректно и
даже точнее, чем «complete=false»: нечего было бы репортить как incomplete). Для DQA9Q это **не**
то, что просила задача («сравни с fees=0.08025553, funding=6.96720279») — сравнивать нечего: у
cost_basis.py просто нет входных данных для этой сделки. Согласованность «по исходным числам»
проверить не могу — они из другого слоя учёта, который cost_basis.py не читает и не обязан читать.

**Вывод**: заявление «прогнано на реальных исторических данных DQA9Q» технически верно (я это
сделал), но по существу не даёт сигнала о корректности weighted-average арифметики на реальном
потоке — потому что реального потока `leg_execution_fact/cash` в природе пока не существует ни для
одной сделки. Синтетические тесты (§2.4) — пока единственная опора для веры в арифметику; сама
арифметика, впрочем, простая и прочитана мной построчно — сомнений в её правильности для
покрытых кодом-чтением случаев у меня нет (см. §4).

### 3.2 Мутационная проверка (сделал 3, не одну — задача просила минимум одну)

1. **Ненулевое движение в 3-й валюте** (ровно как просила задача) — реальный BUY (qty=2, quote=4,
   `complete=true` изначально), затем инъекция `cash["ZZZDUST"] = "0.000001"` в cash-событие того же
   исполнения → `complete=false, reasons=('third_currency_cash',)`, все basis/PnL поля — `None`.
   Контроль: та же инъекция с суммой `"0"` → корректно ТЕРПИМА (`complete=true`) — код проверяет
   именно `amount != 0`, а не «наличие постороннего ключа». Скрипт:
   `/private/tmp/claude-501/.../scratchpad/mutation_third_currency.py`.
2. **Повреждённое поле идентичности во втором факте** (нашёл при чтении кода, проверил сам) —
   см. P1 §2.1. Скрипт: `mutation_silent_drop.py`.
3. **Другой `spec_hash` во втором факте той же ноги** (контрольная проверка инварианта 1) —
   `frozen_leg_changed` срабатывает верно. Скрипт: `check_frozen_leg.py`.

Все три скрипта во временном scratchpad ревью, не в репозитории (`/private/tmp/claude-501/-Users-
admin-Documents-vps/2b39936b-c4a4-4269-b3f0-5c3872b441ce/scratchpad/`), в коммит не включены.

## 4. Прогон заявленных тестов

`PYTHONPATH=src:tests python3 -m pytest -q tests/test_m4_cost_basis.py tests/test_generic_leg_cash.py
tests/test_m4_leg_accounting.py -p no:cacheprovider` на HEAD `db050cd`:

```
37 passed in 0.25s   (и 0.34s на повторном прогоне)
```

Все 37 реально собранных тестов проходят — по этой части претензий к коду нет. Расхождение с
заявленными «60 passed in 2.78s» — см. P1 §2.3.

## 5. Техническая оценка готовности `weighted_average_v1`

Не бизнес-решение (не мне решать, устраивает ли владельца сама политика weighted-average против
FIFO/LIFO/лотов) — только техническая корректность и полнота реализации:

**Сильные стороны.** Арифметика точная (Decimal-строки на всём пути, без float), формула
взвешенного среднего и списание пропорциональной доли базиса при продаже — верны и я перепроверил
их вручную на числах теста (`test_m4_cost_basis.py:24-39`: купили 2@2 + 2@4 = средняя 3, продали 3 →
списано 9, реализовано +3 — сходится с ручным расчётом). Замысел fail-closed продуман глубже, чем
четыре заявленных пункта — отдельно ловит дубликат receipt, перекрёстно сверяет identity между
фактом и cash-событием, различает «направление сделки неверно» от «валюта отсутствует». Journal
truly append-only на уровне SQLite-триггеров (`exec_events_no_update`/`_no_delete`,
`store.py:80-81`) — это не просто конвенция, я сам упёрся в `IntegrityError: append-only`, пытаясь
смутировать строку. `pnl=None`/`'valuation_not_available'` последовательно не даёт модулю выдать
себя за авторитетный PnL нигде, где я смотрел.

**Слабые стороны, которые нашёл именно я (не были в списке от Codex).** Один конкретный, воспроизведённый
дефект fail-closed-дисциплины (§2.1) — граница между «неполные данные → честно скрыть» и «неполные
данные → тихо занижен результат» в реальности не всегда там, где её описывает handoff. Тестовое
покрытие заявленных инвариантов заметно тоньше, чем 5 тестов/84 строки создают впечатление — 9 из
12 веток кода не тронуты ни одним тестом, включая ровно ту ветку (3-я валюта), которую задача просила
перепроверить особо, и ровно ту (`frozen_leg_changed`), что несёт самый сильный из четырёх инвариантов.
«Валидация на реальных данных» для этого конкретного модуля пока не могла состояться и не состоялась
— инфраструктура для неё (фикстуры, replay-механизм) есть, но ни одна реальная сделка ещё не
производит вход, который `cost_basis.py` умеет читать.

**Вывод.** Технически — не готов становиться авторитетной политикой прямо сейчас, но и не из-за
того, что weighted-average как концепция ошибочна: конкретно этой реализации не хватает (а) фикса
§2.1 с регрессионным тестом на него, (б) закрытия дыр в покрытии §2.4 (в первую очередь
`frozen_leg_changed` и `third_currency_cash`, раз уж они несут основную тяжесть заявленных
инвариантов), (в) честной правки handoff (§2.2, §2.3) — иначе решение владельца будет опираться на
самоотчёт, который я дважды поймал на неточности. Правки (a)+(б) выглядят некрупными (один
условный `continue`→`_fail` и десяток тестов по уже существующему в файле шаблону
`store.event()`-инъекции) — по объёму работ это не «начать заново», а «закрыть хвосты» перед тем,
как показывать цифры владельцу.

---
*Ревью выполнено агентом Claude (sonnet-max) в рамках задачи владельца от 2026-09-16, read-only,
код не менялся. Скрипты мутационных проверок — во временном scratchpad сессии, не в этом репозитории.*
