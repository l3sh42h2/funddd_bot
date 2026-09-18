# binance-perp-spot-adapters-20260918: Binance USDⓈ-M Futures + Spot — новые ноги-адаптеры (ВЫКЛЮЧЕНЫ)

Автор: Claude (Sonnet max)
Task: владелец 18.09 — «добавь в торговлю binance. данные есть в чате twap/transfer. бинанс фьючи и спот.»
Уровень 4-5 (новая биржа = новая связка), денежный код — максимальная осторожность.
Исходная база: `codex/migration-m4-m5` @ d77f9d8ab27001a5bb32004dc90fb396f3d031b6 (16.09 21:56:48 +0300).
Ветка: `claude/binance-perp-spot-adapters`. **НЕ ЗАПУШЕНО** — по прямому указанию задачи, владелец пушит сам после
независимой проверки (для новой биржи — по правилам, два независимых прогона).
**Живых сетевых вызовов к Binance не делалось. Sim-гейт, .env, trade.db на VPS, деплой — не трогал.**

## Что реализовано

1. **`src/funding_bot/trade/binance_trade.py`** — нативный трейдер Binance USDⓈ-M Futures (перп-нога):
   подпись HMAC-SHA256 (query-string, `X-MBX-APIKEY`), эндпоинты `/fapi/v1/…` и `/fapi/v2/…` (positionRisk,
   account, balance — v2), чтение позиции/баланса/фильтров/фандинга, IOC-ордер (обычный и reduce-only), запрос
   исхода по `origClientOrderId`, `settle_unknown` (без повторной отправки), история сделок и фандинга
   (`fills`/`funding_income`), настройка one-way/margin-type/leverage (`setup`), учёт веса
   (`X-MBX-USED-WEIGHT-1M`) и бана/429 через уже существующий `client.BinanceLike`. Класс `BinanceTrade`
   наследует `adapters.signing_fence.JournalBoundIoc` — тот же барьер подписи, что у Aster/Gate.
2. **`src/funding_bot/trade/binance_spot_trade.py`** — нативный трейдер Binance Spot (спот-нога): та же подпись
   (переиспользует `binance_trade.sign`/`build_query`/`_Secret`/`load_env_keys` — один аккаунт, один секрет для
   обеих площадок, не два параллельных HMAC), эндпоинты `/api/v3/…` (account, order, myTrades, exchangeInfo,
   depth, time). Ордер — MARKET (метод клиента, не используется адаптерным слоем) и LIMIT+IOC (используется
   адаптерным слоем — см. «Архитектурное решение» ниже). `settle_unknown` по образцу перп-ноги.
3. **`src/funding_bot/trade/adapters/outcomes.py`** — добавлена `cex_spot()`: перевод нативного исполнения
   спот-ордера CEX (без wallet/gas/chain/token) в `Result` v2; переиспользует уже существующий венда-независимый
   `_spot_amounts()` (тот же, что у `evm_swap`/`sol_swap`).
4. **`src/funding_bot/trade/adapters/native.py`** — добавлен `CexSpotAdapter` (`market_kind='spot'`,
   `network_family=None`) — **первая боевая (не examples/) реализация** паттерна из
   `docs/migration/examples/cex_spot.py`, который никогда не был зарегистрирован в `production_registry()`.
5. **`src/funding_bot/trade/adapters/cex_bindings.py`** (новый файл) — биндинг CEX-spot ↔ `NativeAdapter`,
   симметричный `futures_bindings.py` (тот — для Aster/Gate/Hyperliquid): quote/submit/resolve/observe/executions
   без кошелька/газа/сети. `Adapter.cancel()` намеренно НЕ поддержан (как и у futures_bindings/spot_bindings —
   ни один существующий venue его не поддерживает; IOC истекает сам).
6. **`src/funding_bot/trade/adapters/registry.py`** — `production_registry()`: добавлены `'binance'` →
   `FuturesAdapter` (как aster/gate/hyperliquid — переиспользует существующий класс, свой код не нужен) и
   `'binance_spot'` → `CexSpotAdapter`.
7. **`src/funding_bot/trade/adapters/futures_bindings.py`** — `close_exempt` (reduce-only исключение из
   min_notional при закрытии позиции) дополнен `'binance'` — было `{aster, gate, hyperliquid}`.
