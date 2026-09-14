# M4: подключение учёта с доказанным счётом

- Исполнитель: Codex; ревьюер: назначенный владельцем Astra xhigh.
- База: c5d571b; ветка codex/migration-m4-m5.
- Статус: ready (проверенный пакет исходников, не выкат); обновлено 2026-09-14.
- Область: accounting boundary, native history Aster/Gate, engine, marks, reconcile, core readmodel/status.

Bound deals читают/пишут только scoped sources/cursors. Старые final marks и final.cost_usd без
source revision не используются после binding. Комиссии требуют exact fill/order qty+quote coverage
и известной валюты. Полнота funding требует отдельного доказанного временного окна; list/пустой
ответ сами по себе proof не дают. FundingWindow — контракт для native adapters/проверенного импорта,
а не автоматическая декларация полноты существующих HTTP методов.

Native strict history readers отказывают при потерях namespace, конфликтующих дублях и насыщении
временной метки; Gate point_fee сохраняет неизвестность полной комиссии. Gate identity — authenticated
user UID + endpoint/settlement, Aster требует explicit signed user; send_user=False без signer-owner
proof для scoped ingest отказывается. Старые native public methods сохраняют compatibility.

Binding в production trade.db атомарно повышает min_reader до 3. Наличие кода читателя 3 само по себе
не активирует новую атрибуцию: исторические сделки не перепривязываются к текущему кошельку.
Неизвестные истории остаются неизвестными; SOL ledger и новые live-площадки не включаются этим пакетом.
Ресайз/SL остаются следующим патчем. Выката нет; M4/M5 ещё не приняты.

Проверки и ограничения этого пакета:
- Финальный профиль учёта/engine/M3/M4/M5/deploy/replay: 281 passed, 1 Linux-only skipped, 10.65 s.
- Независимое bounded closure Astra xhigh: 117 passed, 0.95 s; дополнительные WAL snapshot и nonce sibling
  сценарии закрыли замечания. Это не полная приёмка всего M4/M5 и не полный Linux artifact profile.
- Подтверждённое количество ордера сохраняется в финальном отчёте при ещё не загруженных fills; стоимость
  неизвестна до доказанной полноты. Source revision охватывает входы расчёта до чтения receipt и после сети.
- Отсутствующие/некорректные receipt costs не становятся нулём. REPLACED без receipt допустим лишь при одном
  нашем доказанном mined sibling того же chain/wallet/nonce. Gas guard проверяет имеющиеся строки транзакций;
  полнота соответствия каждого исполненного DEX clip его receipts этим guard НЕ доказана.
- Positive cancellation aliases в старом журнале bound-сделки дают неполный учёт до согласованной проекции
  всех денежных потоков. Исторические строки не переписываются ради числового PnL.
- Active funding readers используют явный временной срез. Новое поступление вне доказанного funding evidence
  инвалидирует старую полноту; неизвестные комиссии/фандинг не проходят дневной лимит как нулевые издержки.
- Секреты: exact-value scan выполнен; повторяется после последних правок перед публикацией.
- Полное время подготовки/ревью и счётчики токенов недоступны; время тестов приведено по выводу pytest.

Источники для history contract: официальные Aster V3 API docs (fromId без времени имеет retention)
и Gate API v4 futures docs (account.user, история my_trades, fee/point_fee).
https://github.com/asterdex/api-docs/blob/master/V3%28Recommended%29/EN/aster-finance-futures-api-v3.md
https://www.gate.com/docs/developers/apiv4/en/futures/
