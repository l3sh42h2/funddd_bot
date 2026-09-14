# EVM execution/recovery: inventory и проверка

Рабочий кандидат после 21d63bf; пакет ещё проходит приёмку. Production пока aa446f2.

| Вызов | Общая граница | Native authority |
|---|---|---|
| Engine._child (entry/exit/rehedge) | execution.submit_ioc, PerpJournal | Aster/Gate IOC + SigningFence |
| Engine._child UNKNOWN | execution.settle_ioc | native.settle_unknown, Result v2 |
| reconcile.resolve_orders | execution_scope.account_for, recover_not_submitted, settle_ioc | тот же журнал; recovery не отправляет |
| Engine._approve | spot_execution.ensure_evm_allowance | EVM ensure_allowance + wallet nonce journal |
| Engine._dex | spot_execution.submit_evm → production registry → EvmSpotAdapter | EVM swap/nonce/signing/receipt journal |
| reconcile.resolve_txs | evm_recovery._resolve_nonce → resolve_receipt | nonce group, включая bump/cancel |
| reconcile.resolve_wallet_txs | тот же _resolve_nonce | approve без clip_id |
| reconcile.resolve_clips | evm_recovery.stored_settlement / _refetch_amounts | тот же Result v2 gate для подтверждённых сумм |

Клипы и operation roots остаются единственными резервами. Common spot events
сохраняют подготовку/claim/result; не хранят подписанную транзакцию и не являются
второй таблицей расходов. Native dex_txs/perp_orders продолжают существовать.

## Идентичность и совместимость

- Новый draft связывается с аккаунтом до proposal approval.
- Старой сделке нужен frozen account, существующая доказанная scoped attribution
  либо execution-only binding на основе authenticated raw order evidence.
- Execution binding не активирует scoped accounting и не переносит fills/funding.
- Native transport фиксирует одну DB identity; connections к той же БД имеют
  отдельные immutable signing fences. Копия БД не получает второй sender.
- Reader schema 4 поддерживает common EVM semantics; min_reader повышается
  атомарно при binding/common prepare. Старый код не должен управлять такой БД.
- Legacy instrument без ident_ev может продолжать только ранее замороженную
  экспозицию; это не подтверждение нового листинга. Entry/resume-entry требуют
  identity evidence. Rehedge ограничен известной фактической дельтой.

## Доказательства текущего кандидата

На свежем read-only срезе VPS DQA9Q была OPEN: один исторический ордер прошёл
строгую authenticated identity проверку; шорт биржи совпал с книгой. Никаких
ордеров, binding или изменений production DB эта проверка не выполняла.

Финансовый whitelist снят с авторитетной /var/lib/funding-bot/core/trade.db.
SHA-256 снимка: ea095ce25a49cb9098df007ee43c8f71a5baac3ea0d62d424355beb2fc988c96.
Replay относительно aa446f2160479e037d4c99a956cc8a83c35dbe54: mismatches=[];
unsafe_projection_differences=[]. Classification exact_strict_unknown:
историческая полнота PnL не доказана, неизвестные суммы не объявлены известными.

Offline startup на том же финансовом срезе: ноль отправок, OPEN, book и inst_json
не изменились. Offline exit: CLOSED, только BUY reduceOnly по перпу, сеть заблокирована.
Owner/account/request limits в этом execution fixture синтетические: они намеренно
не входят в разрешённый финансовый экспорт. Реальная account provenance проверялась
отдельно на VPS. Это не тест живого выхода и не полная реплика production recovery.

## Приёмка

Профильные проверки и независимое review продолжаются. Текущие результаты отдельных
прогонов не заменяют final Linux artifact profile. До выпуска нужны закрытие review,
проверенная активация bindings/reader floor на сервере и свежая сверка после переключения.

## Закрытые замечания и проверенные ограничения

- Ревью Astra xhigh: закрыты повторный sender через копию БД, пропущенный reader
  floor, min-notional у reduceOnly закрытия Aster, некорректные native order IDs,
  UNKNOWN при mined receipt, точность raw units и частичная активация bindings.
- Все активные EVM позиции проверяются до общей транзакции активации. Отказ второй
  позиции либо изменение snapshot откатывает bindings и reader floor целиком.
- Genuine legacy без verified instrument/account evidence блокирует активацию.
  Такая позиция не объявляется поддержанной; перед выкатом обязательна проверка
  полного active inventory. Изменение inst_json уже связанной сделки также блокирует send.