8. **`src/funding_bot/trade/adapters/credentials.py`** — добавлен `CredentialProvider.binance()` (по образцу
   `.gate()`): грузит `BINANCE_API_KEY`/`BINANCE_API_SECRET` один раз, регистрирует в общем реестре редактирования
   логов (`keys._remember_exact`), отдаёт `(ApiSecret, ApiSecret)`. Один загрузчик для обеих ног (общий аккаунт).
9. **Тесты**: `tests/test_binance_trade.py` (35), `tests/test_binance_spot_trade.py` (27),
   `tests/test_binance_cex_adapters.py` (4) — 66 тестов, все на фейковом `requests.Session`, подпись сверяется
   НЕЗАВИСИМЫМ HMAC-SHA256 внутри фейка (как у `test_trade_aster.py`/`test_gate_trade.py`), никаких реальных
   вызовов.

Ключи `BINANCE_API_KEY`/`BINANCE_API_SECRET` — **владелец впишет сам** в `.env` (в репозитории `.env.example`
нет — конвенция проекта: имена документируются в докстринге модуля, как `GATE_API_KEY` у Gate). Схема лимитов
для перп-стороны уже существует и не тронута: `"binance"` давно в `config.PERP_VENUES` (используется дашбордом
как read-only источник), поэтому `owner.py` уже генерирует секцию `[perp.binance]` (leverage, margin_type,
max_slip_bps, …) — пусто = запрещено, как у всех площадок; owner.py в этой задаче не менялся.

## Откуда взят паттерн

- **Структура/стиль ноги** — `trade/aster_trade.py` (эндпоинты Binance-подобного fapi буквально те же пути,
  поля, заголовок веса — Aster клонирует настоящий Binance Futures API) и `trade/gate_trade.py` (загрузка
  ключей `_Secret`/`load_env_keys`/`from_env`, ворота `mode_state()` на каждый вызов). Числа кодов ошибок
  (-1021, -1022, -1111, -2013, -2019, -2022, -4046, -4059, -4164, -4168, -1006/-1007) — те же, что уже проверены
  и обработаны в `aster_trade.py` для того же API.
- **`hyper/transfer_binance_futures_client.py` и `hyper/binance_futures_client.py`** (код ДРУГОГО бота — Transfer;
  прочитан ТОЛЬКО ради механики API, ключи/секреты/бизнес-логика Transfer не копировались и не воспроизводились
  — из файлов взята только ИДЕЯ, не код): подтверждено оттуда независимо от документации — (а) подписанные
  запросы Binance (включая POST/DELETE) уходят query-string в URL, без JSON-тела; (б) заголовок `X-MBX-APIKEY`
  на весь запрос; (в) реальные пути `/fapi/v1/order`, `/fapi/v2/positionRisk`, `/fapi/v2/account`,
  `/fapi/v1/userTrades`, `/fapi/v1/leverage`, `/fapi/v1/marginType`; (г) отсутствие обработки hedge-режима
  (`positionSide`) в обоих файлах — ожидаемо для one-way, как и здесь; (д) синхронизация времени с поправкой на
  половину RTT. Эти файлы НЕ читались ради спот-API (там только Futures) — Binance Spot сделан по официальной
  документации Binance (`/api/v3/…`) и по аналогии со структурой Futures-ноги этой же задачи.
- **CEX-spot паттерн** — `docs/migration/ADAPTER_GUIDE.md` + `docs/migration/examples/cex_spot.py` (шаблон
  регистрации; пример его никогда не проверял на реальном исполнителе — это первая боевая реализация).

## Архитектурное решение и открытый вопрос владельцу

**Перп-нога** реализована как полноценный аналог Aster/Gate: подходит и под старую схему `runtime.py`
(`AsterPerpFactory`/`GatePerpFactory`-подобная обёртка), и под новый `futures_bindings.bind()`. Она НЕ добавлена
в `runtime.py` в виде `BinancePerpFactory` в этой задаче — такой класс требует `profile: str`, который должен
существовать в `owner.PROFILE_IDS`; без профиля класс был бы непроверяемым мёртвым кодом (`cfg.profile_mode()`
падает на неизвестном id). Добавление профиля — часть следующего пункта.

