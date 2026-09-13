"""Hyperliquid (HIP-3, para:ANSEM): чистые правила без сети — ТЗ §10, §13, appendices/HYPERLIQUID.md, read_hl.md.

Здесь только функции над уже полученными ответами: asset id, цена/размер, комиссия HIP-3, msgpack + хэш действия
и EIP-712 phantom agent (как sign_l1_action официального SDK 0.24.0), cloid, разбор ответов /exchange и orderStatus,
строки fills/funding и постраничный обход по времени. Сеть, ключи и БД — в hyperliquid_trade.py.

Деньги и количества — Decimal (строки ответа → Decimal(str)), float не принимается нигде: wire-строка цены — это
подписываемые байты, и repr float в ней = другая цена (ловушка float_to_wire SDK: f"{x:.8f}").

Asset id HIP-3 = 100000 + dex_index·10000 + local_index, где dex_index — позиция в СЫРОМ perpDexs (первый элемент
null = основной dex), local_index — позиция в СЫРОМ universe meta(dex) до любых фильтров (isDelisted тоже на месте).
Имя рынка — точное и регистрозависимое («para:ANSEM»; «ANSEM» без dex — другой рынок, kPEPE ≠ KPEPE).

Цена перпа: ≤ 5 значащих цифр и ≤ 6 − szDecimals знаков после запятой; целая цена допустима всегда. Округление —
в безопасную сторону: SELL (нижняя граница продажи) вверх, BUY (верхняя граница покупки) вниз — округление не
расширяет допуск. Размер — вниз к 10^−szDecimals: не продать больше, чем пришло со спота; 0 → заявки нет (H06).

Исходы заявки. HTTP 200 и status "ok" ещё не значат исполнения: итог — в statuses[0]. IOC «could not immediately
match» — это EXPIRED движка (0 исполнено, финал), прочие ошибки ордера — REJECTED с категорией; нет ответа / 5xx /
битый JSON / resting у IOC — UNKNOWN (не повторять, выяснять по cloid). Категории — по текстам документации Error
responses (подстроки); своих записанных отказов HL пока нет — фикстуры отказов синтетические, помечены.
"""
from __future__ import annotations
import hashlib, json, re
from dataclasses import dataclass
from decimal import ROUND_CEILING, ROUND_FLOOR, Decimal, InvalidOperation
from typing import Any, Callable, Iterable

D = Decimal
_D0 = Decimal(0)

MAX_SIG = 5                 # значащих цифр в цене
PERP_MAX_DEC = 6            # знаков после запятой у перпа: 6 − szDecimals
SPOT_MAX_DEC = 8
MIN_NOTIONAL_USD = Decimal(10)      # MinTradeNtl по документации Error responses
HIP3_BASE = 100_000
HIP3_DEX_STRIDE = 10_000
IOC_NO_MATCH = "could not immediately match"
_ADDR_RE = re.compile(r"^0x[0-9a-fA-F]{40}$")
_CLOID_RE = re.compile(r"^0x[0-9a-f]{32}$")


class HlRuleError(ValueError):
    """Нарушено правило площадки или формат ответа не тот, что закреплён фикстурой."""


class HlIdentityChanged(HlRuleError):
    """Соответствие fullcoin ↔ asset id (или szDecimals/коллатераль) изменилось со времени плана (H02)."""


# --- числа ----------------------------------------------------------------------------------------
def to_dec(x: Any, what: str = "число") -> Decimal:
    """Строка/int/Decimal ответа → Decimal. float и bool — отказ (float в деньги не пускаем)."""
    if isinstance(x, bool) or x is None or isinstance(x, float):
        raise HlRuleError(f"{what}: не число/запрещённый тип {type(x).__name__}: {x!r}")
    if isinstance(x, Decimal):
        d = x
    else:
        try:
            d = Decimal(str(x))
        except (InvalidOperation, ValueError):
            raise HlRuleError(f"{what}: не число: {x!r}") from None
    if not d.is_finite():
        raise HlRuleError(f"{what}: не конечное число: {x!r}")
    return d


def wire(d: Decimal) -> str:
    """Decimal → каноничная wire-строка HL: без экспоненты и хвостовых нулей ('600', '0.16608'), как float_to_wire
    SDK для точно представимых чисел. Только неотрицательный Decimal."""
    if isinstance(d, bool) or not isinstance(d, Decimal):
        raise TypeError(f"ожидался Decimal, получен {type(d).__name__}")
    if not d.is_finite() or d < 0:
        raise HlRuleError(f"wire: недопустимое число {d}")
    s = format(d.normalize(), "f")
    return "0" if s in ("-0", "") else s


def _max_dec(sz_decimals: int, spot: bool) -> int:
    if isinstance(sz_decimals, bool) or not isinstance(sz_decimals, int) or sz_decimals < 0:
        raise HlRuleError(f"szDecimals — целое ≥ 0, а не {sz_decimals!r}")
    m = (SPOT_MAX_DEC if spot else PERP_MAX_DEC) - sz_decimals
    if m < 0:
        raise HlRuleError(f"szDecimals {sz_decimals} больше предела знаков цены")
    return m


def valid_px(px: Decimal, sz_decimals: int, *, spot: bool = False) -> bool:
    """Цена допустима: > 0 и (целая ИЛИ ≤ 5 значащих и ≤ max_dec знаков после запятой)."""
    if isinstance(px, bool) or not isinstance(px, Decimal) or not px.is_finite() or px <= 0:
        return False
    md = _max_dec(sz_decimals, spot)
    if px == px.to_integral_value():
        return True
    t = px.normalize().as_tuple()
    decimals = -int(t.exponent)
    return len(t.digits) <= MAX_SIG and decimals <= md


