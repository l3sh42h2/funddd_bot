"""Статическая конфигурация фазы 1. Денежных параметров здесь нет и быть не должно:
всё, что касается размера, стопов и порогов, живёт в runtime/owner.toml и задаётся владельцем.
"""
from __future__ import annotations
import os
from pathlib import Path

ROOT = Path(os.environ.get("FUNDING_BOT_ROOT", Path(__file__).resolve().parents[2]))
RUNTIME = Path(os.environ.get("FUNDING_BOT_RUNTIME", ROOT / "runtime"))
DB_PATH = RUNTIME / "funding_bot.db"
TABLE_PATH = RUNTIME / "table.json"        # живое состояние коллектора для веб-процесса
OWNER_PATH = RUNTIME / "owner.toml"        # параметры владельца (фаза 2), пусто = запрет

USER_AGENT = "funding_bot/0.1 (+ireland)"
HTTP_TIMEOUT = 15

# --- площадки ----------------------------------------------------------------------
# Порядок = порядок, в котором владелец добавляет биржи (10.09: aster, binance, затем hyperliquid;
# «биржи добавляй в том порядке, в котором я говорю, а не сразу все»). Он же задаёт ориентацию пары
# во futures/futures: A — площадка, что раньше в списке.
# 12.09: «добавить фьючи kucoin bitget и gate, lighter; lighter нужны 2 вида: мейн и robinhood, оба спот и фьюч» — в этом
# порядке после hyperliquid. Клиенты: kucoin_fut.py, bitget_fut.py, gate_fut.py, lighter.py («lighter» — основной
# инстанс Lighter, «lighter_rh» — инстанс Robinhood Chain: те же тикеры там — ДРУГИЕ рынки).
# 13.09: «добавь биржи backpack, variational, edgex, extended, pacifica, apex во фьючи» — в этом порядке после lighter_rh,
# только перпы. Клиенты: backpack.py, variational.py, edgex.py, extended.py, pacifica.py, apex.py (в шапке каждого — замеры,
# лимиты, ловушки). Все шесть — перпы на оракуле площадки (identity.ORACLE_PERPS), квота USDC/USD/USDT.
PERP_VENUES = ("aster", "binance", "hyperliquid", "kucoin", "bitget", "gate", "lighter", "lighter_rh",
               "backpack", "variational", "edgex", "extended", "pacifica", "apex")
PERP_EXCHANGES = PERP_VENUES
# Споты — в порядке владельца (12.09: «нужны споты gate, kucoin, bitget» — после Binance; затем споты Lighter основного
# инстанса и Robinhood). Порядок задаёт и порядок сделок монеты в spot/futures. Клиенты Gate/KuCoin/Bitget — spot.py
# (свои API, не клоны Binance), Lighter — lighter.py.
SPOT_VENUES = ("binance_spot", "gate_spot", "kucoin_spot", "bitget_spot", "lighter_spot", "lighter_rh_spot")
# Классы активов перпов (12.09, сверка «покрываем всю линию»): crypto / equity (акции, ETF) / commodity / index / preipo.
# Пары и сделки строятся только внутри класса: Quant (QNT, монета) ≠ Quantinuum (xyz:QNT, акция); акции спотов
# (CRCLX, rCRCL, CRCLB…) — только перпу на акцию; золото-токены — только перпу на золото; индексам и pre-IPO спота нет.
# Binance/Aster/Hyperliquid называют часть акций своими тикерами — приводим к биржевому тикеру акции:
# 12.09 (новые перп-биржи): имена сырья и валют у бирж разные — приводим по определению (цена — только проверка единиц:
# живой срез 12.09, марки за единицу сошлись в пределах 0.2 %): медь — Aster/Lighter XCU против COPPER у Binance/HL/
# KuCoin/Bitget/Gate (6.52 $/фунт у всех); платина/палладий — HL PLATINUM/PALLADIUM против XPT/XPD у остальных; Brent —
# HL/Lighter BRENTOIL против BZ; валюта — Bitget EURUSD/GBPUSD/USDJPY против EUR/GBP/JPY у HL, Gate, Lighter (JPY у HL
# котируется как USDJPY, 153.5 у всех). Индексы (SPX500/SP500/US500, NAS100/NDX100/XYZ100…) — не приводятся: разные
# продукты с одним словом в имени не доказываются определением.
PERP_CANON = {"equity": {"QNTX": "QNT", "BBX": "BB", "STXX": "STX", "SKHX": "SKHYNIX", "SMSN": "SAMSUNG"},
              "commodity": {"GOLD": "XAU", "SILVER": "XAG", "XCU": "COPPER", "PLATINUM": "XPT", "PALLADIUM": "XPD",
                            "BRENTOIL": "BZ"},
              "fx": {"EURUSD": "EUR", "GBPUSD": "GBP", "USDJPY": "JPY"}}
