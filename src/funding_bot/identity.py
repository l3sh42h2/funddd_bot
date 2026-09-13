"""«Не тот актив» — по составу индекса и контрактам токенов, НЕ по цене (владелец 12.09: «надо отмечать их по
контрактам где это возможно, либо по другим признакам, но не курс»). Модуль чистый: сеть — в identity_src.py.

Прежний флаг сравнивал цены (индекс перпа против мида спота, порог 3 %) и ошибался в обе стороны: 17 из 31 флага
12.09 — та же монета с закрытым вводом на споте (ESPORTS, SIREN, GUA, MANTRA, ONE: цена уходит, актив тот же), а
у разных монет с похожей ценой флага не было бы вовсе. Исследование 12.09 (прототип на живом срезе, скептик):

ИСТОЧНИКИ
- перп Binance: /fapi/v1/constituents — рынки, из которых биржа считает индекс (757 из 761 перпа);
- перп Aster: /fapi/v3/indexreferences — то же (у 510 из 515 общих с Binance символов состав совпадает);
- перп Hyperliquid основного dex: по документации оракула — спот той же монеты на Binance (вес 3), KuCoin, Gate (1)
  (OKX/Bybit/Kraken/MEXC не наши); HIP-3 крипто — только имя из описания рынка;
- спот: контракты во всех сетях и имя монеты — Gate /spot/currencies, KuCoin /api/v3/currencies, Bitget
  /api/v2/spot/public/coins (имён нет — занимаются через общий контракт), Binance getNetworkCoinAll + список Alpha;
- DEX-пулы в индексе (Aster AI, MEME — токены сети Robinhood): поиск DexScreener, только однозначный доминирующий пул.

ВЕРДИКТ строки спот/перп (первое совпадение):
  same  index_market      этот рынок спота сам есть в индексе перпа
  same  contract          контракты спот-монеты и монеты индекса пересекаются
  other index_other_market индекс берёт ДРУГОЙ рынок этой же площадки, контракты не общие, имена разные (Gate EDGE)
  other contract_conflict контракты у обеих сторон есть, не пересекаются, имена разные (Aster AI, UP)
  same  name_bridged      контракты разные, имя то же (мост / миграция: FLOW, NEO, VELO)
  same  name              у одной стороны контракта нет, имена равны
  same  native_chain      родная монета одной и той же сети (ONE, MANTRA)
  unknown                 всё остальное — с причиной. Отсутствие рынка в индексе — НЕ «не тот»: биржа включает в
                          индекс не все рынки (ONG на Gate есть в индексе, на KuCoin — нет, а монета та же).
futures/futures: индекс одного ссылается на другой перп; общий рынок (наш, чужой биржи, поставщика цен, DEX-пул);
общий контракт; дальше как выше.
Акции, сырьё, валюта — по признакам площадок (класс перпа, категория токена); pre-IPO — единицы у площадок разные.

РЕЙТИНГ НАДЁЖНОСТИ «тот» (владелец 13.09: «ставь с названием монеты рейтинг надежности, A, B, C»): насколько надёжно
доказано, что две ноги строки — один актив. Новые поля вердикта rel / rel_ev / rel_d (буква, код причины, подробность у C);
ident / ident_ev / ident_why не меняются (их читают trade/engine.find_pair и audit). Торговлю рейтинг не ограничивает.
  A — биржа сама указала рынок или контракт (рынок в индексе перпа, контракт монеты такого рынка, ссылка индекса на перп,
      общая нога обоих индексов: рынок, чужая биржа, поставщик цены);
  B — через первоисточник: токен Binance Alpha по тикеру, код монеты в пространстве кодов той же биржи, родная сеть,
      реестр токенизированных акций и золота, тикер в классе, который указали биржи, общий пул DEX по названию пары;
  C — догадка: совпало только имя, пул DEX найден поиском по тикеру (AIW3), оракул Hyperliquid по тикеру.
Класс записи индекса — по её ПРЯМОМУ источнику (без наследования через склейку групп в _finish); сила строки — слабейшее
звено цепочки «нога ↔ запись индекса ↔ нога», из нескольких путей — сильнейший. Считается в сработавшей ветке вердикта
(плюс одна досчитка: «name» при общей родной сети — B): может занизить, но не завысит.
"""
from __future__ import annotations
import collections, re, logging
from . import config
from .symbols import norm_symbol_factor

log = logging.getLogger(__name__)

# --- нормализация идентификаторов токенов -------------------------------------------------------------------
_PLACEHOLDER = re.compile(r"^(invalid-.*|0x0+|0+|none|null|-|n/a)$", re.I)


def norm_id(a) -> str | None:
    """Канонический id токена. Выбрасываются заглушки ('', 0x0…0, invalid-* у Gate), тикеры вместо адреса и id,
    не уникальные без сети (eosio.token у Telos/Ultra/XPR, числовые)."""
    a = str(a or "").strip()
    if not a or _PLACEHOLDER.match(a):
        return None
    a = re.sub(r"_bak$", "", a)                                   # Bitget: списанный контракт
    m = re.fullmatch(r"0x([0-9a-fA-F]+)", a)
    if m:                                                          # EVM — нижний регистр; Starknet/Aptos — до 64 знаков
        h = m.group(1).lower()
        return "0x" + (h if len(h) == 40 else h.lstrip("0").rjust(64, "0"))
    m = re.fullmatch(r"0x([0-9a-fA-F]+)(::.+)", a)                 # Move: адрес::модуль::Имя
    if m:
        return "0x" + m.group(1).lower().lstrip("0").rjust(64, "0") + m.group(2)
    if re.fullmatch(r"[0-9a-f]{56}(\.?[0-9a-f]*)", a):              # Cardano: policy id
        return "ada:" + a[:56]
    m = re.fullmatch(r"[A-Za-z0-9]{1,12}_(G[A-Z2-7]{55})", a)       # Stellar CODE_ISSUER → issuer
    if m:
        return m.group(1)
    m = re.fullmatch(r"[0-9A-Fa-f]{40}\.(r[1-9A-HJ-NP-Za-km-z]{24,34})", a)   # XRPL hex.issuer → issuer
    if m:
        return m.group(1)
    m = re.fullmatch(r"(S[PM][0-9A-Z]{28,41}\.[\w-]+)(::[\w-]+)?", a)          # Stacks principal.contract
    if m:
        return m.group(1)
    if a == "eosio.token" or a.isdigit() or re.fullmatch(r"\d+:\d+", a):
        return None
    if re.fullmatch(r"[A-Za-z0-9]{1,10}", a):
        return None                                                # тикер вместо адреса ('BNB', 'NOOT')
    return a                                                       # Solana, TRON и т.п. — регистр значим


_CH = {"erc20": "eth", "ethereum": "eth", "bep20": "bsc", "bnbsmartchain": "bsc", "bnbchain": "bsc", "solana": "sol",
       "trc20": "trx", "tron": "trx", "baseevm": "base", "arbevm": "arb", "arbitrum": "arb", "arbitrumone": "arb",
       "matic": "polygon", "avaxcchain": "avaxc", "avalanche": "avaxc", "cchain": "avaxc", "opeth": "op",
       "optimism": "op", "suinew": "sui", "ton2": "ton", "toncoin": "ton", "aptos": "apt", "zksera": "zksync",
       "zksync2": "zksync", "zksyncera": "zksync", "cardano": "ada", "nearprotocol": "near", "algorand": "algo",
       "cosmos": "atom", "cosmoshub": "atom", "hedera": "hbar", "hederahashgraph": "hbar", "internetcomputer": "icp",
       "stellarlumens": "xlm", "stellar": "xlm", "ripple": "xrp", "xrpl": "xrp", "polkadot": "dot",
       "polkadotassethub": "dot", "statemint": "dot", "dotsm": "dot", "kusama": "ksm", "injective": "inj",
       "celestia": "tia", "kaspa": "kas", "stacks": "stx", "arweave": "ar", "babylon": "baby", "berachain": "bera",
       "bchsv": "bsv", "bitcoinsv": "bsv", "bitcoincash": "bch", "litecoin": "ltc", "dogecoin": "doge", "zcash": "zec",
       "vechain": "vet", "zetachain": "zeta", "terra": "luna", "terra2": "luna", "terraclassic": "lunc",
       "polymesh": "polyx"}
# родная монета сети, у которой тикер не совпадает с именем сети
_CHAIN_NATIVE = {"bsc": "BNB", "avaxc": "AVAX", "polygon": "POL", "sonic": "S", "plasma": "XPL", "monad": "MON",
                 "hyperevm": "HYPE", "hyperliquid": "HYPE", "bera": "BERA", "luna": "LUNA", "cronos": "CRO",
                 "mantle": "MNT", "flare": "FLR", "arb": "ETH", "base": "ETH", "op": "ETH", "zksync": "ETH",
                 "linea": "ETH", "scroll": "ETH", "klay": "KAIA"}
