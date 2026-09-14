# Общий координатор: пакет для приёмки

Дата: 15.09.2026. Исполнитель: Codex. Назначенное владельцем итоговое независимое
ревью: Claude. Реализацию и профильные проверки выполняли основной Codex и два
GPT-5.6 Terra high. Их проверки не выдаются за независимое ревью Claude.

База: принятый и установленный EVM-пакет `0f9037c`, release
`0f9037cf72de-f6e1e96f711e`. Common-пакет на момент подготовки этого документа
не установлен; финальные SHA/receipt и результаты — в патчноуте и отчёте проверки.

## Что изменилось

- Одно `OperationController.run_operation` владеет admission и внешним lifecycle
  legacy EVM, legacy SOL и generic планов. `commit_transition` — общая атомарная
  граница завершения/паузы/recovery: frozen context, root link, доказательство,
  settlement, unresolved gate, root/deal/intent и `operation_end`.
- Старые форматы читают compatibility policies; они сохраняют EVM clipping,
  SOL routing/finality и разные исторические правила округления. Эти policies
  не владеют отдельным внешним admission/terminal циклом. Native readers остаются
  для незавершённых и исторических attempts.
- Реальные spot/perp bindings собираются по двум scoped LegSpec через registry.
  SOL не требует методов аккаунта HL при CEX perpetual; специфический HL preflight,
  история и finality links находятся на границе perpetual adapter.
- Generic OperationPlan хранится в прежних intents/operations; обе ноги заморожены,
  approval покрывает план. Частичное продолжение использует прежний root и остаток
  цели, отдельное одобрение. UNKNOWN удерживает резерв; resolver не посылает новый ордер.
- Количество, cash/fees/funding по leg_id восстанавливаются из exec_events. CEX-only
  записи обходятся без выдуманных chain/token. Валюты остаются раздельными.
- Core queue, startup, позиции, private DTO и карточка понимают generic формат.
  `/positions` может разрешить исход исходной попытки, но не отправляет новую ногу;
  busy-позиция не разрешается параллельно исполнителю.
- Пять независимых исполнимых adapter examples и инструкция подключения проверены
  через production registry/context/journal/coordinator, с fake внешним транспортом.

## Матрица доказательств

| Требование | Исполнимое доказательство | Граница результата |
|---|---|---|
| AC-11/12: EVM/SOL × Aster/Gate/HL | test_generic_adapter_matrix; test_common_lifecycle_terra; test_rh_gate_engine; SOL C2/C3 | Шесть synthetic сочетаний; native fake-world SOL CEX без HL API; прежние live aliases сохранены |
| AC-13: CEX spot и perp/perp | test_generic_adapter_matrix, test_generic_leg_cash | Обе стороны long/short, reduce-only exit, multiplier, разные валюты, read-model |
| AC-14: несовместимость | test_m4_operation_plan, test_m4_c1_assembly, test_perp_preflight, generic matrix | Неверные identity/capability/режим/валюта/границы — отказ до send |
| AC-16/19: crash и resume | test_generic_operations, test_m4_common_lifecycle, test_m4_c2_sol_port, SOL C2, EVM port/root tests | DB reopen после reserve/prepare/dispatch/claim/ACK/partial/final; UNKNOWN не дублирует исполнение |
| AC-18: frozen history/units | operation plan/scope tests; differential replay; native Gate tests | Старые inst_json не переписываются; contracts/underlying и scopes раздельны |
| AC-20: rebuild/DTO | test_generic_leg_cash, generic matrix, test_m4_domain_notifications | Quantity/cash facts и DTO fence; неизвестная стоимость не равна нулю |
| AC-29: пять типов | docs/migration/examples + common_adapter_fixtures + generic matrix | Подключение каждого примера не требует отдельного executor/учёта/UI |
| AC-15: эквивалентность | tools/compare_legacy_lifecycle.py | Те же старые EVM/SOL fixtures: состояния, roots, clips, orders, qty, fees; не полный исторический PnL-аудит |
| AC-07/28 | platform checks и scope diff | Процессная изоляция проверяется отдельно; resize/SL и новые live стратегии не включены |

