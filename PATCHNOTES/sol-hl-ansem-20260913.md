# sol-hl-ansem-20260913: связка Solana (Jupiter/OKX) × Hyperliquid para:ANSEM, профиль sol_best_hyperliquid

Автор: claude
Task: ТЗ владельца SOL_HYPERLIQUID_ANSEM_TZ_20260913 (владелец назначил Claude)
Исходная база: R0 (VPS 13.09.2026 13:51 UTC; фаза 1 + рейтинг A/B/C)

## Проблема и результат
Новая связка: лонг ANSEM (Solana, mint 9cRCn9rGT8V2imeM2BaKs13yhMEais3ruM3rPvTGpump, Token-2022) за USDC через лучший
из Jupiter Build V2 / OKX V6 (Jupiter Order — только показ) × шорт Hyperliquid para:ANSEM (HIP-3). Профиль в движке
рядом с BSC/Aster: вход одним клипом, хедж после finalized чека Solana, полный выход по фактическому списанию (возврат
DEX → остаток захеджирован), восстановление после сбоя без повторной покупки и двойного хеджа. Профиль ВЫКЛЮЧЕН, пока
владелец не заполнит owner.toml и runtime/instruments.json.

## Затронутые файлы / инварианты
- Новое: trade/solana/* (RPC, счета, чеки, резолвер, журнал, сообщения, валидатор инструкций по IDL, подпись),
  trade/{hl_rules,hyperliquid_trade,jupiter_spot,okx_sol_spot,spot_router,sol_route_validator,fees,instruments}.py,
  sol_doctor.py (`funding_bot sol-hl doctor|quote-compare|hl-preflight|record`), тесты и фикстуры.
- Изменено: trade/{types,store,owner,keys,tconfig,engine,planner,reconcile,marks,report}.py, tg/*, cabinet.py, cli.py,
  deploy/* (зависимости в .next, sol_gate, трейдер в откате), pyproject.toml.
- Инварианты: Δ = T·Fs − S·Fp в пределах шага или PAUSED с открытой экспозицией; UNKNOWN не превращается в ноль;
  одна незакрытая сделка на (площадка, сеть, счёт, dex, fullcoin); отпечаток инструмента DQA9Q e17075932fe21844 прежний.

## Проверки и evidence
- Полный набор на Маке: 2023 passed, 1 skipped (сетевая проба doctor).
- DQA9Q/BSC: 78 из 78 текстов бота и 21 из 21 рендер кабинета совпали байт в байт с версией до изменения.
- Ревью Opus по трём линзам (деньги и восстановление; регрессия и выпуск; безопасность и интерфейс) + перепроверка:
  7 подтверждённых находок исправлены (в т.ч. отозванный агент HL до свопа, чужой SOL в маршруте), 22 мутации ловятся.
- Скан секретов ветки по точным значениям ключей: совпадений нет.

## Миграция / совместимость открытых сделок
Схема trade.db 1 → 2 накатывается при старте нового трейдера одной транзакцией (новые таблицы и колонка
deals.perp_scope); ворота версии схемы. DQA9Q читается без изменений. Прежний код (R0) читает схему 2.

## Ограничения и порядок отката
- Откат на R0 допустим, пока нет сделок и попыток SOL/HL (sol_gate это проверяет); откат теперь перезапускает трейдер.
- Зависимости ставятся в боевой venv на месте (не атомарно); после отката новые версии пакетов остаются.
- Не проверено в сети до выката: Build V2 и OKX v6 с ключами, чек OKX на Solana, колёса cp311 (проверит remote_test).
- Перед первой живой сделкой — проверка уровня 5 (Fable max) по vps/CLAUDE.md.