# сети со множеством токенов: пустой адрес там — пропуск данных, а не «родная монета сети»
_MULTI = {"eth", "bsc", "sol", "trx", "base", "arb", "polygon", "avaxc", "op", "sui", "ton", "apt", "zksync", "ada",
          "near", "linea", "scroll", "blast", "mantle", "celo", "kava", "kavaevm", "cronos", "ftm", "sonic", "xlm", "xrp",
          "algo", "hbar", "stx", "eos", "waves", "neo", "kcc", "heco", "okc", "hyperevm", "hype", "bera", "unichain",
          "sei", "seievm", "plasma", "monad", "taiko", "zora", "mode", "world", "worldchain", "ink", "soneium",
          "abstract", "brc20", "btc", "runes", "ordinals", "klay", "kaia", "movr", "glmr", "one", "cfx", "cfxevm",
          "flare", "core", "merlin", "bitlayer", "bouncebit", "opbnb", "manta", "metis", "aurora", "gnosis", "xdai",
          "fuse", "iotx", "iotaevm", "rootstock", "rsk", "vet", "icp", "dot", "ksm", "atom", "osmo", "inj", "kujira",
          "terra", "luna", "bnb", "avax", "matic20", "robinhood", "hyperliquid", "ethw", "etc", "celoevm"}


def chain_canon(c) -> str:
    k = re.sub(r"[^a-z0-9]", "", str(c or "").lower())
    return _CH.get(k, k)


# сети OKX DEX (владелец 12.09: Solana, BSC, Robinhood) по каноническому имени сети: 'bsc' → '56', 'sol' → '501'
DEX_CHAINS = {chain_canon(k): v for k, v in config.OKX_DEX_CHAINS.items()}


_GENERIC = re.compile(r"\b(token|protocol|network|coin|games?|inc|labs?|finance|the|official|foundation|dao)\b")


def _words(s) -> list[str]:
    return [w for w in re.split(r"[^a-z0-9]+", _GENERIC.sub(" ", str(s or "").lower())) if w]


def name_eq(a, b, strict: bool = False) -> bool:
    """Имена равны: те же буквы без общих слов («Yooldo» = «Yooldo Games», «OntologyGas» = «Ontology Gas»). Нестрого —
    ещё и набор слов одного внутри другого (от 4 букв). Ревью 12.09: нестрогое равенство годится, чтобы НЕ объявить
    «не тот» (Filecoin / Filecoin (IPFS)), но не чтобы объявить «тот»: Bitcoin ⊂ Bitcoin Cash, Terra ⊂ Terra Classic."""
    wa, wb = _words(a), _words(b)
    if not wa or not wb:
        return False
    if "".join(wa) == "".join(wb):
        return True
    if strict:
        return False
    small, big = (wa, wb) if len("".join(wa)) <= len("".join(wb)) else (wb, wa)
    return len("".join(small)) >= 4 and set(small) <= set(big)


def coin_record(name, chains, code: str = "") -> dict:
    """chains: [(сеть, адрес, ввод_открыт, вывод_открыт)] → запись монеты (JSON-совместимая, хранится в БД).
    Пустой адрес = родная монета сети — кроме сетей со множеством токенов (там это пропуск данных), если только тикер
    не монета самой сети (SOL на sol, BNB на bsc, ETH на arb)."""
    ids, nat, dex = set(), set(), {}
    cu = str(code or "").upper()
    dep = wd = False
    for c, a, d_open, w_open in chains:
        k, cc = norm_id(a), chain_canon(c)
        if k:
            ids.add(k)
            if cc in DEX_CHAINS:                       # контракт на сети OKX DEX — кандидат спот-ноги DEX (dexleg)
                dex.setdefault(DEX_CHAINS[cc], set()).add(k)
        elif not str(a or "").strip() and cc and (cc not in _MULTI or cc == cu.lower() or _CHAIN_NATIVE.get(cc) == cu):
            nat.add(cc)
        dep |= bool(d_open); wd |= bool(w_open)
    return {"name": name or None, "ids": sorted(ids), "nat": sorted(nat), "dex": {ci: sorted(v) for ci, v in dex.items()},
            "dep_closed": bool(chains) and not dep, "wd_closed": bool(chains) and not wd}


# --- ноги индекса --------------------------------------------------------------------------------------------
OURX = {"binance": "binance_spot", "binance_cross": "binance_spot", "binance_cross2": "binance_spot",
        "binance_cross3": "binance_spot", "gateio": "gate_spot", "kucoin": "kucoin_spot", "bitget": "bitget_spot"}
OTHX = {"okex": "okx", "okx": "okx", "mexc": "mexc", "mxc": "mexc", "bybit": "bybit", "coinbase": "coinbase",
        "kraken": "kraken", "bitfinex": "bitfinex", "apollox": "apollox",
        "huobi": "huobi", "htx": "huobi", "cryptocom": "cryptocom", "bingx": "bingx"}      # 12.09: индексы Gate и Bitget
DEXF = {"pancakeswapv3": ("pancakeswap", "v3"), "pancakeswapv2": ("pancakeswap", "v2"),
        "pancakeswap_v2": ("pancakeswap", "v2"), "pancakeswap": ("pancakeswap", None), "pancakeswapv4": ("pancakeswap", "v4"),
        "uniswapv2": ("uniswap", "v2"), "uniswap_v2": ("uniswap", "v2"), "uniswapv3": ("uniswap", "v3"),
        "uniswap_v3": ("uniswap", "v3"), "uniswap_v4": ("uniswap", "v4"), "uniswapv4": ("uniswap", "v4"),
        "raydiumclmm": ("raydium", "clmm"), "meteora_dlmm": ("meteora", "dlmm"), "pumpswapamm": ("pumpswap", None)}
# поставщики цен акций/сырья в индексах Gate и Bitget (12.09) — только явные поставщики традиционных рынков
VENDORS = {"pyth_pro", "pyth", "dxfeed", "kaiko", "massive", "databento", "polygon_forex", "itick", "alltick",
           "hyperliquid", "intrinio", "tiger", "itick_kr", "itick_jp", "itick_index", "infoway", "infoway_index",
           "gate_tradfi", "ondo"}
# ссылки на перпы и чужие индексы — не свидетельство ни в одну сторону (чужой индекс по тикеру — та самая ловушка)
PERPISH = {"aster", "aster_spot", "aster_futures_index", "gateio_futures",
           "bitget_future", "bitget_futures", "bitget_cross", "bybit_futures", "okx_futures", "hyperliquid_futures",
           "binance_index", "okx_index", "bitget_index", "bybit_index", "gvol"}
# собственная цена перпа в его же индексе: индекс «только из себя» — причина «не проверено» self_only
SELF_LEGS = {"gate": {"gateio_futures"}, "bitget": {"bitget_cross", "bitget_future", "bitget_futures"}}
# CEX-перп без собранного состава индекса: монета СВОЕЙ спот-площадки с тем же кодом — слабое свидетельство
# (одна биржа — одно пространство кодов), как «venue_code» у Binance. Состав индекса Gate и Bitget клиенты отдают
# (index_legs), но коллектор пока собирает его только у Binance/Aster; у KuCoin в индексе лишь имена бирж, без рынков.
VENUE_SPOT = {"kucoin": "kucoin_spot", "bitget": "bitget_spot", "gate": "gate_spot"}
# перпы на оракуле площадки без публичного состава (Lighter обоих инстансов): «не проверено», пока нет контракта или имени.
# 13.09: все шесть новых перп-площадок — тоже оракул площадки, состава индекса в API нет ни у одной (Backpack — не публичен;
# Variational — свой оракул; edgeX и Extended — Stork; Pacifica — статическая таблица оракула лишь в документации; ApeX — нет
# ни состава, ни контрактов). Свидетельство — только заявленное имя рынка (perp_names: Backpack, Variational, Extended,
# ApeX), у edgeX и Pacifica имён нет вовсе.
ORACLE_PERPS = {"lighter", "lighter_rh", "backpack", "variational", "edgex", "extended", "pacifica", "apex"}
_KUCOIN_RENAME = {"XBT": "BTC"}
_QS = ("USDT", "USDC", "FDUSD", "USD1", "USDG", "USD", "TRY", "EUR", "BTC", "ETH", "BNB", "UST", "BRL", "JPY")
_UIDX = re.compile(r"uindex\(([^)]*)\)")
_REFTOK = {"WBNB", "BNB", "WETH", "ETH", "USDT", "USDC", "USDG", "USD1", "SOL", "WSOL", "ZEC", "BTC", "WBTC", "CBBTC", "USD"}
HL_ORACLE_W = {"binance_spot": 3.0, "kucoin_spot": 1.0, "gate_spot": 1.0}   # документация оракула HL; прочие биржи не наши
DEX_MIN_LIQ = 25_000.0            # тоньше — не то, по чему биржа считает индекс перпа
_DEX_CHAINS = {"pancakeswap": {"bsc"}, "raydium": {"solana"}, "meteora": {"solana"}, "pumpswap": {"solana"}}
_ALPHA_CHAIN = {"56": "bsc", "1": "eth", "8453": "base", "CT_501": "sol", "4663": "robinhood", "42161": "arb"}


