# M4: AC-07 закрыт полностью — desk-план, fix-план и все отказы core тоже стали DTO

Date: 2026-09-16
Author: Claude

Ref: `docs/MIGRATION_ACCEPTANCE.md` AC-07 — «core не зависит от Telegram/presenters». Продолжение коммита
`db050cd` («Move EVM execution notices to DTO boundary», patch `m4-evm-execution-notice-dto-20260916`), который
закрыл только EVM execution/recovery уведомления и прямо перечислил, что осталось: «`trade/engine.py` всё ещё
создаёт HTML для desk/planning и некоторые legacy `Refused.html` пути». Это и есть предмет этого патча — плюс
`trade/sol_flow.py` (SolDesk/SolEngine), который тем фиксом вообще не затрагивался.

## Что перенесено

### 1. Отказы (`Refused`) — ~135 мест в `trade/engine.py` и `trade/sol_flow.py`

`Refused` раньше был `Exception` с полем `html` — уже отрисованным Telegram-текстом (`_views().refused(text)`
вызывался в момент отказа, то есть Desk импортировал `tg.views` и рендерил HTML сам). Теперь `Refused` несёт
`(topic, facts)` — по умолчанию `topic='refused', facts={'reason': text}` (позиционный `Refused("текст")`
работает как раньше, просто без HTML); три места с более сложной формой (`busy`, `owner_missing`,
`owner_config_error` — «занят исполнением», «не задан ключ», «owner.toml не прочитан») получили свои топики с
собственными полями (`intent_id`; `keys`+`action`; `reason`).

Оба места, где `Refused` ловится и уходит владельцу (`core/commands.py: Bot.propose`, `tg/bot.py: Bot.propose`,
плюс `cli.py` для `funding_bot plan`), больше не читают `.html` — только `hooks.notice`/`sender.execution_notice`
с `(e.topic, e.facts)`, рендерит `interface.presenter.render_execution_notice` (то же место, что уже рендерило
EVM execution-уведомления в `db050cd`).

`trade/sol_flow.py` почти все свои `Refused` строил через один внутренний метод `SolDesk.refuse(text)` — правка
одного метода закрыла ~62 места разом; отдельно так же поправлены `common()` (busy/owner_missing) — зеркало
`Desk._common_checks`.

### 2. Desk-план и fix-план (кнопка «✅ Да») — `Proposal.html` → `Proposal.view_topic`/`view_facts`

`Desk.propose_entry/propose_exit/_propose_entry_more/_propose_exit_more` строили `Proposal.html` через
`_views().plan(self.plan_view(...))`; `Desk.propose_fix` (дохедж/откат) — через `v.fix_plan(v.FixPlanView(...))`;
`SolDesk` — то же для `sol_views.plan(SolPlanView(...))`. Всё это тоже был рендер HTML внутри Desk.

`plan_view()` (и его Sol-аналог) теперь возвращает обычный `dict` тех же полей, что раньше шли в
`PlanView`/`SolPlanView` (только факты — Decimal/str/bool/кортежи скаляров, без HTML); `Proposal` несёт
`view_topic` (`'plan'` | `'sol_plan'` | `'fix_plan'`) и этот словарь как `view_facts`. `core/journal.py:
Outbox.plan_proposed` больше не принимает `legacy_body` — принимает `(view_topic, view_facts)`, валидирует
их (`ipc.notifications.validate_proposal_view`, новый `PROPOSAL_VIEW_VERSION=6`) и кодирует Decimal, как и
остальные DTO. `interface.presenter.render_proposal_view` — новая функция, тот же паттерн, что
`render_execution_notice`: декодирует факты и вызывает `views.plan`/`views.fix_plan`/`sol_views.plan` через
`SimpleNamespace(**facts)` (как уже делает `render_execution_notice` для `halt`/`progress`/`fix_done` — тот же
приём, не выдумывал новый).

«Продолжить» (`propose_resume`) раньше в двух случаях (сверено / расхождение) возвращал уже готовую строку
(`v.resume_checked(...)`/`v.resume_mismatch(...)`) без плана и кнопок. Добавлен маленький `Notice(topic, facts)`
— тот же принцип, что `Refused`, но без исключения (штатный, не отказной, исход); `resume_checked`/
`resume_mismatch` стали топиками `execution_notice`.

