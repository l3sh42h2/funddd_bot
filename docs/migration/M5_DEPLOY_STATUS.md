# M5 deploy checkpoint — не приёмка

Дата: 2026-09-14. База исполнителя: `aaf5737`; интегрирован согласованный core contract commit `ca44f45`
(эквивалент родительского `1a37ea8`). Deploy implementation: `18b5405` плюс последующие документальные/hardening
правки этой ветки. Production deployment не запускался.

Локально пройден профиль M5: 25 tests (`test_m5_deploy_*`, `test_migration_m5`, `test_core_drain`) за 0.17 s.
Проверяются exact fingerprint, tamper, unsafe archive, STALE_BASE до service mutations, epoch/recovery/ownership,
WAL/state/offset/secret migration, mixed-version UI gate, reader-compatible code-only rollback, health identities и
порядок drain → stop → backup → switch → readiness → end_drain.

Расширенный набор migration дал 94 passed; 2 failures и 2 setup errors были вызваны запретом sandbox bind для
локальных Unix/TCP sockets, а не assertion торгового/deploy кода. Повтор вне sandbox и Linux functional staging ещё
не выполнены. Полный suite на точном финальном artifact также не выполнен; receipt не создан.

Независимый reviewer Astra xhigh назначен координатором и ещё не завершил проверку. Доступные token/usage counters
исполнителю не предоставлены. Время всей подготовки отдельно не измерялось. Время production deploy равно нулю:
он не проводился.