- Контрольный профиль: 726 passed, 14.32 s (EVM, engine, RH, SOL common port, M3/M4).
  Дополнительно проверен настоящий core Bot.startup: DQA9Q OPEN, zero sends,
  backfill сохраняется в отчёте, повтор идемпотентен.
- Native balance mismatch удерживает operation reserve при повторном recovery
  и при недоступности архивного state; после доказанного успеха apply ровно один раз.
- Raw amount сохраняется через production quote/prepare/submit для 10^28±1,
  2^96 и 29-значного неокруглого количества.

Эти профильные результаты получены до полного Linux-прогона; актуальный итог
и оставшиеся ограничения приведены ниже. Второе ТЗ остаётся следующим пакетом.

## Кандидат 0f9037c: bounded evidence после полного regression-прогона

Первый Linux candidate e0065dc не принят: 2653 passed, 36 failed, 12 skipped,
1317.47 s. Исправление 0f9037c сохраняет native inheritance в connection view;
Astra xhigh закрыл diff, включая actual Aster/Gate two-connection race и nested
recovery. Повторный Linux build прошёл: 2693 passed, 12 skipped за 1336.02 s. Receipt проверен локально по артефакту и SHA лога.

Профили после исправления: 223 passed/6.07 s (phase0/phase1/signing/collector gate),
543 passed/12.33 s (M4, Engine, RH, SOL common port). Они пересекаются: суммы нельзя
выдавать за число уникальных тестов. На immutable 0f9037c отдельно JavaScriptCore
8 passed/0.48 s и synthetic Linux DAC 2 passed/0.74 s.

На прежнем разрешённом финансовом срезе с тем же SHA снимка выполнены:
- два actual core startup: ноль отправок, book и frozen instrument сохранены;
- полный simulated exit, только BUY reduceOnly на фьючерсах;
- частичный simulated exit → stop после клипа → startup → manual resume того же root;
  confirmed_raw равен исходной цели, reserved_raw=0, затем полный выход;
- repeated startup после закрытия: никакого повторного исполнения/изменения книги.

Финансовые количества взяты из экспортированного среза, но owner/account/request
limits в этой проверке синтетические (экспорт их исключает). Она не доказывает
актуальный баланс или original request/account configuration на VPS.
Replay aa446f2 → 0f9037c: mismatches=[], unsafe_projection_differences=[];
exact_strict_unknown сохраняется: native_quote отсутствует, полнота fills/funding
не доказана. verified_equivalent=False, историческая полнота не объявлена.

### Связь с общей матрицей приёмки

| AC | Что подтверждает EVM-пакет | Что ещё не закрыто этим доказательством |
|---|---|---|
| AC-03 | Два signing callers/connections, одна native отправка; другая DB не получает execution authority | Вся межпроцессная архитектура оценивается отдельно |
| AC-11 | EVM submit/resolve использует production registry и scoped native bindings | Общая независимая сборка обеих ног — пакет C1 |
| AC-15 | Одинаковый финансовый срез даёт одинаковые projections, unknown сохранён | Полная историческая evidence completeness не доказана |
| AC-16 | Claims/signing/POST/receipt, native absence proofs, UNKNOWN, minOut/balance mismatch | Нельзя выдавать EVM-проверки за все сценарии будущего generic coordinator |
| AC-17 | Account/symbol/order proof isolation, строгие native IDs/history rows | Полнота всей исторической пагинации — отдельная сверка |
| AC-18 | Frozen bindings, genuine legacy activation, multiplier, exact raw units; tampering блокируется | Generic identity без chain/token — C1/C4 |
| AC-19 | Один EVM root, refund, stop и manual resume точного остатка | Общее lifecycle EVM/SOL — C3 |
| AC-20 | Идемпотентный apply/repeated recovery и текущие report boundary tests | Generic leg-aware rebuild — C4/C5 |
| AC-23 | UNKNOWN/reserve/signing gates, current floor в health и installer | Свежая проверка реального состояния перед переключением ожидается |
| AC-25 | Reader4 floor атомарен; rollback к старому reader отказан до switch; БД не откатывается | Фактический production rollout/postcheck этого кандидата не выполнен |

Свежая проверка production аккаунтов/балансов и shadow activation на VPS ожидает
отдельного разрешения после auto-review. На момент последней проверки только
release metadata установлен aa446f216047-a82af1f56cf0; это не финансовый snapshot.
