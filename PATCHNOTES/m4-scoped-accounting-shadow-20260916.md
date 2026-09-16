# M4 — execution_scope → scoped_accounting bridge, SHADOW-ONLY (owner-directed)

- Исполнитель: Claude (Sonnet 5), по прямому поручению владельца (через координатора).
- Ветка: `claude/m4-scoped-accounting-shadow`, поверх `codex/migration-m4-m5` @ `571d86f`
  (актуальный HEAD ветки на момент работы; дальше `571d86f`/`f6b96d1` ветка не уходила —
  `git merge-base codex/migration-m4-m5 origin/claude/m4-real-data-replay` == `571d86f`).
- Независимая проверка: не запускал сам — по разрешению владельца после его независимой
  проверки. Не пуш.
- Production/VPS: не трогал. Живой `trade.db` не открывал и не писал в него ни разу; всё
  ниже — на локальных временных SQLite-базах (materialized fixtures / `tmp_path`).
- **Это ТЕНЕВОЙ (shadow), НЕ авторитетный путь.** Sim-гейт (`operation_roots.py`,
  `generic live execution is not enabled by this coordinator`) не трогал вообще — новый код
  не имеет к нему отношения (легаси EVM binding-путь, не generic/operations root).
  `cabinet.py`, `dashboard.py`/`serve.py`, `tg/*` по-прежнему читают только legacy-путь —
  ничего в них не менял, и ниже объясняю, почему поведение `accounting.py`/`engine.py`/
  `marks.py` для них тоже не изменилось.

## Контекст: чего не хватало

`docs/migration/M4_REAL_DATA_REPLAY_REPORT_20260916.md` §5.1/§9 (ветка
`claude/m4-real-data-replay`, ещё не смёржена) явно назвал этот пробел: для `DQA9Q`
identity аккаунта уже доказана биржей модулем `adapters/execution_scope.py`
(`exec_events.kind='execution_account_binding_v1'`, `account="acct:v1:aster:..."`,
`provenance="authenticated_legacy_orders"`), но конвертера в `scoped_accounting.ProvenScope`
не было, и сама схема `scoped_accounting` (`scoped_accounting_schema`/`scoped_deal_accounts`/
`scoped_account_proofs`/…) никогда не мигрирована на прод — `scoped_accounting.migrate()`
нигде не вызывался вне тестов.

## Находка, определившая архитектуру: «теневой» путь нельзя строить на настоящих таблицах

При изучении кода обнаружилось, что `engine.deal_fills()`, `accounting.sources()`/
`accounting.is_bound()` и, через `marks.journal()` (`marks.py:120-182`, ветка
`if scoped is not None`), — **уже сейчас** переключаются на scoped-путь, как только
`scoped_accounting.deal_scope(con, deal_id)` возвращает не-`None`, а это происходит, как
только в `scoped_deal_accounts` появляется строка для этой сделки (пишет её только
`scoped_accounting.bind_deal()`). Это и есть штатный, уже написанный и протестированный
(`test_m4_scoped_accounting.py`, `test_m4_accounting_integration.py`) механизм переключения
M4/M5 — просто ни разу не активированный на реальных данных.

Значит: если бы теневой путь писал в `scoped_deal_accounts`/`scoped_account_proofs`
(настоящие таблицы `scoped_accounting.py`) для боевой сделки, это **не было бы тенью** — это
реально переключило бы `marks.journal()` (а через неё — cabinet/dashboard/Telegram) на новый
движок для этой сделки, без ревью и без ведома владельца. Именно это явно запрещено заданием.

Поэтому: **новый код никогда не вызывает `scoped_accounting.bind_deal()`** и не пишет в
`scoped_deal_accounts`/`scoped_account_proofs`. Вместо этого — собственная, полностью
отдельная таблица `shadow_scope_bindings` (модуль `scope_bridge.py`), которую не читает ни
один существующий модуль. Инвариант проверен тестами и мутацией (см. «Проверки» ниже):
подмена кода, которая заставила бы теневой путь дополнительно вызвать `bind_deal()`, тут же
ловится двумя тестами (`test_shadow_write_never_activates_scoped_accounting_for_a_real_deal`,
`test_production_hook_end_to_end_on_the_real_dqa9q_binding`).

## Что сделано