# Рынки перпов, которые не встают ни в одну пару (futures/futures и spot/futures): единица цены не та, что у того же тикера
# на других биржах, и множителем её не привести (курс валюты плавает). Цена здесь — только проверка единиц, не «тот ли актив».
# 13.09, живой срез с Мака: Extended XIAOMI-USD — марк 26.28 при 3.36 у Bitget/Gate (и Aster, Lighter, edgeX в пределах 1 %):
# ×7.81 = курс HKD/USD — цена акции в гонконгских долларах при расчёте в USD (у рынка referenceMarket «us_equity»). Как
# акция XIAOMI он дал бы пары «тот же тикер в классе акция» с курсовым −87 %.
# 13.09 (тестировщик: «gate KR200_USDT — пары hyperliquid:xyz:KR200»): Gate KR200_USDT — KOSPI 200 в долларах, марк 0.80 при
# 1100 пунктах у HL xyz:KR200 и Bitget KR200USDT (индекс Gate: GateTradFi KR200_USD 0.812, iTick KOSPI200_KRW 0.813 = пункты ÷
# курс KRW). До 13.09 его уводила из пар своя база «KR200USD» в gate_fut.CANON — тестировщик её не видел и искал пары по
# тикеру биржи; исключение теперь одно, здесь, и его соблюдают и вселенная, и тестировщик.
PERP_UNPAIRED = {"extended": {"XIAOMI-USD": "цена в HKD за акцию (×7.8 к остальным биржам), множителем не привести"},
                 "gate": {"KR200_USDT": "KOSPI 200 в долларах (≈0.8 против ≈1100 пунктов у HL / Bitget): пункты ÷ курс KRW, "
                                        "множителем не привести"}}
# Синонимы «база перпа → база спота»: та же монета под другим тикером на споте. ТОЛЬКО проверенные (контракт токена,
# состав индекса перпа Binance, история цен — исследование 12.09 со скептиком); одинаковая цена бывает и у разных монет
# (ALL≈TWT, APR≈AT, ZCAT≈A — отклонены). Площадки — где синоним верен (None = на всех): на Binance и Gate «AI» — Sleepless
# AI, а не Gensyn; rON на Bitget — акция. Цену за токен всё равно проверяет «не тот актив».
SPOT_ALIASES: dict[str, dict[str, list[tuple[str, tuple | None]]]] = {
    "crypto": {
        "SPORTFUN": [("FUN", ("gate_spot", "kucoin_spot", "bitget_spot"))],       # Sport.fun
        "NEIRO": [("NEIROCTO", ("kucoin_spot", "bitget_spot"))],                  # First Neiro on Ethereum
        "RAYSOL": [("RAY", None)],                                               # Raydium
        "RONIN": [("RON", ("gate_spot",))],
        "LUNA2": [("LUNA", None)],                                               # Terra 2.0 (LUNC — отдельно)
        "BEAMX": [("BEAM", ("bitget_spot",))],
        "DODOX": [("DODO", None)],
        "RED": [("REDSTONE", ("kucoin_spot",))],
        "TST": [("TSTBSC", ("gate_spot",))],
        "BROCCOLI714": [("BROCCOLI", ("gate_spot",))],                           # CZ's Dog
        "AIGENSYN": [("AI", ("kucoin_spot", "bitget_spot"))],                    # Gensyn
        "DATAIP": [("DATA", ("gate_spot", "kucoin_spot", "bitget_spot"))],       # DATA Network (бывш. Story)
        "AXL": [("WAXL", ("gate_spot", "kucoin_spot"))],                         # Axelar
        "PHAROS": [("PROS", ("kucoin_spot", "bitget_spot"))],
        "EDGE": [("EDGEX", ("gate_spot",))],                                     # edgeX; EDGE на Gate — Definitive
        "CAT": [("SIMONSCAT", ("gate_spot",))],                                  # Simons Cat (перп 1000CAT)
    },
    "commodity": {
        "XAU": [("PAXG", None), ("XAUT", None), ("XAUM", ("kucoin_spot",))],     # 1 токен = 1 тройская унция
    },
}
# OKX DEX (владелец 12.09): сети и порог ликвидности — ЕГО решения; ключ — только в .env (см. okxdex.py)
OKX_DEX_CHAINS = {"solana": "501", "bsc": "56", "robinhood": "4663"}   # chainIndex OKX (EVM = EIP-155 id)
OKX_DEX_MIN_LIQUIDITY_USD = 50_000          # строка DEX появляется при ликвидности токена от $50k (порог владельца)
OKX_DEX_QUOTE_USD = 15                      # клип котировки — верх диапазона владельца $10–15
OKX_DEX_RPS = 1                             # триальный ключ: 1 запрос в секунду
# стейбл, за который котируется клип, и его знаки: USDT на BSC — 18 знаков (ловушка), USDC на Solana, USDG на Robinhood
# Chain (USDT/USDC там нет — исследование 12.09)
OKX_DEX_STABLES = {"56": ("0x55d398326f99059ff775485246999027b3197955", 18),
                   "501": ("EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v", 6),
                   "4663": ("0x5fc5360d0400a0fd4f2af552add042d716f1d168", 6)}
