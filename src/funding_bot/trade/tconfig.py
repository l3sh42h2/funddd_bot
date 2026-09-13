"""Технические константы фазы 2 — НЕ деньги. Размеры, пороги, стопы, проскальзывание, плечо живут только в
runtime/owner.toml и задаются владельцем (пусто = запрещено, см. owner.py). Здесь — таймауты, ритм, адреса
контрактов, увиденные вживую, и пути файлов. Источник каждого числа — trade_spec §1/§3/§6 и отчёты 12.09.
"""
from __future__ import annotations
import os
from decimal import Decimal
from .. import config

# --- файлы runtime -------------------------------------------------------------------------------
TRADE_DB_PATH = config.RUNTIME / "trade.db"       # отдельно от funding_bot.db: не спорит с чистками коллектора
OWNER_PATH = config.OWNER_PATH
OKX_PACE_LOCK = config.RUNTIME / "okxdex.pace"    # общий темп ключа OKX (1 запрос/с) между коллектором и трейдером
TRADING_BUSY = config.RUNTIME / "trading.busy"    # пока есть — коллектор пропускает задание DEX
TG_HEALTH = config.RUNTIME / "tg_health.json"     # живость опроса для remote_verify.sh
EVM_LOCK_FMT = "evm_{addr}.lock"                  # один писатель на кошелёк: flock рядом с trade.db

# --- команды и Telegram ----------------------------------------------------------------------
PLAN_TTL_S = 60             # кнопка «да» живёт минуту: котировка старше уже не та
STALE_CMD_S = 90            # команда старше — «устарела» (после простоя Telegram отдаёт до 24 ч очереди)
POLL_S = 25                 # удержание long-poll getUpdates
POLL_TIMEOUT = (5, POLL_S + 10)   # (connect, read): полуоткрытый сокет умирает за 35 с, а не висит
TG_RENEW_AFTER_S = 3 * (POLL_S + 10)   # ~105 с без успешного опроса — новая сессия (опрос умирал молча на 12 ч)
TG_RENEWS_PER_H_MAX = 10    # больше — выход с кодом 3, но только пока исполнитель свободен
TG_SEND_GAP_S = 1.05        # темп отправителя (~1 сообщение/с на чат)
TG_SPLIT_CHARS = 4000       # лимит sendMessage 4096 после разбора сущностей — режем с запасом
TG_EDIT_MIN_S = 5           # сообщение прогресса правится не чаще
TG_START_REPLY_GAP_S = 600  # незнакомцу на /start — его id не чаще раза в 10 мин на чат

# --- пороги показа в сообщениях Telegram (НЕ денежные лимиты) --------------------------------------------
# Пороги показа, владелец 12.09 принял предложенные (вариант C «только то, что требует внимания»): штатное число в
# сообщение не пишется вовсе; вышло за порог — отдельная строка «⚠️ …» сразу под заголовком. Торговлю не меняют:
# только то, что владелец видит. Дельта ног сравнивается с шагом перпа (его даёт биржа), здесь числа нет.
SHOW_GAS_USD = Decimal("0.50")          # газ за команду (свопы + approve) больше — ⚠️
SHOW_IMPACT_X = Decimal("1.5")          # удар спота больше плана во столько раз — ⚠️
SHOW_COST_OVER_PLAN = Decimal("0.25")   # фактические издержки больше плана на 25 % — ⚠️
UNIT_PX_RATIO_MAX = Decimal(3)          # вход: цена контракта / цена токена на DEX вне [1/3, 3] — единицы не сходятся
                                        # (множитель, не видный в имени символа, или не тот токен) — отказ; техн. инвариант
SHOW_LIQ_ALERT_X = Decimal("1.25")      # до ликвидации меньше 1.25 × порога тревоги — ⚠️. Не 2×: при
                                        # liq_alert "auto" (½ расстояния на входе) 2× = само расстояние
                                        # на входе, и строка мигала бы от любого сдвига цены (12.09)