1. **`src/funding_bot/trade/scope_bridge.py`** (новый модуль).
   - `to_proven_scope(binding)` — чистая функция: `execution_account_binding_v1`-payload
     (+ `deal_id`, отдельная колонка `exec_events`, не часть JSON) → `scoped_accounting.
     ProvenScope`. Узкий, явный отказ (`ScopeBridgeError`), без подстановки дефолтов, для:
     - `sim=True` (синтетический `simulation:<venue>`, не `acct:v1:...` — конвертировать
       нечего);
     - любого `provenance`, кроме `authenticated_legacy_orders` (см. «Открытый вопрос» ниже);
     - отсутствующих/некорректных типов полей;
     - результата, не прошедшего собственную валидацию `ProvenScope.__post_init__`.
   - `proof_kind='public_api_identity'` для `authenticated_legacy_orders`: `account` там —
     результат `native.history_account()` (API-идентичность биржи, не секрет), независимо
     подтверждённый по историческим ордерам сделки (`legacy_evidence.prove_order`) — это и
     есть «public account identity», а не `frozen_leg` (это `InstrumentSpec.perp_account`,
     другое поле) и не `migration_manifest` (это про явный ревьюнутый manifest, а не
     автоматическую конвертацию).
   - `proof_ref='exec_events:execution_account_binding_v1:<deal_id>'` — ссылка на
     единственную (уникальный частичный индекс в `store.py`) строку `exec_events`, без
     дублирования содержимого.
   - `ensure_scoped_accounting_schema(con)` — тонкая обёртка над настоящим
     `scoped_accounting.migrate()` (закрывает п.3 задания: схема
     `scoped_accounting_schema`/`scoped_deal_accounts`/`scoped_account_proofs`/… у боевой
     БД никогда не создавалась). Безопасно и идемпотентно: только `CREATE TABLE/INDEX/
     TRIGGER IF NOT EXISTS`, `store.SCHEMA_VERSION` не трогает (как и раньше), и, пока ничего
     не вызывает `bind_deal()`, `scoped_deal_accounts` остаётся пустой — поведение
     `deal_scope()` не меняется.
   - `record_shadow_binding(con, binding)` / `shadow_scope(con, deal_id)` — запись/чтение
     собственной таблицы `shadow_scope_bindings` (append-only триггеры, как у прочих
     денежных журналов в этом проекте). Идемпотентно; конфликтующая повторная запись —
     явный `ScopeBridgeError`, не тихая перезапись.

2. **`src/funding_bot/trade/adapters/execution_scope.py`** — точка вызова.
   - Новая `_shadow_bridge_installed(con, deal)`: читает уже сохранённое биндинг-событие
     через существующий `_saved()`, вызывает `ensure_scoped_accounting_schema` +
     `record_shadow_binding`. **Целиком обёрнута в `try/except Exception`** с
     `log.warning(..., redact(exc))` — любая ошибка здесь логируется и никогда не
     пробрасывается наружу.
   - Вызывается из `bind_legacy()` (оба пути — «уже привязано» и «только что привязали») и
     из `prepare_active_accounts()` — единственного пути, который реально работает в проде
     (`core/commands.py Bot.startup()`; `bind_legacy()` в проде сейчас не вызывается нигде,
     только в тестах — проверено `grep`).
   - В `prepare_active_accounts()` вызов теневого шага вынесен **за пределы**
     `with exclusive_transaction(con):` — он не должен ни на миллисекунду держать открытой
     эксклюзивную блокировку настоящей биндинг-транзакции. К этому моменту `failures` уже
     гарантированно пуст (иначе функция уже упала бы раньше), так что каждая сделка из
     `evm_deals` либо только что привязана, либо была привязана раньше.
   - Важное следствие: реальный биндинг `DQA9Q` был записан ядром 2026-09-15, **до**
     появления этого кода. `_shadow_bridge_installed` читает его через `_saved()`
     (не «только что установленное»), поэтому при следующем перезапуске бота (когда этот код
     попадёт на прод) он сам «доберёт» существующий биндинг `DQA9Q` — это проверяет
     `test_production_hook_backfills_a_binding_written_before_this_code_existed`.

3. **Тесты — `tests/test_scope_bridge.py`** (новый файл, 35 тестов).
   - Юнит-тесты конвертера: валидные входы, все отсутствующие/некорректные поля (по
     каждому полю отдельно, параметризовано), sim-отказ, отказ по неизвестному/недоопределённому
     `provenance` (включая `new_draft_before_approval`), невалидный `account`, лишние поля
     игнорируются.
   - Интеграционные тесты на **реальных** данных `DQA9Q`:
     `tests/replay_fixtures/real_data/m4_real_dqa9q_20260916.json` — скопирован из ветки
     `claude/m4-real-data-replay` (сама ветка не смёржена; только эти 2 JSON-фикстуры). Само
     привязочное событие `execution_account_binding_v1` НЕ входит в whitelist-схему снимка
     (`replay_snapshot.COMMON_COLUMNS`), поэтому оно воспроизведено в тесте дословно из уже
     опубликованного отчёта (`M4_REAL_DATA_REPLAY_REPORT_20260916.md` §5.1) — не выдумано.
   - `test_shadow_write_never_activates_scoped_accounting_for_a_real_deal` и
     `test_production_hook_end_to_end_on_the_real_dqa9q_binding` — ключевая гарантия:
     `scoped_accounting.deal_scope()`/`accounting.is_bound()`/`engine.deal_fills()` не меняют
     ответ для `DQA9Q` до/после теневой записи.
   - `test_converted_scope_reproduces_real_fees_and_funding_via_real_scoped_pipeline` —
     запрошенная сверка на реальных числах: конвертированный `ProvenScope` плюс реальные
     строки `perp_fills`/`funding_income` из фикстуры проведены через настоящие
     `scoped_accounting.bind_deal`/`add_fills`/`add_funding` (намеренно **не** через теневой
     путь и не в проде — отдельный временный `tmp_path`-файл, только чтобы доказать, что
     конвертер выдаёт корректный `ProvenScope`). Результат: `SUM(commission_abs)` ==
     `SUM(income)` == **fees=0.08025553, funding=6.96720279** — ровно те числа, что уже
     независимо посчитаны в отчёте и `test_real_dqa9q_open_deal_baseline_matches_target_
     exactly` (сверка «до полного Decimal», не округление).
     `accounting.sources()` намеренно не проверял на `funding` (это отдельный, более строгий
     контракт с доказательством полноты окна через `accounting_funding_window`, требует
     реального `sync_funding()`/`FundingWindow` — не входит в задачу конвертера).
   - Мутационная проверка ключевых условий (сделана и **откачена**, код в git не остался):
     - отключение sim-отказа и provenance-отказа → упали ровно 5 ожидаемых тестов;
     - отключение проверки конфликта в `_record` → упал тест идемпотентности;
     - **добавление вызова `scoped_accounting.bind_deal()` внутрь `record_shadow_binding`**
       (симуляция самой опасной возможной ошибки — случайной активации настоящего пути) →
       мгновенно и точно упали оба теста-стража инварианта «shadow never activates».
       Это была самая важная проверка для денежного кода этого изменения.