OKX_DEX_JOB_S = 180                         # цены пачкой раз в 3 мин (Market API: 100K бесплатных вызовов в месяц)
OKX_DEX_INFO_S = 3600                       # ликвидность и имена токенов — раз в час
OKX_DEX_QUOTE_BUDGET_S = 60                 # котировкам на клип в одном задании — не дольше (ключ 1 запрос/с)
OKX_DEX_STALE_S = 600                       # цена DEX старше — строка «устарела»
OKX_DEX_QUOTE_MAX_AGE_S = 7200              # котировка старше — «Комиссия» DEX-строки «—»
# Подпись площадки в колонке «Биржа» (у перпов — имя площадки как есть, кроме инстанса Lighter на Robinhood Chain:
# «lighter·rh», как «hyperliquid·xyz» у HIP-3)
LABELS = {"binance_spot": "binance", "gate_spot": "gate", "kucoin_spot": "kucoin", "bitget_spot": "bitget", "okxdex": "okx",
          "lighter_spot": "lighter", "lighter_rh_spot": "lighter·rh", "lighter_rh": "lighter·rh"}
# Aster — клон Binance-futures-API: те же пути, поля и заголовок веса. Hyperliquid — свой клиент (hyperliquid.py).
EXCHANGES = {
    "aster":        dict(base="https://fapi.asterdex.com", weight_limit=2400, kind="perp"),
    "binance":      dict(base="https://fapi.binance.com",  weight_limit=2400, kind="perp"),
    "binance_spot": dict(base="https://api.binance.com",   weight_limit=6000, kind="spot"),
}
QUOTE = "USDT"                              # квота спота Binance
# публичный список продуктов Binance: тег «bStocks» отличает токенизированные акции (CRCLB, NVDAB) от монет
BINANCE_PRODUCTS_URL = "https://www.binance.com/bapi/asset/v2/public/asset-service/product/get-products?includeEtf=true"
# Перпы: квоты-«доллары» в порядке предпочтения. На одной площадке одна монета — один рынок: USDC/USD1/U-перп
# берётся, только если у монеты нет USDT-перпа (тестировщик 11.09: 38 USDC-перпов Binance — все дубли USDT,
# а у Aster в USD1 есть уникальные листинги GPRO, SKHYNIX, SNDK, MU). BTC-квота — не доллар, не берём.
PERP_QUOTES = ("USDT", "USDC", "USD1", "U")
# TRADIFI_PERPETUAL — перпы Binance на акции/сырьё (NATGAS, MSTR, GPRO…): 191 шт., №1 по фандингу на сайте 11.09.
PERP_CONTRACTS = ("PERPETUAL", "TRADIFI_PERPETUAL", "")
# доля бюджета веса, после которой тик пропускается (429 — уже поздно, 418 — бан до 3 дней)
WEIGHT_SOFT_LIMIT = 0.70
# Пауза между вызовами истории. Binance: 500 вызовов fundingRate за 5 мин на IP. Hyperliquid: 1200 веса
# в минуту, история ~20 + 1 на 20 строк, заголовка веса нет — поэтому темп с запасом.
# 12.09 (новые перп-биржи, замер с Мака): KuCoin — пул 2000 веса / 30 с общий со спотом KuCoin, история вес 5;
# Bitget — 20 запросов/с на точку; Gate — 200 / 10 с на точку; Lighter — 60 запросов в минуту на IP у каждого инстанса.
# 13.09: Backpack — лимитов в документации нет, 429 не видели (0.5 с — с запасом); edgeX — чисел нет, 300 вызовов подряд
# без отказа; Extended — 1000/мин на IP; Pacifica — вызов истории ~90 из 1000 кредитов в минуту на IP (7.5 с = ~720 в
# минуту с тиком); ApeX — 600/мин на IP. У Variational истории нет вовсе (своя книга расчётов в памяти, сети нет).
FUNDING_HISTORY_MIN_GAP_S = {"binance": 0.7, "aster": 0.25, "hyperliquid": 4.0, "kucoin": 0.25, "bitget": 0.2, "gate": 0.1,
                             "lighter": 1.5, "lighter_rh": 1.5,
                             "backpack": 0.5, "edgex": 0.3, "extended": 0.25, "pacifica": 7.5, "apex": 0.25}
