# M3 — независимые адаптеры и результаты

Исполнитель: Codex, поручение владельца «продолжай M3 потом».
Ревьюер: не назначен владельцем. Статус: in_progress. Обновлено: 2026-09-14.
База: d53e7e6 (M0–M2 проверены); ветка codex/migration-m3.

Scope: contracts/registry/credentials, совместимые aliases старых профилей,
нормализация результатов и отказов, preflight внутри адаптеров, contract/recovery tests.
Инварианты: immutable inst_json/hash; Decimal и доказанные единицы; UNKNOWN без слепого повтора;
режим и полномочия каждой ноги; без новых live-комбинаций, без переключения VPS.
Общий lifecycle/Ledger относится к M4; deploy к M5; resize и SL — следующий патч.
Затрагиваются trade/runtime, сборка ног core/tg, адаптеры и профильные тесты.