# рейтинг надёжности (см. шапку): класс записи индекса по её прямому источнику (it["src"] без «link:»)
_REL_ORD = {"A": 0, "B": 1, "C": 2}
_SRC_REL = {"leg": ("A", "leg"), "alpha": ("B", "alpha"), "venue_code": ("B", "code"), "dex": ("C", "pool"),
            "hl_oracle": ("C", "oracle")}
_REL_D_MAX = 32                   # подробность у C (пул, имя) — коротко: строк в table.json десятки тысяч


def _rel_of(it: dict) -> tuple | None:
    """Запись индекса → (буква, код, подробность): у пула DEX подробность — сам пул («pancakeswap AIW3-USDT»)."""
    s = str(it.get("src") or "")
    g = _SRC_REL.get(s[5:] if s.startswith("link:") else s)
    return g and (g[0], g[1], (it.get("pool") or "")[:_REL_D_MAX] or None if g[1] == "pool" else None)


def _best(*gs):
    """Сильнейший путь (A > B > C); None — пропуск; при равенстве — первый."""
    gs = [g for g in gs if g]
    return min(gs, key=lambda g: _REL_ORD[g[0]]) if gs else None


def _worst(*gs):
    """Слабейшее звено цепочки; None — пропуск; при равенстве — первый."""
    gs = [g for g in gs if g]
    return max(gs, key=lambda g: _REL_ORD[g[0]]) if gs else None


def _rel(letter: str, code: str, detail=None) -> tuple:
    return letter, code, (str(detail)[:_REL_D_MAX] if detail else None)


def _as(g, code: str):
    """Цепочка из двух прямых звеньев A (общий рынок / контракт обоих индексов) — свой код строки, а не «leg»."""
    return (g[0], code, None) if g and g[0] == "A" else g


def _nat_rel(n: set, *Is) -> tuple | None:
    """Общая родная сеть: слабейшее из (B «родная сеть», записи индексов с этой сетью — HL-оракул даёт C); из нескольких
    сетей — сильнейшая. Сетей нет — None."""
    return _best(*(_worst(_rel("B", "native"), *((X.get("nat_rel") or {}).get(x) for X in Is)) for x in sorted(n)))


def split_market(sym: str) -> str:
    """Символ рынка → код монеты: BTC-USDT, BTC_USDT, BTCUSDT → BTC."""
    s = str(sym or "").upper()
    if "-" in s or "_" in s:
        return re.split(r"[-_]", s)[0]
    for q in _QS:
        if s.endswith(q) and len(s) > len(q):
            return s[:-len(q)]
    return s


def dex_leg(x: dict) -> dict | None:
    """Нога индекса на DEX-пул: {family, ver, pool, toks} или None. Два вида: пул биржи-DEX ('pancakeswapV3',
    'WBNB-SIREN') и внутренний индекс Binance 'uindex(BOBPANCAKEV2USDT)*1000000'."""
    exl = str(x.get("exchange") or "").lower()
    s = str(x.get("symbol") or "")
    if exl in DEXF:
        fam, ver = DEXF[exl]
        pool = re.sub(r"[*/]?uindex\([^)]*\)[*/]?", "", s)
        pool = re.sub(r"\*\d+(\.\d+)?", "", re.sub(r"^\d+(\.\d+)?/", "", pool)).strip("*/ ")
        return dict(family=fam, ver=ver, pool=pool.upper(), toks=[t for t in pool.upper().split("-") if t])
    if exl in OURX or exl == "binance_index_combination":
        refs = _UIDX.findall(s)
        rest = re.sub(r"\*\d+(\.\d+)?(?![A-Za-z])", "", _UIDX.sub("", s)).strip("*/ ")
        if refs and not rest:
            m = re.match(r"^([A-Z0-9]+?)(PANCAKE|UNISWAP)(V2|V3|V4)?(USDT|USDC|WBNB|WETH)$", refs[0].upper())
            if m:
                return dict(family={"PANCAKE": "pancakeswap", "UNISWAP": "uniswap"}[m.group(2)],
                            ver=(m.group(3) or "").lower() or None, pool=f"{m.group(1)}-{m.group(4)}",
                            toks=[m.group(1), m.group(4)])
    return None


def perp_base(sym: str) -> str:
    """Код монеты перпа без квоты и множителя: 1000PEPEUSDT → PEPE, 1MBABYDOGEUSDT → BABYDOGE."""
    b = split_market(sym)
    m = re.match(r"^(1000000|1000|1M)(?=[A-Z0-9])(.+)$", b)
    return m.group(2) if m else b


def venue_code(pv: str, sym: str) -> tuple[str, float] | None:
    """Код монеты перпа в пространстве кодов ЕГО биржи и единица контракта: gate BTC_USDT → (BTC, 1), MBABYDOGE_USDT →
    (BABYDOGE, 1e6); bitget 1000BONKUSDT → (BONK, 1000); kucoin XBTUSDTM → (BTC, 1). Разбор — как у клиентов
    (norm_symbol_factor по имени, не по шагу количества). None — площадка не из VENUE_SPOT или символ чужой формы."""
    s = str(sym or "")
    if pv == "gate":
        if not s.endswith("_USDT"):
            return None
        b = s[:-5]
        if b.upper() == "MBABYDOGE":
            return "BABYDOGE", 1_000_000.0
    elif pv == "bitget":
        b = s[:-4] if s.endswith("USDT") else ""
    elif pv == "kucoin":
        m = re.fullmatch(r"(.+?)(USDT|USDC)M", s)
        b = _KUCOIN_RENAME.get(m.group(1), m.group(1)) if m else ""
    else:
        return None
    return norm_symbol_factor(b) if b else None


def dex_query(d: dict, base: str) -> str | None:
    """Запрос поиска пула: «ТОКЕН ПАРА» (AI ETH). None — пул не разобран."""
    toks = d["toks"]
    target = next((t for t in toks if t == base), None) or next((t for t in toks if t not in _REFTOK), None)
    counter = next((t for t in toks if t != target), None)
    return f"{target} {counter}" if target and counter else None


def dex_pairs(raw: dict) -> list[dict]:
    """Ответ поиска DexScreener → короткие записи пулов (хранятся в БД вместе с ногами)."""
    out = []
    for p in (raw or {}).get("pairs") or []:
        try:
            out.append({"chain": p.get("chainId"), "dex": str(p.get("dexId") or "").lower(),
                        "labels": [str(x).lower() for x in (p.get("labels") or [])],
                        "b": [p["baseToken"]["symbol"].upper(), p["baseToken"]["address"], p["baseToken"].get("name")],
                        "q": [p["quoteToken"]["symbol"].upper(), p["quoteToken"]["address"], p["quoteToken"].get("name")],
                        "liq": float((p.get("liquidity") or {}).get("usd") or 0)})
        except (KeyError, TypeError, ValueError, AttributeError):
            continue
    return out


# --- тексты для подсказки в дашборде ------------------------------------------------------------------------
UNKNOWN_WHY = {
    "no_sources": "источники ещё не загружены",
    "no_index": "у перпа нет публичного состава индекса",
    "self_only": "индекс перпа — только его собственная цена",
    "stale_only": "индекс ссылается только на неторгуемые рынки",
    "dex_unresolved": "индекс только из DEX-пулов, токен не определён однозначно",
    "empty": "в индексе нет рынков, которые можно проверить",
    "ambiguous": "индекс противоречив: равные по весу группы разных монет",
    "hl_no_oracle_venue_of_ours": "оракул Hyperliquid: на наших спот-площадках этой монеты нет",
    "hip3_no_annotation": "у рынка HIP-3 нет описания",
    "other_cex_only": "в индексе только чужие биржи (OKX, Bybit, MEXC…) или поставщики цен",
    "spot_blank": "у спот-монеты нет ни контракта, ни имени, ни родной сети",
    "absent": "рынка этой площадки нет в индексе, противоречий тоже нет",
    "index_inconsistent": "рынок — в меньшинстве индекса",
    "same_name_other_ticker": "индекс берёт другой тикер этой площадки с тем же именем",
    "contract_disjoint_no_name": "контракты разные, имени у одной стороны нет",
    "unit": "множитель рынка в индексе не совпадает с нашим",
    "preipo": "до IPO: единицы у площадок разные (оценка компании или доля)",
    "no_multiplier": "не получен множитель: сколько акций в одном токене",
    "names_similar": "контракты разные, имена похожи, но не совпадают",
    "resolver_error": "проверка упала — см. заметку «identity»",
    "spot_venue_unknown": "нет данных спот-площадки (список монет не загружен)",
    "oracle_only": "перп на оракуле площадки: состава индекса нет — проверка только по контракту или имени",
    "name_only": "у перпа известно только имя (оракул площадки), с именем спот-монеты оно не совпало",
    "index_not_collected": "состав индекса этой площадки пока не собирается, а монеты с тем же кодом на её споте нет",
    "name_ticker": "совпало только имя, равное тикеру — это совпадение тикеров, а не имён",   # связь DEX-токена (dex_links)
}


