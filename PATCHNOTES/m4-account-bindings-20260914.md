# M4: proven deal account bindings

- Исполнитель: Codex; независимый ревьюер: назначенный владельцем Astra xhigh.
- База: `da92f22`; ветка: `codex/migration-m4-m5`.
- Статус: `ready` (только storage/read primitives); обновлено: 2026-09-14.
- Область: `trade/scoped_accounting.py`, профильные тесты.

Добавлены неизменяемая явная привязка сделки к доказанному счёту и readers fills/funding.
Одинаковые native ID на разных счетах не объединяются. Повторные строки order не умножают fills.
Фандинг ограничен сохранёнными в БД временем открытия и закрытия; параметры caller не расширяют это окно.
Старые строки остаются на месте. Отсутствие binding отличается от пустого scoped результата.

Схема scoped accounting повышена до 2 с additive upgrade с 1; старый scoped reader откажется.
Основная trade schema не изменена. Активации binding на production нет. Эти readers пока не подключены
к engine/marks: частичное переключение смешало бы новый источник fills со старым funding и кэшем PnL.
Это фундамент интеграции, не выполнение AC-17/AC-20 целиком и не завершение M4/M5.

Перед подключением обязательны: доказательство публичного аккаунта адаптера, scoped ingestion и курсоры,
покрытие временного окна, единый переход всех readers (engine/reconcile/marks/core), отказ от старых
scope-blind marks/final.cost_usd, replay и reader gate основной БД. Строка proof_ref сама по себе
не доказывает происхождение счёта. Этот модуль не делает автоматическую атрибуцию старых записей.

Проверки: `test_m4_scoped_accounting.py` — 46 passed, 0.27s (scoped + replay). Независимое ревью Astra xhigh закрыто для этого bounded diff.
Выката и изменения действующего бота нет. Resize/SL не входят в этот патч.

Замечания Astra: отказ при неоднозначном владельце ордера одного account scope; отказ при
пересекающихся funding windows, включая равную границу; ABORTED ограничен saved updated;
проверка persisted venue/symbol/sim в reader. Проверка и выборка идут в одном deferred
read transaction/snapshot, caller transaction сохраняется. WAL race regression использует
два реальных connection и атомарный конкурирующий commit binding + финансовой записи.

Повторный migrate не восстанавливает потерянные финансовые/binding таблицы пустыми.
Установка metadata/table preflight идёт под write transaction; настоящий v1→v2 additive
upgrade остаётся разрешён. Проверки потери metadata row/table и source tables включены.

Astra closure: 36 passed, 0.06s; дополнительно проверены caller transaction при ошибках
трёх readers и потеря каждой из восьми таблиц. Новых blocking findings не обнаружено.
Root: scoped + replay 46 passed, 0.27s; exact-value secret scan passed; diff clean.
Это публикация кода в ветке, не активация scoped accounting и не выкат M4/M5.
