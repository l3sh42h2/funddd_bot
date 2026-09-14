# ТЗ: общий координатор и независимые торговые ноги

Дата: 14.09.2026. Статус: спецификация; реализации этим документом нет.
Последовательность: после [EVM execution/recovery](M4_EVM_EXECUTION_TZ.md), перед окончательной
сверкой учёта и сквозной приёмкой миграции. Это следующий пакет M4, не новый этап M6.
Автор ТЗ и исполнитель миграции: Codex. Назначенный ревьюер M4/M5: Astra xhigh.
Документ подготовлен основным агентом без дополнительных агентов и без заявленной смены модели.
База документации: 1274dc9; последний подтверждённый установленный код: aa446f2.
EVM-пакет на момент написания только специфицирован. До реализации этого пакета зафиксировать его
фактический принятый SHA, заново сверить ветки, патчноуты и состояние VPS.

## 1. Цель

Единый координатор управляет планом, одобрением, порядком действий, экспозицией, резервами,
пауза/resume и recovery. Площадка управляет только своей ногой через общий контракт адаптера.
Профиль выбирает две LegSpec, ограничения и разрешения, но не отдельный Engine для конкретной пары.

При добавлении площадки в пределах поддержанных capabilities меняются адаптер, регистрация,
конфигурация и тесты. Координатор, Ledger/PnL и интерфейс не требуют новой ветки по названию площадки.
Не обещать произвольную торговлю любой парой: совпадение underlying, units, полномочий и capabilities
обязательно; отсутствие нужной возможности даёт явный отказ до побочных эффектов.

Три существующих процесса сохраняются. Не создавать движки/микросервисы под биржу или сеть.

## 2. Граница и зависимости

Входной критерий: EVM-пакет имеет принятые submit/resolve ports, scope-binding, native journal mapping,
crash/recovery тесты и совместимость активных legacy операций. Переиспользовать его код и доказательства.
Не повторять EVM-перенос и не поддерживать два конкурирующих implementation пути для одной попытки.

Входит:
- подключение SOL spot к общей production границе с сохранением native финальности;
- перенос общего lifecycle EVM/SOL в один координатор;
- независимая сборка ног и проверка совместимости в реальном core call path;
- обобщение плана/исполнений для пяти типов адаптеров, включая fake CEX spot и perp/perp;
- минимально необходимые leg-aware учётные проекции и private DTO для этих сценариев;
- доказательство расширяемости интеграционными тестами и инструкцией подключения.

Не входит: реальные новые Bitget/Gate spot, Lighter/Extended futures; разрешение новых live-комбинаций;
resize/SL; смена стратегии/порогов; полный исторический PnL backfill; универсальный движок всех финансовых
инструментов. Здесь futures — perpetual. Inverse/quanto, поставочные контракты и spot margin/borrow
не считаются поддержанными без отдельной модели и тестов.

## 3. Что уже есть и что надо подключить

Общие контракты, registry/compose, Bindings и частичный lifecycle уже реализованы.
Их наличие и старые M3 contract tests не доказывают сквозное исполнение через общий координатор.

Пути относительно src/funding_bot, если не указано иначе.

| Файл/компонент | Работа пакета |
|---|---|
| trade/assembly.py, runtime.py | Собирать независимые runtime legs; aliases профилей сохраняют конфигурацию, не выбирают торговый lifecycle |
| trade/adapters/registry.py, context.py, credentials.py | Использовать compose и scoped ресурсы в production; отказ одной площадки не отключает чужие независимые профили |
| trade/adapters/contracts.py, mapping.py | Две самостоятельные LegSpec; общий underlying и доказательство идентичности, role/direction, account/subaccount, units и валюты |
| trade/operations.py, operation_roots.py | Общий durable lifecycle и единственный root/reserve; расширить существующий OperationController, не заводить второй журнал |
| trade/engine.py | Делегировать операции общему координатору; убрать выбор EVM/SOL бизнес-движка по паре |
| trade/sol_flow.py: SolEngine.execute, _swap, _entry, _exit, _rehedge, recover_deal | Перенести общие решения в координатор, native специфику в bindings; разрешена тонкая legacy facade |
| trade/adapters/spot_bindings.py, native.py, execution.py | Подключить реальные ports и их journals; EVM из предыдущего пакета, SOL с прежними проверками |
| trade/spot_router.py и trade/solana/ | Jupiter/OKX routing, ATA/rent, blockhash, presign, финальность остаются здесь |
| trade/adapters/hl_preflight.py, futures_bindings.py | HL-specific account/margin preflight внутри HL адаптера; не требовать его от Gate/Aster |
| trade/exposure.py, ledger_flows.py, scoped_accounting.py | Рассчитывать по leg_id и доказанным units/currencies, без предположения «первая всегда EVM spot» |
| trade/reconcile.py, core/recovery.py, core/commands.py | Один dispatch операции и восстановления, включая ручные действия и restart |
| ipc/reports.py, interface/presenter.py | Общий формат результата ног; network-specific поля optional, секретов в DTO нет |
| docs/migration/ADAPTER_GUIDE.md и examples/ | Обновить инструкцию и исполнимые примеры для фактического production пути |

