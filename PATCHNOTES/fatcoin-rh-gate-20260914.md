# fatcoin-rh-gate-20260914: связка OKX DEX Robinhood (USDG) × Gate, профиль rh_okx_gate (сделка FATCOIN)

Автор: claude
Task: сделка владельца FATCOIN 13.09 («gate+robinhood», «делай»; владелец назначил Claude)
Исходная база: claude/fatcoin 1f2579b = cb00a77 (выкачено 13.09) + модули Gate/Robinhood (0dadaa1)

## Проблема и результат
Прежний EVM-движок был прибит к одной паре констант CHAIN="bsc" / VENUE="aster". Теперь сеть и площадка берутся из
пары (план) и из сделки (выход, дохедж, откат, сверка, оценка) по таблице связок owner.EVM_PROFILES:
bsc_okx_aster = (bsc, aster) — прежняя, rh_okx_gate = (robinhood, gate) — новая. Токен FATCOIN
0x12d5ee7917ca430073c3a638ee1e6f0648a98a01 (Robinhood Chain 4663), стейбл USDG (6 знаков), газ ETH; перп Gate
FATCOIN_USDT, контракт = 100 токенов (quanto_multiplier), фандинг раз в 4 ч. Связка ВЫКЛЮЧЕНА, пока владелец не
впишет в owner.toml [profiles.rh_okx_gate], [wallets.rh_gate] evm_address, [perp.gate] и allow_contract_multiplier.

## Затронутые файлы / инварианты
- trade/engine.py: сеть и площадка — пары/сделки (_cv/_deal_cv/_cmd_cv, PairInfo.venue, Desk._legs_for/_deal_legs,
  Desk._pair_mode); лимиты, ключи live, плечо/маржа/тревога, разрешение множителя — [perp.<площадка сделки>];
  σ — своя модель площадки (Gate — минутные свечи); тексты USDG/ETH/Gate для Robinhood; отказ reduce-only Gate.
  Разбор имени контракта понимает «FATCOIN_USDT»; у Gate m — quanto_multiplier биржи (имя множитель не несёт).
- trade/runtime.py: EvmGateFactory (ноги RH × Gate, EVM-ключ — тот же, что у BSC, keys.load второй раз не зовётся);
  legs_of — ноги по связке сделки (не BSC для чужих сделок).
- trade/owner.py: профиль rh_okx_gate (enabled/mode), wallets.rh_gate.evm_address, live_required по сети.
- trade/reconcile.py, trade/marks.py: ноги по связке сделки; висящие транзакции кошелька при старте — только в сети
  своей ноги (адрес у BSC и Robinhood общий) и отдельно для каждой EVM-связки.
- trade/tconfig.py: CHAIN_ALIASES rh, STABLE_SYMBOL, allowlist OKX для 4663 (см. ниже); trade/gate_trade.py: sigma_1s.
- tg/bot.py: фабрика связки регистрируется только при enabled = true; tg/views.py: подписи Gate/Robinhood.
- Инварианты: сделка RH никогда не получает ноги BSC × Aster (план, исполнение, сверка, оценка, «позиции»); путь
  BSC × Aster — прежний; режим связки = меньший из её profile_mode и режима ключей (общий live не делает её живой);
  пара не из EVM_PROFILES в колонках сделки — прежняя BSC × Aster, её останавливает сверка с инструментом.

## Адреса OKX для 4663 (снято 14.09 на VPS, только чтение)
Живые /swap (покупка и продажа FATCOIN за USDG) и /approve-transaction с ключом OKX — ответ = неподписанные данные,
ничего не подписано и не отправлено. Роутер 0x6e2a35a7ad683cf634d91492d73bb7ff774c6919 (код 13322 байт), spender
0x42170295f1173c9e5874ea9d00c6d137e1a4f53d (код 1605 байт), метод dagSwapByOrderId (0xf2c42696) разбирается, trim —
тот же получатель 0xfa00…1a1b и доля 100, что у BSC. Нужно подтверждение владельца до включения связки.

## Проверки и evidence
- Полный набор на Маке: 2114 passed, 2 skipped.
- Новое tests/test_rh_gate_engine.py: вход $400 и полный выход (m = 100, USDG 6 знаков) на «боевых» фейках с воротами;
  ноги BSC × Aster «ядовитые» (любое обращение — BaseException) — план, исполнение, сверка на старте, оценка и
  «позиции» их не трогают; висящий approve RH не ищется в сети BSC; режим связки; фабрика ног; регистрация в боте; σ.
- test_rh_swap/test_rh_tconfig: 4663 теперь подтверждён — незнакомый адрес там «OKX сменила», как у BSC; хвост
  живого ответа 4663 проходит _check_trim.

## Миграция / совместимость открытых сделок
Схема trade.db не меняется. owner.toml на VPS не меняется (профиля нет — связка выключена, фабрика не регистрируется,
вход по FATCOIN — отказ «связка не подключена»). DQA9Q (BSC × Aster) идёт прежним путём.

## Ограничения и порядок отката
- Не проверено в сети: подпись и отправка свопа в Robinhood Chain (legacy-транзакция тип 0 на Arbitrum Nitro),
  подписанные заявки Gate на живом счёте, настоящая ликвидность пула под $400 (котировка $10 — удар около 1 %,
  план покажет удар на $400 до кнопки).
- /status показывает балансы только старой связки.
- Откат — deploy.sh (прежняя версия); безопасен, пока нет сделок rh_okx_gate.
- Перед первой живой сделкой — проверка уровня 5 (Fable max) по vps/CLAUDE.md.
