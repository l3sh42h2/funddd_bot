# M4: итоги EVM/Solana операций как данные для interface

- Владелец задачи/исполнитель: Codex; независимый ревьюер: Astra xhigh, назначен владельцем.
- База: e5559fa; ветка codex/migration-m4-m5.
- Статус: ready (проверенный пакет исходников, не выкат); обновлено 2026-09-14.
- Область: EVM/Solana final reports, IPC report snapshots, outbox, interface presenter, DTO reader gate.
- Денежные формулы и торговые отправки не меняются. Core возвращает структурированный итог,
  interface форматирует его после чтения durable outbox. Прочие оставшиеся presenters — отдельные шаги M4.
- Новая форма требует DTO reader 3; старые queued DTO1/2 сохраняются. Выкат не выполняется.
- Независимое ревью Astra xhigh: EVM profile 254 passed, 5.92 s; SOL profile 131 passed, 7.26 s.
  AST обоих snapshot dataclasses и денежных формул совпадает с исходным кодом. Новых blocking findings нет.
- Actual EVM/SOL _final тесты запрещают вызов presenter; тест SOL проходит entry+exit на фейках.
  Boundary profile: 18 passed, 0.39 s. ACK loss/restart не создают повторной доставки в проверенном сценарии.
- Новый event и reader floor фиксируются атомарно; старое DTO2 событие не снижает floor3. Старый core
  отказывается запускаться с новым журналом; M5 проверяет reader при установке/откате.
- Совпавший с публичным ASTER_USER старый WALLET fixture заменён синтетическим адресом; профиль
  engine/rh_gate/boundary после замены: 79 passed, 5.93 s. Exact-value scan после изменений пройден.
- AC07 ещё не закрыт целиком: plans/refusals/progress/halt и отдельные форматтеры core остаются следующими
  шагами. Миграция не установлена; M4/M5 не объявляются завершёнными.