## Открытый вопрос владельцу (не додумывал)

`execution_scope.py` знает два `provenance`: `authenticated_legacy_orders` (сконвертирован,
см. выше) и `new_draft_before_approval` (`bind_draft()`, для новых сделок, без
ретроспективного доказательства по ордерам). Оба в итоге строят `account` одинаково
(`native.history_account()`), так что `new_draft_before_approval`, возможно, заслуживает
того же `proof_kind='public_api_identity'` — но это ровно тот выбор словаря `proof_kind`,
который предыдущий срез (`M4_REAL_DATA_REPLAY_REPORT_20260916.md` §9) явно назвал
«архитектурным решением, не моим» и оставил владельцу. Я сделал тот же выбор: `to_proven_
scope()` конвертирует только `authenticated_legacy_orders`, для `new_draft_before_approval`
(и для любого будущего неизвестного `provenance`) — явный `ScopeBridgeError`, ничего не
теряется молча (никакого биндинга не устанавливается — прод по-прежнему пишет только
`execution_account_binding_v1`, как раньше; теневая запись просто не появляется).
Заодно: этот же хук (`_shadow_bridge_installed`) сейчас вызывается только из
`bind_legacy()`/`prepare_active_accounts()` (легаси-путь), НЕ из `bind_draft()` — по тексту
задания («живой legacy EVM путь, не generic/sim»). Если решение по `new_draft_before_
approval` будет принято, `bind_draft()` тоже потребует такого же вызова.

Второй, менее важный момент: `ensure_scoped_accounting_schema()` вызывается лениво, только
изнутри теневого хука (при первом реальном легаси-биндинге после деплоя), а не из
`store.connect()`/`core/commands.py Bot.startup()` напрямую — сознательно, чтобы не трогать
ни одной уже существующей точки миграции/бута. Если владелец хочет, чтобы схема
`scoped_accounting` создавалась безусловно при каждом старте (а не только когда/если
теневой путь сработает), это отдельная, более инвазивная правка `store.py`/`core/commands.py`,
которую я не делал.

## Проверки

Локально, `python3 -m venv` + `pip install -e ".[dev,trade,sol]"` (Python 3.12.4, macOS;
боевой прогон — Linux, отдельно, как обычно для этого проекта).

- `tests/test_scope_bridge.py`: **35 passed**.
- `tests/test_scope_bridge.py tests/test_m4_*.py tests/test_marks.py tests/test_sol_c1_store.py
  tests/test_phase1_instrument.py`: **556 passed**.
- Полный `tests/`: **2963 passed, 4 skipped**, 1 **не связанный** failure
  (`test_sol_c4_release.py::test_lock_is_closed_and_matches_this_environment` — локальный
  venv разрешил более новые transitive-версии `rlp`/`hexbytes`/`eth-rlp`/`eth-keys`, чем
  зафиксировано в `deploy/requirements.lock`; проверено — падает точно так же на чистом
  `571d86f` без единой моей правки, `git stash` + повтор тем же venv). До моих правок тот же
  полный прогон: 2928 passed, 4 skipped, тот же 1 failure.
- `git diff --check`: чисто (проверено перед коммитом).

## Границы (что сознательно не делал)

- Ничего не писал в `scoped_deal_accounts`/`scoped_account_proofs` — см. «Находка» выше.
- Не трогал `cabinet.py`/`dashboard.py`/`serve.py`/`tg/*` — их поведение не менялось (и не
  могло измениться, см. выше).
- Не трогал `operation_roots.py`/sim-гейт.
- Не трогал `bind_draft()`/`new_draft_before_approval` — открытый вопрос выше.
- Не трогал `.env`/ключи/секреты; ничего из них не печатал.
- Не открывал и не писал в боевой `trade.db` на VPS ни разу.
- `owner.toml`: новых денежных параметров/порогов/флагов нет — добавлять было нечего.
