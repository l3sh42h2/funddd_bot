# Lighter (zkLighter) futures adapter — ВЫКЛЮЧЕНО: подпись L2-транзакций не реализована

- Исполнитель: Claude (Sonnet 5), по поручению владельца (через координатора) — «подключить торговлю на
  Lighter (perp futures)».
- Ветка: `claude/lighter-futures-adapter`, поверх `codex/migration-m4-m5` @ `d77f9d8` (актуальный HEAD ветки
  после `git fetch origin` на момент работы, 18.09.2026).
- Независимая проверка: не запускал сам решение о готовности — пуш и решение о включении делает владелец после
  своей независимой проверки денежного кода (по правилам `vps/CLAUDE.md`, уровень 4-5).
- Production/VPS: не трогал. Живой `trade.db`, `.env`, sim-гейт и деплой не открывал и не менял.
- **Сделки и подписи**: ни одного реального вызова, который подписывает или отправляет транзакцию, не сделано —
  ни в тесте, ни живьём. Публичных (без подписи, без ключей) GET-запросов к `mainnet.zklighter.elliot.ai` было
  ровно два — оба вручную, при разведке архитектуры (см. «Границы» ниже); ключей/секретов ни разу не вводил,
  не печатал и не запрашивал.

## Главный итог (короче некуда)

Подпись ордеров Lighter — **криптографическая схема, для которой я не нашёл ни одной независимо проверяемой
реализации, которую было бы безопасно повторить в этой задаче.** Официальный протокол существует и документирован
на уровне «какие поля» (см. ниже), но сама арифметика подписи распространяется Lighter **только как скомпилированная
платформенная библиотека**, а не как читаемый и воспроизводимый алгоритм. Поэтому: реализовано и покрыто тестами
всё, что можно честно построить на публичной документации без этой арифметики (чтение позиции/баланса/маржи,
инструменты и фильтры рынка, ошибки и rate limit, валидация и построение заявки, чтение статуса транзакции), а
сама подпись — осознанная, явно описанная заглушка (`SigningNotAvailable`), а не тихое угадывание. Ничего не
включено: `owner.toml.example` держит новую секцию с пустыми полями (ПУСТО = ЗАПРЕЩЕНО), ни один профиль не
связывает Lighter со спот-ногой.

## Откуда взят протокол (всё читано 18.09.2026)

- **apidocs.lighter.xyz** — официальная REST/WS-документация:
  - `/docs/get-started`, `/docs/api-keys`, `/docs/trading` («Signing Transactions»), `/docs/rate-limits`,
    `/docs/data-structures-constants-and-errors`.
  - «Сырые» OpenAPI-фрагменты (не пересказ, а машиночитаемая схема — читаны как `<url>.md`, они отдают JSON
    OpenAPI 3.0 прямо в тексте страницы): `reference/account-1.md` (`GET /api/v1/account`), `reference/
    sendtx.md` (`POST /api/v1/sendTx`), `reference/nextnonce.md`, `reference/tokens_create.md`, `reference/
    orderbookdetails.md`, `reference/apikeys.md`, `reference/tx.md`, `reference/accountorders.md`, `reference/
    get_accounts-param-positions.md` (последний — на самом деле отдельный сервис, см. ниже).
  - Ни у одного из перечисленных публичных эндпоинтов (`account`, `nextNonce`, `apikeys`, `tx`, `orderBookDetails`)
    в OpenAPI-схеме нет `security`/`Authorization`-параметра — это подтверждено самой схемой, не только текстом
    доков, поэтому я доверяю этому «публичность» сильнее, чем остальным утверждениям ниже.
- **github.com/elliottech/lighter-python**, файл `lighter/signer_client.py` — официальный Python SDK. Скачан и
  прочитан (не установлен как зависимость — только чтение исходника ради протокола). Ключевой факт: `SignerClient`
  не считает подпись на Python — он грузит через `ctypes.CDLL` платформенную библиотеку
  (`lighter-signer-{darwin,linux,windows}-{amd64,arm64}.{dylib,so,dll}`) и вызывает в ней `SignCreateOrder`,
  `SignCancelOrder`, `SignChangePubKey`, `SignWithdraw`, `SignModifyOrder`, `CreateAuthToken` и т.д. — то есть
  даже **создание read-only auth-токена** (не только торговля) идёт через этот же скомпилированный код.