### 3. `SolEngine` — halt/progress/fix_done (не были частью `db050cd`)

`sol_flow.py` строил HTML прямо в движке: `_settle_fix` (`hooks.report(v.fix_done(v.FixDoneView(...)))` — та же
`FixDoneView`, что у EVM, это не поменялось), `_progress` (`hooks.progress(iid, sv.progress(SolProgressView(...)))`),
`_paused` (`hooks.report(sv.halt(SolHaltView(...)))`). Все три теперь `hooks.notice(topic, facts)` с новыми
топиками `sol_halt`/`sol_progress` (`fix_done` — тот же топик, что и у EVM, т.к. рендерит одна и та же
`views.fix_done`). `sol_progress` несёт `intent_id` только для маршрутизации (какое сообщение редактировать,
throttled-edit) — `sol_views.progress()` его в текст не выводит, ровно как у EVM `progress`; `interface.present()`
и `tg/bot.py: BotHooks.notice` помечают `sol_progress` тем же «без звука»/edit-in-place, что и `progress`.

**Уточнение (Claude, 2026-09-16, патч `m4-progress-edit-in-place-20260916`):** последняя часть фразы выше —
про `interface.present()` — на момент этого патча была неточна, поймано независимым ревью AC-07 и подтверждено
Codex. «Без звука» (`silent=True`) `present()` действительно уже проставлял для `progress`/`sol_progress`; а
вот edit-in-place — нет: `present()` для `kind=='execution_notice'` тогда безусловно возвращал `kind='send'`,
без ветвления по `topic`, то есть в трёхпроцессном режиме (`core`+`interface`) каждая стадия прогресса шла
НОВЫМ сообщением, а не правкой одного. Edit-in-place на момент этого патча реально работал только в
однопроцессном `tg/bot.py: BotHooks.notice` (через `Bot.progress()` — тот самый throttle-механизм, что и
`core/commands.py: Bot.progress()`, но `core/commands.py`'s `BotHooks.notice` эту ветку никогда не звал: она
безусловно шла в `Bot.execution_notice` → DTO). `interface.present()` заработал так же только после
`m4-progress-edit-in-place-20260916`, который добавил `interface/progress_tracker.py: ProgressTracker`
(intent_id → message_id, в памяти interface) и ветвление в `present()`. См. тот патчноут за архитектурным
решением и границами.

## Не тронуто (явно)

- **sim-гейт** (`operation_roots.py` ~266/324) не менялся, даже не открывался.
- **Денежная логика** (размер, вход/выход, хедж, авто-откат, дохедж, round-to-step, все числовые расчёты) —
  ни одна цифра, формула или условие в `trade/engine.py`/`trade/sol_flow.py` не изменены. Правки — только на
  границе «что Desk/Engine передаёт наружу»: у каждого изменённого места было ровно два состояния — «строю
  HTML» → «строю dict/exception с теми же значениями», порядок вызовов и условия отказа не тронуты.
- `.env`/ключи/секреты не читались и не менялись.
- `trade.db` на VPS не открывался вовсе (вся работа — в клонированной рабочей копии на Маке).

## Открытый вопрос (сознательно не менял)

В отказных сообщениях остаётся десятка полтора мест, где `trade/engine.py`/`trade/sol_flow.py` всё ещё зовут
`_views()`/`_sv()` — но только чистые числовые форматтеры без HTML: `num`, `pct`, `dur`, `tok`, `money(...,
html=False)` (то, что раньше давало «0.061», «7.3 %», «17 с» и т.п. внутри текста причины) и две таблицы меток
(`DEAL_STATE_LABEL`, `m_unknown_text`). Сам `tg/views.py` документирует это как намеренное решение уже давно:
«одни форматтеры для views, движка и сверки (trade/engine.py, trade/reconcile.py пишут через них)» — то есть
`trade/engine.py` и `trade/reconcile.py` и раньше умышленно делили эти функции с `tg`, до всякого AC-07. Эти
вызовы не строят HTML (никаких тегов/эмодзи-префиксов — обычные числа-строки, часть значения `reason`), но
формально это всё ещё ленивый импорт `tg.views` из `trade`. Полностью убрать значило бы переносить сами
числовые форматтеры в нейтральный модуль вне `tg` и переключать на него не только `engine.py`/`sol_flow.py`, но
и `trade/report.py`/`trade/reconcile.py` — отдельная, более широкая задача, которая тут не нужна и не была
названа в handoff Codex; я её не делал, чтобы не трогать общий, годами используемый форматирующий код ради
границы, которая по существу уже не про HTML. Список мест — `grep -n "_views()\.\(num\|pct\|dur\|tok\|money\)\|
_views()\.DEAL_STATE_LABEL\|_views()\.m_unknown_text\|_sv()\." src/funding_bot/trade/engine.py
src/funding_bot/trade/sol_flow.py` (плюс few `v.` там, где `v = _views()` сохранён по этой же причине).

