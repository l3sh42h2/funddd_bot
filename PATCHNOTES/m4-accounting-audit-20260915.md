# M4 — accounting audit and R07 proof regressions

- Исполнитель: Codex. Независимое ревью кандидата передаётся Claude по решению владельца.
- Область: тесты generic recovery scope и документированный аудит generic leg-aware accounting.
- Production/VPS: не менялись; этот commit не является разрешением на выкат.

## Изменение

В `tests/test_generic_operations.py` добавлена штатная fail-closed матрица
для retained `partial` после recovery. Она создаёт повреждённые terminal proof
только в тестовой SQLite-базе и проверяет, что disjoint proposal отклоняется,
если отсутствует terminal event, root/intent/status не совпадают, payload
невалиден, reserve не нулевой, target не подтверждён или frozen parent изменён.

`docs/migration/ACCOUNTING_COMPLETENESS_AUDIT_20260915.md` фиксирует проверенную
цепочку Result → quantity/cash facts → fee/funding buckets → read model и её
границы: historical completeness, FX-PnL, late fee correction и production
reconciliation этим срезом не объявляются выполненными.

## Проверки

- `tests/test_generic_operations.py`: 93 passed.
- `tests/test_m4_leg_accounting.py tests/test_generic_leg_cash.py tests/test_m4_execution_scope.py`: 55 passed.
- Общий затронутый профиль: 148 passed.
- `git diff --check`: passed.

Отдельный immutable Linux profile для code SHA `566c035` продолжает выполняться;
его результат не подменяется этими локальными тестами.
