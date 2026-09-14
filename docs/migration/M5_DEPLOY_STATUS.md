# M5 deploy checkpoint — не приёмка

Дата: 2026-09-14. База исполнителя: `aaf5737`; интегрирован согласованный core contract commit `ca44f45`
(эквивалент родительского `1a37ea8`). Deploy implementation: `18b5405` плюс последующие документальные/hardening
правки этой ветки (`2f98134`, `e98c90d`). Production deployment не запускался.

Последний сфокусированный локальный прогон deploy: 37 passed, 1 skipped (root-only Linux DAC) за 1.24 s; также пройдены
`bash -n`, `py_compile` и `git diff --check`. Более ранний локальный профиль M5 дал 29 passed после интеграции
финального core drain checkpoint.
Проверяются exact fingerprint, tamper, unsafe archive, STALE_BASE до service mutations, epoch/recovery/ownership,
WAL/state/offset/secret migration, mixed-version UI gate, reader-compatible code-only rollback, health identities и
порядок drain → stop → backup → switch → readiness → end_drain.

Изолированный root-only Linux DAC test прошёл 1/1 за 0.20 s на staging VPS во временном `/tmp`; fixture и source
archive удалены, production services/data не затрагивались. Это не заменяет полный Linux artifact profile.

Расширенный набор migration дал 94 passed; 2 failures и 2 setup errors были вызваны запретом sandbox bind для
локальных Unix/TCP sockets, а не assertion торгового/deploy кода. Повтор вне sandbox и Linux functional staging ещё
не выполнены. Полный suite на точном финальном artifact также не выполнен; receipt не создан.

Независимый reviewer Astra xhigh назначен координатором; его найденные блокеры внесены, повторная проверка ещё не
завершена. Доступные token/usage counters
исполнителю не предоставлены. Время всей подготовки отдельно не измерялось. Время production deploy равно нулю:
он не проводился.