`trade/engine.py: plan_cli()` (CLI `funding_bot plan`, ревью плана с Мака/сервера вручную) по-прежнему вызывает
`_views().plan(...)` напрямую — это единственная осознанная оговорка: однопроцессный инструмент разработчика,
здесь нет границы core/interface, которую AC-07 защищает (в интерфейсе нет отдельного процесса, которому нужно
что-то передавать). Тест `test_engine_and_sol_flow_never_render_html_for_refusals_and_plans` явно исключает
только эту функцию из проверки и объясняет, почему.

`Hooks.report`/`Hooks.progress` (абстрактные методы) стали недостижимы из `trade/engine.py`/`trade/sol_flow.py`
(проверено `grep` — вызовов не осталось нигде в `src/`), но я их не убирал из `Hooks`/`BotHooks`/`RecHooks`:
это по-прежнему часть публичного контракта, и его удаление — отдельное архитектурное решение, не часть AC-07.

## Тесты

Новый файл `tests/test_m4_ac07_core_interface_boundary.py` (14 тестов): структурная проверка (грепом по AST-
нечувствительному, но accessor-anchored regex), что в исполняемом пути `trade/engine.py`/`trade/sol_flow.py`
не осталось вызовов `views.refused/busy/owner_missing/owner_config_error/plan/fix_plan` и конструкторов
`PlanView/FixPlanView/SolPlanView/SolHaltView/SolProgressView/FixDoneView`; что `Refused`/`Proposal`/`Notice` не
содержат `.html` и их факты — не HTML (нет тегов, нет уже добавленных эмодзи-префиксов); что рендер факта через
`interface.presenter.render_execution_notice`/`render_proposal_view` даёт **тот же текст**, что раньше строился
прямым вызовом `views.*`, для каждого нового топика (`refused`, `busy`, `owner_missing`, `owner_config_error`,
`resume_checked`, `resume_mismatch`, `sol_halt`, `sol_progress`, `sol_plan`, `fix_done` общий для EVM/Sol) и для
реальных `Desk.propose_entry`/`propose_fix` (через `sim_env`/`live_env` из `test_trade_engine.py`); плюс
fail-closed проверка валидаторов (неизвестный топик, лишнее/недостающее поле, не-скаляр — отказ).

`SolDesk`/`SolEngine` в этом окружении (Мак) не прогнать целиком — на Маке нет пакета `solders`
(`pytest.importorskip("solders")` в начале `test_sol_c2_engine.py` штатно пропускает весь файл, так было и до
этого патча). Правки в `test_sol_c2_engine.py` (2 строки, `prop.html` → `fx.render(prop)`) поэтому в этом
окружении не выполнялись ни разу — только читал глазами и сверял с работающим паттерном в 9 остальных файлах.
Чтобы не оставлять `sol_plan`/`sol_halt`/`sol_progress` неподтверждёнными на этой машине, все три топика
проверены отдельно и синтетически в `test_m4_ac07_core_interface_boundary.py` (факты вручную, той же формы,
что строят `SolDesk.plan_view()`/`_paused()`/`_progress()`) — без реального роутера и без `solders`; это тот же
код рендера (`interface.presenter`), который выполнится и для настоящего `SolEngine` на VPS/Linux, где `solders`
есть (`docs/migration/M5_LINUX_ISOLATED_PROFILE_20260916.md`).

