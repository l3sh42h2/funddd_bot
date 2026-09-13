"""Jupiter Swap V2 — два пути спота Solana (ТЗ §5.1, SOLANA_ROUTERS §2): Order (готовая транзакция, managed) и Build
(инструкции под свою транзакцию). Ключ API (x-api-key) — только из окружения JUPITER_API_KEY, никогда не печатается;
без ключа — keyless 0.5 запроса/с (снимок документации 13.09). Приватный ключ кошелька модулю не нужен.

Order V2 (GET /order): transaction base64, requestId, комиссия платформы УЖЕ внутри outAmount (feeBps), статьи сети и
rent с плательщиками. В первом live — только показ (order_execution_enabled=false): /execute не реализован; внешний
payer/RFQ без доказанного восстановления по requestId не исполняется (§9.2, G14). Пустая transaction — не исполнимое
предложение (живая фикстура 13.09: taker без средств → errorCode 1, transaction "").

Build V2 (GET /build): computeBudget (только цена CU), setup, swap, cleanup, other, tip, ALT, blockhash. Сборка своя:
симуляция с лимитом 1.4M → лимит = min(ceil(1.2·CU), 1.4M) → финальная сборка → проверка → повторная симуляция →
пересчёт сети от ЗАПРОШЕННОГО лимита (§5.1 п.4). Сборка, симуляция и полная проверка — за SolanaTools (ждут solders).

Сверка аргументов инструкции роутера (данные инструкции из JSON, не разбор транзакции — solders не нужен):
route_v2 / shared_accounts_route_v2 по on-chain IDL (sha256 ниже): in_amount = запрос, quoted_out = outAmount,
slippage = запрос, platform_fee_bps = 0, positive_slippage_bps = 0. Порог программы = ceil(quoted_out·(10⁴−slip)/10⁴):
правило сверено на двух живых ответах 13.09 (совпало с otherAmountThreshold; floor на единицу меньше). Счета по
позициям IDL: authority — наш кошелёк и подписант, mint и программы токенов — из запроса, получатель — наш счёт.
Все списки инструкций (computeBudget, setup, swap, cleanup, other, tip) — против манифеста v1 (spot_router.json_manifest):
ровно одна инструкция Jupiter и это swapInstruction; Approve/SetAuthority/CloseAccount/Transfer токенов, чужие
программы — отказ. Полная проверка message (ALT, вложенные CPI, эффекты) — валидатор (ждёт solders).
"""
from __future__ import annotations
import hashlib, json, os, struct, time
from dataclasses import dataclass, replace
from decimal import Decimal
from typing import Callable, Mapping
import requests
from .. import config
from .fees import FeeComponent, FeeError, MAX_TX_COMPUTE_UNITS, ceil_decimal, dec_str, lamports, network_components
from .spot_router import (Ix, Payload, PayloadError, QuoteRequest, RateGate, RoutingPolicy, SchemaError, SolanaTools,
                          SwapCandidate, Unavailable, b58encode, body_hash, compute_budget, decode_bytes, ix_from_json,
                          is_pubkey, json_manifest, preparation_fees, signers_of, u64)