**Спот-нога — архитектурно новая территория, и здесь я останавливаюсь и спрашиваю, а не выдумываю.** В
`owner.py` сейчас ровно два готовых профиля-паттерна: (1) `EVM_PROFILES` (спот — DEX-своп на EVM-сети,
`bsc_okx_aster`/`rh_okx_gate`), (2) `SOL_PROFILES` (спот — Solana). Оба жёстко предполагают, что спот-нога —
это своп на цепочке (`Legs.spot` в `trade/engine.py` — методы `.swap()`/`.build_swap()`/`.balances()`,
кошелёк/газ/сеть). Binance spot — обычный ордер в стакане CEX, без кошелька и цепочки: он НЕ ложится ни в один
из двух паттернов. Втиснуть его силой в `EVM_PROFILES`-форму (как сделал `rh_okx_gate` для FATCOIN) означало
бы придумать несуществующие кошелёк/сеть; сделать третий профиль-паттерн («CEX spot × CEX perp») в `owner.py` —
это решение о схеме лимитов и живого гейта денежного кода, которое я не должен принимать тихо. Кроме схемы
`owner.toml`, реальная живая торговля потребовала бы координатора для CEX-spot ноги — сопоставимого с
`trade/sol_flow.py` (который у Solana играет эту роль отдельно от `trade/engine.py`), — этого модуля для CEX
spot не существует; `execution.py`/`cex_bindings.py` дают только generic quote→submit→resolve, но не
Desk/reconcile/marks/tg-интеграцию.

Fail-closed по умолчанию, принятый в этой задаче: **прямого пути в live для Binance нет вообще** — не просто
`enabled=false` в отсутствующей секции, а полное отсутствие потребителя (`owner.PROFILE_IDS` не тронут,
`runtime.py` не тронут, `Desk`/`sol_flow.py`-подобный координатор не написан). Регистрация в
`production_registry()` — по правилу `docs/migration/ADAPTER_GUIDE.md` — сама по себе не разрешение live.

**Вопрос владельцу (нужно явное ТЗ, не мой выбор):** как назвать и какой формы сделать профиль
`binance_spot_binance_futures` (или другое имя по вкусу владельца) — (а) новая секция схемы `owner.py`
(`[profiles.<id>]`, `[wallets.<id>]` или без кошелька вовсе — просто метка счёта, `[limits.<id>]`) по образцу
`SOL_PROFILES`, но без кошелька/сети; (б) кто пишет координатор уровня `sol_flow.py`/`Desk` для CEX-spot клипов
(разложение на клипы, reconcile при рестарте, `tg`-тексты) — это отдельная, более крупная задача уровня 4-5,
которую эта задача сознательно не берёт на себя без вашего решения о её объёме. До ответа: `binance`/
`binance_spot` в реестре адаптеров — инертные, готовые ноги без потребителя.

## Более мелкие решения (без запроса ТЗ — низкий риск, задокументированы здесь)

- **`settle_unknown` — по образцу Aster, не Gate.** Задача просила «аналогично `_finished_by_text`/
  `_trades_since`», но у Binance (как и у Aster, который клонирует его API) `origClientOrderId` ищется без
  ограничения по времени, в отличие от Gate (text-id живёт у площадки лишь ~60 с — то и вызывало нужду в
  двухсписочной проверке). Поэтому взят более простой алгоритм Aster: `-2013` ×3 + неизменная позиция/баланс +
  отсутствие чужих `userTrades`/`myTrades` в окне. Если живые данные покажут иное — усилить до Gate-подобной
  схемы.
- **Margin type — не сужен до ISOLATED.** У Gate это сделано намеренно кодом (решение владельца 07.09 для
  конкретной сделки FATCOIN на Gate). Для Binance такого решения не поступало — оставлен тот же общий выбор,
  что у Aster: любой `margin_type`, который владелец впишет в `owner.toml` (`perp.binance.margin_type`, ISOLATED
  или CROSSED — как и раньше, пусто = запрещено).
