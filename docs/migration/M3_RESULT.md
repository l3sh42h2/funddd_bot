# M3 — независимые ноги и общие результаты

Статус: реализация адаптерного слоя в ветке codex/migration-m3; на VPS не установлена.
База d53e7e6: M0–M2 после финальной Linux-проверки. Исполнитель Codex по поручению владельца.
Независимый ревьюер владельцем не назначен; независимое ревью денежного пути обязательно перед M5.

## Реализовано

- Один trade/assembly для core и legacy trader вместо двух копий build_trader_legs.
- CredentialProvider: отдельные EVM/Aster/Gate/Solana/HL credentials, кэш процесса, без повторного чтения удалённых
  env values. Gate/Robinhood не требует готового Aster runtime или включённого BSC-профиля. Readonly ceiling Gate
  сохраняется при изменении owner-конфигурации. Solana и HL загружаются независимо; старый alias объединяет их.
- build_sol_component и build_hl_component отделены; старый build_sol_legs сохраняет совместимость вызовов.
- Versioned LegSpec/Action/Quote/Prepared/Result/Observation/ExecutionPage; Decimal, явные currencies, multiplier,
  account/network identity, capabilities. Prepared закреплён за полным fingerprint LegSpec и котировки.
- AdapterRegistry/AdapterContext: фабрика получает только одну ногу и её ресурсы. Production registry содержит
  существующие EVM OKX, Solana best-route, Aster, Gate, Hyperliquid; тот же registry используется contract tests.
- Bindings к реальным интерфейсам PerpLeg, OkxEvmSpot, SpotRouter/SolanaExecutor. Futures не требуют свойств HL
  аккаунта от другой площадки; DEX специфика остаётся внутри соответствующего binding/native executor.
- Старые inst_json/hash не меняются; map_legacy создаёт отдельное read-only представление и отказывает без identity
  proof. Разные USDG/USDT/USDC не объединяются.
- Native outcome mappings сохраняют partial/CANCELLED, provisional/finality, evidence и неизвестность комиссий.
  UNKNOWN с нулевым placeholder не становится доказанным нулевым исполнением. Solana confirmed не считается settled.
- Отказы precision/reduce-only вынесены из engine; HL market/agent/margin preflight — в адаптерный модуль.
- Durable submit barrier: PREPARED до внешнего действия, authorize до claim, CLAIMED без слепой повторной отправки
  после падения; иной attempt того же action_id также отказан. Нет коммита чужой открытой SQLite-транзакции.
- EVM swap binding проверяет одобренный min_receive после новой сборки маршрута до подписи. Старые вызовы swap
  без нового optional аргумента сохраняют прежнее поведение. Allowance остаётся отдельным нативным действием.
- Инструкция ADAPTER_GUIDE.md и исполняемый fail-closed шаблон новой площадки; пять синтетических типов площадок
  подключаются одной регистрацией без изменения учёта/Telegram/координатора.

## Проверки

- Полная локальная регрессия: 2193 passed, 3 skipped, 147.30 с. Прогон начат до заключительных локальных изменений;
  после них проверялись затронутые области отдельными прогонами ниже, полный набор не повторялся.
- Последующий широкий профиль: 280 passed, 7.97 с (M3, runtime, SOL engine, legacy ledger, RH/Gate, EVM, keys).
- Финальная изоляция bootstrap/Telegram/core: 78 passed, 5.57 с; сбой clock/init Aster блокирует его профиль,
  не отключая остальные. Для единственного Aster сохраняется configuration refusal.
- Заключительные M3 guards: 30 passed, 0.23 с. Включают защиту fingerprint аккаунта/спецификации и UNKNOWN-zero.
- Матрица: EVM/SOL × Aster/Gate/HL, BUY/SELL; CEX spot и perp/perp без wallet/gas; multiplier; identity mismatch;
  readonly; expiry; UNKNOWN/restart; changed payload/scope; cancelled partial; Solana finality; native bindings.
- Подробные команды/результаты: M3_TESTS.txt. Внешние заявки не отправлялись; синтетические credentials/tests only.
- Отдельного Linux-прогона именно M3 не было; Linux-проверка M2 не выдаётся за него. Linux build/release проверки
  новой общей миграции остаются обязательными перед переключением M5.

## Граница с M4 и M5

Общие submit-методы ещё не включены в боевой OperationController: его реализация и привязка DAO к старым
perp_orders/dex_txs/sol_tx_attempts входят в M4. Действующие бизнес-операции пока ведут совместимые engine/SolEngine.
M3 не означает удаления sol_flow, завершённого общего Ledger/recovery или разрешения новых live-комбинаций.
Минимальная таблица core_leg_attempts создаётся только при явном подключении AttemptJournal; текущий startup не
создаёт её в боевой БД. M4 должен связать её с операциями и доказать replay, reservations и recovery.

Старые profiles/limits и хеши сохранены. Новые CEX/DEX биржи не подключены live. Unit templates, службы и runtime VPS
не менялись. Resize/SL не включены. M4/M5 не выполнены; установка — только общим deploy с актуальной проверкой базы,
состояния открытых операций и независимым ревью. Откат кода не восстанавливает старую торговую БД поверх исполнений.

Работа выполнена в текущей сессии Codex, без дополнительных агентов/смены модели. Точные токены и effort не доступны;
цифры расхода подписки не оценивались. Указаны фактические длительности тестов, не выдуманное время отдельных решений.