BASE = "https://api.jup.ag/swap/v2"
API_KEY_ENV = "JUPITER_API_KEY"
KEYLESS_RPS = 0.5                    # снимок документации 13.09: keyless 0.5 RPS, free 1 RPS (лимит на организацию)
ADAPTER_VERSION = "jupiter_swap_v2/20260913"
JUP_PROGRAM = "JUP6LkbZbjS1jKKwapdHNy74zcZ3tLUZoi5QNyVTaV4"
IDL_SHA256 = "a31edf6096bcd4bb0292b84726722791548b47e41b85d2ed3a110c437e326dca"   # idl_jupiter_v6.json (on-chain 13.09)
ROUTE_V2 = bytes([187, 100, 250, 204, 49, 196, 175, 20])
SHARED_ROUTE_V2 = bytes([209, 152, 83, 147, 124, 254, 216, 233])
# известные, но не разрешённые инструкции: ExactOut (у нас только ExactIn), token_ledger, V1 (другая раскладка)
OTHER_ROUTE_IX = {
    bytes([229, 23, 203, 151, 122, 227, 173, 42]): "route",
    bytes([193, 32, 155, 51, 65, 214, 156, 129]): "shared_accounts_route",
    bytes([150, 86, 71, 116, 167, 93, 14, 104]): "route_with_token_ledger",
    bytes([230, 121, 143, 80, 119, 159, 106, 170]): "shared_accounts_route_with_token_ledger",
    bytes([208, 51, 239, 151, 123, 43, 237, 92]): "exact_out_route",
    bytes([176, 209, 105, 168, 154, 125, 69, 62]): "shared_accounts_exact_out_route",
    bytes([157, 138, 184, 82, 21, 244, 243, 36]): "exact_out_route_v2",
    bytes([53, 96, 229, 202, 216, 187, 250, 24]): "shared_accounts_exact_out_route_v2",
}
# позиции счетов по IDL
ROUTE_V2_ACC = {"authority": 0, "source": 1, "destination": 2, "source_mint": 3, "destination_mint": 4,
                "source_program": 5, "destination_program": 6, "destination_opt": 7}
SHARED_ROUTE_V2_ACC = {"authority": 1, "source": 2, "destination": 5, "source_mint": 6, "destination_mint": 7,
                       "source_program": 8, "destination_program": 9}
CU_MARGIN = Decimal("1.2")           # документация Build: 1.2 × симулированных CU, потолок 1.4M
PATH_ORDER, PATH_BUILD = "jupiter_order_v2", "jupiter_build_v2"


