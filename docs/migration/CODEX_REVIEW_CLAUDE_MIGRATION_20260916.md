# Проверка интеграции веток Claude — 2026-09-16

## Проверенные ветки

- `claude/m4-ac07-closure` (`55a0043`): перенос планов, отказов и Solana execution notices на DTO.
- `claude/m4-m5-staging-fault-injection` (`a1d37b5`): staging evidence M5 и fail-closed обработка отсутствующего cross-role файла.
- `claude/m4-cost-basis-review` (`f633da0`): независимое read-only ревью `weighted_average_v1`.

Все три ветки влиты в `codex/migration-m4-m5` до этой проверки. Этот документ не является
разрешением на production deploy.

## Исправленные находки

1. **Cost basis fail-closed.** Повреждённый spot execution fact с известными `(leg_id, scope)` раньше
   мог быть молча пропущен после уже подтверждённой покупки. Теперь он делает эту ногу `complete=false`;
   регрессия проверяет, что basis и average не публикуются.
2. **DTO rollback fence.** AC-07 добавляет durable execution DTO v5 и proposal DTO v6. До исправления
   `Journal` записывал reader fence только для v2–v4, а deploy manifest всё ещё заявлял v4. Поэтому
   несовместимый старый reader мог быть допущен после записи v5/v6. Journal, restart admission и
   `compatibility.json` теперь согласованно знают v5/v6; тест проверяет rollback denial и обычный
   restart current Journal на v6.

## Важные пределы результата

- `weighted_average_v1` подключён к отображению generic-ног, но не к торговому решению и не к
  авторитетному total PnL. Ни одна историческая legacy-сделка не содержит нужных
  `leg_execution_fact_v1`/`leg_execution_cash_v1`, поэтому existing DQA9Q/D9W8M replay не валидирует
  эту проекцию на реальном generic потоке. Политика требует отдельного продуктового решения.
- AC-07 убрал HTML и готовый текст владельца с плановых/отказных/execution путей. Но
  `trade/engine.py` и `trade/sol_flow.py` всё ещё лениво импортируют `tg.views`/`tg.sol_views` для
  числовых форматтеров и таблиц названий. Поэтому буквальный критерий AC-07 «core не зависит от
  Telegram/presenters» ещё не доказан. Нужно вынести эти нейтральные форматтеры в модуль вне `tg`
  или уточнить критерий приёмки; объявлять AC-07 полностью принятым до этого нельзя.
- M5 staging report подтверждает важные сценарии в изолированном Docker/systemd окружении, но не
  заменяет immutable artifact receipt и свежий production preflight/owner-approved switch.

## Локальные проверки после исправлений

- 300 passed: cost basis, generic cash/accounting, AC-07 DTO boundary и затронутые EVM пути.
- 20 passed: изолированные IPC/three-process tests (вне macOS sandbox, где Unix socket разрешён).
- 43 passed, 1 skipped: M5 deploy/server artifact profile.
- Один Solana final-boundary test не выполнился в локальном окружении до торгового кода: нет
  зависимостей Solana, роутер fail-closed вернул `no_eligible_route`. Это не зелёный результат и
  требует Linux-профиля с установленными зависимостями перед финальным verdict.