Если понадобится новый trade/coordinator.py, это допустимое место общей реализации; имя предлагаемое.
Расположение кода не важнее границ: не оставлять два lifecycle с одинаковыми именами в разных файлах.

## 4. Модель независимых ног и плана

### 4.1. LegSpec

Переиспользовать существующие типы; до расширения описать schema/reader compatibility.
Каждая нога описывает adapter key, venue_kind (cex/dex), market_kind (spot/perpetual), роль,
направление экспозиции, account/subaccount, network когда применимо, instrument и underlying identity,
multiplier/decimals/step/tick, quote/settlement/margin currencies и capabilities.
CEX spot не требует фиктивных wallet/RPC/gas. DEX futures не требует swap/allowance.
EVM и Solana — разные network families одного DEX spot контракта, а не отдельные координаторы.

Identity не строится из одного ticker. Новые CEX-only пары не требуют выдуманного chain/token.
Старые inst_json/hash остаются неизменными; mapping создаёт versioned view и сохраняет источник доказательства.
Нельзя автоматически считать USDT, USDC и USD одной валютой или путать 1 token с 1000-token contract.

### 4.2. Operation plan

Описать общий план с immutable ссылками на две ноги, approved bounds, target exposure, порядком действий,
допустимым незахеджированным остатком, TTL, operation ID и плановой политикой округления.
Текущие планы и стратегии мигрировать без изменения ограничений. Порядок не выбирать по названию биржи:
явная policy определяет ведущую ногу и последующее хеджирование по доказанному исполнению.
Approval привязан к fingerprint плана и обеих ног. Замена account/instrument/capabilities или увеличение
одобренных расходов требует нового согласования; requote не даёт дополнительных полномочий.

Spot BUY — увеличение инвентаря, SELL — продажа доступного инвентаря. Spot short запрещён без модели займа.
Perpetual поддерживает long/short только при capability. Закрытие сокращает именно позицию сделки;
reduce-only и account position mode проверяются адаптером до отправки.
Одинаковый underlying в разных продуктах не отменяет проверки спецификации инструмента.

### 4.3. Состояния и результат

Не вводить произвольный второй state machine. Зафиксировать отображение существующих состояний roots,
intents, clips и attempts в общий lifecycle: approval → reserve/prepare → send → resolve → apply → hedge
→ balanced/paused/completed. На каждом ребре определить владеющую транзакцию и crash recovery.

Исполнение и полнота учёта независимы. Accepted/confirmed не равен окончательному исполнению;
partial/cancelled может иметь ненулевой fill. Unknown удерживает необходимый резерв и блокирует повтор.
Недостающая комиссия не уничтожает доказанное количество, но не становится нулевой стоимостью.

## 5. Последовательность работ

### C1. Общая сборка и совместимость

- Составить инвентарь dispatch по profile/network/venue в assembly, Desk, Engine, SolEngine и recovery.
- Свести профили к aliases двух LegSpec + enabled/mode/limits. Сохранить прежние команды и профили.
- Прогнать live path сборки через registry.compose; generic factory получает только свою LegSpec/context.
- Проверять instrument identity, действия, precision, collateral/margin, account mode и полномочия обеих ног
  до первого внешнего действия. Readonly/sim/live не смешиваются молча.