def _short(k: str) -> str:
    return k if len(k) <= 14 else k[:8] + "…" + k[-4:]


def _tc(r: dict) -> set:
    """Контракты записи монеты на сетях OKX DEX: {(chainIndex, id)}."""
    return {(ci, i) for ci, ids in (r.get("dex") or {}).items() for i in ids}


def _alpha_tc(t: dict, k: str | None) -> set:
    ci = DEX_CHAINS.get(_ALPHA_CHAIN.get(str(t.get("c")), ""))
    return {(ci, k)} if ci and k else set()


class Resolver:
    """spots: площадка → {"coins": {код: запись}, "markets": {символ: [код, торгуется]}, "alpha": [...] (Binance),
    "shares": {символ: {"n", "src"}}, "need_n": [символы]}; legs: перп-площадка → {символ: {"legs": [...] | None,
    "dex": {запрос: [пулы]}}}; hl_ann: рынок HIP-3 → описание; bn_assets: символ перпа Binance → baseAsset;
    aster_names: символ Aster → заявленное имя (tags[0]); perp_names: перп-площадка → {символ → заявленное имя} для
    остальных площадок (12.09: Lighter — имя из tokenlist, name_hint инструмента; без него перпы Lighter «не проверены»)."""

    def __init__(self, spots: dict, legs: dict, hl_ann: dict | None = None, bn_assets: dict | None = None,
                 aster_names: dict | None = None, perp_names: dict | None = None):
        self.T = {}
        for v, blob in (spots or {}).items():
            if not blob or not blob.get("markets"):
                continue
            coins = {c: dict(r, ids=set(r.get("ids") or ()), nat=set(r.get("nat") or ()),
                             dex={ci: set(v) for ci, v in (r.get("dex") or {}).items()})
                     for c, r in (blob.get("coins") or {}).items()}
            markets = {s: (m[0], bool(m[1])) for s, m in blob["markets"].items()}
            self.T[v] = dict(coins=coins, markets=markets, trading={s for s, (_c, t) in markets.items() if t},
                             trading_codes={c for c, t in markets.values() if t},
                             shares=blob.get("shares") or {}, need_n=set(blob.get("need_n") or ()))
        self.alpha = collections.defaultdict(list)
        for t in ((spots or {}).get("binance_spot") or {}).get("alpha") or []:
            self.alpha[str(t.get("s") or "").upper()].append(t)
        self.alpha_ok = bool(self.alpha)          # без списка Alpha «токенов с этим символом нет» не доказать
        self._borrow_bitget_names()
        self.legs = legs or {}
        self.hl_ann = hl_ann or {}
        self.bn_assets = bn_assets or {}
        self.aster_names = aster_names or {}
        self.perp_names = {v: dict(m or {}) for v, m in (perp_names or {}).items()}
        self.cache: dict[tuple[str, str], dict] = {}

    def ready(self) -> bool:
        return bool(self.T) and any(self.legs.get(v) for v in ("binance", "aster"))

    def _borrow_bitget_names(self):
        """У Bitget имён монет нет: имя занимается через общий контракт (другие площадки, потом список Alpha)."""
        bg = self.T.get("bitget_spot")
        if not bg:
            return
        owner = collections.defaultdict(set)
        for v in ("gate_spot", "kucoin_spot", "binance_spot"):
            for r in self.T.get(v, {}).get("coins", {}).values():
                if r.get("name"):
                    for k in r["ids"]:
                        owner[k].add(r["name"])
        for ts in self.alpha.values():
            for t in ts:
                k = norm_id(t.get("a"))
                if k and t.get("n"):
                    owner[k].add(t["n"])
        for r in bg["coins"].values():
            if not r.get("name"):
                names = sorted(set().union(*[owner[k] for k in r["ids"] if k in owner])) if r["ids"] else []
                if names:
                    r["name"] = names[0]

    # --- спот -----------------------------------------------------------------------------------------------
    def code_of(self, venue: str, market: str) -> str:
        m = self.T.get(venue, {}).get("markets", {}).get(market)
        return m[0] if m else split_market(market)

    def coin(self, venue: str, code: str) -> dict:
        r = self.T.get(venue, {}).get("coins", {}).get(code)
        return r or dict(name=None, ids=set(), nat=set(), dex={}, dep_closed=False, wd_closed=False, missing=True)

    # --- перп: что отслеживает индекс ------------------------------------------------------------------------
    def _parse(self, x: dict) -> dict:
        ex = str(x.get("exchange") or ""); exl = ex.lower(); s = str(x.get("symbol") or "")
        L = dict(ex=ex, sym=s, w=float(x.get("weight") or 0))
        if exl == "binance_alpha":
            L.update(kind="alpha", target=split_market(s)); return L
        d = dex_leg(x)
        if d:
            L.update(kind="dex", **d); return L
        if exl in VENDORS:
            L.update(kind="vendor", key=s.split("//")[0].upper()); return L
        if exl == "binance_future":
            L.update(kind="bnperp", ref=s.upper()); return L
        if exl in PERPISH:
            L.update(kind="perpref"); return L
        if exl in OURX or exl in OTHX or exl == "binance_index_combination":
            # множитель «*1000» или в экспоненте «*1e+06» (так пишет его bitget_fut._leg у 1MBABYDOGE, 1000000MOG)
            m = re.search(r"\*(\d+(?:\.\d+)?(?:[eE][+-]?\d+)?)(?![A-Za-z])", s)
            mult = float(m.group(1)) if m else 1.0
            refs = _UIDX.findall(s)
            rest = re.sub(r"\*\d+(\.\d+)?(?![A-Za-z])", "", _UIDX.sub("", s)).strip("*/ ")
            first = re.split(r"[*/]", rest)[0] if rest else ""
            if not first:
                L.update(kind="link", ref=refs[0].upper(), mult=mult) if refs else L.update(kind="skip")
                return L
            if exl == "binance_index_combination":
                L.update(kind="link", ref=first.upper(), mult=mult); return L
            # ревью 12.09: «BTCUSDT/BTCUSDC» — отношение квот (так Binance считает индекс USDCUSDT), а не рынок BTC:
            # перп USDC получал в «свои» контракты BTCB и ETH. «ONTTRY/USDTTRY» (базы разные) — рынок ONT, как раньше.
            parts = [p for p in re.split(r"[*/]", rest) if p]
            if len(parts) >= 2 and split_market(parts[0]) == split_market(parts[1]):
                L.update(kind="skip"); return L
            if exl in OURX:
                v = OURX[exl]
                T = self.T.get(v)
                if T is None:
                    L.update(kind="unknown_venue", venue=v); return L        # список монет площадки не загружен
                if v == "binance_spot" and first not in T["markets"] and first.upper() in self.bn_assets:
                    L.update(kind="link", ref=first.upper(), mult=mult); return L   # Aster 'binance:OPENAIUSDT' — перп
                if first not in T["trading"]:
                    L.update(kind="stale", venue=v, mkt=first); return L     # не торгуется: цены нет, свидетельства тоже
                L.update(kind="our", venue=v, mkt=first, code=self.code_of(v, first), mult=mult); return L
            L.update(kind="cex", venue=OTHX[exl], base=split_market(first), mult=mult); return L
        L.update(kind="unknown_ex"); return L

    def _alpha_pick(self, target: str, tokens: set, dex_chains: set):
        cands = self.alpha.get(target.upper(), [])
        live = [t for t in cands if not t.get("off")]
        if live and len(live) < len(cands):
            cands = live                        # ревью 12.09: мёртвый (offline) токен Alpha не выбирается, если есть живой
        if not cands:
            return None
        if len(cands) == 1:
            return cands[0]
        hit = [t for t in cands if norm_id(t.get("a")) in tokens]
        if len(hit) == 1:
            return hit[0]
        ch = [t for t in cands if _ALPHA_CHAIN.get(str(t.get("c"))) in dex_chains]
        return ch[0] if len(ch) == 1 else None

    @staticmethod
    def _dex_pick(L: dict, base: str, pairs: list[dict] | None):
        """Доминирующий токен пулов этого DEX (≥ $25k и втрое больше следующего) — или None."""
        q = dex_query(L, base)
        if not q or pairs is None:
            return None
        target, counter = q.split(" ", 1)
        chains = _DEX_CHAINS.get(L["family"])
        cset = {counter, "W" + counter, counter[1:] if counter.startswith("W") else counter}
        agg = collections.defaultdict(lambda: [0.0, None])
        for p in pairs:
            if not p["dex"].startswith(L["family"]) or (chains and p["chain"] not in chains):
                continue
            if L["ver"] and p["labels"] and L["ver"] not in p["labels"]:
                continue
            (bs, ba, bn), (qs, qa, qn) = p["b"], p["q"]
            if bs == target and qs in cset:
                addr, name = ba, bn
            elif qs == target and bs in cset:
                addr, name = qa, qn
            else:
                continue
            k = (p["chain"], norm_id(addr) or addr)
            agg[k][0] += p["liq"]; agg[k][1] = name
        if not agg:
            return None
        ranked = sorted(agg.items(), key=lambda kv: -kv[1][0])
        (chain, addr), (liq, name) = ranked[0]
        second = ranked[1][1][0] if len(ranked) > 1 else 0.0
        if liq < DEX_MIN_LIQ or liq < 3 * second:
            return None
        return dict(address=addr, name=name, chain=chain)

    def identity(self, pv: str, sym: str, depth: int = 0) -> dict:
        key = (pv, sym)
        if key in self.cache:
            return self.cache[key]
        I = dict(pv=pv, sym=sym, items=[], cex=set(), vendor=set(), dexpools=set(), refs=set(), notes=[],
                 status="ok", decl_names=set())
        self.cache[key] = I                                        # защита от циклов ссылок
        if pv == "hyperliquid":
            self._hl_identity(I, sym)
            return self._finish(I)
        entry = self.legs.get(pv, {}).get(sym)
        raw = (entry or {}).get("legs")
        if not raw and pv not in ("binance", "aster"):
            return self._finish(self._no_legs(I, pv, sym))
        if not raw:
            I["status"] = "no_index"
            return self._finish(I)
        base = perp_base(sym)
        P = [self._parse(x) for x in raw]
        dex_legs = []
        for L in P:
            k = L["kind"]
            if k == "our":
                r = self.coin(L["venue"], L["code"])
                I["items"].append(dict(src="leg", venue=L["venue"], code=L["code"], mult=L["mult"], w=L["w"],
                                       T=set(r["ids"]), TC=_tc(r), name=r["name"], nat=set(r["nat"]), tag=f"{L['ex']} {L['sym']}"))
            elif k == "stale":
                I["notes"].append("stale")
            elif k == "cex":
                I["cex"].add((L["venue"], L["base"]))
            elif k == "vendor":
                I["vendor"].add(L["key"])
            elif k == "dex":
                I["dexpools"].add((L["family"], L["pool"])); dex_legs.append(L)
            elif k == "bnperp" and pv == "binance" and L["ref"] == sym:
                I["notes"].append("self")
            elif k == "perpref" and L["ex"].lower() in SELF_LEGS.get(pv, ()):
                I["notes"].append("self")                          # Gate/Bitget: своя цена перпа в своём индексе
            elif k in ("bnperp", "link"):
                I["refs"].add(("binance", L["ref"]))
                if depth < 3:
                    sub = self.identity("binance", L["ref"], depth + 1)
                    for it in sub["items"]:
                        I["items"].append(dict(it, w=it["w"] * L["w"], src="link:" + it["src"],
                                               mult=it.get("mult", 1.0) * L.get("mult", 1.0)))
                    I["cex"] |= sub["cex"]; I["vendor"] |= sub["vendor"]; I["dexpools"] |= sub["dexpools"]
                    I["decl_names"] |= sub["decl_names"]
        toks = set().union(*[it["T"] for it in I["items"]]) if I["items"] else set()
        dex_chains = {"bsc" if L["family"] == "pancakeswap" else "sol" for L in dex_legs if L["family"] != "uniswap"}
        for L in P:
            if L["kind"] == "alpha":
                t = self._alpha_pick(L["target"], toks, dex_chains)
                if t:
                    k = norm_id(t.get("a"))
                    I["items"].append(dict(src="alpha", venue=None, code=t.get("id"), w=L["w"], T={k} if k else set(),
                                           TC=_alpha_tc(t, k), name=t.get("n"), nat=set(), tag=f"Binance Alpha {t.get('n')}"))
        toks = set().union(*[it["T"] for it in I["items"]]) if I["items"] else set()
        if not toks and dex_legs:
            self._dex_items(I, dex_legs, base, (entry or {}).get("dex") or {})
        if pv == "aster" and self.aster_names.get(sym):
            I["decl_names"].add(self.aster_names[sym])
        if self.perp_names.get(pv, {}).get(sym):
            I["decl_names"].add(self.perp_names[pv][sym])
        if pv in VENUE_SPOT and not any(it["T"] or it["name"] or it["nat"] for it in I["items"]):
            self._venue_code_item(I, pv, sym)                     # индекс собран, но из него ничего не следует
        if pv == "binance" and not any(it["T"] or it["name"] or it["nat"] for it in I["items"]):
            ba = self.bn_assets.get(sym)
            bs = self.T.get("binance_spot")
            if ba and bs and ba in bs["trading_codes"] and ba in bs["coins"]:
                r = bs["coins"][ba]                  # у Binance одно пространство кодов: слабое свидетельство
                I["items"].append(dict(src="venue_code", venue="binance_spot", code=ba, mult=1.0, w=1e-6,
                                       T=set(r["ids"]), TC=_tc(r), name=r["name"], nat=set(r["nat"]), tag=f"Binance: код {ba}"))
        if not I["items"] and not I["cex"] and not I["vendor"]:
            I["status"] = ("dex_unresolved" if I["dexpools"] else "stale_only" if "stale" in I["notes"] else
                           "self_only" if "self" in I["notes"] else "empty")
        return self._finish(I)

    def _venue_code_item(self, I: dict, pv: str, sym: str) -> bool:
        """Слабое свидетельство (вес 1e-6, как «venue_code» у Binance): монета своей спот-площадки перпа с тем же кодом.
        Единица контракта — в mult (перп 1000BONK против спота BONK — ×1000)."""
        V, vc = VENUE_SPOT.get(pv), venue_code(pv, sym)
        T = self.T.get(V) if V else None
        if not vc or not T:
            return False
        code, f = vc
        if code not in T["trading_codes"] or code not in T["coins"]:
            return False
        r = T["coins"][code]
        I["items"].append(dict(src="venue_code", venue=V, code=code, mult=f, w=1e-6, T=set(r["ids"]), TC=_tc(r),
                               name=r["name"], nat=set(r["nat"]), tag=f"{config.LABELS.get(V, V)}: код {code}"))
        return True

    def _no_legs(self, I: dict, pv: str, sym: str) -> dict:
        """Перп новых площадок (12.09) без собранного состава индекса. KuCoin/Bitget/Gate — монета своего спота с тем же
        кодом (слабое свидетельство); Lighter — оракул площадки: только заявленное имя, если его передали (perp_names).
        Иначе «не проверено» со своей причиной, а не догадка по тикеру."""
        name = self.perp_names.get(pv, {}).get(sym)
        if name:
            I["decl_names"].add(name)
        if pv in VENUE_SPOT and not self._venue_code_item(I, pv, sym) and not I["decl_names"]:
            I["status"] = ("no_sources" if VENUE_SPOT[pv] not in self.T else
                           "no_index" if pv == "kucoin" else "index_not_collected")
        elif not I["items"] and not I["decl_names"]:
            I["status"] = "oracle_only" if pv in ORACLE_PERPS else "no_index"
        return I

    def _dex_items(self, I: dict, dex_legs: list[dict], base: str, dex_cache: dict):
        """Поиск пула по символу — та самая ловушка тикеров: берётся, только если однозначно (единственный токен
        Alpha с этим символом совпал, или токенов Alpha нет и все пулы индекса согласны). Списка Alpha нет (не загрузился) —
        пулы не принимаются: «токенов с этим символом нет» тогда не доказано (ревью 12.09)."""
        if not self.alpha_ok:
            return
        got = []
        for L in dex_legs:
            q = dex_query(L, base)
            t = self._dex_pick(L, base, dex_cache.get(q)) if q else None
            if t:
                got.append((L, t))
        if not got:
            return
        al = self.alpha.get(base, [])
        addrs = {norm_id(t["address"]) or t["address"] for _, t in got}
        if len(al) > 1 or (len(al) == 1 and norm_id(al[0].get("a")) not in addrs) or len(addrs) > 1:
            return
        for L, t in got:
            a = norm_id(t["address"]) or t["address"]
            ci = DEX_CHAINS.get(chain_canon(t.get("chain")))
            # pool — подробность рейтинга C (найден поиском по тикеру): семейство DEX, а не exchange ноги — у uindex
            # Binance там «binance»
            I["items"].append(dict(src="dex", venue=None, code=a, w=L["w"], T={a}, TC={(ci, a)} if ci else set(),
                                   name=t["name"], nat=set(), tag=f"пул {L['ex']} {L['pool']}",
                                   pool=f"{L['family']} {L['pool']}"))

    def _hl_identity(self, I: dict, sym: str):
        if ":" in sym:                                             # HIP-3 крипто: только имя из описания
            desc = self.hl_ann.get(sym) or ""
            m = (re.search(r"price of ([^()]+?) \(([A-Z0-9]+)\)", desc) or re.search(r"price of ([A-Z][\w .]+?)\.", desc)
                 or re.search(r"value of one ([\w .]+?) token", desc))
            if m:
                I["decl_names"].add(m.group(1).strip())
            I["status"] = "ok" if m else "hip3_no_annotation"
            return
        base, fac = norm_symbol_factor(sym)                        # kPEPE → PEPE ×1000: оракул = 1000 × спот PEPE
        for v, w in HL_ORACLE_W.items():
            T = self.T.get(v)
            if T and base in T["coins"] and base in T["trading_codes"]:
                r = T["coins"][base]
                I["items"].append(dict(src="hl_oracle", venue=v, code=base, mult=fac, w=w, T=set(r["ids"]), TC=_tc(r),
                                       name=r["name"], nat=set(r["nat"]), tag=f"оракул HL: {config.LABELS.get(v, v)} {base}"))
        if not I["items"]:
            I["status"] = "hl_no_oracle_venue_of_ours"

    def _finish(self, I: dict) -> dict:
        """Группы: общий контракт, равное имя, одна родная сеть. Рынки, которые биржа САМА положила в индекс, —
        доверенные (цену перпа по чужой монете она не считает); наши догадки (Alpha по символу, пул DEX, оракул HL
        по тикеру) отбрасываются, если им противоречат. Без доверенных — побеждает самая тяжёлая по весу группа."""
        items = I["items"]
        n = len(items)
        par = list(range(n))

        def f(i):
            while par[i] != i:
                par[i] = par[par[i]]; i = par[i]
            return i
        for i in range(n):
            for j in range(i + 1, n):
                a, b = items[i], items[j]
                if (a["T"] & b["T"]) or (a["nat"] & b["nat"]) or (a["name"] and b["name"] and name_eq(a["name"], b["name"])) \
                        or (a.get("venue") and a.get("venue") == b.get("venue") and a.get("code") == b.get("code")):
                    par[f(i)] = f(j)
        cl = collections.defaultdict(list)
        for i in range(n):
            cl[f(i)].append(items[i])
        roots = list(cl)
        info = {r: (set().union(*[it["T"] for it in cl[r]]), {it["name"] for it in cl[r] if it["name"]}) for r in roots}
        wt = {r: sum(it["w"] for it in cl[r]) for r in roots}

        def conflict(a, b):
            (Ta, Na), (Tb, Nb) = a, b
            return bool(Ta and Tb and not (Ta & Tb) and not any(name_eq(x, y) for x in Na for y in Nb))
        trusted = [r for r in roots if any(it["src"] in ("leg", "link:leg") for it in cl[r])]
        acc, rej = set(), set()
        if trusted:
            acc |= set(trusted)
            base = (set().union(*[info[r][0] for r in trusted]), set().union(*[info[r][1] for r in trusted]))
            for r in roots:
                if r not in acc:
                    (rej if conflict(info[r], base) else acc).add(r)
        else:
            informative = sorted([r for r in roots if info[r][0] or info[r][1] or any(it["nat"] for it in cl[r])],
                                 key=lambda r: -wt[r])
            if informative:
                top = informative[0]
                rivals = [r for r in informative[1:] if abs(wt[r] - wt[top]) < 1e-12 and conflict(info[r], info[top])]
                if rivals:
                    I["status"] = "ambiguous"
                    rej |= set(informative)
                    acc |= {r for r in roots if r not in rej}
                else:
                    for r in roots:
                        (rej if r != top and conflict(info[r], info[top]) else acc).add(r)
            else:
                acc |= set(roots)
        keep = [it for r in acc for it in cl[r]]
        drop = [it for r in rej for it in cl[r]]
        I["markets"] = {(it["venue"], it["code"]): it.get("mult", 1.0) for it in keep if it.get("venue")}
        I["mkt_src"] = {(it["venue"], it["code"]): it["src"] for it in keep if it.get("venue")}
        I["leg_markets"] = {k for k, s in I["mkt_src"].items() if s in ("leg", "link:leg")}
        I["rejected"] = {(it["venue"], it["code"]) for it in drop if it.get("venue")}
        I["tokens"] = set().union(*[it["T"] for it in keep]) if keep else set()
        I["tok_src"] = {k: it["tag"] for it in keep for k in it["T"]}
        I["names"] = {it["name"] for it in keep if it["name"]} | I["decl_names"]
        I["name_codes"] = {}                                  # имя → коды монет, у которых оно записано (имя = тикер?)
        for it in keep:
            if it["name"]:
                I["name_codes"].setdefault(it["name"], set()).add(str(it.get("code") or ""))
        I["natives"] = set().union(*[it["nat"] for it in keep]) if keep else set()
        I["dex"] = set().union(*[it.get("TC", set()) for it in keep]) if keep else set()   # (chainIndex, id) на сетях DEX
        # рейтинг надёжности: класс по ПРЯМЫМ записям (собственные T / TC / nat принятой записи), не через склейку групп —
        # токен, пришедший лишь из venue_code, остаётся B, даже если в его группе есть доверенная нога (ревью 13.09, случай F)
        I["tok_rel"], I["dex_rel"], I["nat_rel"], I["mkt_rel"] = {}, {}, {}, {}
        for it in keep:
            g = _rel_of(it)
            for m, keys in ((I["tok_rel"], it["T"]), (I["dex_rel"], it.get("TC") or ()), (I["nat_rel"], it["nat"]),
                            (I["mkt_rel"], [(it["venue"], it["code"])] if it.get("venue") else ())):
                for k in keys:
                    m[k] = _best(m.get(k), g)            # лучший из записей; mkt_src же хранит последнюю
        return I