Существующие текстовые тесты (`test_trade_engine.py`, `test_phase0_hotfixes.py`, `test_phase1_exit_resume.py`,
`test_phase1_frozen.py`, `test_phase1_instrument.py`, `test_phase1_review_fixes.py`, `test_phase1_units.py`,
`test_rh_gate_engine.py`, `test_sol_c1_store.py`, `test_sol_c2_engine.py` — 103 места) читали `.html` у
`Proposal`/`Refused` напрямую; я не менял их проверки текста, только точку получения текста: добавлена
`render(obj)` в `test_trade_engine.py` (`fx.render(...)` у остальных, они уже импортировали `test_trade_engine
as fx`) — рендерит факты ровно так же, как `interface.presenter`, так что все прежние ассерты (включая проверки
экранирования, отсутствия сырых `Decimal`, ASCII-минуса и т.п.) продолжают проверять тот же самый итоговый
текст. Плюс два интеграционных теста починены под новую форму DTO не-текстовых проверок:
`test_migration_m1.py::test_config_change_while_quoting_invalidates_plan` (стаб `Desk.propose_entry` собирал
`SimpleNamespace(html=...)`) и `test_m4_domain_notifications.py::test_plan_guard_precedes_queue_and_ack_binds_
original_intent` (звал `Outbox.plan_proposed` со старой сигнатурой) — оба теперь используют `view_topic`/
`view_facts`, поведение (plan_guard до очереди, ack привязывает исходное намерение) не изменилось.

### Прогон

```
PYTHONPATH=src:tests python3 -m pytest -q tests/ --ignore=tests/test_solana_simulation.py
```
`95 failed, 2775 passed, 10 skipped` (плюс `test_solana_simulation.py` падает на сборе — `ModuleNotFoundError:
solders`, тест даже не запускается).

Все 95 падений и сбой сборки `test_solana_simulation.py` — **проверены построчным diff'ом списка упавших
тестов** до и после этого патча на исходном `db050cd` (`git stash`/`git stash pop` в рабочей копии): списки
совпадают **побайтово**. Причины — окружение Мака, не код: отсутствует пакет `solders` (`test_solana_message.py`,
`test_solana_sign.py`, `test_solana_validate.py`, `test_sol_route_validator.py`, часть `test_common_lifecycle_
terra.py`/`test_m4_common_lifecycle.py`/`test_m4_root_engine.py`/`test_m4_final_report_boundary.py` с `[solana]`
— роутер ловит `ModuleNotFoundError` и превращает его в «нет проверенного маршрута», это тоже отказ по той же
причине, просто не в виде голого traceback) и расхождение версий пакетов с `deploy/lock` в
`test_sol_c4_release.py::test_lock_is_closed_and_matches_this_environment` (pytest 9.0.3 вместо 9.1.1 и т.п.) —
это в точности то, что и `docs/migration/CODEX_SESSION_HANDOFF_20260916_COST_BASIS_M5.md` уже называл («Solana
final-boundary test... deselected... ModuleNotFoundError»). Не устанавливал `solders` и не менял версии пакетов
— это вне задачи и могло затронуть общее окружение Мака.

Узкие прогоны по ходу работы (после каждого шага рефактора): `tests/test_trade_engine.py`,
`tests/test_phase0_hotfixes.py`, `tests/test_phase1_exit_resume.py`, `tests/test_phase1_frozen.py`,
`tests/test_phase1_instrument.py`, `tests/test_phase1_review_fixes.py`, `tests/test_phase1_units.py`,
`tests/test_rh_gate_engine.py`, `tests/test_sol_c1_store.py`, `tests/test_sol_c2_engine.py`,
`tests/test_m4_ac07_core_interface_boundary.py`, `tests/test_tg.py`, `tests/test_m4_domain_notifications.py`,
`tests/test_migration_m1.py` — все зелёные (`439 passed, 1 skipped`; skip — `test_sol_c2_engine.py`, см. ниже).

## AC-07 — статус

Полностью закрыт для реальной границы процессов core/interface: `trade/engine.py` и `trade/sol_flow.py`
(исполняемый путь, без `plan_cli`) не строят HTML/готовый текст владельцу ни в одном из проверенных мной путей
(desk-план, fix-план, все отказы Desk/SolDesk, halt/progress/fix_done/perp_closed/auto_unwind/executor_crash/
resume_checked/resume_mismatch у обоих движков) — только факты через `Refused`/`Proposal`/`Notice`/
`hooks.notice`. Рендер — исключительно `interface.presenter` (плюс легаси-путь `tg/bot.py`, который сам
физически часть `tg`/interface и рендерит тем же кодом `interface.presenter`, а не своим). Единственная
оговорка — `plan_cli`, вне границы процессов, объяснена выше и в тесте.
