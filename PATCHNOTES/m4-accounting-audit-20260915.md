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

- `tests/test_generic_operations.py`: 101 passed (число для `2dd7337`; после кейса
  `incomplete_payload`, добавленного ниже в дополнении 16.09, — 103 passed).
- `tests/test_m4_leg_accounting.py tests/test_generic_leg_cash.py tests/test_m4_execution_scope.py`: 55 passed.
- Общий затронутый профиль: 156 passed (после дополнения 16.09 — 158, см. ниже).
- `git diff --check`: passed.

Отдельный immutable Linux profile для code SHA `566c035` продолжает выполняться;
его результат не подменяется этими локальными тестами.

## Дополнение после завершения Linux profile — 16.09.2026

Профиль, упомянутый выше как выполняющийся, завершился для точного runtime SHA
`566c03558e5d5543b4da504eac11e627a3f63ec9`: **2905 passed, 10 skipped,
0 failed**, exit code 0. SHA-256 построенного артефакта:
`e1bb870031983cc8a0d4d71e35b058883195e1af7822d28aec363fa24439a090`.
Это дополнение не переименовывает профиль в прогон follow-up `6a8b456`: тот
добавляет только тесты и документацию, а runtime source остался `566c035`.

## Дополнение — независимое ревью и фикс, 16.09.2026

Исполнитель: Claude. Независимое ревью коммита `2dd7337` (verdict передан
владельцем) нашло три находки в этом и предыдущем срезе; все три исправлены
здесь, поверх `2dd7337`, новым коммитом (история не переписывалась).

Разрешение владельца: внести эти правки без обычного согласования с Codex —
дано явно 16.09.2026 в чате FUNDING.

**1) Арифметика в разделе «Проверки» выше была неверна.** Было указано
`93 passed` / `148 passed общий затронутый профиль`; фактический прогон той
же команды на этой ветке даёт `tests/test_generic_operations.py: 101 passed`,
и общий затронутый профиль — **156 passed** (число исправлено выше по тексту,
не только здесь). Это совпадает с независимым подсчётом ревьюера
(`03_TARGETED_TESTS.txt` и `00_REVIEW_REQUEST.md`), но число 156 подтверждено
не копированием чужого отчёта, а собственным запуском pytest на этой ветке.

Причина расхождения: сам коммит `2dd7337` ("Strengthen recovery proof
regressions") добавил вторую ось `@pytest.mark.parametrize("recovery_status",
...)` к тесту `test_recovered_open_peer_requires_intact_terminal_proof`,
удвоив его матрицу с 8 до 16 кейсов (+8 к файлу: 93→101). Тот же коммит
дописал только раздел «Дополнение после завершения Linux profile» в этом
патчноуте — раздел «Проверки» выше остался нетронутым и не был пересчитан
после удвоения матрицы.

**2) `match=` в `test_recovered_open_peer_requires_intact_terminal_proof` был
слишком широким.** Общий `match="scope|identity|settled|plan"` на все 8 типов
повреждения заменён на точное сообщение под каждый тип (см.
`expected_message` в тесте), полученное запуском кода, а не предположением:
- `missing_terminal`, `other_root`, `other_intent`, `status_mismatch`,
  `malformed_payload`, `incomplete_payload` → все шесть фактически проваливаются
  на одной и той же fail-closed проверке `_active_generic_peer_scopes` /
  `_recovered_open_intents` в `operation_roots.py` и дают одно и то же
  сообщение `"active peer generic scope plan is missing"` — это не ослабление
  теста, а честное отражение того, что код ловит эти шесть искажений одним и
  тем же durable guard'ом (терминальное proof-событие не подтверждает scope).
- `nonzero_reserve`, `incomplete_target` → `"open peer generic scope root is
  not fully settled"`.
- `changed_parent` → `"active peer legacy scope is incomplete"`.

Мутационная проверка: временная подмена ожидаемого сообщения для
`changed_parent` на сообщение другого случая ловится тестом (AssertionError на
несовпадение regex) — то есть новая точность реально работает, а не
косметическая.

**3) Добавлен кейс `incomplete_payload`** (валидный JSON `"{}"` без ожидаемых
ключей) в ту же матрицу — до этого `malformed_payload` проверял только
синтаксически невалидный JSON (`"{not-json"`), но не валидный-но-пустой.
Прочитан код-читатель terminal proof payload при recovery —
`_recovered_open_intents` в `src/funding_bot/trade/operation_roots.py`
(строки 171-202, единственное место в дереве `src/`, которое разбирает
`exec_events.kind='operation_end'` json для recovery-scope proof). Он уже
безопасен: каждое поле читается через `payload.get(...)`, отсутствие любого
ожидаемого ключа уводит в `continue` (событие просто не попадает в `proven`),
а единственное прямое обращение по ключу (`payload["intent_state"]`) логически
недостижимо для отсутствующего ключа — до него код доходит только после того,
как та же строка `payload.get("intent_state") not in (...)` уже была False,
то есть ключ заведомо присутствует. Прод-код НЕ менялся; добавлен только тест,
подтверждающий это поведение (проходит через реальный `propose()`, а не через
отдельный reader-only юнит — так же, как остальные кейсы этой матрицы).

Прогон после всех трёх правок:
`tests/test_generic_operations.py tests/test_m4_leg_accounting.py
tests/test_generic_leg_cash.py tests/test_m4_execution_scope.py`: **158
passed** (156 из исправленного выше подсчёта + 2 новых кейса
`incomplete_payload` × `partial`/`interrupted`).
