# M5: isolated Linux profile — `4c4e7c8` (2026-09-16), final-review fix candidate

Собрано Claude через `deploy/deploy.sh build` на VPS ireland, изолированно от боевого рантайма (тот же метод,
что и для `571d86f`/`8eca9d0` ранее сегодня).

## Кандидат

Отвечает на внешнее независимое ревью (GPT-6 Astra Pro) кандидата `8eca9d0`, verdict `not approved`
(`FINAL_REVIEW_8eca9d0_ALL.zip`, передан владельцем). Три исправленных находки:
- FINAL-01 (P1) — позднее связывание в `interface/runtime.py: on_sent()` могло ACK'ать чужое уведомление;
- FINAL-02 (P2) — `tg/sender.py: Sender.edit(throttle=True)` теряла `on_done` вытесненной задачи;
- FINAL-03 (P2) — `trade/cost_basis.py` тихо пропускала факты с повреждённым `market_kind`.

Плюс закрыт один P3, признанный, но не потребованный этим внешним ревью (краевой случай `_put()==False` при
вытеснении в `Sender.edit()`) — закрыт до отправки повторно, не после.

- source commit: `4c4e7c8a22aa776bfc1c9756a8f1c5fd42e8c33c` (`claude/final-review-8eca9d0-fixes`,
  родитель `8eca9d0`/`32662be`);
- source_sha256: `699f80c5d12019f94c47bac177937e538ef753182c9ebbfcffc7d36c9a57952f`;
- artifact_sha256: `318b4cca815418cdf5250b20fef5a1dc4204baf4457c17c63bfc570c6f61bde1`;
- release_id: `4c4e7c8a22aa-699f80c5d120`;
- patchnote: `PATCHNOTES/final-review-01-02-notification-ack-fix-20260916.md` (с дополнением о P3).

## Результат

```
3028 passed, 12 skipped in 1216.21s (0:20:16)
```
`exit_code=0`, `failed=0`. Те же 12 объяснимых skip, что и на `8eca9d0` (см.
`M5_LINUX_ISOLATED_PROFILE_8eca9d0_20260916.md`) — состав не изменился.

## Read-only preflight на живом VPS (перед сборкой)

Релиз на проде не менялся — по-прежнему `571d86fd7455-a19b18bf6ce3`. Три службы активны.

**Отдельно, не связано с этой задачей — владелец активно пользовался ботом через Telegram сегодня вечером:**
`deals`: `DQA9Q` всё ещё `OPEN` (не изменилась), `D9W8M` `ABORTED` (без изменений). Но `operations` теперь
содержит 4 строки — все `deal_id=DQA9Q, side=exit, mode=live, profile_id=bsc_okx_aster, state=EXPIRED,
confirmed_raw=0` — то есть четыре попытки закрыть DQA9Q через Telegram (`propose_fix`/desk fix-plan), каждая
протухла до подтверждения (никакого частичного или сломанного состояния, `confirmed_raw=0` во всех). Это не
generic/sim-путь (`operation_roots.generic_propose`, отдельно и явно всё ещё запрещён для `sim=False`) — это
уже штатная, задеплоенная M4-инфраструктура резервирования спот-действия (`trade/operations.py`), которую
легаси EVM-движок использует для входа/выхода с 0f9037c. Позиция цела, ничего не сломано — но стоит показать
владельцу: 4 подряд протухших попытки выхода могут означать проблему с UX/TTL плана, не техническую поломку.

## Что дальше

Передать SHA `4c4e7c8` + evidence (этот отчёт, patchnote, receipt) владельцу для повторной отправки внешнему
ревьюеру (тем же путём, каким пришло исходное ревью — файлом/архивом, не автоматически через git). Production
switch не разрешён ни этим отчётом, ни предыдущим verdict «not approved» — ждёт нового положительного ревью и
отдельного явного решения владельца.