SHOW_NATIVE_SWAPS_MIN = 20              # газовой монеты сети (BNB/ETH, см. NATIVE_SYMBOL) меньше чем на 20 свопов — ⚠️
SHOW_LEVERAGE = 1                       # плечо не 1x …
SHOW_MARGIN_TYPE = "ISOLATED"           # … ISOLATED — ⚠️

# --- оценка сделок (trade/marks.py → deal_marks): кабинет и «позиции» (НЕ деньги) ----------------------------
# «PnL сейчас» и «PnL при выходе» считает трейдер (кабинет в биржи не ходит). Только чтение: котировка OKX, стакан,
# positionRisk, income. Во время исполнения — пропуск.
MARK_S = 300                            # раз в 5 мин для каждой активной сделки (одна котировка выхода на сделку)
MARK_STALE_S = 900                      # расчёт старше 15 мин — в кабинете серым «устарело»
MARK_KEEP_S = 7 * 86400                 # строки deal_marks старше 7 суток чистятся (последняя строка сделки остаётся)

# --- EVM: транзакции (BSC, Robinhood Chain) --------------------------------------------------------
TX_LOOKUP_S = 15            # нет чека — ищем по хэшу; неизвестна — те же сырые байты ещё раз
TX_BUMP_S = 30              # тот же nonce и calldata по цене ×TX_BUMP
TX_CANCEL_S = 90            # 0 нативных самому себе на том же nonce → SentUnknown, сделка на паузу
TX_BUMP = Decimal("1.2")    # geth-узлам нужна надбавка ≥10 %
TX_RECEIPT_POLL_S = 1.0
CANCEL_GAS = 21_000
GAS_LIMIT_TX_MULT = Decimal("1.5")    # документация OKX: «increase this value by 50%»
GAS_LIMIT_EST_MULT = Decimal("1.3")   # запас над eth_estimateGas (он же — предполётная симуляция); на Robinhood
                                      # Chain (Arbitrum Nitro) этот же запас покрывает и L1-комиссию — см. ниже
# chainId сети = chainIndex OKX для EVM-сетей (config.OKX_DEX_CHAINS, тот же EIP-155 id). Проверено вживую 13.09.2026:
# eth_chainId публичного RPC Robinhood Chain вернул 0x1237 = 4663 — совпадает.
CHAIN_IDS = {"bsc": 56, "robinhood": 4663}
# Нативный газ сети — только для текста гардов и отчёта (расчёты везде в wei, от сети не зависят): BSC — BNB,
# Robinhood Chain — ETH (docs.robinhood.com/chain: L1 — Ethereum, газ в ETH; eth_getBalance ноги той же цепи).
NATIVE_SYMBOL = {"bsc": "BNB", "robinhood": "ETH"}
STABLE_SYMBOL = {"bsc": "USDT", "robinhood": "USDG"}   # стейбл сети из config.OKX_DEX_STABLES (4663 — USDG, 6 знаков)
ERC20_TRANSFER_TOPIC = "0xddf252ad1be2c89b69c2b068fc378daa952ba7f163c4a11628f55a4df523b3ef"
SEL_BALANCE_OF = "0x70a08231"
SEL_ALLOWANCE = "0xdd62ed3e"
SEL_APPROVE = "0x095ea7b3"