HL_SHORT_HISTORY_GAP_S = 1.5    # добор последнего часа — ответ в 1-2 строки, вес ~21: можно чаще длинных страниц
HL_HIP3_TTL_S = 40              # HIP-3 dex обновляются по одному за тик — ставка живёт медленно, лимит IP общий

# --- такты коллектора ------------------------------------------------------------
TICK_S = 10                 # ставки, марки, книги всех площадок + спот-тикеры
SNAPSHOT_S = 3600           # снимок таблиц в БД; владелец 12.09: раз в час, 30 сут (строк ×4 с новыми биржами: ~21 тыс.,
                            # ≈ 2.5 МБ за снимок → ~1.8 ГБ за 30 сут; раз в 15 мин было бы ~7 ГБ при 8 ГБ свободных)
                            # → ~1.6 ГБ за 30 сут хранения (на ireland свободно 8.2 ГБ; ниже DISK_MIN_FREE_GB снимки не пишутся)
FUNDING_INCR_S = 900        # досбор истории (пакетный, где есть) + проверка полноты и добор
INTERVALS_S = 900           # fundingInfo чаще вселенной: Aster меняет интервал на ходу (ревью 10.09)
UNIVERSE_S = 3600           # инструменты всех площадок, пересборка пар и сделок
HISTORY_DAYS = 30           # глубина бэкфилла (решение владельца 10.09: 7-30 суток тоже собирать)
# добор за проход. Пакета «все монеты» нет у Hyperliquid (~290 ног каждый час), Lighter (~200 + ~57 каждый час), Bitget
# (~790 ног после 00/08/16 UTC) и Gate (~980): в 00/08/16 это ~2300 вызовов — с прежним пределом 1000 хвост ждал
# следующего прохода (15 мин) и успевал стать «дырой» после CATCHUP_S
# 13.09: +6 перп-бирж. Пакета «все монеты» нет у Backpack (89 ног), Extended (320), Pacifica (75), ApeX (122) — это ~606
# вызовов добора каждый час сверх прежних; в 00/08/16 UTC вышло бы ~2900+ при пределе 3000 (живой срез ног 13.09). У edgeX
# пакет — последний расчёт каждого контракта; его строки несут prev_ms, и funding.apply_batch продлевает курсор через новый
# расчёт (ревью 13.09: без этого все ~170 ног edgeX уходили в посимвольный добор после каждого 4-часового расчёта, до
# ~3070 вызовов в 00/08/16). У Variational история своя (сети нет).
REPAIR_MAX_CALLS = 4000
WINDOWS_H = (4, 24, 72, 168, 720)     # окна истории в дашборде (эталон владельца: день / 3 дня / неделя / месяц)
WINDOW_LABELS = {4: "4 ч", 24: "1 день", 72: "3 дня", 168: "1 неделя", 720: "1 месяц"}
# Комиссии тейкера по прайс-листу базового тира (Aster 0.04 %, Binance futures 0.05 %, спот 0.1 %,
# Hyperliquid 0.045 %). Это факты бирж, не лимиты владельца; фактический тир счёта может быть ниже — поправить здесь.
# Споты 12.09 (официальные страницы комиссий, VIP0 без скидки токеном биржи): Gate 0.1 % (gate.com/fee), Bitget 0.1 %
# (support 12560603820584), KuCoin 0.1 / 0.2 / 0.3 % по классу пары A/B/C (feeCategory) — у KuCoin комиссию
# конкретной пары берёт клиент (spot.KucoinSpot → taker_fee), здесь — класс A. Поля комиссий в API Gate («0.2») и
# Bitget (takerFeeRate 0.002 у старых монет) — НЕ тариф VIP0, не использовать.
# Перпы 12.09: KuCoin 0.06 % (takerFeeRate у всех 687 контрактов, = ccxt tier 0), Bitget 0.06 % (VIP0 USDT-M; takerFeeRate
# у всех 787), Gate 0.05 % (VIP0, gate.com announcement 36485; поле taker_fee_rate «0.00075» — НЕ тариф), Lighter —
# стандартный счёт 0 % / 0 % на обоих инстансах и на споте (premium-счёт был бы 0.028 % основной / 0.035 % RH).
FEES_TAKER = {"aster": 0.0004, "binance": 0.0005, "binance_spot": 0.0010, "hyperliquid": 0.00045,
              "gate_spot": 0.0010, "kucoin_spot": 0.0010, "bitget_spot": 0.0010,
              "kucoin": 0.0006, "bitget": 0.0006, "gate": 0.0005,
              "lighter": 0.0, "lighter_rh": 0.0, "lighter_spot": 0.0, "lighter_rh_spot": 0.0,
              # 13.09, базовый тир: Backpack 0.05 % (Tier-1 perps, документация); Variational 0 % («no trading fees on Omni»
              # — издержка в спреде котировки RFQ: её добавляет calc.leg_cost, см. QUOTE_COST_VENUES); edgeX 0.045 % (VIP0 /vip/config/list и
              # defaultTakerFeeRate у всех контрактов — живой замер); Extended 0.025 % (документация, без ключа не проверить);
              # Pacifica 0.04 % (tier 1, документация и уровень 0 точки комиссий); ApeX 0.05 % (Level-1, документация)
              "backpack": 0.0005, "variational": 0.0, "edgex": 0.00045, "extended": 0.00025, "pacifica": 0.0004,
              "apex": 0.0005}
