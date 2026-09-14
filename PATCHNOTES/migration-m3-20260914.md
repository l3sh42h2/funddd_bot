# M3 — независимые адаптеры и результаты

Исполнитель: Codex, поручение владельца «продолжай M3 потом».
Ревьюер: не назначен владельцем. Статус: ready (не установлен). Обновлено: 2026-09-14.
База: d53e7e6 (M0–M2 проверены); ветка codex/migration-m3.

Scope: contracts/registry/credentials, совместимые aliases старых профилей,
нормализация результатов и отказов, preflight внутри адаптеров, contract/recovery tests.
Инварианты: immutable inst_json/hash; Decimal и доказанные единицы; UNKNOWN без слепого повтора;
режим и полномочия каждой ноги; без новых live-комбинаций, без переключения VPS.
Общий lifecycle/Ledger относится к M4; deploy к M5; resize и SL — следующий патч.
Затрагиваются trade/runtime, сборка ног core/tg, адаптеры и профильные тесты.

Результат: статус ready (код для review, не deployed). Отчёт docs/migration/M3_RESULT.md,
инструкция docs/migration/ADAPTER_GUIDE.md. Реализованы registry/contracts/native bindings,
независимые credentials и SOL/HL constructors, общий assembly, outcome mapping/preflight,
защита prepare/submit/fingerprint, read-only mapping исторических спецификаций.
Проверки: полный Mac 2193 passed/3 skipped/147.30 с; последующий профиль 280 passed/7.97 с;
заключительные guards M3 30 passed/0.23 с. Секреты/runtime/БД в Git не включаются.
Включение общего OperationController/DAO/Ledger — M4, переключение — M5. Новые live-пары,
resize/SL и изменение служб VPS не выполнялись. Независимое ревью ещё не проведено.

Финальная проверка startup isolation: 78 passed/5.57 с. Clock failure Aster изолирован; одиночный профиль
сохраняет configuration refusal. Exact-secret scan пройден, исключён только старый публичный fallback RPC literal.
