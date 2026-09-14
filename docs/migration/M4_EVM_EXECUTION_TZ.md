# ТЗ: завершение EVM execution/recovery через общие адаптеры

Дата: 14.09.2026. Статус: спецификация, реализация этим документом не выполнена.
Исполнитель: Codex, в рамках порученной миграции. Независимый ревьюер денежного кода: Astra xhigh,
назначенный владельцем для M4/M5. Документ подготовлен основным агентом; отдельные агенты не запускались.
Исходная версия кода: aa446f2160479e037d4c99a956cc8a83c35dbe54; документация: 2824fd0.
Перед реализацией заново сверить Git, патчноуты и реально установленный release-state.
Основания: THREE_PROCESS_MIGRATION_TZ.md, MIGRATION_ACCEPTANCE.md, MIGRATION_RESULT.md.

## 1. Результат и границы

EVM-вход, выход, ручной rehedge/resume и восстановление используют общий контракт исполнения.
Координатор принимает решения об объёме и порядке ног; адаптеры владеют отправкой и доказательством
её результата. Добавление общего интерфейса поверх прежних прямых отправок не считается завершением.
Существующие EVM сети и разрешённые площадки сохраняют поведение и ограничения.

Включено:
- EVM futures submit/settle через существующий общий порт;
- EVM spot submit/resolve через общий контракт с существующим native EVM journal;
- prerequisite allowance/approve внутри границы EVM адаптера, включая восстановление;
- единые доказательства исхода при немедленном ответе и после рестарта;
- минимально необходимое доказанное связывание account scope для исполнения legacy-сделок;
- совместимость существующих операций, открытых позиций, резервов и frozen instrument.

Не включено: resize/SL, новые реальные биржи, общий SOL lifecycle, полная матрица произвольных пар,
переработка стратегии входа/выхода, изменение PnL, полный backfill исторического учёта, перенос всего UI.
Закрытие этого ТЗ не означает закрытия всей M4 или всей матрицы AC-01…29.
Не удалять engine.py/sol_flow.py ради сокращения файлов. Не создавать новый сервис.

## 2. Подтверждённые точки изменения

Пути ниже относительно корня funding_bot_migration; номера строк не фиксируются.

| Файл / символ | Сейчас | Требуемое изменение |
|---|---|---|
| src/funding_bot/trade/engine.py: Engine._child | Прямые perp.ioc и settle_unknown, собственная обработка результата | Подключить adapters.execution.submit_ioc / settle_ioc; оставить объёмы, лимиты, child/attempt и решение о допустимом повторе в координаторе |
| trade/engine.py: Engine._dex и вызов ensure_allowance | Прямой spot.swap и обработка native результата | Общий spot-порт, подготовка allowance через EVM binding; единый Result и атомарное применение |
| trade/reconcile.py: resolve_orders | Прямые settle_unknown/query и интерпретация INTENT/NOT_FOUND | Тот же общий resolve, что в live; отдельная доказательная совместимость legacy |
| trade/reconcile.py: resolve_txs, resolve_wallet_txs, resolve_clips | Native nonce/receipt recovery и применение клипов | EVM resolver внутри адаптерной границы; общая нормализация и применение результата |
| trade/adapters/execution.py | Общие submit_ioc, settle_ioc, recover_not_submitted | Переиспользовать; расширять только доказанно необходимые EVM compatibility случаи |
| trade/adapters/native.py, registry.py, mapping.py, contracts.py | Контракты и EvmSpotAdapter уже есть | Подключить к реальному EVM потоку, не вводить параллельный контракт |
| trade/adapters/futures_bindings.py, native_journal.py, signing_fence.py | Общий durable claim и native signing barriers | Сохранить committed-before-send, CAS и отказ при чужой транзакции |
| trade/aster_trade.py, gate_trade.py | Native HTTP и journal binding | Транспорт и venue-specific доказательства остаются здесь/в bindings |
| trade/evm.py, evm_swap.py | EVM nonce, подпись, broadcast, receipts | Сохранить native доказательства; подключить bindings без второго журнала отправок |
| trade/operations.py, operation_roots.py, store.py | Общие roots/резервы/settlement | Сохранить единственный источник истины, расширять связи идемпотентно |
| core/commands.py: startup, core/service.py, core/recovery.py | Startup/drain/recovery gate | Проверить подключение нового recovery и корректное блокирование при UNKNOWN |