- Нет credentials для неиспользуемой площадки — не причина отказать независимой паре.
- Новая запись registry по умолчанию не разрешает live; никаких автоматических fallback на другую биржу.

Выход: production assembly независим, несовместимые пары отказаны до submit.

### C2. SOL spot port и journal mapping

- Встроить spot_bindings.solana в общий execution port, сопоставив common attempt с существующими
  sol_tx_attempts/operations и резервами. Не создать второй источник подписи или расхода.
- Сохранить выбор Jupiter/OKX по действующей политике, min receive, route validation, ATA/rent,
  blockhash expiry, signature-before-send и presign guards. Не менять экономику выбора маршрута этим пакетом.
- Немедленный ответ и startup используют одинаковый resolver/Result gate.
- Истечение blockhash само по себе не разрешает новый swap: сначала доказать исход исходной подписи.
- Финальность не ослаблять; provisional количество не выдавать за окончательное. Recovery не переоценивает
  старую операцию по текущему mint/market registry и не меняет frozen identity.
- HL preflight оставить внутри HL boundary; SOL spot должен работать с fake Gate/Aster без HL аккаунта.

Выход: native SOL особенности скрыты адаптером, общий координатор получает проверенный Result.

### C3. Один lifecycle входа/выхода/rehedge/resume

- Перенести общие решения из Engine и SolEngine в OperationController/координатор.
- Entry/exit/rehedge/manual resume и startup должны пользоваться одним планом, reserve и resolve/apply.
- Ведущую ногу исполнять по policy; вторую рассчитывать по фактической доказанной экспозиции первой,
  с учётом fees in base, multiplier, partial fill, rounding, carry и прежних лимитов.
- UNKNOWN первой или второй ноги не запускает слепую замену/продажу/докупку. Сохраняется незавершённая
  операция, адресная причина блокировки и возможность безопасного resolve.
- Не увеличивать общий approved target на округлении. Принудительный final close не переворачивает позицию.
- На выходе частичный результат сохраняет остаток обеих ног; completed/CLOSED только по доказанному состоянию.
- Общий executor не содержит веток «SOL+HL», «EVM+Aster»; network/venue ветки остаются в адаптерах/mapping.
- Legacy facade может читать старые планы и форматировать совместимость, но не содержит второй submit loop.

Выход: одна исполнимая state machine, сохранены старые политики, нет скрытых отдельных путей.

### C4. Leg-aware проекции и пять подключаемых типов

- Для новых generic операций передавать leg_id в исполнения/экспозиции/денежные потоки; сохранить mapping
  legacy spot/perp полей. Оба perp в perp/perp имеют собственные qty, fees, funding и margin currency.
- Валюты не суммировать без доказанного курса/as_of; если пересчёт нужен, нет курса → неизвестная оценка/отказ
  там, где оценка необходима для risk gate. Не пересчитывать исторический PnL старых сделок новой формулой.
- Реализовать пять независимых synthetic adapters: CEX spot, CEX futures, DEX spot EVM,
  DEX spot Solana, DEX futures. Использовать production registry/context/journal/coordinator с fake transport.
- Для каждого показать отдельный diff подключения: adapter/tests + регистрация/config. Изменений
  координатора, учёта и UI для добавления очередного адаптера быть не должно после завершения обобщения.
- Fake adapters не должны напрямую выставлять итоговую позицию, обходя reserve/submit/apply/recovery.
- Обновить общий private DTO/presenter для двух ног без обязательных EVM/HL полей.

Выход: архитектурная расширяемость доказана сквозными операциями, а не вызовом compose без исполнения.

### C5. Совместимость и приёмка

- Сравнить новый common путь с принятым EVM-пакетом и прежним SOL lifecycle на одинаковых fixtures.
- Проверить legacy interrupted operations, frozen hashes, account scopes, клипы/корневые резервы и restart.
- Провести read-model rebuild: новая generic операция восстанавливается из фактов обеих ног.
- Подготовить evidence, diff, матрицу сбоев и остаточные ограничения для независимого Astra xhigh review.
- Удалить недостижимые бизнес-ветки только после покрытия; native recovery readers не удалять,
  пока они нужны для исторических attempts.

## 6. Обязательные сценарии