# --- вердикты ------------------------------------------------------------------------------------------------
def _v(verdict: str, ev: str, why: str, g: tuple | None = None) -> dict:
    """Вердикт; g — рейтинг (буква, код, подробность), только у «тот». Ключи rel* есть у ВСЕХ вердиктов: resolve() делает
    it.update(d), и после смены «тот» → «не проверено» прежняя буква на строке не должна остаться."""
    g = g if verdict == "same" and g else (None, None, None)
    return dict(ident=verdict, ident_ev=ev, ident_why=why, rel=g[0], rel_ev=g[1], rel_d=g[2])


def _u(reason: str, extra: str = "") -> dict:
    return _v("unknown", reason, UNKNOWN_WHY.get(reason, reason) + (f" ({extra})" if extra else ""))


def _names_match(n, names, strict: bool = False) -> bool:
    return any(name_eq(n, x, strict) for x in names)


def _nm(r: dict) -> str:
    return f" ({r['name']})" if r.get("name") else ""


def _tickerish(name, code) -> bool:
    """«Имя» монеты, равное её собственному тикеру, — не имя. Живой срез 12.09: KuCoin NIGHT записан как «NIGHT» (Cardano),
    Binance NIGHT — «Midnight» (BSC); это одна монета (индекс Binance NIGHTUSDT берёт KuCoin NIGHT-USDT), а «разные
    контракты и разные имена» давали ложный «≠»."""
    a, b = "".join(_words(name)), "".join(_words(code))
    return bool(a) and a == b