def px_decimals_at(px: Decimal, sz_decimals: int, *, spot: bool = False) -> int:
    """Сколько знаков после запятой допустимо у цены этого порядка: min(max_dec, 4 − порядок)."""
    return min(_max_dec(sz_decimals, spot), max(0, (MAX_SIG - 1) - px.adjusted()))


def tick_at(px: Decimal, sz_decimals: int, *, spot: bool = False) -> Decimal:
    """Шаг цены в окрестности px — ПРОИЗВОДНАЯ правила 5 значащих (для Filters.tick старого движка), не константа."""
    return Decimal(1).scaleb(-px_decimals_at(px, sz_decimals, spot=spot))


def quantize_px(px: Decimal, sz_decimals: int, side: str, *, spot: bool = False) -> Decimal:
    """Ближайшая допустимая цена в безопасную сторону: SELL — вверх (не продать дешевле границы), BUY — вниз.
    Не представимая после округления (0) — отказ, а не «ближайшая похожая»."""
    if side not in ("BUY", "SELL"):
        raise HlRuleError(f"side BUY|SELL, а не {side!r}")
    if isinstance(px, bool) or not isinstance(px, Decimal) or not px.is_finite() or px <= 0:
        raise HlRuleError(f"цена — положительный Decimal, а не {px!r}")
    rnd = ROUND_CEILING if side == "SELL" else ROUND_FLOOR
    q = px
    for _ in range(3):              # вверх через степень 10 (9.99995 → 10.0000) — второй проход уже целый
        if valid_px(q, sz_decimals, spot=spot):
            return q
        d = px_decimals_at(q, sz_decimals, spot=spot)
        q = q.quantize(Decimal(1).scaleb(-d), rounding=rnd)
        if q <= 0:
            raise HlRuleError(f"цена {px} не представима правилами HL (szDecimals {sz_decimals})")
    if not valid_px(q, sz_decimals, spot=spot):     # pragma: no cover — страховка, правило не сошлось
        raise HlRuleError(f"цена {px}: квантизация не сошлась ({q})")
    return q


def sz_step(sz_decimals: int) -> Decimal:
    _max_dec(sz_decimals, True)
    return Decimal(1).scaleb(-sz_decimals)


