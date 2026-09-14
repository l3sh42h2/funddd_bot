# Common coordinator: карта переноса

Актуальный срез 15.09.2026: [реализация, доказательства и ограничения](M4_COMMON_COORDINATOR_RESULT.md).
Ниже — исходный инвентарь переноса; формулировки «ещё не реализовано» относятся к его базе.

Подготовка по M4_COMMON_COORDINATOR_TZ.md. База: принятый EVM release 0f9037c.
Первый пакет установлен и проверен 14.09.2026 19:25:10 UTC; разрешение на production
аудит получено, аудит до/после установки прошёл. Этот инвентарь не доказывает
завершение C1–C5 общего координатора; актуальный прогресс — в его патчноуте.

## Что объединяется

| Сейчас | Общая ответственность | Что остаётся в адаптере/политике |
|---|---|---|
| Engine._execute / SolEngine.execute | Один dispatch, frozen plan и scopes, admission, RUNNING, recovery gate | Чтение старых форматов плана |
| Engine._entry/_exit / SolEngine._entry/_exit | Один порядок prerequisites → reserve → lead → actual exposure → hedge → apply | Multi-clip или один route, rounding change/target, clipping/requote bounds |
| Два _run_spot_clips | OperationController.run_clips — единственный цикл | Конкретные quote/native submit/resolve ports |
| Engine._hedge/_child / SolEngine._hl_hedge | Общий расчёт объёма и частичного остатка | IOC decomposition, precision/reduceOnly semantics, transport errors |
| Engine._rehedge / SolEngine._rehedge | Один исправляющий цикл с fresh approval и known exposure | Проверки margin/account/setup конкретного perp |
| Engine._root_stopped / SolEngine._paused | Одна атомарная классификация root/intent/deal | Только формат legacy notifications |
| reconcile + sol_flow.recover_deal | Recovery по двум frozen legs и единому evidence gate | EVM nonce/receipt, SOL finality/blockhash, venue order lookup |
| RuntimeRegistry/SolFactory/EvmGateFactory | Aliases выбирают независимые factories и policy | Secrets/transport выдаются одной точной ноге |

## Сохраняемые различия политики

EVM: несколько клипов, динамическая перекотировка остатка без роста target, allowance,
один повтор после доказанного revert, last-full snapshot, alpha/beta/refill и разрешения multiplier.
SOL: одна одобренная route, reselect/presign, blockhash/finality, native cash reserve,
лимиты hedge capacity и dust, прежний запрет entry resume. HL setup/agent/margin относится
к perpetual adapter, а не к Solana spot. Не заменять change rounding на target rounding молча.

## Порядок

1. Read-only recovery и совместимость frozen pair, обеих capabilities, account mode, quotes и bounds.
2. Атомарно admission/RUNNING/root; затем prerequisites, которые могут делать journaled native action.
3. Единственный clip loop: reserve → prepare/claim → submit/resolve → доказанный apply → hedge → invariant.
4. Finish/pause одной транзакцией; уведомление после commit. Не заводить второй финансовый journal.
5. Native UNKNOWN (включая approve до spot reserve и perp после его освобождения) означает PAUSED_UNKNOWN.
   Нулевой reserve сам по себе не доказывает отсутствие неопределённого исхода.

## Обязательные дифференциальные проверки

- EVM и SOL сохраняют frozen IDs/JSON, фактические raw quantities, fees, partial/refund и ручной resume.
- Stop между ногами не прерывает допустимый hedge; UNKNOWN leading leg запрещает следующий hedge/send.
- EVM zero-reserve perp UNKNOWN блокирует уже proposal/approval, а не только поздний исполнитель.
- Genuine legacy без доказанных units/account остаётся activation blocker; не создавать фиктивный multiplier.
- Пять новых synthetic adapters проходят тот же production coordinator, roots, journal и projection.
- SOL spot + fake Aster/Gate обходится без HL методов; perp/perp сохраняет обе funding/margin currencies.
- Adapter examples показывают diff подключения, без изменения Coordinator/Ledger/UI для каждого нового adapter.

## Наблюдение baseline

В e0065dc EVM perp UNKNOWN после settled spot оставляет root PAUSED_RISK при reserve=0.
Новый resume proposal возможен, но executor отказывает до нового clip/send по неизвестной книге.
Денежный повтор независимой проверкой не воспроизведён; классификация и ранний запрет approval
должны быть исправлены в общей state policy. Это не доказательство готовности общего координатора.

## План общей границы исполнения (до реализации)

- `LifecycleContext`: intent/deal, frozen owner/plan, две LegSpec и их fingerprints,
  operation_id, часы, policy. Legacy Run/SolRun читаются mapping-слоем; запись старого
  inst_json не меняется.
- `OperationController.run_operation`: проверка frozen плана → readonly resolve обеих ног →
  проверка полномочий → атомарный CAS intent/root в RUNNING → journaled prerequisites →
  `run_clips` → проверка фактической экспозиции → атомарный finish либо pause.
- Legacy command facade только строит план и форматирует ответ. Ни `SolEngine.execute`,
  ни отдельный dispatch EVM/SOL не остаются владельцами состояния операции.