def _only_tickers(I: dict) -> bool:
    """Все имена стороны перпа — лишь тикеры своих монет (заявленные имена площадок — настоящие)."""
    nc = I.get("name_codes") or {}
    return bool(I["names"]) and all(any(_tickerish(n, c) for c in nc.get(n, ())) for n in I["names"])


def decide_sf(R: Resolver, row: dict) -> dict:
    cls = row.get("cls") or "crypto"
    V = row.get("spot_ex") or "binance_spot"
    if cls != "crypto":
        return _decide_sf_tradfi(R, row, cls, V)
    if V not in R.T:
        return _u("spot_venue_unknown")
    code = R.code_of(V, row["spot"])
    c = R.coin(V, code)
    I = R.identity(row["perp_ex"], row["perp"])
    lab = config.LABELS.get(V, V)
    if (V, code) in I["markets"]:
        m = I["markets"][(V, code)]
        src = I["mkt_src"][(V, code)]
        exp = (row.get("perp_factor") or 1.0) / (row.get("spot_factor") or 1.0)
        if abs(m - exp) > 1e-9 * max(1.0, exp):
            return _u("unit", f"в индексе ×{m:g}, у нас ×{exp:g}")
        if src == "hl_oracle":
            return _v("same", "hl_oracle_market", f"оракул Hyperliquid берёт {lab} {code}", _rel("C", "oracle"))
        if "venue_code" in src and row["perp_ex"] != "binance":
            return _v("same", "venue_code", f"состава индекса у перпа нет; спот {lab} той же биржи с тем же кодом {code}",
                      _rel("B", "code"))
        if "venue_code" in src:
            return _v("same", "venue_code", f"индекс Binance — только цена перпа; актив Binance с тем же кодом {code}",
                      _rel("B", "code"))
        return _v("same", "index_market", f"рынок {lab} {code} есть в индексе перпа", _rel("A", "idx"))
    hit = c["ids"] & I["tokens"]
    if hit:
        k = sorted(hit)[0]
        # сила — класс записи, в чьём СОБСТВЕННОМ списке контрактов общий контракт; из нескольких — сильнейший
        return _v("same", "contract", f"общий контракт {_short(k)} — {I['tok_src'].get(k, 'индекс перпа')}",
                  _best(*(I["tok_rel"].get(x) for x in sorted(hit))))
    for (v, cc) in sorted(I["leg_markets"]):
        if v != V or cc == code:
            continue
        oc = R.coin(V, cc)
        if c["ids"] & oc["ids"]:
            return _v("same", "contract", f"общий контракт с рынком индекса {lab} {cc}", _rel("A", "leg"))
        if c.get("name") and oc.get("name") and name_eq(c["name"], oc["name"]):
            return _u("same_name_other_ticker", f"{lab} {cc}")
        return _v("other", "index_other_market", f"индекс перпа берёт {lab} {cc}{_nm(oc)}, а это {code}{_nm(c)}")
    if (V, code) in I["rejected"]:
        return _u("index_inconsistent")
    names = I["names"]
    if c["ids"] and I["tokens"]:
        if c.get("name") and names:
            if _names_match(c["name"], names, strict=True):
                return _v("same", "name_bridged", f"контракты разные, имя то же: {c['name']} (мост или миграция)",
                          _rel("C", "bridged", c["name"]))
            if _names_match(c["name"], names):
                return _u("names_similar", f"{c['name']} / {sorted(names)[0]}")
            if _tickerish(c["name"], code) or _only_tickers(I):
                return _u("contract_disjoint_no_name", "имя — лишь тикер")
            pk = sorted(I["tokens"])[0]
            return _v("other", "contract_conflict",
                      f"{c['name']} {_short(sorted(c['ids'])[0])} против {sorted(names)[0]} {_short(pk)} "
                      f"({I['tok_src'].get(pk, 'индекс перпа')})")
        return _u("contract_disjoint_no_name")
    if c.get("name") and names and _names_match(c["name"], names, strict=True):
        # «первое правило»: имя сработало раньше сети; общая родная сеть доказана — B, а не C (ADA, ALGO на kucoin-перпе)
        return _v("same", "name", f"то же имя: {c['name']} (у одной стороны нет контракта)",
                  _nat_rel(c["nat"] & I["natives"], I) or _rel("C", "name", c["name"]))
    if c["nat"] & I["natives"]:
        return _v("same", "native_chain", "родная монета сети " + ", ".join(sorted(c["nat"] & I["natives"])),
                  _nat_rel(c["nat"] & I["natives"], I))
    if I["status"] != "ok":
        return _u(I["status"])
    if not (I["tokens"] or names or I["natives"]):
        return _u("other_cex_only")
    if not (c["ids"] or c.get("name") or c["nat"]):
        return _u("spot_blank")
    if I["pv"] in ORACLE_PERPS and not I["items"]:
        return _u("name_only", f"{sorted(names)[0]} / {c.get('name') or '—'}")
    return _u("absent")