def floor_sz(qty: Decimal, sz_decimals: int) -> Decimal:
    """Вниз к шагу 10^−szDecimals. Отрицательное — отказ; результат 0 = заявки нет (не поднимать до минимума)."""
    if isinstance(qty, bool) or not isinstance(qty, Decimal) or not qty.is_finite() or qty < 0:
        raise HlRuleError(f"количество — Decimal ≥ 0, а не {qty!r}")
    step = sz_step(sz_decimals)
    return (qty // step * step).quantize(step)


def valid_sz(qty: Decimal, sz_decimals: int) -> bool:
    if isinstance(qty, bool) or not isinstance(qty, Decimal) or not qty.is_finite() or qty <= 0:
        return False
    return qty == floor_sz(qty, sz_decimals)


# --- рынок: asset id и идентичность ------------------------------------------------------------------
@dataclass(frozen=True)
class AssetRef:
    """Точная привязка рынка на момент чтения. identity_hash — только то, что задаёт смысл asset id и единиц
    (dex/позиции/asset/szDecimals/коллатераль); rules_hash — вся строка universe (плечо, fee scale, режим маржи)."""
    fullcoin: str
    dex: str
    dex_index: int
    local_index: int
    asset: int
    sz_decimals: int
    max_leverage: int
    only_isolated: bool
    margin_mode: str | None
    margin_table_id: int | None
    collateral_token: int | None
    deployer_fee_scale: Decimal | None
    growth_mode: str | None
    is_delisted: bool
    identity_hash: str
    rules_hash: str


def split_fullcoin(fullcoin: str) -> tuple[str, str]:
    """'para:ANSEM' → ('para', 'para:ANSEM'); 'BTC' → ('', 'BTC'). Регистр не трогаем."""
    if not isinstance(fullcoin, str) or not fullcoin or fullcoin != fullcoin.strip():
        raise HlRuleError(f"имя рынка: {fullcoin!r}")
    if ":" in fullcoin:
        dex, coin = fullcoin.split(":", 1)
        if not dex or not coin or ":" in coin:
            raise HlRuleError(f"имя рынка HIP-3 — dex:COIN, а не {fullcoin!r}")
        return dex, fullcoin
    return "", fullcoin


def _hash(obj: Any) -> str:
    return hashlib.sha256(json.dumps(obj, sort_keys=True, separators=(",", ":"), default=str).encode()).hexdigest()


def resolve_asset(perp_dexs_raw: Any, meta_raw: Any, fullcoin: str) -> AssetRef:
    """Asset id по СЫРЫМ ответам perpDexs и meta(dex): ровно одно совпадение dex по имени и ровно одно — рынка по
    точному имени. Порядок нашего конфига и фильтр isDelisted на индекс не влияют (H01/H02)."""
    dex, name = split_fullcoin(fullcoin)
    if not isinstance(perp_dexs_raw, list) or not perp_dexs_raw or perp_dexs_raw[0] is not None:
        raise HlRuleError("perpDexs: первый элемент не null (основной dex) — формат ответа изменился")
    if dex:
        hits = [i for i, d in enumerate(perp_dexs_raw) if isinstance(d, dict) and d.get("name") == dex]
        if len(hits) != 1:
            raise HlRuleError(f"perpDexs: dex {dex!r} найден {len(hits)} раз(а)")
        di = hits[0]
    else:
        di = 0
    uni = meta_raw.get("universe") if isinstance(meta_raw, dict) else None
    if not isinstance(uni, list):
        raise HlRuleError(f"meta({dex or 'main'}): нет universe")
    hits = [j for j, a in enumerate(uni) if isinstance(a, dict) and a.get("name") == name]
    if len(hits) != 1:
        raise HlRuleError(f"meta({dex or 'main'}): рынок {name!r} найден {len(hits)} раз(а)")
    li = hits[0]
    if li >= HIP3_DEX_STRIDE:
        raise HlRuleError(f"{name}: локальный индекс {li} ≥ {HIP3_DEX_STRIDE}")
    asset = li if di == 0 else HIP3_BASE + di * HIP3_DEX_STRIDE + li
    a = uni[li]
    szd = a.get("szDecimals")
    if isinstance(szd, bool) or not isinstance(szd, int):
        raise HlRuleError(f"{name}: szDecimals {szd!r}")
    _max_dec(szd, False)
    lev = a.get("maxLeverage")
    if isinstance(lev, bool) or not isinstance(lev, int) or lev < 1:
        raise HlRuleError(f"{name}: maxLeverage {lev!r}")
    ct = meta_raw.get("collateralToken")
    dfs = a.get("deployerFeeScale")
    ident = {"fullcoin": name, "dex": dex, "dex_index": di, "local_index": li, "asset": asset, "szDecimals": szd,
             "collateralToken": ct}
    return AssetRef(fullcoin=name, dex=dex, dex_index=di, local_index=li, asset=asset, sz_decimals=szd,
                    max_leverage=lev, only_isolated=bool(a.get("onlyIsolated")), margin_mode=a.get("marginMode"),
                    margin_table_id=a.get("marginTableId"), collateral_token=ct,
                    deployer_fee_scale=None if dfs is None else to_dec(dfs, "deployerFeeScale"),
                    growth_mode=a.get("growthMode"), is_delisted=bool(a.get("isDelisted")),
                    identity_hash=_hash(ident), rules_hash=_hash({"entry": a, "collateralToken": ct, **ident}))


def check_same_identity(saved: AssetRef, fresh: AssetRef) -> None:
    """Сохранённая в плане привязка против свежей: расхождение — отказ, а не молчаливая перепривязка (H02)."""
    diff = [f"{k}: {getattr(saved, k)!r} → {getattr(fresh, k)!r}"
            for k in ("fullcoin", "dex", "dex_index", "local_index", "asset", "sz_decimals", "collateral_token")
            if getattr(saved, k) != getattr(fresh, k)]
    if diff or saved.identity_hash != fresh.identity_hash:
        raise HlIdentityChanged(f"{saved.fullcoin}: метаданные HIP-3 изменились ({'; '.join(diff) or 'хэш'}) — "
                                "заявка по старому asset id не отправляется, нужен новый план")


# --- комиссии -------------------------------------------------------------------------------------
def hip3_fee_scale(deployer_fee_scale: Decimal | None) -> Decimal:
    """Множитель HIP-3 по опубликованной формуле (Fees → fee formula for developers): dfs < 1 → 1 + dfs, иначе 2·dfs.
    Для para:ANSEM (dfs 0.5) = 1.5, не «универсальные ×2»."""
    if deployer_fee_scale is None:
        raise HlRuleError("deployerFeeScale не известен")
    dfs = to_dec(deployer_fee_scale, "deployerFeeScale")
    if dfs < 0:
        raise HlRuleError(f"deployerFeeScale < 0: {dfs}")
    return dfs + 1 if dfs < 1 else dfs * 2


def taker_rate_estimate(user_cross_rate: Decimal, ref: AssetRef) -> Decimal | None:
    """Оценка taker-ставки по рынку: userCrossRate × множитель HIP-3. Скидки из отдельных полей userFees
    (activeReferralDiscount/activeStakingDiscount) НЕ применяются — оценка не занижает. growthMode включён — ставка
    неизвестна (None): скидку growth mode не объявляем действующей. Факт — только fee из fills (H15)."""
    rate = to_dec(user_cross_rate, "userCrossRate")
    if not ref.dex:
        return rate
    if ref.growth_mode not in (None, "", "disabled"):
        return None
    return rate * hip3_fee_scale(ref.deployer_fee_scale)


# --- msgpack (подмножество) ----------------------------------------------------------------------
def packb(obj: Any) -> bytes:
    """msgpack как msgpack.packb 1.x по умолчанию (use_bin_type=True, минимальная кодировка): dict (ключи str,
    порядок вставки), list/tuple, str, int, bool, None, bytes. float не поддержан намеренно. Сверено с msgpack 1.2.1
    (tests/data/hl/golden_sdk_20260913.json)."""
    out = bytearray()
    _pack(obj, out)
    return bytes(out)


def _pack(o: Any, out: bytearray) -> None:
    if o is None:
        out.append(0xC0)
    elif o is True:
        out.append(0xC3)
    elif o is False:
        out.append(0xC2)
    elif isinstance(o, int):
        _pack_int(o, out)
    elif isinstance(o, str):
        b = o.encode("utf-8")
        n = len(b)
        if n <= 31:
            out.append(0xA0 | n)
        elif n <= 0xFF:
            out += bytes((0xD9, n))
        elif n <= 0xFFFF:
            out.append(0xDA); out += n.to_bytes(2, "big")
        elif n <= 0xFFFFFFFF:
            out.append(0xDB); out += n.to_bytes(4, "big")
        else:
            raise HlRuleError("msgpack: строка длиннее 2^32")
        out += b
    elif isinstance(o, (bytes, bytearray)):
        n = len(o)
        if n <= 0xFF:
            out += bytes((0xC4, n))
        elif n <= 0xFFFF:
            out.append(0xC5); out += n.to_bytes(2, "big")
        elif n <= 0xFFFFFFFF:
            out.append(0xC6); out += n.to_bytes(4, "big")
        else:
            raise HlRuleError("msgpack: bytes длиннее 2^32")
        out += bytes(o)
    elif isinstance(o, (list, tuple)):
        _hdr(len(o), 0x90, 0xDC, 0xDD, out)
        for x in o:
            _pack(x, out)
    elif isinstance(o, dict):
        _hdr(len(o), 0x80, 0xDE, 0xDF, out)
        for k, v in o.items():
            if not isinstance(k, str):
                raise HlRuleError(f"msgpack: ключ не str: {k!r}")
            _pack(k, out)
            _pack(v, out)
    else:
        raise HlRuleError(f"msgpack: тип {type(o).__name__} не поддержан (float запрещён)")


def _hdr(n: int, fix: int, c16: int, c32: int, out: bytearray) -> None:
    if n <= 15:
        out.append(fix | n)
    elif n <= 0xFFFF:
        out.append(c16); out += n.to_bytes(2, "big")
    elif n <= 0xFFFFFFFF:
        out.append(c32); out += n.to_bytes(4, "big")
    else:
        raise HlRuleError("msgpack: контейнер длиннее 2^32")


def _pack_int(v: int, out: bytearray) -> None:
    if v >= 0:
        if v <= 0x7F:
            out.append(v)
        elif v <= 0xFF:
            out += bytes((0xCC, v))
        elif v <= 0xFFFF:
            out.append(0xCD); out += v.to_bytes(2, "big")
        elif v <= 0xFFFFFFFF:
            out.append(0xCE); out += v.to_bytes(4, "big")
        elif v <= 0xFFFFFFFFFFFFFFFF:
            out.append(0xCF); out += v.to_bytes(8, "big")
        else:
            raise HlRuleError("msgpack: int > 2^64-1")
    else:
        if v >= -32:
            out.append(v & 0xFF)
        elif v >= -0x80:
            out.append(0xD0); out += v.to_bytes(1, "big", signed=True)
        elif v >= -0x8000:
            out.append(0xD1); out += v.to_bytes(2, "big", signed=True)
        elif v >= -0x80000000:
            out.append(0xD2); out += v.to_bytes(4, "big", signed=True)
        elif v >= -0x8000000000000000:
            out.append(0xD3); out += v.to_bytes(8, "big", signed=True)
        else:
            raise HlRuleError("msgpack: int < -2^63")


# --- подпись L1-действия (формула sign_l1_action SDK) -----------------------------------------------
def _keccak(data: bytes) -> bytes:
    from eth_utils import keccak     # здесь: dry-режим и чистые правила работают без extra `trade`
    return bytes(keccak(data))


def action_hash(action: dict, vault_address: str | None, nonce: int, expires_after: int | None) -> bytes:
    """keccak(msgpack(action) ‖ nonce u64 ‖ (0x00 | 0x01‖vault20) ‖ [0x00‖expiresAfter u64]) — байт в байт как
    hyperliquid.utils.signing.action_hash."""
    if isinstance(nonce, bool) or not isinstance(nonce, int) or not 0 < nonce < 2 ** 64:
        raise HlRuleError(f"nonce: {nonce!r}")
    data = packb(action) + nonce.to_bytes(8, "big")
    if vault_address is None:
        data += b"\x00"
    else:
        if not _ADDR_RE.match(str(vault_address)):
            raise HlRuleError(f"vaultAddress не адрес: {vault_address!r}")
        data += b"\x01" + bytes.fromhex(vault_address[2:])
    if expires_after is not None:
        if isinstance(expires_after, bool) or not isinstance(expires_after, int) or not 0 < expires_after < 2 ** 64:
            raise HlRuleError(f"expiresAfter: {expires_after!r}")
        data += b"\x00" + expires_after.to_bytes(8, "big")
    return _keccak(data)


def l1_typed_data(connection_id: bytes, mainnet: bool) -> dict:
    """EIP-712 phantom agent: домен Exchange/1/1337/0x0…0, Agent(source 'a'|'b', connectionId = хэш действия)."""
    if not isinstance(connection_id, (bytes, bytearray)) or len(connection_id) != 32:
        raise HlRuleError("connectionId — 32 байта")
    return {"domain": {"chainId": 1337, "name": "Exchange",
                       "verifyingContract": "0x0000000000000000000000000000000000000000", "version": "1"},
            "types": {"Agent": [{"name": "source", "type": "string"}, {"name": "connectionId", "type": "bytes32"}],
                      "EIP712Domain": [{"name": "name", "type": "string"}, {"name": "version", "type": "string"},
                                       {"name": "chainId", "type": "uint256"},
                                       {"name": "verifyingContract", "type": "address"}]},
            "primaryType": "Agent",
            "message": {"source": "a" if mainnet else "b", "connectionId": bytes(connection_id)}}


# --- действия -------------------------------------------------------------------------------------
def derive_cloid(network: str, account: str, client_id: str) -> str:
    """128-битный cloid дочернего действия из внутреннего client_id: 0x + sha256(network|account|client_id)[:32].
    Внутренний формат движка (fb-D7K2-e03-c1-a1) wire-cloid'ом не бывает (H13); новая попытка = новый client_id."""
    if not network or not _ADDR_RE.match(str(account or "")) or not client_id:
        raise HlRuleError("cloid: нужны сеть, адрес счёта и client_id")
    return "0x" + hashlib.sha256(f"{network}|{account.lower()}|{client_id}".encode()).hexdigest()[:32]


def is_cloid(s: Any) -> bool:
    return isinstance(s, str) and bool(_CLOID_RE.match(s))


def order_action(asset: int, is_buy: bool, px: Decimal, sz: Decimal, reduce_only: bool, cloid: str, *,
                 sz_decimals: int) -> dict:
    """Одна limit IOC заявка в порядке ключей SDK (порядок уходит в msgpack и значим), без builder."""
    if isinstance(asset, bool) or not isinstance(asset, int) or asset < 0:
        raise HlRuleError(f"asset: {asset!r}")
    if not isinstance(is_buy, bool) or not isinstance(reduce_only, bool):
        raise HlRuleError("is_buy/reduce_only — bool")
    if not valid_px(px, sz_decimals):
        raise HlRuleError(f"цена {px} не проходит правила HL (≤5 значащих, ≤{6 - sz_decimals} знаков)")
    if not valid_sz(sz, sz_decimals):
        raise HlRuleError(f"размер {sz} не кратен 10^-{sz_decimals} или ≤ 0")
    if not is_cloid(cloid):
        raise HlRuleError(f"cloid — 0x + 32 hex в нижнем регистре: {cloid!r}")
    return {"type": "order",
            "orders": [{"a": asset, "b": is_buy, "p": wire(px), "s": wire(sz), "r": reduce_only,
                        "t": {"limit": {"tif": "Ioc"}}, "c": cloid}],
            "grouping": "na"}


def update_leverage_action(asset: int, leverage: int) -> dict:
    """Плечо isolated (isCross всегда False: у para:ANSEM marginMode=noCross)."""
    if isinstance(leverage, bool) or not isinstance(leverage, int) or leverage < 1:
        raise HlRuleError(f"плечо — целое ≥ 1, а не {leverage!r}")
    return {"type": "updateLeverage", "asset": asset, "isCross": False, "leverage": leverage}


def noop_action() -> dict:
    """Подписанное действие, которое только сжигает nonce: проверка агента/vaultAddress без торговли."""
    return {"type": "noop"}


# --- разбор ответов /exchange --------------------------------------------------------------------
_ERR_KINDS = (          # (подстрока нижним регистром, категория) — тексты страницы Error responses
    (IOC_NO_MATCH, "ioc_no_match"),
    ("tick size", "tick_size"), ("invalid price", "tick_size"), ("price must be divisible", "tick_size"),
    ("minimum value of", "min_notional"),
    ("reduce only", "reduce_only"),
    ("open interest", "oi_cap"),
    ("oracle", "oracle_bounds"), ("reference price", "oracle_bounds"),
    ("insufficient margin", "margin"), ("margin tier", "margin"),
    ("no liquidity", "no_liquidity"),
    ("nonce", "nonce"),
    ("signature", "signature_or_agent"), ("does not exist", "signature_or_agent"), ("agent", "signature_or_agent"),
    ("too many", "rate_limit"), ("rate limit", "rate_limit"),
    ("vault", "vault"),
)
_STATUS_KINDS = {"tickRejected": "tick_size", "minTradeNtlRejected": "min_notional", "perpMarginRejected": "margin",
                 "perpMaxPositionRejected": "margin", "reduceOnlyRejected": "reduce_only",
                 "reduceOnlyCanceled": "reduce_only", "oracleRejected": "oracle_bounds",
                 "positionIncreaseAtOpenInterestCapRejected": "oi_cap",
                 "positionFlipAtOpenInterestCapRejected": "oi_cap",
                 "tooAggressiveAtOpenInterestCapRejected": "oi_cap", "openInterestIncreaseRejected": "oi_cap",
                 "openInterestCapCanceled": "oi_cap", "marketOrderNoLiquidityRejected": "no_liquidity",
                 "marginCanceled": "margin"}
ZERO_FILL_TERMINAL = frozenset({"canceled", "iocCancelRejected"})    # IOC без встречных — EXPIRED движка


def classify_error(text: Any) -> str:
    s = str(text or "").lower()
    for sub, kind in _ERR_KINDS:
        if sub in s:
            return kind
    return "other"


@dataclass(frozen=True)
class OrderOutcome:
    """status — термины движка (FILLED | PARTIALLY_FILLED | EXPIRED | REJECTED | UNKNOWN);
    outcome — термины ТЗ §10 (FILLED_TERMINAL | PARTIAL_TERMINAL | REJECTED_ZERO_FILL | UNKNOWN)."""
    status: str
    outcome: str
    filled: Decimal
    avg_px: Decimal
    oid: int | None
    err_kind: str | None = None
    err_text: str | None = None
    anomaly: bool = False


def _unknown(text: str, oid: int | None = None, anomaly: bool = False) -> OrderOutcome:
    return OrderOutcome("UNKNOWN", "UNKNOWN", _D0, _D0, oid, None, text[:300], anomaly)


def _oid(x: Any) -> int | None:
    return x if isinstance(x, int) and not isinstance(x, bool) and x >= 0 else None


def parse_order_response(http_status: int | None, body: Any, *, req_sz: Decimal, cloid: str) -> OrderOutcome:
    """Ответ /exchange на ОДНУ IOC заявку → исход. Доказанный отказ только по явному status "err" или ошибке
    ордера; всё непонятное — UNKNOWN (пусть лучше разбор по cloid, чем двойной шорт)."""
    if http_status is None:
        return _unknown("нет ответа")
    if not isinstance(body, dict) or "status" not in body:
        return _unknown(f"HTTP {http_status}: ответ не JSON-объект со status")
    if body.get("status") == "err":
        text = str(body.get("response"))
        return OrderOutcome("REJECTED", "REJECTED_ZERO_FILL", _D0, _D0, None, classify_error(text), text[:300])
    if http_status != 200 or body.get("status") != "ok":
        return _unknown(f"HTTP {http_status} status {body.get('status')!r}")
    resp = body.get("response")
    data = resp.get("data") if isinstance(resp, dict) else None
    sts = data.get("statuses") if isinstance(data, dict) else None
    if not isinstance(resp, dict) or resp.get("type") != "order" or not isinstance(sts, list) or len(sts) != 1:
        return _unknown(f"status ok, но statuses не из одной записи: {str(resp)[:200]}", anomaly=True)
    s = sts[0]
    if isinstance(s, dict) and "error" in s:
        text = str(s.get("error"))
        kind = classify_error(text)
        if kind == "ioc_no_match":
            return OrderOutcome("EXPIRED", "REJECTED_ZERO_FILL", _D0, _D0, None, kind, text[:300])
        return OrderOutcome("REJECTED", "REJECTED_ZERO_FILL", _D0, _D0, None, kind, text[:300])
    if isinstance(s, dict) and isinstance(s.get("filled"), dict):
        f = s["filled"]
        oid = _oid(f.get("oid"))
        if f.get("cloid") not in (None, cloid):
            return _unknown(f"filled с чужим cloid {f.get('cloid')!r}", oid, anomaly=True)
        try:
            tot, avg = to_dec(f.get("totalSz"), "totalSz"), to_dec(f.get("avgPx"), "avgPx")
        except HlRuleError as e:
            return _unknown(f"filled: {e}", oid, anomaly=True)
        if oid is None or tot <= 0 or avg <= 0 or tot > req_sz:
            return _unknown(f"filled вне ожиданий: totalSz {tot} при заявке {req_sz}, oid {oid}", oid, anomaly=True)
        if tot == req_sz:
            return OrderOutcome("FILLED", "FILLED_TERMINAL", tot, avg, oid)
        return OrderOutcome("PARTIALLY_FILLED", "PARTIAL_TERMINAL", tot, avg, oid)
    if isinstance(s, dict) and "resting" in s:
        r = s.get("resting") if isinstance(s.get("resting"), dict) else {}
        return _unknown("IOC оказалась resting — заявка стоит в книге", _oid(r.get("oid")), anomaly=True)
    return _unknown(f"неизвестный статус ордера: {str(s)[:200]}", anomaly=True)


def parse_action_response(http_status: int | None, body: Any) -> tuple[str, str | None]:
    """Ответ /exchange на не-ордерное действие (updateLeverage, noop) → ("ok"|"err"|"unknown", текст)."""
    if http_status is None:
        return "unknown", "нет ответа"
    if isinstance(body, dict) and body.get("status") == "err":
        return "err", str(body.get("response"))[:300]
    if http_status == 200 and isinstance(body, dict) and body.get("status") == "ok":
        return "ok", None
    return "unknown", f"HTTP {http_status}: {str(body)[:200]}"


# --- orderStatus ----------------------------------------------------------------------------------
@dataclass(frozen=True)
class OrderStatus:
    """orderStatus по cloid/oid. found=False — unknownOid (ОДНО наблюдение, не доказательство «не выставлена»)."""
    found: bool
    status: str | None = None
    oid: int | None = None
    cloid: str | None = None
    coin: str | None = None
    side: str | None = None
    orig_sz: Decimal | None = None
    remaining_sz: Decimal | None = None
    limit_px: Decimal | None = None
    reduce_only: bool | None = None
    tif: str | None = None
    status_ts: int | None = None

    @property
    def executed(self) -> Decimal | None:
        if self.orig_sz is None or self.remaining_sz is None:
            return None
        return self.orig_sz - self.remaining_sz

    @property
    def terminal(self) -> bool:
        s = self.status or ""
        return s in ("filled", "canceled", "rejected") or s.endswith("Rejected") or s.endswith("Canceled")


def parse_order_status(body: Any) -> OrderStatus:
    if isinstance(body, dict) and body.get("status") == "unknownOid":
        return OrderStatus(found=False)
    wrap = body.get("order") if isinstance(body, dict) and body.get("status") == "order" else None
    o = wrap.get("order") if isinstance(wrap, dict) else None
    if not isinstance(o, dict):
        raise HlRuleError(f"orderStatus: неожиданный ответ {str(body)[:200]}")
    return OrderStatus(found=True, status=str(wrap.get("status")), oid=_oid(o.get("oid")), cloid=o.get("cloid"),
                       coin=o.get("coin"), side=o.get("side"), orig_sz=to_dec(o.get("origSz"), "origSz"),
                       remaining_sz=to_dec(o.get("sz"), "sz"), limit_px=to_dec(o.get("limitPx"), "limitPx"),
                       reduce_only=o.get("reduceOnly"), tif=o.get("tif"), status_ts=wrap.get("statusTimestamp"))


def status_zero_fill(st: OrderStatus) -> tuple[str, str | None]:
    """Финальный статус с нулевым исполнением → (статус движка, категория)."""
    s = st.status or ""
    if s in ZERO_FILL_TERMINAL:
        return "EXPIRED", "ioc_no_match"
    return "REJECTED", _STATUS_KINDS.get(s, "other")


# --- учёт: fills и funding -------------------------------------------------------------------------
def _canon(raw: Any) -> str:
    return json.dumps(raw, sort_keys=True, separators=(",", ":"), default=str)


def fill_row(raw: dict, *, network: str, account: str) -> dict:
    """userFillsByTime → строка журнала. Ключ (network, account, coin, time, tid); oid/hash — для проверки коллизий.
    fee уже включает builderFee (повторно не вычитать); closedPnl — только для сверки, не вторая прибыль."""
    try:
        t, tid, oid = raw["time"], raw["tid"], raw["oid"]
        if any(isinstance(x, bool) or not isinstance(x, int) for x in (t, tid, oid)):
            raise HlRuleError("time/tid/oid не целые")
        row = {"network": network, "account": account.lower(), "coin": raw["coin"], "time": t, "tid": tid,
               "oid": oid, "cloid": raw.get("cloid"), "side": raw["side"], "px": to_dec(raw["px"], "px"),
               "sz": to_dec(raw["sz"], "sz"), "fee": to_dec(raw["fee"], "fee"), "fee_token": raw.get("feeToken"),
               "builder_fee": to_dec(raw["builderFee"], "builderFee") if raw.get("builderFee") is not None else None,
               "closed_pnl": to_dec(raw.get("closedPnl", "0"), "closedPnl"), "dir": raw.get("dir"),
               "start_position": to_dec(raw.get("startPosition", "0"), "startPosition"), "hash": raw.get("hash"),
               "crossed": raw.get("crossed"), "twap_id": raw.get("twapId"), "raw": _canon(raw)}
    except (KeyError, TypeError) as e:
        raise HlRuleError(f"fill: нет поля {e}") from None
    if row["side"] not in ("A", "B") or row["sz"] <= 0 or row["px"] <= 0:
        raise HlRuleError(f"fill: side/sz/px вне формата: {raw}")
    return row


def fill_key(r: dict) -> tuple:
    return (r["network"], r["account"], r["coin"], r["time"], r["tid"])


def funding_row(raw: dict, *, network: str, account: str) -> dict:
    """userFunding → строка. usdc со знаком как есть (+ получено шортом, − уплачено). Ключ (network, account,
    coin, time, hash): hash у funding обычно нулевой, реально уникальны (account, coin, time)."""
    try:
        d = raw["delta"]
        if d.get("type") != "funding":
            raise HlRuleError(f"userFunding: не funding: {d.get('type')!r}")
        t = raw["time"]
        if isinstance(t, bool) or not isinstance(t, int):
            raise HlRuleError("userFunding: time не целое")
        return {"network": network, "account": account.lower(), "coin": d["coin"], "time": t, "hash": raw.get("hash"),
                "usdc": to_dec(d["usdc"], "usdc"), "szi": to_dec(d["szi"], "szi"),
                "rate": to_dec(d["fundingRate"], "fundingRate"), "n_samples": d.get("nSamples"), "raw": _canon(raw)}
    except (KeyError, TypeError, AttributeError) as e:
        raise HlRuleError(f"userFunding: нет поля {e}") from None


def funding_key(r: dict) -> tuple:
    return (r["network"], r["account"], r["coin"], r["time"], r["hash"])


def fee_totals(rows: Iterable[dict]) -> dict[str, Decimal]:
    """Фактические комиссии fills по валюте (feeToken). builderFee уже внутри fee — отдельно не прибавляется
    и не вычитается; оценка по ставке сюда не подмешивается (H15)."""
    out: dict[str, Decimal] = {}
    for r in rows:
        tok = r.get("fee_token")
        if not tok:
            raise HlRuleError(f"fill без feeToken: {r.get('tid')}")
        out[tok] = out.get(tok, _D0) + r["fee"]
    return out


def hourly_funding_usd(rate: Decimal, notional_usd: Decimal) -> Decimal:
    """ПРОГНОЗ фандинга за час по опубликованной ставке часа: rate × нотионал. Не делить на 8, не ×0.6 — период HL
    уже час, множитель деплоера уже внутри ставки (H18). Полученным не является."""
    return to_dec(rate, "rate") * to_dec(notional_usd, "notional")


@dataclass
class TimePage:
    """Итог обхода по времени: rows (без дублей, по времени), complete — история окна полна; gap — почему нет."""
    rows: list
    complete: bool
    gap: str | None
    pages: int
    last_time: int | None


def paginate_by_time(fetch: Callable[[int], list], start_ms: int, *, page_limit: int, parse: Callable[[dict], dict],
                     key: Callable[[dict], tuple], max_pages: int, total_cap: int | None = None,
                     collide: Iterable[str] = ()) -> TimePage:
    """Постранично с перекрытием: следующая страница — с ПОСЛЕДНЕГО времени включительно (без +1: пачка на одном
    timestamp), дубли по ключу отсеиваются, тот же ключ с другим oid/hash — коллизия (отказ). Страница, целиком
    занятая одним временем, или предел total_cap (10 000 последних fills) — gap, не «полная история»."""
    seen: dict[tuple, dict] = {}
    cursor, pages, gap = int(start_ms), 0, None
    complete = False
    fields = tuple(collide)
    while pages < max_pages:
        raw = fetch(cursor)
        pages += 1
        if not isinstance(raw, list):
            raise HlRuleError("страница истории — не список")
        times = []
        for x in raw:
            r = parse(x)
            k = key(r)
            times.append(r["time"])
            old = seen.get(k)
            if old is None:
                seen[k] = r
            elif any(old.get(f) != r.get(f) for f in fields):
                raise HlRuleError(f"коллизия ключа {k}: {[(f, old.get(f), r.get(f)) for f in fields]}")
        if total_cap is not None and len(seen) >= total_cap:
            gap = f"достигнут предел {total_cap} записей API — ранние могли не отдаться"
            break
        if len(raw) < page_limit:
            complete = True
            break
        if min(times) == max(times):
            gap = f"страница из {len(raw)} записей на одном времени {times[0]} — дальше курсором по времени не пройти"
            break
        cursor = max(times)
    else:
        gap = f"больше {max_pages} страниц — сузь окно"
    rows = sorted(seen.values(), key=lambda r: (r["time"], str(key(r))))
    return TimePage(rows=rows, complete=complete, gap=gap, pages=pages, last_time=rows[-1]["time"] if rows else None)


# --- режим аккаунта и маржа ------------------------------------------------------------------------
@dataclass(frozen=True)
class AccountMode:
    """userAbstraction → режим. trade_supported — поддержан ли первой версией (только Standard, ТЗ §10); решение
    по остальным — позже, у владельца. note — откуда трактовка."""
    raw: Any
    mode: str
    trade_supported: bool
    note: str


_MODES = {
    "disabled": ("standard", True, "раздельные балансы spot / основной perp / каждый HIP-3 dex (userSetAbstraction "
                                   "'disabled' в SDK 0.24.0)"),
    "unifiedAccount": ("unified", False, "Unified Account: коллатераль общая по USDC, свободная маржа — в spot "
                                         "state; первой версией не поддержан"),
    "portfolioMargin": ("portfolio", False, "Portfolio Margin: объединяет активы; первой версией не поддержан"),
    "dexAbstraction": ("dex_abstraction", False, "DEX abstraction помечен в документации discontinued"),
    "default": ("default", False, "значение 'default' документацией не закреплено; наблюдение 13.09: у адреса с "
                                  "'default' маржа HIP-3 лежит в clearinghouseState(dex).withdrawable, как в "
                                  "Standard, — это наблюдение, не контракт: торговля до решения запрещена"),
}


def parse_abstraction(body: Any) -> AccountMode:
    if isinstance(body, str) and body in _MODES:
        m, ok, note = _MODES[body]
        return AccountMode(body, m, ok, note)
    return AccountMode(body, "unknown", False, f"неизвестное значение userAbstraction: {str(body)[:60]!r}")


@dataclass(frozen=True)
class MarginView:
    """Доступная маржа по режиму. available None — неизвестно (никогда не 0 вместо «не знаю»)."""
    mode: str
    available: Decimal | None
    source: str
    trade_supported: bool
    reason: str | None = None


def margin_view(mode: AccountMode, clearinghouse: dict | None, spot: dict | None,
                collateral_token: int | None = 0) -> MarginView:
    """Standard/default: withdrawable ledger'а dex (USDC основного perp или spot маржой para НЕ считается, H03).
    Unified/Portfolio: spot USDC — tokenToAvailableAfterMaintenance, иначе total − hold (hold = маржа позиций)."""
    if mode.mode in ("standard", "default"):
        w = (clearinghouse or {}).get("withdrawable") if isinstance(clearinghouse, dict) else None
        if w is None:
            return MarginView(mode.mode, None, "clearinghouseState(dex).withdrawable", mode.trade_supported,
                              "нет ответа clearinghouseState нужного dex")
        return MarginView(mode.mode, to_dec(w, "withdrawable"), "clearinghouseState(dex).withdrawable",
                          mode.trade_supported, None if mode.trade_supported else mode.note)
    if mode.mode in ("unified", "portfolio"):
        if not isinstance(spot, dict) or not isinstance(spot.get("balances"), list):
            return MarginView(mode.mode, None, "spotClearinghouseState", False, "нет ответа spot state")
        tok = 0 if collateral_token is None else collateral_token
        after = {t: v for t, v in (spot.get("tokenToAvailableAfterMaintenance") or []) if isinstance(t, int)}
        if tok in after:
            return MarginView(mode.mode, to_dec(after[tok], "availableAfterMaintenance"),
                              f"spot tokenToAvailableAfterMaintenance[{tok}]", False, mode.note)
        row = next((b for b in spot["balances"] if isinstance(b, dict) and b.get("token") == tok), None)
        if row is None:
            return MarginView(mode.mode, None, f"spot token {tok}", False, f"нет строки токена {tok} в spot state")
        return MarginView(mode.mode, to_dec(row["total"], "total") - to_dec(row["hold"], "hold"),
                          f"spot token {tok} total−hold", False, mode.note)
    return MarginView(mode.mode, None, "—", False, mode.note)