- **github.com/elliottech/lighter-go** — официальный Go SDK; в корне репозитория каталоги `signer/` (Go-исходник)
  и `sharedlib/` (сборка в те самые библиотеки выше). Сам Go-исходник подписи построчно не читал и не портировал —
  порт криптографии из Go в Python без единого способа проверить результат живым вызовом (задача прямо это
  запрещает) был бы именно тем «неверным тихим угадыванием», от которого предостерегает постановка задачи, а не
  инженерным решением.
- **crates.io/lighter-rs** (docs.rs: `lighters`) — **независимый от Lighter** сторонний порт на Rust. Его
  собственное описание: «a from-scratch Rust port of the official Go SDK… producing byte-identical transaction
  hashes and Schnorr signatures… ECgFp5 curve… Poseidon2… Goldilocks field… ports from the official lighter-go
  implementation, **though they have not been independently audited**». Это единственный источник, назвавший
  саму схему по имени (Schnorr-подпись над кривой ECgFp5, хеш Poseidon2, арифметика в поле Goldilocks — zk-
  дружественная конструкция в духе StarkEx, как и предполагала постановка задачи). Использовать этот сторонний,
  прямо помеченный как неаудированный порт как основу для НОВОЙ Python-реализации в этой задаче означало бы
  положиться на неаудированный порт порта — риск тихой ошибки денежного кода, а не её отсутствие.

## Что публично (без подписи) и поэтому реализовано

Все проверены не только пересказом текста, но и «сырой» OpenAPI-схемой (нет объявленного `Authorization`):

- `GET /api/v1/orderBookDetails?filter=perp` — инструменты/фильтры рынка (та же форма, что уже независимо
  использует существующий read-only скринер `audit_truth/lighter.py`, но код не переиспользован — свой запрос,
  своя ошибка/rate-limit обработка, отдельная зона ответственности, как и у самого этого скринера с `lighter.py`).
- `GET /api/v1/account?by=index&value=...` — `DetailedAccounts{accounts:[DetailedAccount{available_balance,
  collateral, positions:[AccountPosition{symbol, sign, position, ...}], ...}]}` — позиция и маржа.
- `GET /api/v1/nextNonce?account_index=&api_key_index=` — публичный (нонс не секрет).
- `GET /api/v1/apikeys?account_index=&api_key_index=` — публичный реестр ПУБЛИЧНЫХ ключей аккаунта (без
  приватного материала) — можно свериться, что account_index/api_key_index зарегистрированы.
- `GET /api/v1/tx?by=hash&value=...` — статус транзакции по хэшу (`status`: 0 Failed/1 Pending/2 Executed/
  3 Pending-Final). **Не доказано**: поле `event_info` (JSON-строка) вероятно несёт экономику исполнения
  (по аналогии с `TradeWithFunding` у ОТДЕЛЬНОГО сервиса `explorer.elliot.ai/api/accounts/{acc}/logs` — другая
  OpenAPI-схема, не проверялась живым вызовом) — но типизированной схемы `event_info` в OpenAPI нет, поэтому
  `query()` читает только верхнеуровневый `status` и **не** вытаскивает qty/price из непроверенного поля:
  `status=Executed` тоже возвращает `PerpFill(status='UNKNOWN', ...)`, а не угаданный `FILLED`.

## Что реализовано (`src/funding_bot/trade/lighter_trade.py`, новый файл)

- `LighterTrade` — native perp-клиент (mainnet-инстанс; Robinhood-инстанс — вне scope задачи, см. «Границы»).
  `venue = 'lighter'`, `ioc_partial_terminal = False` (см. ниже, почему не угадано `True`).
- Публичные чтения, полностью рабочие и покрытые тестами: `instrument()`, `filters()`, `position()`,
  `available_margin()`, `next_nonce()`, `registered_public_key()`, `health()`, `query()` (статус транзакции),
  `history_account()`.
- Собственная иерархия ошибок (`LighterError`/`LighterApiError`/`LighterNetError`) и классификация документированных
  числовых кодов биржи (`classify_code()`, коды из `data-structures-constants-and-errors`) — по тому же принципу,
  что у Aster/Gate/Hyperliquid (каждый venue сам решает свою обработку ошибок; обобщённый `adapters`-слой не
  требует единого маппинга — см. `native.py:_read`, который и так сводит любую чужую ошибку к TRANSIENT/UNKNOWN).