def _decide_sf_tradfi(R: Resolver, row: dict, cls: str, V: str) -> dict:
    """Акции, сырьё, валюта: пара уже построена по признакам площадок (класс перпа; категория токена у Gate, рынок
    Stocks у KuCoin, r-токены Bitget, тег bStocks Binance; золото — только проверенные токены). Проверить остаётся
    множитель: у xStocks и Ondo в одном токене бывает несколько акций (NFLXX — 10)."""
    tag = row.get("spot_tag") or row["spot"]
    if cls == "equity":
        T = R.T.get(V)
        if T is not None and row["spot"] in T["need_n"] and row["spot"] not in T["shares"]:
            return _u("no_multiplier")
        n = (T or {}).get("shares", {}).get(row["spot"])
        extra = f", в токене {n['n']:g} акц. ({n['src']})" if n and abs(n["n"] - 1) > 1e-9 else ""
        return _v("same", "stock_token", f"токен акции {row['base']}: {tag}{extra}", _rel("B", "stock"))
    if cls == "commodity":
        return _v("same", "gold_token", f"токен золота {tag}: 1 токен = 1 тройская унция", _rel("B", "gold"))
    if cls == "fx":
        return _v("same", "fx", f"та же валюта {row['base']}", _rel("B", "fx"))
    return _u("preipo" if cls == "preipo" else "empty")


def decide_ff(R: Resolver, row: dict) -> dict:
    cls = row.get("cls") or "crypto"
    A = R.identity(row["va"], row["sa"])
    B = R.identity(row["vb"], row["sb"])
    for X, Y in ((A, B), (B, A)):
        if (Y["pv"], Y["sym"]) in X["refs"]:
            return _v("same", "index_ref", f"индекс {X['pv']} {X['sym']} ссылается на перп {Y['pv']} {Y['sym']}",
                      _rel("A", "ref"))
    sm = set(A["markets"]) & set(B["markets"])
    if sm:
        v, c = sorted(sm)[0]
        # рейтинг: по каждому общему рынку — слабейшая из сторон (нога индекса A, код той же биржи B, оракул HL C), из
        # рынков — сильнейший
        g = _best(*(_as(_worst(A["mkt_rel"].get(m), B["mkt_rel"].get(m)), "mkt") for m in sorted(sm)))
        if "venue_code" in (A["mkt_src"].get((v, c)), B["mkt_src"].get((v, c))):   # у одной ноги индекса нет (12.09)
            return _v("same", "shared_leg", f"{config.LABELS.get(v, v)} {c}: в источнике цены одной ноги, у другой — "
                                            f"спот её биржи с тем же кодом", g)
        return _v("same", "shared_leg", f"оба индекса берут {config.LABELS.get(v, v)} {c}", g)
    sc = A["cex"] & B["cex"]
    if sc:
        v, b = sorted(sc)[0]
        return _v("same", "shared_leg", f"оба индекса берут {v} {b}", _rel("A", "cex"))
    sv = (A["vendor"] & B["vendor"]) or {"/".join(p) for p in (A["dexpools"] & B["dexpools"])}
    if sv:
        # общий пул назван лишь парой тикеров (адреса в индексе нет) — B; общий поставщик цены — A
        return _v("same", "shared_leg", f"общий источник цены {sorted(sv)[0]}",
                  _rel("A", "vendor") if A["vendor"] & B["vendor"] else _rel("B", "pool2"))
    if cls != "crypto":
        if cls == "preipo":
            return _u("preipo")
        return _v("same", "class_ticker", f"тот же тикер в классе «{ {'equity': 'акция', 'commodity': 'сырьё', 'index': 'индекс', 'fx': 'валюта'}.get(cls, cls) }»",
                  _rel("B", "class"))
    hit = A["tokens"] & B["tokens"]
    if hit:
        return _v("same", "contract", f"общий контракт {_short(sorted(hit)[0])}",
                  _best(*(_as(_worst(A["tok_rel"].get(k), B["tok_rel"].get(k)), "contract") for k in sorted(hit))))
    for (v, c1) in sorted(A["leg_markets"]):
        for (w, c2) in sorted(B["leg_markets"]):
            if v == w and c1 != c2:
                r1, r2 = R.coin(v, c1), R.coin(v, c2)
                if r1.get("name") and r2.get("name") and name_eq(r1["name"], r2["name"]):
                    continue
                lab = config.LABELS.get(v, v)
                return _v("other", "index_other_market", f"индексы берут разные монеты: {lab} {c1}{_nm(r1)} и {lab} {c2}{_nm(r2)}")
    if A["tokens"] and B["tokens"]:
        if A["names"] and B["names"]:
            if any(_names_match(n, B["names"], strict=True) for n in A["names"]):
                return _v("same", "name_bridged", f"контракты разные, имя то же: {sorted(A['names'])[0]}",
                          _rel("C", "bridged", sorted(A["names"])[0]))
            if any(_names_match(n, B["names"]) for n in A["names"]):
                return _u("names_similar", f"{sorted(A['names'])[0]} / {sorted(B['names'])[0]}")
            if _only_tickers(A) or _only_tickers(B):
                return _u("contract_disjoint_no_name", "имя — лишь тикер")
            return _v("other", "contract_conflict", f"разные монеты: {sorted(A['names'])[0]} и {sorted(B['names'])[0]}")
        return _u("contract_disjoint_no_name")
    if A["names"] and B["names"] and any(_names_match(n, B["names"], strict=True) for n in A["names"]):
        return _v("same", "name", f"то же имя: {sorted(A['names'])[0]}",       # общая родная сеть — B, как в spot/futures
                  _nat_rel(A["natives"] & B["natives"], A, B) or _rel("C", "name", sorted(A["names"])[0]))
    if A["natives"] & B["natives"]:
        return _v("same", "native_chain", "родная монета сети " + ", ".join(sorted(A["natives"] & B["natives"])),
                  _nat_rel(A["natives"] & B["natives"], A, B))
    st = A["status"] if A["status"] != "ok" else B["status"]
    if st == "ok" and any(X["pv"] in ORACLE_PERPS and not X["items"] for X in (A, B)):
        st = "name_only"                                      # у ноги на оракуле только имя, и оно не сошлось
    return _u(st if st != "ok" else "other_cex_only")