| Группа | Проверка и ожидаемый результат |
|---|---|
| EVM/SOL × Aster/Gate/HL | Шесть совместимых synthetic сочетаний, entry/exit/rehedge/resume через один coordinator; SOL+Gate/Aster не вызывает HL methods |
| CEX spot × CEX/DEX futures | Без wallet/gas/allowance; partial fill и комиссия в base меняют net exposure правильно |
| CEX futures × DEX futures | Long/short и обратное направление, отдельные funding/currencies; закрытие обеих ног без переворота |
| Пять adapters | Подключаются независимо; registry diff без изменения общего coordinator/Ledger/UI |
| Несовместимость | Разный underlying, неизвестный multiplier, неподдержанный short/reduce-only/hedge mode, stale quote, readonly → ноль внешних действий |
| Identity | Одинаковые тикеры разных активов, одинаковые order ID разных accounts, EVM case, Solana mint case → корректная изоляция |
| Crash | После reserve, prepare, подписи, send, ACK, partial, receipt и перед apply → нет двойной отправки/списания |
| Native recovery | EVM nonce replacement, SOL blockhash/finality, CEX cancel/partial/UNKNOWN → исход доказан или остаётся неизвестным |
| Паузы | Drain, падение UI, restart core, повтор команды/ACK → новая операция не возникает; manual resume остаётся ручным |
| Денежные границы | Multiplier, off-grid история, dust, refund, slippage, недостаточная маржа, fee currency → прежние лимиты и точные количества |
| История | Frozen IDs/JSON/hash неизменны; replay/rebuild на одном срезе; unknown не переименован в complete |
| Расширяемость | Все fixture scenarios запускают общий production core path; нет hidden simulated pair-specific executor |

Для отрицательных тестов проверять не только exception, но и количество внешних submit, резервов и записей.
Для crash tests перезапускать процесс/соединение; тесты только на одном объекте не доказывают durability.
Существующие tests/test_migration_m3.py и M4 operation/recovery tests расширять интеграционным профилем.
Полный набор не запускать на каждом механическом переносе; после изменений — профильные проверки,
на неизменном release candidate — полный Linux artifact profile и необходимые отдельные platform checks.

## 7. Критерий готовности

- Один coordinator фактически ведёт EVM и SOL, без выбора отдельного lifecycle по profile/бирже.
- Пять типов адаптеров проходят подключение и сквозную матрицу; unsupported не является пустой заглушкой.
- Native network/exchange details не требуются второй ноге и общему бизнес-коду.
- Новые leg-aware операции корректно rebuild; старые операции продолжаются с прежними IDs и лимитами.
- Закрыты существенные замечания независимого ревью; доказательства AC-11…14 и AC-29 обновлены.
  AC-15…20, AC-07/28 отметить только в покрытой части, не объявлять всю миграцию завершённой.
- ADAPTER_GUIDE содержит исполнимый пример и перечень файлов подключения для всех пяти типов,
  требования к secrets/config, тестам, capabilities и отдельному разрешению live.

## 8. Выкат, rollback и результат

Перед выпуском заново проверить release-state, чужие изменения и реальные открытые позиции;
не считать AIW3 или любой ранее снятый snapshot неизменным. Для каждой активной позиции доказать
совместимость exit/recovery в offline-копии, затем read-only сверить venue balances/positions.

Выпуск только через deploy/deploy.sh, по проверенному артефакту с drain, execution lock,
schema/reader gates и свежим postcheck. Legacy attempt не может одновременно исполняться двумя путями.
Новый persisted план/leg-aware event требует атомарного reader floor до записи несовместимого события.
Откат — только к совместимому reader на текущей авторитетной БД; старую БД поверх состоявшихся сделок не ставить.
Новые live сочетания и тестовые реальные сделки этим пакетом не разрешаются.

Артефакты: call-site inventory, схема lifecycle/legacy mapping, код+тесты, differential/rebuild report,
пять adapter examples и diff подключения, review closure, патчноут, evidence матрицы;
при выкате — receipt/установленный SHA и сверка после переключения.
Следом остаются полный аудит полноты исторического учёта и окончательная сквозная приёмка миграции;
добор/сокращение/SL — отдельный следующий функциональный патч после её завершения.