# Площадки, где сделка идёт по котировке самой площадки, а комиссии нет (ревью 13.09): Variational Omni — RFQ, «There are no
# trading fees on Omni», доход площадки — спред [D docs.variational.io/omni/trading/fees]. Круг ноги = 2 × тейкер (0) + ПОЛНЫЙ
# спред котировки на $1k (вход по ask/bid, выход обратно) — calc.leg_cost; котировки нет — «Комиссия» «—», как у DEX-ноги.
# Иначе строки с ногой Variational шли в сортировке по комиссии самыми дешёвыми. Живой срез /metadata/stats 12.09 23:14 GMT
# [L]: полный спред $1k — медиана 26.5 бп по 547 рынкам (p25 16.0, p75 39.5), шире 10 бп (круг тейкером Binance / Backpack /
# ApeX) — 86.5 %; BTC 1.1, ETH 1.1, SOL 1.7, DOGE 7.1, 1000PEPE 11.7, FARTCOIN 14.0, WIF 21.1. Размер позиции больше $1k
# платит больше (котировка size_100k шире) — здесь нижняя граница издержки [A].
QUOTE_COST_VENUES = frozenset({"variational"})
# Страницы токена на биржах (клик по имени биржи в дашборде). Aster: pro-режим = тот, что торгует API.
# Споты: {symbol} — символ пары ровно как в API (Gate BTC_USDT, KuCoin BTC-USDT, Bitget BTCUSDT). Страницы всех трёх —
# одностраничные приложения: несуществующая пара не даёт 404 (Gate молча показывает BTC), поэтому ссылка строится
# только из символов текущего списка инструментов.
URLS = {"aster": "https://www.asterdex.com/en/trade/pro/futures/{symbol}",
        "binance": "https://www.binance.com/en/futures/{symbol}",
        "binance_spot": "https://www.binance.com/en/trade/{base}_USDT",
        "gate_spot": "https://www.gate.com/trade/{symbol}",
        "kucoin_spot": "https://www.kucoin.com/trade/{symbol}",
        "bitget_spot": "https://www.bitget.com/spot/{symbol}",
        "hyperliquid": "https://app.hyperliquid.xyz/trade/{symbol}",
        # перпы 12.09 ({symbol} — как в API: XBTUSDTM, BTCUSDT, BTC_USDT). Ссылку Bitget/Gate/Lighter клиент кладёт в сам
        # инструмент («url»: 龙虾 — в %-кодировке, kPEPE у Lighter вместо 1000PEPE) — она важнее шаблона (calc.url).
        # У инстанса Lighter на Robinhood Chain страницы рынка нет (app.lighter.xyz показала бы рынок основного) —
        # шаблона нет, на странице имя без ссылки.
        "kucoin": "https://www.kucoin.com/trade/futures/{symbol}",
        "bitget": "https://www.bitget.com/futures/usdt/{symbol}",
        "gate": "https://www.gate.com/futures/USDT/{symbol}",
        "lighter": "https://app.lighter.xyz/trade/{symbol}",
        "lighter_spot": "https://app.lighter.xyz/trade/{base}_USDC",
        # 13.09: все шесть новых клиентов кладут ссылку в сам инструмент (calc.url берёт её первой); шаблон — запасной. Их
        # страницы — одностраничные приложения (Pacifica и ApeX отвечают 200 на любой путь), ссылка — только из текущего
        # списка. Backpack: BTC_USDC_PERP → 308 на BTC_USD_PERP; edgeX: /perpetuals/ (путь /trade/ молча уводит на BTC);
        # ApeX: {symbol} = crossSymbolName (BTCUSDT). У Extended шаблона НЕТ: приложение ведёт по uiName (1000PEPE → kPEPE),
        # шаблон по имени API у 40 переименованных рынков молча показал бы BTC-USD.
        "backpack": "https://backpack.exchange/trade/{symbol}",
        "variational": "https://omni.variational.io/perpetual/{symbol}",
        "edgex": "https://pro.edgex.exchange/en-US/perpetuals/{symbol}",
        "pacifica": "https://app.pacifica.fi/trade/{symbol}",
        "apex": "https://omni.apex.exchange/trade/{symbol}"}
