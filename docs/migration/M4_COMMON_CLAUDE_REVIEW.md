# Common coordinator — пакет независимого ревью

Назначение владельца: Claude. Реализация: Codex и два GPT-5.6 Terra high помощника.
Их внутренние проверки не заменяют это ревью.

## Зафиксированный scope

- Repository branch: `codex/migration-m4-m5`.
- Принятая production baseline: `0f9037cf72de88131dcbcf8129bfd9455973dcba`.
- Common source candidate: `b6832d7de2f50bc9f22b4ed7583d839dd5f215dc`.
- Старый `f64b604` не выпускать: отсутствовали некоторые native admission gates.
- Проверять diff `git diff 0f9037c b6832d7`, а не общий многомесячный diff ветки.
- R0 reproductions, full Linux receipt и точные вопросы reviewer: `R0_REVIEW_HANDOFF.md`.
- Код кандидата заморожен отдельно. Последующие docs/evidence commits не являются
  новым проверенным исполняемым артефактом.

## Материалы

1. M4_COMMON_COORDINATOR_TZ.md — требования; назначение Claude заменяет историческую
   ссылку на Astra, но не требования к независимости и закрытию замечаний.
2. M4_COMMON_COORDINATOR_RESULT.md — реализации и конкретные тесты по критериям.
3. ADAPTER_GUIDE.md + examples/ — пять типов подключаемых адаптеров.
4. M4_COMMON_LOCAL_EVIDENCE.json — локальный development checkpoint.
5. Итоговый verification report кандидата — SHA, Linux receipt, platform checks,
   differential и пределы исторического replay; отсутствие receipt не считать pass.
6. PATCHNOTES/m4-common-coordinator-20260914.md — история изменений и инвариантов.

## Приоритет ревью

- operations.py: atomic admission/activation и commit_transition; frozen контекст,
  правильная связь intent/root/deal, состояние после исключения в formatter.
- generic_operations.py / operation_roots.py: обе native scopes; чужие позиции,
  reserve/approved target; first-dispatch TTL после quote и prepare; защитный hedge
  и resolve старого dispatch после expiry; нельзя начать второй root сверх approval.
- adapters/native.py / EventAttemptJournal: durable claim до отправки, native receipt
  отличен от внутреннего attempt ID, UNKNOWN без повторного submit; resume после
  перезапуска, нулевой reserve не доказывает отсутствие обязательства.
- leg_accounting.py / leg_cash.py: units/multiplier, net base fee, неиспользование
  perp notional как cash расхода, отдельные валюты, неизвестная оценка/комиссия.
- hl_preflight.py / perp_preflight.py: margin = notional/leverage + абсолютная fee;
  нет повторного multiplier, нет неявного приравнивания USDC к USDT.
- EVM/SOL compatibility policies: общий внешний lifecycle, старые rounding/clips/
  recovery readers сохранены. SOL+Gate/Aster fake-world не требует методов HL.
- Startup/read-model/DTO: generic формат не проходит через legacy mapping;
  reader floor атомарен до нового факта, rebuild не выдумывает chain/token/PnL.

## Не заявлено как завершённое

Новые реальные биржи и live generic execution, FX risk engine, полный исторический
PnL-аудит, late fee corrections, вся AC-01…29 приёмка, resize и SL. Эти функции не
должны случайно активироваться в этом патче. Generic примеры имеют fake transport.

## Ожидаемый результат

По каждому существенному замечанию: файл/строка, воспроизводимый сценарий и
нарушенный инвариант. Отдельно указать, что мешает выпуску, и что является следующим
этапом. Финальное заключение привязать к точному SHA; найденные ошибки исправляются
и проверяются до приёмки. Реальные ордера для ревью не отправлять.