- Spot boundary получает одобренную quote/route и конкретный clip. SOL binding не выбирает
  новую экономическую политику внутри submit: прежний reselect и presign должны сохранить
  одобренные ограничения. Native receipt + apply остаются одной транзакцией.
- Perpetual boundary получает Action с точным leg_id, side, qty, reduceOnly. IOC child orders,
  venue account mode, leverage/margin и recovery принадлежат этой ноге. Он не выбирает сделку
  для изменения и не переключает состояния root/intent.
- Политика количества хеджа — явный параметр плана: изменение экспозиции с carry для legacy
  EVM либо целевая экспозиция остатка для legacy SOL. Сам расчёт не зависит от venue/network.
- Finish decision вычисляется из двух известных позиций и policy dust/partial; применение
  состояния сделки, root и intent атомарно. Fee/PnL completeness остаётся отдельной величиной.
- Уведомления и добор учётных сведений не держат SQLite write transaction вокруг сетевого
  запроса. Ошибка Telegram не запускает финансовое действие повторно.
- Generic projections должны хранить leg_id для каждой стороны. Perp/perp нельзя кодировать
  фиктивным spot token или объединять funding двух валют без курса.

Этот план требует реализации и сквозных тестов; само наличие интерфейсов/инвентаря не закрывает C3/C4.

## C1: независимая сборка — уточнение по коду 0f9037c

- `assembly.build_trader_legs` сейчас выбирает factories по SOL_HL/RH_GATE и выводит
  общий keys_mode из legacy runtime. Сохранить возвращаемый tuple для старого core,
  но новые права определять для каждой используемой ноги отдельно.
- `CredentialProvider.legacy` разделить на независимый Aster loader и композицию
  EVM+Aster. EVM×Gate уже использует независимые evm/gate credentials; повторно
  объявлять устранение этой зависимости новой реализацией нельзя.
- `SolFactory.keys` вызывает combined sol_hl. Wrapper должен получить solana и
  hyperliquid независимо; собственные SOL factory/binding не требуют HL credentials.
- `build_sol_component`/`build_hl_component` получают явные public identity/network
  и mode своей LegSpec. Старые wallets.sol_hl/профиль читаются только legacy mapping.
- `RuntimeRegistry.adapters` пока создан, но assemble не вызывает compose. Реальный
  путь должен создавать exact AdapterContext обеих ног и вызывать compose; наличие
  одноимённого метода без использования в core не считается выполненным C1.
- Per-connection/per-attempt journals создавать после привязки к настоящим clip/action
  IDs. Нельзя использовать временный фиктивный journal для доказательства composition,
  а затем отправлять ордер обходным legacy executor.
- Credential cache повторно сверяет public identity и mode ceiling. Другой профиль
  не может получить первый загруженный wallet/account только потому, что adapter_id совпал.
- Aliases текущих трёх профилей сохраняются. EVM×HL, SOL×Gate/Aster и новые пять типов
  проверяются synthetic specs; разрешённых live aliases этим пакетом не прибавляется.

Источник уточнения: назначенный помощник m4_replay, read-only анализ без сетевых запросов
и правок кода. Реализация C1 всё ещё не выполнена.

## C3/C4: persisted модель — обязательные решения при реализации

Текущий `operations` хранит raw target одного действия; `_target` в operation_roots
пока выводит его из spot chain/token. Для generic pair target должен явно содержать
ведущую leg_id, asset/unit/decimals и семантику (input budget либо native quantity).
Perp/perp нельзя выдавать за EVM-спот ради прохождения старого `_target`.

В `deals` legacy chain/token поля nullable; для CEX-only не требуется выдумывать
адрес контракта. Нужен versioned frozen record двух LegSpec с immutable fingerprints,
а legacy inst_json/hash сохраняются как исходное доказательство. Перед первой записью
нового формата атомарно повышается reader floor; конкретная версия фиксируется в коде
и compatibility manifest после проверки текущей схемы, а не обещается этим инвентарём.

Две futures ноги требуют отдельных scope ownership и позиции сделки. Одного legacy
`deals.perp_scope` недостаточно для двух perp; общей активации нужны атомарные проверки
владения обеими scopes. Проверка только одинакового тикера или только net position
на бирже не заменяет связывания исполнения со своей сделкой.

`exec_events`/native attempts остаются источником фактов. Leg-aware исполнения содержат
leg_id, immutable scope, native reference, quantities/flows с валютами и evidence quality;
read model восстанавливается из них. Отдельный журнал, который может сам расходовать
те же деньги, не создаётся. Для старых сделок остаётся совместимое чтение spot/perp полей.

Completion должен различать:
- terminal partial fill ведущей ноги: неисполненный input освобождён, target остаётся;
- UNKNOWN второй ноги: lead reserve может быть нулём, но новое действие запрещено;
- fee unknown: qty известна, стоимость неизвестна;
- close обеих perp: только сокращение известной позиции каждой ноги, без переворота;
- разные settlement/margin currencies: раздельные потоки; USD risk gate без нужного
  курса/as_of отказывает, а не принимает USDT/USDC/USD за одну валюту.

Это проект решений для последующей реализации, не свидетельство наличия generic execution.