SETTLE_GRACE_S = 600        # расчёт может появиться в истории с задержкой; раньше — не «дыра»
# Последний расчёт, которого ещё нет в БД, но меньше CATCHUP_S после него, — плановый досбор, а не дыра: для страницы
# ни «неполных», ни «!» (владелец 12.09: «эти предупреждения тут зачем?»). Досбор всё равно идёт. Hyperliquid пакетного
# «все монеты» не имеет — его 290 ног после hh:10:30 добираются по одной ~7.5 мин; с запасом — 25 мин после расчёта.
CATCHUP_S = 1500
# Ревью 11.09 (внешнее): планировщик обслуживания истории и опрос бирж.
TICK_DEADLINE_S = 8         # тик ждёт площадки не дольше; медленная площадка отвечает в следующем тике, остальные не ждут
TICK_RETRIES = 1            # тиковые запросы (ставки, книги) — одна попытка: повтор — это следующий тик, а не 3×15 с
TICK_HTTP_TIMEOUT = 8
MAINT_DEEP_BUDGET_S = 300   # бэкфилл глубины за один проход обслуживания — не дольше; свежие расчёты идут первыми всегда
RETRY_BACKOFF_S = 900       # нога, у которой история падает, повторяется через 15 мин, 30, 60 … до RETRY_BACKOFF_MAX_S
RETRY_BACKOFF_MAX_S = 4 * 3600
# Площадки с неизменным интервалом: разрыв больше интервала + льгота — уже дыра (у Hyperliquid всегда ровно 1 ч; у Lighter
# обоих инстансов — каждый час ровно на часе, шаг истории 3600 с, замер 12.09). KuCoin, Bitget, Gate меняют интервал на
# ходу (KuCoin TENCENT 8 → 4 ч, Bitget IOST 8 → 1 ч, Gate STORJ 4 → 1 ч; у KuCoin TRUST сетка 4 ч со сдвигом 3 ч) — их тут нет.
# 13.09: Backpack, Extended, ApeX — каждый час ровно на часе (30 сут без единого разрыва у всех рынков, живой замер).
# Не «неизменные»: Variational (интервал меняется на ходу, 1/4/8 ч), edgeX (интервал у контракта свой, сейчас 4 ч у всех),
# Pacifica (1 ч, но площадка пропускала расчёты 05.09 и 09.09 00:00 UTC — с «неизменным» это были бы вечные дыры).
FIXED_INTERVAL_H = {"hyperliquid": 1, "lighter": 1, "lighter_rh": 1, "backpack": 1, "extended": 1, "apex": 1}
# «Не тот актив» — по составу индекса перпа и контрактам монет, НЕ по цене (владелец 12.09: «надо отмечать их по
# контрактам где это возможно, либо по другим признакам, но не курс»). Решение — identity.py, сеть — identity_src.py.
BINANCE_COINS_URL = "https://www.binance.com/bapi/capital/v1/public/capital/getNetworkCoinAll"
BINANCE_ALPHA_URL = "https://www.binance.com/bapi/defi/v1/public/wallet-direct/buw/wallet/cex/alpha/all/token/list"
INDEX_LEGS_PATHS = {"binance": ("/fapi/v1/constituents", "constituents"), "aster": ("/fapi/v3/indexreferences", "references")}
DEXSCREENER_SEARCH_URL = "https://api.dexscreener.com/latest/dex/search?q="
XSTOCKS_MULTIPLIER_URL = "https://api.xstocks.fi/api/v2/public/assets/{symbol}/multiplier?network=Solana"
ONDO_TOKENLIST_URL = "https://raw.githubusercontent.com/ondoprotocol/ondo-global-markets-token-list/main/tokenlist.json"
ONDO_ORACLES = (("https://bsc-rpc.publicnode.com", "0xF4Fd8a1B412633e10527454137A29Db7Aa35F15e", 56),     # SyntheticSharesOracle
                ("https://ethereum-rpc.publicnode.com", "0x9BC39DB6fbB44B91a48b8D5A6C208B82B1741bE6", 1))