Новые файлы, если нужны: trade/adapters/evm_bindings.py и trade/adapters/spot_execution.py.
Это предлагаемые имена, а не утверждение об уже существующей реализации.
Перед правками составить полный перечень фактических submit/resolve вызовов, включая ручной rehedge,
перезапуск, approve и wallet-level recovery; закрыть его ссылками на новые пути и тесты.

## 3. Обязательные инварианты

1. Одним кошельком/аккаунтом и журналом управляет единственный execution owner. Второй core не отправляет ничего.
2. Идентичность: network/venue/account/instrument/simulation + связь deal → intent → clip → attempt.
   Client ID и frozen inst_json/hash существующих сделок не переписываются.
3. EVM-адрес сравнивается канонически как адрес; это не разрешает lowercase Solana mint или произвольных ID.
4. До внешнего действия durable intent/claim и native signing evidence зафиксированы.
   Ошибка записи, чужая транзакция, проигранный CAS или mismatch запрещают отправку.
5. UNKNOWN не равен нулевому исполнению. Timeout, отсутствие ответа, один NOT_FOUND, отсутствие nonce
   в общей таблице или неизменный баланс по отдельности не доказывают отсутствие отправки.
6. Новый attempt разрешён только после доказанного terminal исхода прежнего и повторной проверки
   одобренного остатка. UNKNOWN сохраняет резерв и блокирует конфликтующее действие.
7. Количества спота — целые raw units; расчёты контрактов — Decimal. Сохраняются multiplier, tick/step,
   разные политики округления входа и выхода, carry, off-grid история и reduceOnly.
8. Частичное исполнение учитывается по факту. Отмена после fill не превращается в нулевой результат.
   Неполные/противоречивые amounts не становятся FINAL с нулём.
9. Clip/result и reserve/settle применяются атомарно и идемпотентно; повтор evidence не меняет деньги второй раз.
10. Startup разрешает исходы и сохраняет прежнюю семантику паузы. Не создаёт новый вход, swap или повторный
    ордер и не превращает ручной resume в автоматический.
11. Drain запрещает новый риск и допускает разрешение начатого согласно действующим ограничениям.
12. Комиссии/газ неизвестной стоимости остаются unknown; доказанное количество не теряется из-за задержки fees.

## 4. Последовательность реализации

### E1. Зафиксировать совместимость и account scope

- Снять baseline тестами действующего поведения entry/exit/rehedge/resume и startup.
- Разделить legacy попытки и новые common попытки по durable evidence/версии, а не по предположению
  «нет поля — значит не отправлена». Зафиксировать таблицу допустимых переходов для обоих форматов.
- Использовать существующие scope primitives. Для legacy binding проверить конфигурацию, frozen instrument,
  native записи и наблюдения нужного аккаунта; текущее значение env само по себе не доказывает историю.
- Минимальное связывание для исполнения не объявляет исторические fills/funding полными.
- Binding хранить отдельно, идемпотентно, с источником доказательства; не менять старые hashes/JSON.
- Неоднозначность → адресная блокировка и причина. До выката все активные позиции должны иметь доказанный
  доступный recovery/exit; иначе этот кандидат не выкатывается. Не оставлять AIW3 без управления ради миграции.

Выход E1: согласованная в коде модель идентичности и тесты mismatch/повторного binding/legacy recovery.

### E2. Переключить EVM futures

- В Engine._child заменить native отправку общим submit_ioc, передав существующий client_id, clip, account,
  venue, side, quantity, price cap, reduceOnly, clock и authorization callback.
- Общий порт владеет claim/signing lifecycle; убрать дублирующую запись состояния из caller там, где
  она конфликтует с PerpJournal. Не создавать второй perp_orders или новый номер попытки при повторе callback.
