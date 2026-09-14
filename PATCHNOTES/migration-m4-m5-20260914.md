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

Checkpoint общего lifecycle и корневых операций (2026-09-14, ещё in_progress):
- EVM и SOL используют общий цикл клипов; EVM approval/start/reserve/settle/resume теперь связаны с существующей
  таблицей operations. Новая команда продолжения сохраняет исходную цель и требует свежего одобрения остатка.
- Детерминированное отображение незавершённого legacy intent в root не переписывает исторические JSON/hash;
  подтверждённое списание и неизвестный резерв восстанавливаются отдельно. Неполное доказательство блокирует миграцию.
- Refused/requote до первого действия освобождает неиспользованный root; approved intent и root принимаются атомарно.
- Профильные результаты при интеграции: 78 passed/1 fixture mismatch (исправлено), SOL 58 passed/1 старый transition
  в тесте (исправлено), затем EVM/root integration + SOL callbacks 57 passed. Финальный полный набор ещё не выполнен.
- M5 получил durable transition journal, проверку артефакта/runner/runtime при reuse, независимые health identities,
  повтор доказательства drain после UI ACK. Astra повторно проверяет crash/retry/rollback; замечания ещё открыты.
- Это исходный код ветки, не установленный релиз. M4/M5 и итоговая приёмка остаются незавершёнными.

Исправления после независимого ревью AC19:
- Resume выбирает durable root последнего исполнения, а не первый отменённый план; отклонённая/истёкшая свежая
  котировка не отнимает возможность продолжить исходный остаток. PAUSED_RISK допускает новое одобрение.
- Не начатый root освобождается при restart сразу после approval и при отказе до первого эффекта; наличие
  неизвестного/подтверждённого исполнения запрещает считать операцию не начатой.
- SOL full-exit resume сохраняет root, цель и остаток. Свежие сравнения маршрутов получают новые раунды;
  новые попытки используют intent в logical_action_id, исторические ID не переписываются.
- Отказ чтения метаданных у SOL continuation сохраняет остаток в STOPPED; не объявляет старую цель отменённой.
- Настоящий get_drain_state возвращает durable release_id, необходимый механизму отката.
- Профиль EVM/SOL/root: 115 passed / 7.61s. IPC/process профиль вне sandbox: 20 passed / 4.70s.
- По отдельному разрешению владельца получен приватный финансовый срез двух EVM сделок с VPS в /tmp для offline
  replay; ключей, конфигурации кошельков и подписанных payload в выгрузке нет. В Git эти данные не включаются.

Следующий checkpoint адаптеров и учёта (in_progress, не релиз):
- Replay не принимает отсутствие SOL fills, receipt network_total или данные после среза за полную эквивалентность.
  Исправления закрыты независимым Astra review; 13 snapshot tests passed.
- Result v2 сохраняет scope, фактические raw суммы, валюты и native reference. Исполнение и полнота комиссий
  разделены; неизвестные fees не становятся нулём. Пустой PARTIAL остаётся UNKNOWN. Astra закрыл эти замечания.
- Добавлены общий rejection kind и явное доказательство terminal IOC partial; старый API rejection сохранён.
- PerpJournal использует существующий perp_orders как барьер отправки и exec_events для fingerprint;
  повторный claim отказан. Futures binding сохраняет hedge permission и HL journal links.
- Новый scoped_accounting — пока изолированный фундамент ingest, НЕ подключённый к production readers/writers.
  Разные аккаунты/символы имеют отдельные ключи; legacy attribution неизменяема; неприписанные строки сохраняются.
  Неполная страница и устаревший watermark не доказывают полноту данных. Последний профиль: 16 passed.
- Последний совместный adapter profile: 60 passed. Фундамент native journals, spot mapping и scoped ingest
  ещё не означает сквозное подключение общего API к денежному пути или закрытие AC-07/11–17/29.
- Оба помощника Sol остановлены сервисом из-за usage limit; завершённые коммиты и незавершённые файлы сохранены,
  продолжение и проверки выполняет координатор. Лимит не обходился сменой модели.
- Боевой бот, его службы и trade.db не менялись. Полная Linux-проверка артефакта и M5 переключение не выполнены.

### Проверенные барьеры отправки и перенос решений команд

- PerpJournal отказывает при внешней незавершённой транзакции: rollback вызывающего кода не может отменить
  claim уже отправленного ордера и разрешить повторную отправку. Native Aster/Gate сохраняют доказанный
  terminal partial, Hyperliquid предоставляет instrument для общего адаптера. Astra закрыл bounded review;
  профиль native/adapter/M3: 67 passed.
- Одобрение, nonce/TTL/CAS и проверка полномочий перенесены в core.approvals/core.authority; грамматика команд —
  в operator_commands. Telegram compatibility facades сохранены. Inbox bytes/hash/key/offset не изменены.
- Независимое Astra сравнение authority: 47 639 совпавших сценариев; 4 876 malformed-входов вместо прежнего
  исключения дают IGNORE, иных различий не найдено. Профиль reviewer: 232 passed. Локальный IPC/approval: 26 passed.
- Это промежуточный перенос AC-07: presenters/outbox ещё требуют отделения. Generic execution и scoped accounting
  пока не подключены сквозным образом. M4/M5 остаются in_progress, боевого выката не было.