@dataclass(frozen=True)
class RouteArgs:
    kind: str
    in_amount: int
    quoted_out: int
    slippage_bps: int
    platform_fee_bps: int
    positive_slippage_bps: int

    @property
    def onchain_min_out(self) -> int:
        return -(-self.quoted_out * (10000 - self.slippage_bps) // 10000)


def decode_route_args(data: bytes) -> RouteArgs:
    """Фиксированный префикс аргументов route_v2/shared_accounts_route_v2 (до route_plan). Остальное — отказ."""
    d = bytes(data[:8])
    if d == ROUTE_V2:
        kind, off = "route_v2", 8
    elif d == SHARED_ROUTE_V2:
        kind, off = "shared_accounts_route_v2", 9           # у shared первым аргументом id: u8
    elif d in OTHER_ROUTE_IX:
        raise SchemaError(f"инструкция {OTHER_ROUTE_IX[d]} не разрешена (только ExactIn V2)")
    else:
        raise SchemaError(f"неизвестный дискриминатор {d.hex()} (IDL обновился? — кандидат непригоден до пересмотра)")
    if len(data) < off + 22 + 4:
        raise SchemaError("данные инструкции короче префикса аргументов")
    return RouteArgs(kind, *struct.unpack_from("<QQHHH", data, off))


def route_account_reasons(kind: str, ix: Ix, req: QuoteRequest) -> list[str]:
    lay = ROUTE_V2_ACC if kind == "route_v2" else SHARED_ROUTE_V2_ACC
    if len(ix.accounts) <= max(lay.values()):
        return ["ix_accounts_short"]
    g = lambda k: ix.accounts[lay[k]]            # noqa: E731
    r = []
    if g("authority").pubkey != req.wallet or not g("authority").is_signer:
        r.append("ix_authority")
    if g("source_mint").pubkey != req.input.mint or g("destination_mint").pubkey != req.output.mint:
        r.append("ix_mint")
    if g("source_program").pubkey != req.input.program or g("destination_program").pubkey != req.output.program:
        r.append("ix_token_program")
    if req.input_account is None:
        r.append("source_unverified")
    elif g("source").pubkey != req.input_account:
        r.append("ix_source")
    if req.output_account is None:
        r.append("recipient_unverified")
    elif g("destination").pubkey != req.output_account:
        r.append("ix_recipient")
    if "destination_opt" in lay:        # V2: необязательный получатель; заглушка — id программы
        opt = g("destination_opt").pubkey
        if opt != JUP_PROGRAM and (req.output_account is None or opt != req.output_account):
            r.append("ix_recipient")
    return r


# --- нормализация ответов --------------------------------------------------------------------------------------
def _echo(body: dict, req: QuoteRequest, r: list[str]) -> tuple[int, int, int]:
    in_amt = u64(body.get("inAmount"), "inAmount")
    out = u64(body.get("outAmount"), "outAmount")
    thr = u64(body.get("otherAmountThreshold"), "otherAmountThreshold")
    if body.get("inputMint") != req.input.mint:
        r.append("echo_mismatch:inputMint")
    if body.get("outputMint") != req.output.mint:
        r.append("echo_mismatch:outputMint")
    if in_amt != req.amount_in_raw:
        r.append("echo_mismatch:inAmount")
    if body.get("swapMode") != "ExactIn":
        r.append("swap_mode")
    slip = body.get("slippageBps")
    if isinstance(slip, bool) or not isinstance(slip, int):
        raise SchemaError("slippageBps не целое")
    if slip != req.slippage_bps:
        r.append("echo_mismatch:slippageBps")
    code, err = body.get("errorCode"), body.get("error") or body.get("errorMessage")
    if code not in (None, 0) or err:
        r.append(f"provider_error:{code}")
    return in_amt, out, thr


def _impact_bps(body: dict) -> Decimal | None:
    v = body.get("priceImpactPct")                       # доля (0.0003 = 3 бп), не проценты
    return None if v in (None, "") else dec_str(v, "priceImpactPct") * 10000


def _fingerprint(path: str, plan, extra: str = "") -> str:
    items = []
    for s in plan or []:
        si = (s or {}).get("swapInfo") or {}
        items.append([si.get("ammKey"), si.get("label"), si.get("inputMint"), si.get("outputMint"), (s or {}).get("bps")])
    return hashlib.sha256(json.dumps([path, extra, items], sort_keys=True, default=str).encode()).hexdigest()[:32]


def _platform_fee(body: dict, req: QuoteRequest, in_amt: int, out: int) -> FeeComponent | None:
    """feeBps Order уже внутри котировки: статья для объяснения (included), сумма — оценка."""
    bps = body.get("feeBps")
    if bps is None:
        return None
    if isinstance(bps, bool) or not isinstance(bps, int) or not 0 <= bps < 10000:
        raise SchemaError("feeBps не целое 0..9999")
    if bps == 0:
        return None
    mint = body.get("feeMint")
    if mint == req.input.mint:
        dec, amt = req.input.decimals, in_amt * bps // 10000
    elif mint == req.output.mint:
        dec, amt = req.output.decimals, out * 10000 // (10000 - bps) - out
    else:
        dec, amt = 0, None
    return FeeComponent(kind="platform", asset=mint if isinstance(mint, str) and mint else "unknown", decimals=dec,
                        amount_raw=amt, payer=req.wallet, included_in_input_output=True, estimated=True,
                        source=PATH_ORDER, note=f"feeBps={bps}: уже внутри котировки")


def _order_lamports(body: dict, req: QuoteRequest) -> tuple[list[FeeComponent], list[str], list[str]]:
    """Статьи сети/rent Order с плательщиками. Плательщик не taker — спонсор (не наш расход, но внешний подписант)."""
    fees, ext, notes = [], [], []
    for key, kind in (("signatureFee", "network_base"), ("prioritizationFee", "network_priority"),
                      ("rentFee", "rent_deposit")):
        amt, payer = body.get(key + "Lamports"), body.get(key + "Payer")
        amt = u64(amt, key + "Lamports") if amt is not None else None
        if payer is not None and not is_pubkey(payer):
            raise SchemaError(f"{key}Payer не адрес")
        fees.append(lamports(kind, amt, payer, estimated=True, source=PATH_ORDER, note="со слов провайдера"))
        if payer is not None and payer != req.wallet:
            notes.append(f"{key}: платит {payer[:6]}… (спонсор) — не наш расход")
    sp = body.get("signatureFeePayer")
    if sp is not None and sp != req.wallet:
        ext.append("external_signer:fee_payer")
    if body.get("router") == "jupiterz" or body.get("swapType") == "rfq":
        ext.append("external_signer:rfq")            # подпись маркет-мейкера добавляет /execute
    if body.get("gasless") is True:
        notes.append("gasless: сеть платит не taker — не значит «бесплатно», проверять плательщика каждой статьи")
    return fees, ext, notes


def _audit(body: dict, keys: tuple[str, ...]) -> tuple[tuple[str, str], ...]:
    return tuple((k, str(body.get(k))[:80]) for k in keys if k in body)


def normalize_order(body, req: QuoteRequest, *, policy: RoutingPolicy, received_at: float, received_mono: float,
                    latency_ms: int | None = None) -> SwapCandidate:
    if not isinstance(body, dict):
        raise SchemaError("ответ /order не объект")
    r: list[str] = []
    notes: list[str] = []
    in_amt, out, thr = _echo(body, req, r)
    if body.get("taker") != req.wallet:
        r.append("echo_mismatch:taker")
    if body.get("error") or body.get("errorMessage"):
        notes.append(f"Jupiter: {str(body.get('error') or body.get('errorMessage'))[:80]}")
    tx = body.get("transaction")
    if tx is None:
        tx = ""
    if not isinstance(tx, str):
        raise SchemaError("transaction не строка")
    payload = None
    if not tx:
        r.append("no_transaction")
    else:
        try:
            decode_bytes(tx, "base64")
            payload = Payload("tx", "base64", data=tx)
        except PayloadError as e:
            r.append("payload_encoding")
            notes.append(str(e)[:80])
    rid = body.get("requestId")
    if not isinstance(rid, str) or not rid:
        r.append("no_request_id")
        rid = None
    fees: list[FeeComponent] = []
    pf = _platform_fee(body, req, in_amt, out)
    if pf is not None:
        fees.append(pf)
        top, plat = body.get("feeBps"), (body.get("platformFee") or {}).get("feeBps")
        if isinstance(plat, int) and plat != top:
            notes.append(f"feeBps {top} ≠ platformFee.feeBps {plat}: разница — возмещение gasless, уже в котировке")
    if payload is not None:                     # сеть и rent имеют смысл только у исполнимой транзакции
        lam, ext, n2 = _order_lamports(body, req)
        fees += lam
        notes += n2
        if ext and not (policy.allow_external_signer_managed_routes and policy.external_signer_recovery):
            r += ext
    if not policy.order_execution_enabled:
        r.append("capability:order_preview_only")
    lvbh = body.get("lastValidBlockHeight")
    lvbh = u64(lvbh, "lastValidBlockHeight") if lvbh is not None else None
    if body.get("expireAt") is not None:        # формат не закреплён фикстурой — не угадываю; срок RFQ неизвестен
        r.append("rfq_expiry_unknown")
    notes.append("порог внутри транзакции Order проверяет валидатор (ждёт solders)")
    return SwapCandidate(
        provider="jupiter", path=PATH_ORDER, adapter_version=ADAPTER_VERSION, request_hash=req.request_hash,
        side=req.side, input_mint=str(body.get("inputMint")), output_mint=str(body.get("outputMint")),
        input_program=req.input.program, output_program=req.output.program, input_decimals=req.input.decimals,
        output_decimals=req.output.decimals, amount_in_raw=in_amt, expected_out_raw=out, min_out_raw=thr,
        onchain_min_out_raw=None, fees=tuple(fees), payload=payload, last_valid_block_height=lvbh,
        price_impact_bps=_impact_bps(body), route_fingerprint=_fingerprint(PATH_ORDER, body.get("routePlan"),
                                                                           str(body.get("router"))),
        request_id=rid, received_at=received_at, received_mono=received_mono, latency_ms=latency_ms,
        reasons=tuple(dict.fromkeys(r)), notes=tuple(notes), response_hash=body_hash(body),
        audit=_audit(body, ("router", "mode", "swapType", "feeBps", "feeMint", "gasless", "requestId", "expireAt",
                            "errorCode", "totalTime")))


def normalize_build(body, req: QuoteRequest, *, policy: RoutingPolicy, received_at: float, received_mono: float,
                    latency_ms: int | None = None) -> SwapCandidate:
    if not isinstance(body, dict):
        raise SchemaError("ответ /build не объект")
    r: list[str] = []
    notes: list[str] = []
    in_amt, out, thr = _echo(body, req, r)

    def lst(k: str) -> list[Ix]:
        v = body.get(k)
        if not isinstance(v, list):
            raise SchemaError(f"{k} не список")
        return [ix_from_json(x, f"{k}[{i}]") for i, x in enumerate(v)]

    cb, setup, other = lst("computeBudgetInstructions"), lst("setupInstructions"), lst("otherInstructions")
    swap = ix_from_json(body.get("swapInstruction"), "swapInstruction")
    cleanup = ix_from_json(body["cleanupInstruction"], "cleanupInstruction") if body.get("cleanupInstruction") else None
    tip = ix_from_json(body["tipInstruction"], "tipInstruction") if body.get("tipInstruction") else None
    ixs = cb + setup + [swap] + ([cleanup] if cleanup else []) + other + ([tip] if tip else [])
    onchain = None
    if swap.program_id != JUP_PROGRAM:
        r.append("jup_program")
    else:
        try:
            a = decode_route_args(swap.data)
        except SchemaError as e:
            r.append("jup_ix")
            notes.append(str(e)[:100])
        else:
            if a.in_amount != req.amount_in_raw:
                r.append("ix_in_amount")
            if a.quoted_out != out:
                r.append("ix_quoted_out")
            if a.slippage_bps != req.slippage_bps:
                r.append("ix_slippage")
            if a.platform_fee_bps:
                r.append("ix_platform_fee")           # интеграторскую комиссию не берём
            if a.positive_slippage_bps:
                r.append("ix_positive_slippage")      # удержание положительного проскальзывания
            onchain = a.onchain_min_out
            r += route_account_reasons(a.kind, swap, req)
            notes.append(f"инструкция {a.kind}")
    r += json_manifest(ixs, req, router_program=JUP_PROGRAM, router_ix=swap, tip_ix=tip)   # S10/S12 по всем спискам
    cu_limit, cu_price, cbp = compute_budget(ixs)
    r += cbp
    if cu_limit is not None:
        r.append("cu_conflict")                       # в Build лимит ставим мы — по симуляции
    prep, pr = preparation_fees(ixs, req, PATH_BUILD, tip_ix=tip)
    r += pr
    signers = signers_of(ixs, req.wallet)
    if len(signers) > 1:
        r.append("external_signer")
    base, prio = network_components(n_signatures=len(signers), cu_limit=MAX_TX_COMPUTE_UNITS,
                                    cu_price_micro=cu_price if cu_price is not None else 0, payer=req.wallet,
                                    source=PATH_BUILD, limit_is_upper_bound=True)
    alts_raw = body.get("addressesByLookupTableAddress") or {}
    if not isinstance(alts_raw, dict):
        raise SchemaError("addressesByLookupTableAddress не объект")
    alts = []
    for k, v in alts_raw.items():
        if not is_pubkey(k) or not isinstance(v, list) or not all(is_pubkey(x) for x in v):
            raise SchemaError("ALT не по контракту")
        alts.append((k, tuple(v)))
    bh = body.get("blockhashWithMetadata") or {}
    jup_bh = jup_lvbh = ptime = None
    try:
        raw = bytes(bh.get("blockhash") or b"")
        jup_bh = b58encode(raw) if len(raw) == 32 else None
        jup_lvbh = u64(bh.get("lastValidBlockHeight"), "lastValidBlockHeight")
        fa = bh.get("fetchedAt") or {}
        ptime = int(fa["secs_since_epoch"]) + int(fa.get("nanos_since_epoch", 0)) / 1e9
    except (TypeError, ValueError, KeyError, SchemaError):
        notes.append("blockhashWithMetadata не разобран")
    notes.append("подписываем своё сообщение со своим blockhash; пара Jupiter — только в аудит")
    audit = _audit(body, ("priceImpactPct",)) + (("jup_blockhash", str(jup_bh)), ("jup_lvbh", str(jup_lvbh)),
                                                  ("cu_price_micro", str(cu_price)))
    return SwapCandidate(
        provider="jupiter", path=PATH_BUILD, adapter_version=ADAPTER_VERSION, request_hash=req.request_hash,
        side=req.side, input_mint=str(body.get("inputMint")), output_mint=str(body.get("outputMint")),
        input_program=req.input.program, output_program=req.output.program, input_decimals=req.input.decimals,
        output_decimals=req.output.decimals, amount_in_raw=in_amt, expected_out_raw=out, min_out_raw=thr,
        onchain_min_out_raw=onchain, fees=(base, prio, *prep), payload=Payload("instructions", "json", ixs=tuple(ixs),
                                                                                alts=tuple(alts)),
        required_signers=signers, cu_price_micro=cu_price, price_impact_bps=_impact_bps(body),
        route_fingerprint=_fingerprint(PATH_BUILD, body.get("routePlan")), received_at=received_at,
        received_mono=received_mono, provider_time=ptime, latency_ms=latency_ms, reasons=tuple(dict.fromkeys(r)),
        notes=tuple(notes), response_hash=body_hash(body), audit=audit)


def _short(err) -> str:
    return str(err or "?").replace("\n", " ")[:60]


# --- адаптер ----------------------------------------------------------------------------------------------------
class JupiterSpot:
    group = "jupiter"
    paths = (PATH_ORDER, PATH_BUILD)

    def __init__(self, session: requests.Session | None = None, *, api_key: str | None = None, base: str = BASE,
                 rps: float | None = None, policy: RoutingPolicy = RoutingPolicy(), tools: SolanaTools = SolanaTools(),
                 gate: RateGate | None = None, clock: Callable[[], float] = time.monotonic,
                 wall: Callable[[], float] = time.time, http_timeout: float = float(config.TICK_HTTP_TIMEOUT),
                 environ: Mapping[str, str] | None = None):
        env = os.environ if environ is None else environ
        self._key = ((env.get(API_KEY_ENV) or "") if api_key is None else api_key).strip()
        self._s = session or requests.Session()
        self.base = base.rstrip("/")
        rate = KEYLESS_RPS if rps is None else rps       # тариф с ключом — по факту проекта; без значения — keyless
        if not rate > 0:
            raise ValueError("rps > 0")
        self.gate = gate or RateGate(1.0 / (rate * config.WEIGHT_SOFT_LIMIT))
        self.policy, self.tools, self.clock, self.wall, self.http_timeout = policy, tools, clock, wall, http_timeout

    def __repr__(self) -> str:
        return f"JupiterSpot(base={self.base!r}, ключ={'есть' if self._key else 'нет'})"

    def has_key(self) -> bool:
        return bool(self._key)

    def _clean(self, s: str) -> str:
        return s.replace(self._key, "<key>") if self._key else s

    def _get(self, path: str, params: dict, req: QuoteRequest, which: str):
        if self.clock() >= req.deadline_mono:
            return None, {}, Unavailable("jupiter", which, "deadline", "срок сбора прошёл до запроса", self.clock())
        headers = {"user-agent": config.USER_AGENT, "accept": "application/json"}
        if self._key:
            headers["x-api-key"] = self._key
        with self.gate.slot(req.purpose, req.deadline_mono) as ok:
            if not ok:
                return None, {}, Unavailable("jupiter", which, "deadline", "квота Jupiter не успевает к сроку", self.clock())
            t0 = self.clock()
            try:
                resp = self._s.get(self.base + path, params=params, headers=headers,
                                   timeout=max(0.2, min(self.http_timeout, req.deadline_mono - t0)))
            except Exception as e:      # noqa — сеть: путь недоступен в этом сборе
                return None, {}, Unavailable("jupiter", which, "http", type(e).__name__, self.clock())
            t1 = self.clock()
        meta = dict(received_at=self.wall(), received_mono=t1, latency_ms=int((t1 - t0) * 1000))
        st = resp.status_code
        try:
            body = resp.json()
        except ValueError:
            body = None
        if st == 429:
            return None, meta, Unavailable("jupiter", which, "rate_limited", "HTTP 429", t1)
        if st in (401, 403):
            return None, meta, Unavailable("jupiter", which, "auth", f"HTTP {st}", t1)
        if st >= 400 or not isinstance(body, dict):
            err = body.get("error") if isinstance(body, dict) else getattr(resp, "text", "")
            return None, meta, Unavailable("jupiter", which, f"http:{st}" if st >= 400 else "schema",
                                           self._clean(str(err))[:120], t1)
        if t1 > req.deadline_mono:
            return None, meta, Unavailable("jupiter", which, "deadline", "ответ пришёл после срока сбора", t1)
        return body, meta, None

    def _params(self, req: QuoteRequest) -> dict:
        # только эти поля: без platformFeeBps/feeAccount (интеграторской комиссии нет), без mode=fast,
        # без excludeRouters (не замена проверки внешнего подписанта)
        return {"inputMint": req.input.mint, "outputMint": req.output.mint, "amount": str(req.amount_in_raw),
                "taker": req.wallet, "slippageBps": str(req.slippage_bps)}

    def order(self, req: QuoteRequest) -> SwapCandidate | Unavailable:
        body, meta, un = self._get("/order", self._params(req), req, PATH_ORDER)
        if un:
            return un
        try:
            return normalize_order(body, req, policy=self.policy, **meta)
        except (SchemaError, PayloadError, FeeError) as e:
            return Unavailable("jupiter", PATH_ORDER, "schema", str(e)[:120], self.clock())

    def build(self, req: QuoteRequest) -> SwapCandidate | Unavailable:
        body, meta, un = self._get("/build", self._params(req), req, PATH_BUILD)
        if un:
            return un
        try:
            return normalize_build(body, req, policy=self.policy, **meta)
        except (SchemaError, PayloadError, FeeError) as e:
            return Unavailable("jupiter", PATH_BUILD, "schema", str(e)[:120], self.clock())

    def finalize_build(self, c: SwapCandidate, req: QuoteRequest) -> SwapCandidate:
        """Цикл CU (§5.1 п.4, SOLANA_ROUTERS §2): симуляция с потолком → лимит 1.2× → финал → проверка → симуляция."""
        if c.hard_reasons or c.payload is None:
            return c
        t = self.tools
        if not t.can_build():
            return c.with_notes("сборка/симуляция не подключены (ждут solders) — кандидат только для показа")
        if self.clock() >= req.deadline_mono:
            return c.with_reasons("deadline")
        try:
            bh, lvbh = t.chain.latest_blockhash()
            common = dict(payer=req.wallet, ixs=c.payload.ixs, alts=c.payload.alts, recent_blockhash=bh,
                          last_valid_block_height=lvbh)
            s1 = t.simulator.simulate(t.assembler.assemble(cu_limit=MAX_TX_COMPUTE_UNITS, **common))
            if not s1.ok:
                return replace(c, simulation_ok=False).with_reasons(f"simulation_failed:{_short(s1.err)}")
            if s1.units_consumed is None or s1.units_consumed <= 0:
                return replace(c, simulation_ok=False).with_reasons("simulation_no_units")
            limit = min(ceil_decimal(Decimal(s1.units_consumed) * CU_MARGIN), MAX_TX_COMPUTE_UNITS)
            if self.clock() >= req.deadline_mono:
                return c.with_reasons("deadline")
            final = t.assembler.assemble(cu_limit=limit, **common)
            vr = t.validator.validate(final, c, req) if t.validator is not None else None
            s2 = t.simulator.simulate(final)
        except Exception as e:      # noqa — сбой RPC/сборщика: путь не проверен
            return c.with_reasons(f"simulation_error:{type(e).__name__}")
        r = []
        if not s2.ok:
            r.append(f"simulation_failed:{_short(s2.err)}")
        elif s2.units_consumed is None or s2.units_consumed > limit:
            r.append("cu_exceeded_final")
        if not final.signers or any(s != req.wallet for s in final.signers):
            r.append("external_signer")
        if final.recent_blockhash != bh:
            r.append("blockhash_changed")
        r += [f"validator:{x}" for x in (vr or ())]
        base, prio = network_components(n_signatures=max(1, len(final.signers)), cu_limit=limit,
                                        cu_price_micro=c.cu_price_micro or 0, payer=req.wallet, source=PATH_BUILD)
        fees = (base, prio) + tuple(f for f in c.fees if f.kind not in ("network_base", "network_priority"))
        return replace(c, payload=final.payload, message_hash=final.message_hash, required_signers=final.signers,
                       recent_blockhash=bh, last_valid_block_height=lvbh, cu_limit=limit, fees=fees,
                       simulation_ok=s2.ok, sim_units=s2.units_consumed, sim_slot=s2.slot, built_mono=self.clock(),
                       validated=None if vr is None else not vr).with_reasons(*r)

    def finalize_order(self, c: SwapCandidate, req: QuoteRequest) -> SwapCandidate:
        """Только при включённом исполнении Order: готовая транзакция как есть (не модифицируем) → проверка → симуляция."""
        if c.hard_reasons or c.payload is None or not self.policy.order_execution_enabled:
            return c
        t = self.tools
        if t.assembler is None or t.simulator is None:
            return c.with_notes("разбор/симуляция готовой транзакции не подключены (ждут solders)")
        try:
            tx = t.assembler.wrap(c.payload, c.last_valid_block_height)
            vr = t.validator.validate(tx, c, req) if t.validator is not None else None
            s = t.simulator.simulate(tx)
        except Exception as e:      # noqa
            return c.with_reasons(f"simulation_error:{type(e).__name__}")
        r = [] if s.ok else [f"simulation_failed:{_short(s.err)}"]
        ext_ok = self.policy.allow_external_signer_managed_routes and self.policy.external_signer_recovery
        if any(x != req.wallet for x in tx.signers) and not ext_ok:
            r.append("external_signer")
        r += [f"validator:{x}" for x in (vr or ())]
        return replace(c, message_hash=tx.message_hash, required_signers=tx.signers,
                       recent_blockhash=tx.recent_blockhash, last_valid_block_height=tx.last_valid_block_height,
                       simulation_ok=s.ok, sim_units=s.units_consumed, sim_slot=s.slot, built_mono=self.clock(),
                       validated=None if vr is None else not vr).with_reasons(*r)

    def candidates(self, req: QuoteRequest) -> list:
        """Order и Build последовательно (одна квота организации); каждый — до своей финальной версии."""
        out = []
        if PATH_ORDER in self.policy.paths:
            x = self.order(req)
            out.append(self.finalize_order(x, req) if isinstance(x, SwapCandidate) else x)
        if PATH_BUILD in self.policy.paths:
            x = self.build(req)
            out.append(self.finalize_build(x, req) if isinstance(x, SwapCandidate) else x)
        return out