# Публичные RPC по умолчанию; своё — переменной окружения (через запятую, см. RPC_ENV_BY_CHAIN). Чтение
# переключается по списку, отправка — нет (исход неизвестной отправки выясняется по хэшу, а не повтором через
# другой узел).
BSC_RPC_DEFAULTS = ("https://bsc-rpc.publicnode.com", "https://bsc-dataseed.bnbchain.org")
# Robinhood Chain (13.09.2026, документация + живой RPC с Мака). Единственный публичный узел в документации:
# rpc.mainnet.chain.robinhood.com (docs.robinhood.com/chain/run-a-full-node, robinhood.com/us/en/support/articles/
# robinhood-chain-mainnet) — RPC_URLS с одним адресом, как у любой новой сети, второго публичного не нашлось.
# eth_chainId → 0x1237 (4663); блок несёт l1BlockNumber/sendRoot/sendCount — признаки Arbitrum Nitro, и
# документация это подтверждает прямо: «Robinhood Chain is an Arbitrum Chain running Arbitrum Nitro» (L1 —
# Ethereum, нативный газ — ETH). eth_feeHistory отдаёт baseFeePerGas > 0 (в сети есть EIP-1559), но reward
# (приоритетная комиссия) на всех последних блоках — 0, eth_maxPriorityFeePerGas тоже 0 — подписываем как и
# раньше legacy-транзакцией (тип 0, EIP-155): Arbitrum Nitro принимает её наравне с EIP-1559, EvmWallet._sign_persist
# менять не нужно. L1-комиссию отдельно не считаем и оракул L1 (как у OP-стека) не дёргаем: eth_estimateGas сети
# уже включает её в L2-газ — это поведение Arbitrum Nitro, живой замер 13.09: перевод ETH без calldata
# 21000 → 21225 газа, с ~40 байтами calldata → 21890 (регрессия — tests/test_rh_evm.py); существующий запас
# GAS_LIMIT_EST_MULT (×1.3 над оценкой) и так с запасом это покрывает.
ROBINHOOD_RPC_DEFAULTS = ("https://rpc.mainnet.chain.robinhood.com",)
RPC_DEFAULTS_BY_CHAIN = {"bsc": BSC_RPC_DEFAULTS, "robinhood": ROBINHOOD_RPC_DEFAULTS}
RPC_ENV_BY_CHAIN = {"bsc": "BSC_RPC_URLS", "robinhood": "ROBINHOOD_RPC_URLS"}


def rpc_urls(chain: str, environ=None) -> tuple[str, ...]:
    """Публичные RPC сети по умолчанию, переопределяемые своей переменной окружения (список через запятую).
    Неизвестная сеть — KeyError (гард не должен молча взять чужой список)."""
    c = str(chain).strip().lower()
    if c not in RPC_DEFAULTS_BY_CHAIN:
        raise KeyError(f"нет RPC по умолчанию для сети: {chain}")
    env = os.environ if environ is None else environ
    raw = (env.get(RPC_ENV_BY_CHAIN[c]) or "").strip()
    urls = tuple(u.strip() for u in raw.split(",") if u.strip())
    return urls or RPC_DEFAULTS_BY_CHAIN[c]


def bsc_rpc_urls(environ=None) -> tuple[str, ...]:
    return rpc_urls("bsc", environ)


def robinhood_rpc_urls(environ=None) -> tuple[str, ...]:
    return rpc_urls("robinhood", environ)


# --- OKX DEX: allowlist контрактов, увиденных вживую 12.09 --------------------------------------
# Адреса берутся из каждого ответа API, а НЕ отсюда; здесь — только список допустимых. Роутер OKX менялся 30.03 и
# 04.08.2026 («update whitelist»): незнакомый роутер или spender = стоп и отчёт владельцу, а не молчаливое «принять».
# Ключ — chainIndex OKX (как config.OKX_DEX_STABLES); адреса в нижнем регистре.
# Robinhood Chain (4663) — сняты 13.09.2026 живыми /swap (покупка и продажа FATCOIN за USDG) и /approve-transaction
# с ключом OKX на VPS (только чтение: ответ — неподписанные данные транзакции, ничего не подписано и не отправлено):
# роутер 0x6e2a…6919 в обе стороны, метод тот же dagSwapByOrderId (decode_dag_swap разбирает), spender 0x4217…f53d
# у обоих токенов; у обоих адресов в сети есть код (eth_getCode: 13322 и 1605 байт). У 4663 роутер и spender свои
# (другой деплой, чем у BSC). Незнакомый адрес на 4663 теперь — «OKX сменила», как у BSC: стоп и отчёт владельцу.
OKX_ROUTERS = {"56": frozenset({"0x5994814f2c4040b863a0125a45de152a8c2a4dec"}),
               "4663": frozenset({"0x6e2a35a7ad683cf634d91492d73bb7ff774c6919"})}
