# M0–M2: отдельное торговое ядро и интерфейс

Автор/исполнитель: Codex, назначен владельцем 14.09.2026. Ветка codex/migration-m0-m2.
База: a976bec + receipt 0f88fd7 + ТЗ 9d80256. Статус review — реализация M0/M1/M2 завершена локально; финальный Linux-повтор ожидает разрешения auto-review.
Ревьюер реализации: не назначен владельцем; независимое ревью требуется перед M5.
Scope: M0, M1, M2 последовательно. M3–M5, resize, SL и новые live-площадки не включены.
M0 завершён: docs/migration/BASELINE.md и полный M0_MANIFEST.json. Боевой код не менялся.
Далее: headless core/IPC/inbox/outbox/singleton; UI-проекции, env/права/health, профильные тесты.
Торговые алгоритмы существующих engine/SolEngine сохраняются, данные/ключи наружу не передаются.

M1: отдельный headless core, durable inbox/outbox, UID/version/frame guards, общий execution lock,
отдельный interface transport. Результат docs/migration/M1_RESULT.md. Коммит M1: 198a266.
M2: Cabinet→IPC, core-owned read model, lossless Decimal DTO, свежие небольшие health, отдельные env/runtime,
три unit-шаблона и staging-подготовка прав/секретов; результаты docs/migration/M2_RESULT.md.
Полный Mac набор: 2164 passed / 2 skipped, 147.05 с; последующий профиль 215 passed, 9.61 с.
Linux до заключительных правок: 28 passed + 1 DAC permission test. Финальный повтор отдельно запрошен у владельца:
auto-review требует явное разрешение передачи src/tests/deploy на штатный VPS 34.65.234.12, хотя deploy.sh указывает
этот адрес. Не выдавать прошлый Linux-результат за проверку последних правок.
Изменения алгоритмов исполнения, новые live-площадки, resize/SL и выкат не выполнялись. Runtime/секреты/БД не в Git.
Самопроверка Codex; отдельных агентов не запускал. Метрики токенов/усилия модели в этом отчёте не подтверждены.

Заключительный локальный профиль M1/M2 после интерфейсного health: 31 passed, 5.96 с.