- Immediate UNKNOWN и startup используют settle_ioc и один gate нормализации Result v2.
- Compatibility PerpFill выдавать только после проверки client ID, объёмов, цены/quote и terminal semantics.
- Сохранить bounded precision retry и partial IOC policy; причины отказа получать из общей категории ошибок.
  Повторная котировка не увеличивает одобренную цель. Reduce-only отказ не разрешает обычный BUY/SELL.
- Перевести resolve_orders на общий порт, сохранив проверяемую обработку старых INTENT/SENT/UNKNOWN.
- recover_not_submitted применять только при выполнении его native+common доказательств и DB ownership.

Выход E2: все EVM futures отправки и восстановление проходят один общий gate; SOL port не регрессировал.

### E3. Подключить EVM spot

- Реализовать production EVM bindings существующего EvmSpotAdapter; собрать через production_registry.
- Quote/prepare сохраняют token in/out, chain, wallet, raw amount, min output, deadline и ограничения газа.
  Адаптер проверяет допустимый spender/router и соответствие транзакции одобренной операции.
- Nonce, approve, подпись, replacements, broadcast и receipts остаются в native EVM реализации.
  Не переносить подписанные payload/секреты в публичные DTO или новый общий журнал.
- Approve — отдельное journaled prerequisite, не исполнение swap. UNKNOWN approve блокирует зависимый swap.
  Wallet-level recovery находит его после рестарта, даже если clip ещё не создан.
- Engine._dex вызывает общий port; reserve/begin и settle остаются в OperationController.
- Сопоставить native attempt/hash/nonce с common attempt. Replacement того же nonce не создаёт второе
  экономическое исполнение. Подтверждённый revert допускает прежний ограниченный retry по свежей котировке;
  новая попытка имеет отдельный ID и не скрывает потерянный газ предыдущей.
- Receipt normalizer доказывает success/revert/finality и реальные amounts для правильных token/account.
  Receipt success без доказанных amounts не разрешает хедж по запрошенному количеству.
- При частичном расходовании input/refund резервируется и списывается фактическое; остаток не теряется.

Выход E3: прямая spot.swap/ensure_allowance отправка из координатора устранена; native EVM код вызывают bindings.

### E4. Единое восстановление и совместимость текущей позиции

- Перевести resolve_txs/resolve_wallet_txs/resolve_clips на тот же resolver/normalizer, что immediate результат.
- Повторное восстановление не меняет остатки, комиссии и корневой резерв. Противоречивые receipts блокируются.
- До health ready выполнить recovery gate; не скрывать UNKNOWN за готовностью нового процесса.
- Проверить активные позиции по свежему авторитетному срезу при подготовке кандидата.
  На прошлом выкате наблюдалась DQA9Q/AIW3 (BSC+Aster); не считать её состояние неизменным без новой проверки.
- Offline-копия с тем же финансовым состоянием: startup с нулём отправок, сохранённый book/inst hash,
  подтверждённый simulated exit, manual resume и repeated recovery.
- Баланс общего кошелька не считать балансом конкретной сделки; сверять book и распределение экспозиции.

Выход E4: единый immediate/startup путь, доказанная совместимость открытых и незавершённых legacy операций.

## 5. Матрица обязательных тестов

| Группа | Сценарии | Доказательство |
|---|---|---|
| Futures lifecycle | fill, zero fill, partial+cancel/expired, reject, precision retry, reduceOnly reject | Точные qty/quote, caps, child/attempt; единственный submit |
| Futures crash | До claim; после claim; после native prepare; после signing; после POST до ACK; после ACK до DB commit | После рестарта нет второго POST, либо доказанный NOT_PLACED до новой попытки |
| Concurrency | Два callers, nested transaction rollback, одновременные native prepare/recovery | CAS/lock допускает одного отправителя; нельзя отменить уже отправленный claim |
| EVM lifecycle | Approve unknown/revert, swap success/revert/partial refund, receipt without amounts | Нет swap после неизвестного approve; точные raw flows и reserve |
| EVM crash | Подпись до broadcast; timeout broadcast; hash неизвестен; nonce replacement; receipt до settle | Поиск исходной попытки, отсутствие второго расхода и двойного settlement |
| Identity | Чужие account/chain/symbol/deal/clip, live/sim, EVM case, multiplier | Отказ до внешнего действия, прежние hashes сохранены |
| Recovery | Legacy INTENT/SENT/UNKNOWN, несколько unresolved orders, повтор startup, reorg/finality | Нет ложного NOT_PLACED/FINAL; UNKNOWN остаётся блокером |
| Operations | Entry, exit, rehedge, manual resume, full/partial exit, off-grid dust | Прежние лимиты и округления; единственный root/reserve |
| Integration | Engine и реальный production registry/native bindings с fake transport | Доказано подключение, а не только unit-тест общего адаптера |
| Compatibility | Финансовый replay, AIW3 snapshot, SOL common-port regression, drain | Book/status/cash flows сохранены; unknown не объявлен известным |