OKX_SPENDERS = {"56": frozenset({"0x2c34a2fb1d0b4f55de51e1d0bdefaddce6b7cdd6"}),   # TokenApprove; старый 0x6015…ffaba8 — 0 событий
                "4663": frozenset({"0x42170295f1173c9e5874ea9d00c6d137e1a4f53d"})}
# calldata роутера: dagSwapByOrderId(uint256 orderId, BaseRequest, RouterPath[]) — единственный метод во всех живых
# ответах /swap 12.09 (8 монет BSC; покупка и продажа; 0.5 % и 3 %; сверено по 4byte и разбором). Получатель у
# него — msg.sender. Другой метод — стоп и отчёт, а не «принять».
SEL_DAG_SWAP = "0xf2c42696"
DAG_SWAP_TYPES = ("uint256", "(uint256,address,uint256,uint256,uint256)",
                  "(address[],address[],uint256[],bytes[],uint256)[]")
# Селектор/ABI — общий на все сети (не per-chain словарь): для 4663 подтверждён 13.09 теми же живыми /swap (покупка
# и продажа FATCOIN): метод 0xf2c42696, decode_dag_swap разбирает calldata.
# Хвост calldata после ABI (роутер читает его с конца) — «trim» OKX, 64 байта во всех живых ответах: слово 1 = флаг +
# 0x80 + ожидаемый выход (= toTokenAmount), слово 2 = флаг + доля + получатель. Доля берётся только с выхода СВЕРХ
# ожидаемого. Иное в хвосте (например, флаги комиссии 0x3ca20afc2aaa/…2bbb на чужой адрес) — стоп.
OKX_TRIM_FLAG = "777777771111"
OKX_TRIM_RATE_MAX = 100                 # увиденная вживую доля; больше — стоп
# 4663 — тот же получатель и та же доля (100), что у BSC: хвост обоих живых ответов /swap 13.09.
OKX_TRIM_RECEIVERS = {"56": frozenset({"0xfa00a9ed787f3793db668bff3e6e6e7db0f92a1b"}),
                      "4663": frozenset({"0xfa00a9ed787f3793db668bff3e6e6e7db0f92a1b"})}


# Имена сетей (ТЗ SOL×HL 13.09): разбор команд и строка таблицы пишут «sol» (dexleg.TAG), коллектор и OKX —
# «solana». Синоним нормализует только ИМЯ сети, адресов не касается (base58 Solana регистрозависим). chainIndex
# 501 — id сети у OKX, а не наш: сама сеть сверяется по getGenesisHash (trade/instruments.py).
CHAIN_ALIASES = {"sol": "solana", "rh": "robinhood"}   # «okx·rh» из команды/таблицы (dexleg.TAG) → robinhood


def canonical_chain(chain: str) -> str:
    """'sol' / 'SOL' / 'solana' → 'solana'; 'bsc' → 'bsc'. Неизвестная сеть — KeyError."""
    c = str(chain).strip().lower()
    c = CHAIN_ALIASES.get(c, c)
    if c in config.OKX_DEX_CHAINS:
        return c
    raise KeyError(f"неизвестная сеть: {chain}")


def chain_index(chain: str) -> str:
    """'bsc' → '56'; '56' → '56'; 'sol' → '501'. Неизвестная сеть — KeyError (гард не должен молча пропустить)."""
    c = str(chain).strip().lower()
    c = CHAIN_ALIASES.get(c, c)
    if c in config.OKX_DEX_CHAINS:
        return config.OKX_DEX_CHAINS[c]
    if c in config.OKX_DEX_CHAINS.values():
        return c
    raise KeyError(f"неизвестная сеть: {chain}")


def router_allowed(chain: str, addr: str | None) -> bool:
    return bool(addr) and addr.lower() in OKX_ROUTERS.get(chain_index(chain), frozenset())


def spender_allowed(chain: str, addr: str | None) -> bool:
    return bool(addr) and addr.lower() in OKX_SPENDERS.get(chain_index(chain), frozenset())