- Rate limit: Standard-тир — 60 запросов/минуту на IP (документация, одно и то же число для чтений и `sendTx`);
  429/405 — пауза (тот же `BAN_S`, что уже проверен вживую в `audit_truth/lighter.py` для этого же хоста).
- `ioc()`/`cancel_order()`/`setup()` — **строят и валидируют заявку полностью** (существование рынка,
  `force_reduce_only`, точное масштабирование цены/количества в целые `price`/`base_amount` по `price_decimals`/
  `size_decimals` рынка, детерминированный `client_order_index` из строкового `client_id` бота — SHA-256, младшие
  48 бит, **наша** конвенция, не предписание Lighter), проверяют ворота режима dry/readonly/live (`keys.gate`,
  как и у остальных трёх venue), и **только после всего этого** зовут `_require_signer()`, который бросает
  `SigningNotAvailable` с точным объяснением. То есть неверный вызов получает конкретную ошибку валидации, а не
  общую «нет подписи» — это разница между «код готов» и «код упирается в один известный пробел».
- `SignerProtocol` — задокументированный интерфейс, которому должен соответствовать настоящий подписант (например,
  обёртка над `lighter-python.SignerClient`), когда/если владелец решит его подключить. Ничего в этом файле его
  не реализует.
- `history_fills()` — явный `SigningNotAvailable`: достоверная история сделок требует auth-токена
  (`trades`/`accountOrders`: «auth is required for master accounts and sub accounts»), а создание ЛЮБОГО
  auth-токена (даже read-only) само требует подписи (`CreateAuthToken` тоже идёт через скомпилированный сигнер).
- `ioc_partial_terminal = False`: Order Status биржи (0-16, `data-structures-constants-and-errors`) не показывает
  отдельного «partially filled» отдельно от `FilledOrder`/`CanceledOrder` — по аналогии с Gate («только
  status=finished даёт PARTIALLY_FILLED» — комментарий `gate_trade.py`) выбран консервативный дефолт, не угадано
  `True`. Требует перепроверки владельцем/будущим исполнителем на первом живом ответе.
- `Filters.max_qty_limit`/`max_qty_market = Decimal('Infinity')`: `orderBookDetails` не публикует максимальный
  размер заявки (в отличие от `order_size_max` у Aster/Gate) — подтверждено по «сырой» OpenAPI-схеме, не только
  пересказом; число не выдумано, локального потолка нет, отказ — на стороне биржи.

## Реестр и конфигурация (изменения существующих файлов)

- `src/funding_bot/trade/adapters/registry.py`: `'lighter'` добавлен в `production_registry()` как `FuturesAdapter`
  (одна строка + комментарий). Это **не разрешает live** (см. `docs/migration/ADAPTER_GUIDE.md`: «Новая пара в
  реестре не означает разрешение live») — регистрация фабрики инертна, пока ни один `LegSpec`/профиль не
  указывает `adapter_id='lighter'`. Ни `runtime.py`, ни `RuntimeRegistry` не тронуты: с какой спот-ногой связывать
  Lighter — решение владельца (нашёл прецедент в памяти: «архитектура/ANSEM — решение владельца»), эта задача его
  не принимала.
- `deploy/owner.toml.example`: добавлена секция `[perp.lighter]` со всеми полями пустыми (`leverage`,
  `margin_type`, `max_slip_bps`, `touch_frac_max`, `maker_allowed`, `liq_alert_pct` — те же ключи общей схемы
  `_PERP`, что у `[perp.aster]` выше в файле; `"lighter"` уже входит в `config.PERP_VENUES`, поэтому секция
  проходит существующую строгую валидацию `owner.py` без единой правки схемы). Идентификация аккаунта
  (`account_index`/`api_key_index`/приватный ключ подписанта) в схему **не добавлена**: без выбранной пары и без
  подписанта класть эти ключи было бы фиктивной готовностью, которой нет (см. «Границы»).
  Схема `.env`-переменной зарезервирована как имя `LIGHTER_API_PRIVATE_KEY` (константа
  `lighter_trade.LIGHTER_API_KEY_ENV`) — загрузчик значения НЕ реализован ни в `lighter_trade.py`, ни в
  `trade/keys.py`/`credentials.py` (туда я вообще не полез — не хотел трогать общий для всех venue файл ключей
  ради константы, которая пока ничего не грузит).

  Первая попытка добавить секцию с придуманными ключами (`enabled`, `api_base`, `ws_url`, `agent_key_env`,
  `wallets.lighter.*`) сломала `tests/test_sol_c1_owner_example.py::test_uncommented_proposal_loads_disabled_
  with_pilot_numbers` (этот тест раскомментирует ВСЕ блоки `>>> ПРЕДЛОЖЕНИЕ … <<<` во всём файле и грузит их как
  одно целое) — `OwnerConfigError: неизвестный ключ perp.lighter.enabled` и т.п. Это поймано полным прогоном
  тестов ДО коммита (см. «Проверки») и исправлено переходом на реально существующую схему `_PERP`, а не на
  выдуманную. Оставляю это здесь явно, а не молчу про найденную-и-исправленную у себя ошибку.

