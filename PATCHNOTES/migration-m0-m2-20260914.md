# M0–M2: отдельное торговое ядро и интерфейс

Автор/исполнитель: Codex, назначен владельцем 14.09.2026. Ветка codex/migration-m0-m2.
База: a976bec + receipt 0f88fd7 + ТЗ 9d80256. Статус ready — реализация M0/M1/M2 завершена и проверена; не установлена.
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
Финальный Linux-профиль после разрешения владельца: 30 passed, 1 deselected, 8.13 с; DAC 1 passed, 0.07 с.
Архив содержит только исходники/тесты/шаблоны, одноразовый каталог удалён; боевые службы не менялись.
Изменения алгоритмов исполнения, новые live-площадки, resize/SL и выкат не выполнялись. Runtime/секреты/БД не в Git.
Самопроверка Codex; отдельных агентов не запускал. Метрики токенов/усилия модели в этом отчёте не подтверждены.

Заключительный локальный профиль M1/M2 после интерфейсного health: 31 passed, 5.96 с.