def router_confirmed(chain: str) -> bool:
    """Есть ли для сети хоть один подтверждённый вживую адрес роутера (allowlist не пуст). False у новой сети
    (например 4663) — evm_swap даёт отдельное сообщение «не подтверждён», а не «OKX сменила роутер»."""
    return bool(OKX_ROUTERS.get(chain_index(chain)))


def spender_confirmed(chain: str) -> bool:
    return bool(OKX_SPENDERS.get(chain_index(chain)))


# --- Aster v3 ----------------------------------------------------------------------------------
ASTER_BASE = "https://fapi.asterdex.com"    # fapi3 отвечал 403 (AWS ELB) 12.09
ASTER_EIP712_CHAIN_ID = 1666                # домен подписи AsterSignTransaction (не 56)
ASTER_CLOCK_SKEW_MAX_S = 2.0                # live не стартует при |часы − /fapi/v3/time| больше
ASTER_NONCE_TOLERANCE_S = 10                # документация: то 60 с, то 10 с — закладываемся на 10
ASTER_DEPTH_LIMIT = 50                      # /depth limit ≤ 50 стоит вес 2
ASTER_UNKNOWN_QUERIES = 3                   # «не выставлена» — только после -2013 трижды …
ASTER_UNKNOWN_SPAN_S = 5.0                  # … за ~5 с и неизменных userTrades и positionRisk
ASTER_IOC_PARTIAL_RETRIES = 3               # дальше — HEDGE_DEFICIT и пауза
ASTER_CLIENT_ID_RE = r"^[\.A-Z\:/a-z0-9_-]{1,36}$"


def aster_send_user(environ=None) -> bool:
    """Слать `user` в подписанных вызовах (как пример noop в документации). ASTER_SEND_USER=0 — запасной вариант,
    если первый readonly GET /fapi/v3/balance его не примет."""
    env = os.environ if environ is None else environ
    return (env.get("ASTER_SEND_USER") or "1").strip() != "0"


# --- планировщик (технические, не деньги) -------------------------------------------------------
CALIB_FRACS = (Decimal(1) / 8, Decimal(1) / 4, Decimal(1) / 2, Decimal(1))   # 4 котировки S/8…S для k и c0
R_PRIOR = Decimal(0)        # восстановление пула до первых своих клипов: в двух живых всплесках AIW3 его не было
# Допуск «пул восстановился» ε: допущение [A] до первых клипов бота — пересмотреть по замерам (выборка AIW3 = 2).
RECOVERY_EPS = Decimal("0.1")
# clips_max = "auto" (владелец 12.09) или пусто: перебор n = 1…PLAN_N_SCAN; раньше обрывается, когда клип мельче
# минимума перпа. Не потолок владельца — техническая граница перебора.
PLAN_N_SCAN = 50

# --- «auto» владельца 12.09 (технические правила подбора, не деньги) ------------------------------------
# α/β «auto»: «искать самый сбалансированный вариант под каждый токен (максимально выгодный мне)». План перебирает
# α × β × n и берёт минимум ожидаемых издержек; равенство — к меньшим дочерним, потом к меньшему β.
AB_ALPHA_GRID = tuple(Decimal(x) for x in ("0.1", "0.2", "0.3", "0.5", "0.7", "1"))
AB_BETA_LEVELS = 5                  # β-кандидаты: расстояние до каждого из первых 5 уровней стакана + тик
AB_BETA_FLOOR_TICKS = 3             # пол β = max(3 тика, 5 б.п.): IOC с узким кэпом не встаёт, если стакан
AB_BETA_FLOOR_BPS = Decimal(5)      # дрогнул между планом и отправкой
# exec_time_max_s «auto» = 3 × ожидаемая длительность плана + 60 с; число замораживается в плане
EXEC_TIME_AUTO_MULT = Decimal(3)
EXEC_TIME_AUTO_ADD_S = Decimal(60)
# liq_alert_pct «auto»: тревога, когда расстояние до ликвидации меньше половины расстояния на входе
LIQ_ALERT_AUTO_FRAC = Decimal("0.5")