def dex_tokens(R: Resolver, pv: str, sym: str) -> list[tuple[str, str, bool]]:
    """Токены перпа на сетях OKX DEX: [(chainIndex, адрес, мост)]. Только доказанные составом индекса и контрактами —
    никакого поиска по тикеру (у PEPE на Solana 12 подделок с миллионами ликвидности). Мост — родная монета другой сети
    (BTC → BTCB на BSC, XRP → Binance-Peg XRP); признаки по имени токена добавляет dexleg."""
    I = R.identity(pv, sym)
    nat = I.get("natives") or set()
    home = {ci: name for name, ci in DEX_CHAINS.items()}
    return [(ci, a, bool(nat) and home.get(ci) not in nat) for ci, a in sorted(I.get("dex") or ())]


def dex_token_rel(R: Resolver, pv: str, sym: str) -> dict:
    """Рейтинг своих DEX-токенов перпа: {(chainIndex, адрес): (буква, код, подробность)} — класс сильнейшей из ПРЯМЫХ
    записей индекса с этим контрактом: монета рынка из индекса — A, токен Alpha и код той же биржи — B, пул по поиску тикера
    и оракул HL — C (AIW3 — пул pancakeswap AIW3-USDT, найден поиском). ident_ev своей DEX-строки остаётся «contract»
    (dexleg, его читает трейдер); сигнатура и кортежи dex_tokens — прежние."""
    rel = R.identity(pv, sym).get("dex_rel") or {}
    return {(ci, a): rel[(ci, a)] for ci, a, _b in dex_tokens(R, pv, sym) if rel.get((ci, a))}


def _link_ff(R: Resolver, a: tuple, b: tuple, base: str | None) -> dict:
    """Пара перп/перп для связи DEX-токена: decide_ff, но «тот» по одному лишь имени-тикеру — не «тот» (ревью 12.09:
    KuCoin записывает имя монеты тикером — «UP», «NIGHT»; имя рынка Lighter или описание HIP-3 «price of UP (UP)» — тот же
    тикер; совпадение таких имён — совпадение тикеров, та самая ловушка UP/MEME/EDGE). Для строк перп/перп decide_ff прежний."""
    d = decide_ff(R, dict(va=a[0], sa=a[1], vb=b[0], sb=b[1], cls="crypto"))
    if d["ident"] != "same" or d["ident_ev"] not in ("name", "name_bridged"):
        return d
    A, B = R.identity(*a), R.identity(*b)
    hit = sorted({n for n in A["names"] for m in B["names"] if name_eq(n, m, strict=True)})
    codes = {base or ""} | {c for X in (A, B) for n in hit for c in (X.get("name_codes") or {}).get(n, ())}
    if hit and all(any(_tickerish(n, c) for c in codes if c) for n in hit):
        return _u("name_ticker", hit[0])
    return d


def dex_links(R: Resolver, pv: str, sym: str, src: dict, own: set, group=(), base: str | None = None,
              memo: dict | None = None) -> dict:
    """DEX-токены монеты для одного её перпа (владелец 12.09: «не вижу пары с монетой ANSEM» — токен ANSEM на Solana
    доказан индексами Aster и Gate, а у HIP-3 para:ANSEM и Lighter индекса с контрактами нет, и строк DEX не было вовсе).
    src: токен (сеть, адрес) → перпы той же монеты, чей индекс его доказал (dex_tokens); own — доказанные этим перпом;
    group — все крипто-перпы монеты на наших площадках (с токенами и без), base — её база во вселенной, memo — общий
    для группы кэш пар. Сам токен не ослабляется — только связь «токен ↔ перп», как у пары перп/перп (_link_ff):
      свой токен → None (same, как раньше);
      «не тот» хоть против одного источника → токена нет; «тот» → same (dex_link:…); иначе → unknown с причиной;
      «тот» лишь по имени-тикеру → unknown (dex_link:name_ticker);
      у перпа в той же сети свой доказанный токен, другой → unknown (мост, миграция или ошибка списка — не «тот»);
      «сверстники» — перпы группы, с которыми этот перп «тот» (и делящие с ним свой токен): источник токена «не тот» против
      сверстника — при unknown токена нет, при «тот» — unknown (противоречие). Ревью 12.09: сверстники — вся группа, а не
      только источники токенов: Binance MEME (Memecoin, контракт лишь в Ethereum) токенов DEX не даёт, но Lighter «Memecoin»
      с ним «тот», а Aster MEME (Alpha «A Meme Coin», Robinhood) против него «не тот» — токен Aster Lighter не получает.
    Возвращает {токен: вердикт | None}."""
    me = (pv, sym)
    memo = {} if memo is None else memo

    def ff(a, b):
        k = (a, b) if a <= b else (b, a)                       # вердикт пары симметричен — кэш общий для группы
        if k not in memo:
            memo[k] = _link_ff(R, k[0], k[1], base)
        return memo[k]
    lab = lambda p: f"{config.LABELS.get(p[0], p[0])} {p[1]}"
    peers = {q for q in set(group) | {p for s in src.values() for p in s} if q != me and ff(q, me)["ident"] == "same"}
    peers |= {p for t in own for p in src.get(t, ()) if p != me}
    peers = sorted(peers)
    out = {}
    for tok, srcs in sorted(src.items()):
        if tok in own:
            out[tok] = None
            continue
        res = [ff(p, me) for p in srcs]
        if any(d["ident"] == "other" for d in res):
            continue
        via = "токен доказан контрактом для " + ", ".join(lab(p) for p in srcs[:2]) + (" …" if len(srcs) > 2 else "")
        mine = sorted(a for c, a in own if c == tok[0])
        d = next((d for d in res if d["ident"] == "same"), None)
        clash = next(((p, q) for p in srcs for q in peers if p != q and ff(p, q)["ident"] == "other"), None)
        if clash and not d:
            continue                                            # «не проверено», а сверстник говорит «не тот» — нет
        if clash:
            out[tok] = _v("unknown", "dex_link:conflict", f"{via}; с этим перпом «тот» ({d['ident_why']}), но и с "
                                                          f"{lab(clash[1])} «тот», а он против {lab(clash[0])} «не тот»")
        elif mine:
            out[tok] = _v("unknown", "dex_link:own_token", f"{via}; у этого перпа в той же сети доказан другой: {_short(mine[0])}")
        elif d:
            # рейтинг: слабейшее из (связь перпов «тот», класс токена у источника); из источников «тот» — сильнейший.
            # При равенстве букв — код связи: подсказка страницы и так добавляет «токен доказан для другого перпа»
            g = _best(*(_worst((x["rel"], x["rel_ev"], x["rel_d"]), (R.identity(*p).get("dex_rel") or {}).get(tok))
                        for p, x in zip(srcs, res)
                        if x["ident"] == "same" and x.get("rel") and (R.identity(*p).get("dex_rel") or {}).get(tok)))
            out[tok] = _v("same", "dex_link:" + d["ident_ev"], f"{via}; с этим перпом: {d['ident_why']}", g)
        else:
            out[tok] = _v("unknown", "dex_link:" + res[0]["ident_ev"],
                          f"{via}; связь с этим перпом не проверена: {res[0]['ident_why']}")
    return out


def transfers(R: Resolver, row: dict) -> str | None:
    """Ввод/вывод монеты на спот-площадке закрыт во всех сетях — отдельный признак, не «не тот актив»: цена такой
    монеты уходит от перпа (ESPORTS −30 %, SIREN −40 % на 12.09), а переложить её нельзя."""
    V = row.get("spot_ex") or "binance_spot"
    if V not in R.T or (row.get("cls") or "crypto") != "crypto":
        return None
    c = R.coin(V, R.code_of(V, row["spot"]))
    if c.get("missing"):
        return None
    d, w = bool(c.get("dep_closed")), bool(c.get("wd_closed"))
    return "ввод и вывод закрыты" if d and w else "ввод закрыт" if d else "вывод закрыт" if w else None


def resolve(ff: list[dict], sf: list[dict], R: Resolver | None, fallback: str = "no_sources") -> dict:
    """Проставить вердикты в строки (ident, ident_ev, ident_why, mismatch = «не тот актив», у спот/перп — xfer).
    R = None — вердиктов нет (источники не загружены или проверка упала: fallback — причина). Возвращает сводку."""
    cnt = collections.Counter()
    for kind, items in (("ff", ff), ("sf", sf)):
        for it in items:
            xf = None
            if R is None or not R.ready():
                d = _u(fallback)
            else:
                try:
                    d = decide_ff(R, it) if kind == "ff" else decide_sf(R, it)
                    xf = transfers(R, it) if kind == "sf" else None
                except Exception as e:  # noqa — одна кривая запись источника не должна оставить таблицу без вердиктов
                    log.warning("вердикт %s: %s: %s", it.get("key"), type(e).__name__, e)
                    d = _u("empty", f"ошибка разбора: {type(e).__name__}")
            it.update(d)
            it["mismatch"] = d["ident"] == "other"
            if kind == "sf":
                it["xfer"] = xf
            cnt[f"{kind}:{d['ident']}"] += 1
    return dict(cnt)
