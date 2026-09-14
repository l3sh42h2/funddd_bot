# M4/M5 — общие операции и переключение трёх процессов

Исполнитель: Codex, назначен владельцем. Ветка codex/migration-m4-m5; база 95354c4.
Статус in_progress. Обновлено 2026-09-14. Независимый ревьюер: GPT-6 Astra xhigh, назначен владельцем; запускается на подготовленный результат.
Scope: OperationController, Ledger/recovery/replay, архитектурные границы, verified artifact/deploy/drain,
миграция runtime и service accounts, итоговая проверка VPS по AC-01..29.
Ресайз, SL и новые live-стратегии не включены. Исторические inst_json/hash и исполненные деньги сохраняются.
Текущий код VPS сверяется до начала и повторно под замком перед переключением; старую БД поверх исполнений не возвращаем.
Чужая рабочая копия не меняется. Git fetch не выявил новых коммитов Claude; его рабочая копия без изменений кода.

Промежуточный результат (M4 ещё не завершён):
- Общие Exposure/HedgeDecision используются EVM/SOL для хеджа и rehedge; исторические политики округления сохранены.
- OperationController.begin_spot/settle_spot используются обоими исполнителями и EVM recovery.
  Клип + резерв одной существующей операции обновляются атомарно, повторный чек не списывает бюджет повторно.
- Новых таблиц/миграций нет. У EVM пока сохраняется legacy-модель корневой операции; общий lifecycle и M5 впереди.
- Профильные проверки: 156 passed за 8.03s (включая 11 новых тестов M4); локальный macOS Python 3.12.
- VPS read-only baseline: исходные src/deploy/tests/pyproject совпадают с M0; quick_check=ok,
  одна OPEN, одна ABORTED, approved/running intents и незавершённые отправки не обнаружены.
  Это снимок журналов, не внешняя сверка биржевых балансов. Боевых изменений нет.

Следующий checkpoint:
- Общие проекции quote-потоков подключены к EVM PnL, оценке шорта и SOL ledger.
- Подготовлены read-only preflight и primitives verified artifact/flock/STALE_BASE/SQLite backup.
  Deploy-only drain status показывает журналы, но переключение по-прежнему запрещено.
- Полный локальный набор: 2219 passed, 3 skipped, 148.03s; последующие preflight guards — 8 passed, 0.07s.
- Подробный статус и оставшиеся обязательные работы: docs/migration/M4_M5_PROGRESS.md.
- M4/M5 НЕ завершены, production НЕ изменён. Полный lifecycle, replay, независимое ревью и переключение впереди.
- Linux checkpoint: 158 passed / 82.12s, DAC 1 passed / 0.05s, без live-данных и нагрузочного теста.

Владелец разрешил двух помощников: m4_replay — GPT-5.6 Sol high (replay/учёт), m5_deploy — GPT-5.6 Sol high (deploy). Каждый работает в отдельном worktree; интеграция и выкат остаются у основного Codex.