## Тесты (`tests/test_trade_lighter.py`, новый файл, фейковый HTTP-транспорт, никаких реальных вызовов)

32 теста: instrument/filters по `orderBookDetails`, позиция (long/short знак, отсутствие символа = флэт, а не
`None`), маржа, «аккаунт не найден», 429/405 (пауза и восстановление после неё), 5xx (ретраи, потом `LighterNetError`),
документированный код ошибки (21507 → `insufficient_funds_or_margin`), `next_nonce`/`apikeys`, детерминированность
`client_order_index`, точность `_scale` (и отказ не выровненного по шагу значения), валидация `ioc()` ДО подписи
(неверная сторона / `force_reduce_only` рынок — конкретная ошибка, не `SigningNotAvailable`), `ioc()`/
`cancel_order()`/`setup()`/`history_fills()` без подписанта — `SigningNotAvailable`, dry-режим блокирует `ioc()`
даже с подключённым (фейковым) подписантом (оборона в глубину), **dry-run вход+выход на фейковом подписанте**
(`FakeSigner` — тестовый дублёр, явно не настоящая криптография, см. докстроки файла и класса) с проверкой
масштабирования количества/цены и `is_ask`/`reduce_only`, `cancel_order()` на фейках, `query()` по `/tx`
(Failed → REJECTED, Pending/Executed/Pending-Final → UNKNOWN — экономика исполнения не подтверждена, см. выше),
регистрация в `production_registry()`.

Мутационная проверка ключевых условий (сделана и **откачена** — в git не осталась, каждая мутация проверена
сравнением с резервной копией файла):
1. отключение отказа `force_reduce_only` → упал `test_ioc_force_reduce_only_market_blocks_new_entry_before_signer`;
2. отключение проверки выравнивания по шагу в `_scale` → упал `test_scale_exact_and_rejects_off_step`;
3. `position()` перестаёт учитывать знак (`sign`) → упал `test_position_long_and_short_sign`;
4. удаление вызова ворот режима (`_check_mode`) перед подписью в `ioc()` → упал
   `test_dry_mode_blocks_ioc_even_with_a_signer_configured`;
5. отключение записи паузы после 429 (`banned_until`) → упал `test_429_bans_and_subsequent_call_is_refused_
   locally_then_recovers` (вторая заявка внутри окна бана перестала отказывать локально);
6. перестановка проверки подписанта ПЕРЕД валидацией входа в `ioc()` → упал `test_ioc_invalid_input_raises_
   before_signing_gate` (неверный `side` стал получать `SigningNotAvailable` вместо конкретной ошибки).

Все шесть — ожидаемые падения именно тех тестов, что и должны их ловить; после каждой мутации файл сверен
`diff` с резервной копией и восстановлен побайтово.

Результаты (venv `.venv-lighter/`, Python 3.12.4, macOS, `pip install -e ".[dev,trade,sol]"` — не коммитится,
не часть репозитория):
- `tests/test_trade_lighter.py`: **32 passed**.
- `tests/test_trade_lighter.py tests/test_migration_m3.py tests/test_m4_adapter_results.py tests/
  test_m4_execution_history_port.py tests/test_m4_native_adapter_journal.py tests/test_generic_adapter_matrix.py
  tests/test_generic_leg_cash.py`: **210 passed**.
- Полный `tests/`: **3038 passed, 3 skipped**, 1 **не связанная** ошибка — `tests/test_sol_c4_release.py::
  test_lock_is_closed_and_matches_this_environment` (мой локальный `pip install` разрешил более новые transitive-
  версии `rlp`/`hexbytes`/`eth-rlp`/`eth-keys`, чем зафиксировано в `deploy/requirements.lock`; тест сам говорит,
  что боевая проверка идёт «в чистом venv `.next` (Python 3.11 Linux)» — другое окружение, которое эта задача не
  трогала; ни разу не касался `pyproject.toml`/`deploy/requirements.lock`/deploy-механики).