- **cex_bindings.py всегда шлёт LIMIT+IOC, никогда MARKET.** Контракт `Quote` несёт границу (`max_spend`/
  `min_receive`), которая должна быть известна ДО отправки (как `price_cap` у перпа, как on-chain `min_receive`
  у EVM-свопа) — чистый MARKET её не даёт. `BinanceSpotTrade.order(..., price=None)` (MARKET) остаётся методом
  клиента (задача явно просила market и limit IOC) — просто адаптерный слой его не вызывает.
- **`resolve()` спот-ноги — только в пределах процесса.** `cex_bindings.py` хранит `{attempt_id: side}` в
  памяти (не в БД): `attempts.AttemptJournal` — общий, venue-агностичный durable barrier, но он не несёт `side`.
  Восстановление стороны заявки после рестарта процесса требует отдельного персистентного поля — отложено вместе
  с остальной нерешённой профильной обвязкой (это deталь реализации, не денежный риск: до `submit()` крах
  безопасен всегда, `NativeAdapter.submit` уже превращает любое исключение после claim в `UNKNOWN`).

## Тесты и прогон

- `tests/test_binance_trade.py` — 35 тестов: подпись (независимый HMAC-SHA256 в фейке), ворота режима
  (dry/readonly/live/пауза, hedge-исключение), фильтры/инструмент, позиция/баланс (`None` ≠ 0), статусы IOC
  (FILLED/PARTIALLY_FILLED/EXPIRED), коды ошибок (-2019/-4164/-1111/-2022/-1022/-1001/-1006 — не повторяет
  отправку), транспортные сбои → UNKNOWN, вес/429/418/бан, `settle_unknown` (найдено/не найдено/чужая сделка),
  часы, `setup` (idempotent margin-type/leverage, конфликт Multi-Assets -4168), секрет никогда не печатается,
  история сделок.
- `tests/test_binance_spot_trade.py` — 27 тестов: то же (переиспользует общую подпись), плюс MARKET/LIMIT+IOC,
  отмена, фильтры с точностью актива (baseAssetPrecision/quoteAssetPrecision).
- `tests/test_binance_cex_adapters.py` — 4 теста: регистрация в `production_registry()`; полный dry-run
  вход (спот BUY + перп SELL) и выход (спот SELL + перп BUY reduce_only) на фейках через ОБЩИЙ адаптерный слой
  (`registry.compose`, `futures_bindings.bind`, `cex_bindings.bind`) — архитектурный аналог
  `tests/test_rh_gate_engine.py`, но на уровне adapters/ (см. «Архитектурное решение» — Desk-уровня для Binance
  нет); reduce-only исключение из min_notional; пагинация истории исполнений обеих ног.
- Полный набор: `PYTHONPATH=src python -m pytest -q tests/` — **3073 passed, 4 skipped** (без изменений
  относительно базы) + 1 тест (`test_sol_c4_release.py::test_lock_is_closed_and_matches_this_environment`)
  падает в МОЁМ ad-hoc venv из-за версий транзитивных зависимостей (`rlp`/`hexbytes`/`eth-rlp`/`eth-keys` —
  новее, чем в `deploy/requirements.lock`) — это окружение, не код; тест сверяет установленные версии с
  замороженным деплойным lock-файлом, а мой venv собран обычным `pip install -e ".[dev,trade,sol]"`, не из
  lock. Никакие мои изменения lock/pyproject не трогали.

## Что нужно от владельца перед первым включением

Сейчас включить нечего — нет ни `owner.toml`-профиля, ни координатора, которые могли бы прочитать ключи и
начать торговать (см. «Архитектурное решение»). Когда решение о форме профиля будет принято:
1. Значения `BINANCE_API_KEY`/`BINANCE_API_SECRET` в `.env` на исполняющей машине (имена — этот патчноут; сам
   ключ и права на нём — Futures/Spot оба, или что понадобится — владелец создаёт и вписывает сам).
2. `[perp.binance]` в `owner.toml` — leverage, margin_type, max_slip_bps, touch_frac_max, liq_alert_pct,
   allow_contract_multiplier (схема уже есть, ничего не менялось; пусто = запрещено).
3. Ответ на открытый вопрос выше — схема профиля/лимитов для спот-ноги и решение, кто и когда пишет
   Desk-подобный координатор CEX-spot клипов.
4. Отдельное явное «включаю» и лимиты (`enabled=true`, размеры, допуски) — как и для всех связок этого проекта.