Расширять существующие tests/test_m4_execution_port.py, test_m4_native_adapter_journal.py,
test_m4_root_engine.py, test_m4_operation_roots.py, test_m4_replay_recovery.py,
test_trade_evm.py, test_rh_evm.py. Дополнительные файлы группировать по EVM port/recovery.
Использовать fault injection и фальшивый транспорт; никаких реальных ордеров ради тестов.
Статическая проверка запрещает прямые native submit из Engine/reconcile, но разрешает их внутри binding.
Не заменять поведенческие интеграционные тесты одним поиском строк/import check.

## 6. Приёмка пакета

- По инвентарю вызовов ни один EVM денежный путь не обходит общий execution/resolve gate.
- Вход/выход/rehedge/resume и recovery используют существующие native journals без двойного учёта.
- Все crash/identity/concurrency сценарии имеют исполнимые тесты и счётчики внешних отправок.
- Есть differential report базового и нового кода на одном срезе с точными различиями и unknown полями.
  Исторический exact_strict_unknown не превращать в verified_known_complete без новых доказательств.
- Независимое Astra xhigh review закрывает существенные замечания именно этого пакета.
- Обновлены относящиеся к пакету доказательства AC-03, AC-11, AC-15…20, AC-23, AC-25;
  не выставлять целому AC passed, если его более широкий scope остаётся незавершённым.
- Патчноут, список schema/reader изменений, результаты и ограничения опубликованы в рабочей ветке.

## 7. Проверки, выкат и откат

На E1…E4 — профильные тесты; общий полный Linux набор один раз на итоговом неизменном артефакте.
При изменении кода/профиля/runtime, обесценивающем receipt, пересобрать доказательства.
Отдельно измерить разработку, ревью, тесты и выкат; неизвестные токены/проценты подписки не оценивать как факт.

Выкат после выполнения критериев — только deploy/deploy.sh: новая сверка базы, drain, execution lock,
WAL-compatible backup, проверка schema/readers, установка проверенного артефакта, readiness и postcheck.
Авторитетная БД после прошлого выката: /var/lib/funding-bot/core/trade.db; код: /opt/funding-bot/current.
Не использовать замороженную legacy runtime/trade.db как действующую.
Перед переключением проверить фактические позиции, незавершённые заявки/транзакции и account bindings.
После переключения сравнить book, frozen instrument, активные intents/reserves, реальные позиции и release ID.
Не утверждать live exit проверенным, если выполнен только simulated exit.

При новом persisted формате повысить schema/reader floor до первого несовместимого события атомарно.
Откат возможен только на reader, понимающий новые записи и unresolved attempts. При несовместимости
старого релиза не обещать автоматический откат: остановить новый риск и сохранить recovery текущего кода.
Старую БД поверх текущей не восстанавливать. Никаких live переключателей, позволяющих двум путям
одновременно управлять одним attempt. Удаление compatibility readers — отдельная обоснованная задача.

## 8. Артефакты исполнителя

1. Инвентарь call sites и схема old → common → native journal.
2. Реализация E1…E4 с миграцией/binding и точками запрета отправки.
3. Профильные тесты, fault matrix, differential/replay report и независимое ревью.
4. Патчноут, обновлённые evidence матрицы приёмки, схема rollback compatibility.
5. При состоявшемся выкате — immutable receipt, реально установленный SHA и postcheck.

ТЗ готово к реализации; создание документа не меняет действующий бот и не запускает выкат.