## Открытые вопросы владельцу (перед первым включением)

1. **Главное решение**: как получать реальную подпись Lighter?
   - (а) принять `lighter-python` как runtime-зависимость — это значит доверять её скомпилированному бинарному
     артефакту (`lighter-go/sharedlib`), который эта задача не аудировала и не может аудировать (закрытый билд,
     не читаемый Python/Go-исходник арифметики подписи); или
   - (б) ждать независимо аудируемой (не только «неаудированный сторонний порт», как `lighter-rs`) реализации
     схемы Schnorr/ECgFp5/Poseidon2/Goldilocks, прежде чем писать её самостоятельно.
   До этого решения `SigningNotAvailable` остаётся единственным поведением `ioc()`/`cancel_order()`/`setup()`/
   `history_fills()` — код специально не даёт себе шанса угадать неправильно.
2. С какой спот-ногой связывать Lighter (окх-дех, соляна, что-то ещё) и как называть профиль — решение владельца
   (см. память: «архитектура/ANSEM — решение владельца»), эта задача его не принимает и не подставляет плейсхолдер.
3. `event_info` статуса транзакции (`/tx`) — вероятно несёт экономику исполнения, но схема не типизирована в
   OpenAPI; если у владельца есть доступ (аккаунт/API-ключ) для одного живого пробного GET по `/tx` c реальным
   `tx_hash` (это чтение, не подпись и не сделка) — это сняло бы часть неопределённости `query()` без необходимости
   в подписанте вообще.
4. `ioc_partial_terminal` (сейчас `False`, консервативно) и точный смысл `order_expiry`/срока действия IOC-транзакции
   (сейчас `IOC_EXPIRY_S=30.0`, выбрано в документированных рамках «5 минут — 30 дней» для другого поля, не
   проверено вживую) — стоит перепроверить при первом реальном ответе биржи.

## Границы (что сознательно не делал)

- Не подписывал и не отправлял ни одной транзакции Lighter — ни в тесте (только фейковый подписант/транспорт),
  ни живьём.
- Не устанавливал `lighter-python`/`lighter-go` как зависимость проекта — только прочитал исходники ради
  протокола (см. «Откуда взят протокол»), они не в `pyproject.toml` и не импортируются откуда-либо в репозитории.
- Не трогал `trade/keys.py`/`credentials.py` (общий для всех venue файл ключей) — оставил там только имя env-
  переменной как константу в своём модуле, не венчурный загрузчик.
- Не трогал `runtime.py`/`RuntimeRegistry`/любой активный профиль — Lighter не связан ни с какой спот-ногой.
- Не трогал Robinhood-инстанс Lighter (`api.rh.lighter.xyz`, квота USDG) — только mainnet/USDC.
- `history_fills()` не подключён к публичному `explorer.elliot.ai/api/accounts/{account}/logs` как «тихая
  замена» авторизованной истории сделок — это отдельный сервис с не проверенной здесь схемой (см. «Что публично»);
  оставлено явной зацепкой в этом патчноуте для будущего исполнителя, а не кодом с угаданными полями.
- Не переиспользовал и не менял `audit_truth/lighter.py`/`src/funding_bot/lighter.py` (read-only скринер
  дашборда) — отдельная зона ответственности, как и было до этой задачи.
- Ровно два публичных (без подписи, без ключей) GET к mainnet.zklighter.elliot.ai были сделаны РУЧНОЙ проверкой
  в терминале при разведке (не из тестов): `/orderBookDetails` дважды (один раз до фикса owner.toml.example,
  один раз в первой ручной проверке модуля). Ни один не менял состояние счёта, ни один не требовал ключей; после
  этого все дальнейшие проверки (включая весь тестовый файл) шли только на фейковом транспорте.
- Полный `git diff` этой ветки — 4 файла: новый `src/funding_bot/trade/lighter_trade.py`, новый `tests/
  test_trade_lighter.py`, точечная правка `src/funding_bot/trade/adapters/registry.py` (1 запись факторки),
  точечная правка `deploy/owner.toml.example` (1 новая секция). `.venv-lighter/` — локальный, не коммитится.