В `tests/` имена в таблице имеют суффикс `.py`. Полная матрица миграции этим
документом не закрывается. Полный Linux profile запускается на неизменяемом
кандидате; локальные профильные результаты сами по себе не являются release receipt.

## Исправление маржинальной проверки

В preflight передавалась ставка в функцию, ожидающую абсолютную комиссию.
Теперь: `contracts × contract_price / leverage + contracts × contract_price × fee_rate`.
Множитель повторно не применяется, ставка должна быть конечной и неотрицательной.
На границе недостаточной маржи прежний ошибочный допуск сменяется отказом до setup/swap.
Это намеренное исправление; ordinary fixture economics сохраняются.

## Ограничения и review focus

1. Common-пакет не разрешает новые реальные комбинации. SOL legacy policy использует
   USDC; CEX с USDT отвергается до send. Для подключения потребуется явная collateral/FX
   policy и конфигурация резерва, не предположение USDC=USDT. Generic две валюты уже
   хранит отдельно, но не предоставляет универсальный FX risk engine.
2. Generic план исполняет одну ограниченную leading/hedge порцию; запрос сверх
   transient exposure cap отклоняется. Автоматический дополнительный объём без
   approval не появляется. Legacy EVM сохраняет прежние несколько клипов.
3. Late fee correction и доказательство полноты исторических funding/fills требуют
   следующего пакета учёта. Отдельные funding facts не доказывают полноту периода.
   PnL без нужной оценки остаётся неизвестным. Исторический PnL не переписывается.
4. Шаблоны — synthetic adapters, не сертификация настоящих Bitget/Lighter/Extended.
   Новый live adapter обязан доказать native identity, precision, account mode,
   execution bounds, signing/idempotency, cancel/UNKNOWN и recovery своими тестами.
5. Совместимость: generic plan/cash reader floor 5; private generic positions DTO 4.
   Старые legacy notifications остаются DTO 2. Откат — только совместимый reader на
   текущей БД, без восстановления старой базы поверх совершённых сделок.
6. Claude должен проверить общий transition, неизвестные исходы с нулевым/ненулевым
   резервом, обе frozen scopes, фактический net hedge, native margin fix и ограничения
   публичных adapter examples. Замечания закрываются до приёмки/выката этого пакета.

## Следующие работы

1. Закрытие независимого ревью и установка проверенного common-кандидата штатным
   deploy с новым release-state/active-position audit, drain, reader gates и postcheck.
2. Полный аудит исторического учёта на едином срезе: fills, fees, funding, cost basis,
   cash и PnL; полнота страниц, пропуски, дубли, валюты, поздние исправления.
3. Окончательная сквозная приёмка миграции по AC-01…29, включая failure/restart/deploy
   и доказательства для актуальных открытых позиций.
4. После приёмки — отдельный функциональный патч: добор/сокращение позиции и ручной
   SL на фьючерсе с продажей spot после подтверждённого срабатывания; проверка каждой
   позиции раз в минуту по согласованному требованию.
5. Реальные новые площадки — отдельные задания владельца, через adapter/registration/
   config/tests и самостоятельное разрешение live. Исполнителя выбирает владелец.

## Уточнение финального кандидата

f64b604 отменён как кандидат на выкат; его Linux build остановлен. Последующая
правка добавляет native-position/quote freshness, точное соответствие обеих
родительских ног, запрет повторного занятия scope и TTL перед первым dispatch.
Локальный профиль: 713 passed / 19.36 s; актуальные fingerprints differential —
в M4_COMMON_LOCAL_EVIDENCE.json. Это не заменяет Linux receipt и Claude review.
В окончательной приёмке также остаётся AC-07: проверить и устранить оставшуюся
зависимость core от legacy Telegram formatter, прежде чем объявлять границы
трёх процессов полностью принятыми.