COINS_S = 3600              # списки монет спот-площадок (контракты, имена, ввод/вывод) и множители токенов акций
LEGS_S = 86400              # состав индекса перпа — раз в сутки (меняется редко); новый перп — в ближайшем задании
LEGS_BATCH = 80             # символов за одно задание площадки (~25 с): обход не держит её слот надолго
LEGS_PAUSE_S = 20           # между заданиями обхода, пока есть что собирать
LEGS_GAP_S = 0.25           # между вызовами состава индекса: 4 в секунду — малая доля минутного веса
LEGS_DEADLINE_S = 60        # пачка состава индекса — не дольше: зависшая точка не держит слот площадки (досбор истории)
SRC_DEADLINE_S = 90         # обход множителей акций (xStocks, Ondo) — не дольше
SHARES_STALE_MAX_S = 7 * 86400   # множитель, который перестал приходить, живёт прежним не дольше недели
SNAPSHOT_KEEP_DAYS = 30
DISK_MIN_FREE_GB = 2.0      # ниже — снимки не пишутся, история фандинга пишется всегда
STALE_S = 60                # возраст данных, после которого дашборд показывает красный чип
# владелец 13.09: «не показывай в таблице токены с курсовым спредом более 7%» — только страница (|курсовой| > порога, %),
# table.json не фильтруется: его читают трейдер и тестировщик
DASH_GAP_HIDE_PCT = 7.0
# Свой предел у площадки, чей снимок обновляется реже тика (13.09): Variational отдаёт одну выгрузку за Cloudflare-кэшем
# (s-maxage 60, источник меняется ~раз в минуту): ставке 30–60 с, котировкам RFQ 51–109 с (медиана 54–70) — живой замер с
# Мака. С общими 60 с большинство её строк было бы «устаревшими». 180 с — предложение интегратора, решение владельца.
STALE_S_BY_VENUE = {"variational": 180}

WEB_PORT = 8792             # lphedge занимал 8791; доступ только через ssh-туннель
WEB_HOST = "127.0.0.1"
