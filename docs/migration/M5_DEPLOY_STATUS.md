# M5 deploy checkpoint — не приёмка

Дата: 2026-09-14. База исполнителя: `aaf5737`; интегрирован согласованный core contract commit `ca44f45`
(эквивалент родительского `1a37ea8`). Deploy implementation: `18b5405` плюс последующие документальные/hardening
правки этой ветки (`2f98134`, `e98c90d`). Production deployment не запускался.

Последний сфокусированный прогон deploy: 22 passed, 1 skipped (root-only Linux DAC) за 0.16 s; также пройдены
`bash -n`, `py_compile` и `git diff --check`. Более ранний локальный профиль M5 дал 29 passed после интеграции
финального core drain checkpoint.
Проверяются exact fingerprint, tamper, unsafe archive, STALE_BASE до service mutations, epoch/recovery/ownership,
WAL/state/offset/secret migration, mixed-version UI gate, reader-compatible code-only rollback, health identities и
порядок drain → stop → backup → switch → readiness → end_drain.

Расширенный набор migration дал 94 passed; 2 failures и 2 setup errors были вызваны запретом sandbox bind для
локальных Unix/TCP sockets, а не assertion торгового/deploy кода. Повтор вне sandbox и Linux functional staging ещё
не выполнены. Полный suite на точном финальном artifact также не выполнен; receipt не создан.

Независимый reviewer Astra xhigh назначен координатором; его найденные блокеры внесены, повторная проверка ещё не
завершена. Доступные token/usage counters
исполнителю не предоставлены. Время всей подготовки отдельно не измерялось. Время production deploy равно нулю:
он не проводился.
