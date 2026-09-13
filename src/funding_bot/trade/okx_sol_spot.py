"""OKX DEX V6 на Solana (chainIndex 501) — путь okx_solana_v6 (ТЗ §5.1, SOLANA_ROUTERS §2).

Транспорт, подпись запроса (HMAC по path?query+body) и общий между процессами темп 1 запрос/с — существующий
okxdex.OkxDex (тот же файл runtime/okxdex.pace, что у коллектора и BSC-трейдера); здесь он не меняется. Ключ — прежние
имена .env (OKX_DEX_API_KEY / OKX_DEX_SECRET / OKX_DEX_PASSPHRASE); без ключа путь недоступен и запросов нет
(§5.2: без ключа OKX полного сравнения нет). Внутри процесса поверх темпа OkxDex — очередь по приоритету (RateGate).

Основной путь — GET /aggregator/swap-instruction: инструкции (данные base64) и адреса ALT; message собираем сами со
СВОИМ blockhash и его lastValidBlockHeight (G08). /aggregator/swap (готовая tx, tx.data base58, blockhash OKX без
высоты) — только сверка, никогда не исполняется. /aggregator/quote — превью без кошелька.

Параметры v6: chainIndex, amount (raw), fromTokenAddress/toTokenAddress (mint как есть, регистр значим),
userWalletAddress, slippagePercent — ПРОЦЕНТ строкой ("0.5" = 0.5 %), swapMode=exactIn. Никогда не шлём
swapReceiverAddress, closeAuthority, реферальные/комиссионные адреса и maxIn (§5.1: не maxIn = баланс).

Сверка инструкции роутера (данные инструкции из JSON — solders не нужен): swap_v3 / swap_v3_with_cpi_event по on-chain
IDL (sha256 ниже), префикс SwapArgs: amount_in = запрос, expect_amount_out = toTokenAmount, min_return — порог
программы (R09: tx.minReceiveAmount, если он есть, обязан совпасть). Счета: payer — наш кошелёк и подписант, mint —
из запроса, commission/platform_fee — заглушки (id программы), а не чужой счёт. commission_info/platform_fee_rate лежат
после маршрута (Borsh с вариантами enum Dex) — их проверяет валидатор по IDL (ждёт solders вместе с полным message).
Весь instructionLists — против манифеста v1 (spot_router.json_manifest): ровно одна инструкция роутера OKX, кроме неё
только ComputeBudget и создание ATA; Approve, чужие программы и своп другого агрегатора — отказ.
Контракт ответа swap-instruction для Solana подтвердить боевой фикстурой с ключом на VPS (M1): здесь — по документации.
"""
from __future__ import annotations
import hashlib, json, struct, time
from dataclasses import replace
from decimal import Decimal
from typing import Callable
from .. import config
from ..client import BannedError, PermanentHTTPError
from ..okxdex import OkxDex, NoLiquidity, QuotaExhausted, SWAP_PATH, Unsupported
from .fees import USD, FeeComponent, FeeError, dec_str, network_components
from .spot_router import (SYSTEM_PROGRAM, Ix, Payload, PayloadError, QuoteRequest, RateGate, RouteLimits, RoutingPolicy,
                          SchemaError, SolanaTools, SwapCandidate, Unavailable, body_hash, compute_budget, decode_bytes,
                          ix_from_json, is_pubkey, json_manifest, preparation_fees, signers_of, u64)

CHAIN_INDEX = "501"
ADAPTER_VERSION = "okx_v6_solana/20260913"
PATH = "okx_solana_v6"
QUOTE_PATH = "/api/v6/dex/aggregator/quote"
SWAP_IX_PATH = "/api/v6/dex/aggregator/swap-instruction"
OKX_ROUTER = "6m2CDdhRgxpH4WjvdzxAYbGxwdGUz5MziiL5jek2kBma"
NATIVE_PLACEHOLDER = SYSTEM_PROGRAM          # «SOL» в API OKX — не mint, в таблицу токенов не попадает
IDL_SHA256 = "e76b4b28adbe69c01482b4d303a3e878dfee7bf1440c2e2683d20cf7f81e511b"   # idl_okx_router.json (on-chain 13.09)
SWAP_ALLOWED = {bytes([240, 224, 38, 33, 176, 31, 241, 175]): "swap_v3",
                bytes([184, 104, 79, 156, 107, 182, 120, 138]): "swap_v3_with_cpi_event"}
SWAP_UNCONFIRMED = {bytes([14, 191, 44, 246, 142, 225, 224, 157]): "swap_tob_v3",       # разрешить по фикстуре
                    bytes([236, 71, 155, 68, 198, 98, 14, 118]): "swap_tob_v3_enhanced",
                    bytes([248, 198, 158, 145, 225, 117, 135, 200]): "swap"}
SWAP_FORBIDDEN = {bytes([63, 114, 246, 131, 51, 2, 247, 29]): "swap_tob_v3_with_receiver",
                  bytes([19, 44, 130, 148, 72, 56, 44, 238]): "proxy_swap",
                  bytes([62, 198, 214, 193, 213, 159, 108, 210]): "claim",
                  bytes([83, 1, 245, 125, 201, 207, 209, 249]): "claim_cashback_pumpfun",
                  bytes([253, 242, 176, 173, 217, 78, 100, 25]): "claim_cashback_pumpswap",
                  bytes([70, 211, 190, 165, 47, 40, 213, 95]): "wrap_unwrap_v3_with_receiver"}
# подготовка счетов/wSOL внутри роутера: для USDC↔токен не нужна — отказ, пока валидатор не разберёт владельцев
OKX_PREP = {bytes([147, 241, 123, 100, 244, 132, 174, 118]): "create_token_account",
            bytes([125, 191, 239, 140, 66, 8, 9, 228]): "create_token_account_with_seed",
            bytes([180, 178, 191, 54, 70, 8, 13, 224]): "wrap_unwrap_v3"}
SWAP_V3_ACC = {"payer": 0, "source": 1, "destination": 2, "source_mint": 3, "destination_mint": 4, "commission": 5,
               "platform_fee": 6, "source_program": 10, "destination_program": 11}
SWAP_V3_MIN_ACCOUNTS = 14


def slippage_percent(bps: int) -> str:
    """50 бп → "0.5": v6 ждёт ПРОЦЕНТ строкой, не долю (тот же формат, что OkxDex._pct)."""
    return OkxDex._pct(Decimal(bps) / 100)


def _truthy(x) -> bool:
    """isHoneyPot: bool или строка; непонятное — «да» (ошибаемся только в безопасную сторону)."""
    if isinstance(x, bool):
        return x
    if x is None:
        return False
    return str(x).strip().lower() not in ("", "false", "0", "no")


def _tokens(rr: dict, req: QuoteRequest, r: list[str]) -> tuple[str, str, int, int]:
    ft, tt = rr.get("fromToken"), rr.get("toToken")
    if not isinstance(ft, dict) or not isinstance(tt, dict):
        raise SchemaError("routerResult: нет fromToken/toToken")
    out = []
    for tok, a, side in ((ft, req.input, "from"), (tt, req.output, "to")):
        addr = tok.get("tokenContractAddress")
        if addr == NATIVE_PLACEHOLDER:
            r.append(f"native_placeholder:{side}")
        elif addr != a.mint:
            r.append(f"echo_mismatch:{side}Token")
        try:
            dec = int(str(tok.get("decimal")))
        except ValueError:
            dec = -1
            r.append(f"decimals_missing:{side}")
        if dec != a.decimals:
            r.append(f"decimals_mismatch:{side}")         # R01: метаданные провайдера — сверка с mint из RPC
        tax = tok.get("taxRate")
        if tax not in (None, "") and dec_str(tax, "taxRate") != 0:
            r.append(f"transfer_tax:{side}")              # налог токена в первом выпуске не поддержан
        if _truthy(tok.get("isHoneyPot")):
            r.append(f"honeypot:{side}")
        out.append((str(addr), dec))
    return out[0][0], out[1][0], out[0][1], out[1][1]


def _impact_bps(rr: dict) -> Decimal | None:
    v = rr.get("priceImpactPercent")                     # проценты (не доля, как у Jupiter)
    return None if v in (None, "") else dec_str(v, "priceImpactPercent") * 100


def _fingerprint(rr: dict) -> str:
    return hashlib.sha256(json.dumps([PATH, rr.get("router"), rr.get("dexRouterList")], sort_keys=True,
                                     default=str).encode()).hexdigest()[:32]


def _trade_fee(rr: dict, req: QuoteRequest, *, superseded: bool) -> FeeComponent | None:
    """tradeFee — оценка сети в USD. При своей оценке в лампортах — только сверка (superseded), не в сумму."""
    v = rr.get("tradeFee")
    if v in (None, ""):
        return None
    d = dec_str(v, "tradeFee")
    if d < 0:
        raise SchemaError("tradeFee отрицательный")
    k = max(0, -d.as_tuple().exponent)
    return FeeComponent(kind="network_estimate_usd", asset=USD, decimals=k, amount_raw=int(d.scaleb(k)),
                        payer=req.wallet, included_in_input_output=False, estimated=True, superseded=superseded,
                        source=PATH, note="tradeFee OKX (USD)" + (": сверка, есть своя оценка" if superseded else ""))


def router_ix(ixs: list[Ix], req: QuoteRequest, to_amt: int) -> tuple[int | None, list[str], list[str]]:
    """Ровно одна инструкция свопа роутера OKX: префикс аргументов и счета по IDL. → (min_return, причины, заметки)."""
    r, notes = [], []
    swaps = []
    for ix in (x for x in ixs if x.program_id == OKX_ROUTER):
        d = bytes(ix.data[:8])
        if d in SWAP_ALLOWED:
            swaps.append((SWAP_ALLOWED[d], ix))
        elif d in SWAP_UNCONFIRMED:
            r.append(f"okx_ix_unconfirmed:{SWAP_UNCONFIRMED[d]}")
            swaps.append((None, ix))
        elif d in SWAP_FORBIDDEN:
            r.append(f"okx_ix_forbidden:{SWAP_FORBIDDEN[d]}")
        elif d in OKX_PREP:
            r.append(f"okx_ix_prep:{OKX_PREP[d]}")
        else:
            r.append(f"okx_ix_unknown:{d.hex()}")
    if len(swaps) != 1:
        r.append(f"okx_router_ix_count:{len(swaps)}")
        return None, r, notes
    name, ix = swaps[0]
    if name is None:
        return None, r, notes
    if len(ix.data) < 32:
        r.append("okx_ix_short")
        return None, r, notes
    amount_in, expect, min_return = struct.unpack_from("<QQQ", ix.data, 8)
    if amount_in != req.amount_in_raw:
        r.append("ix_in_amount")
    if expect != to_amt:
        r.append("ix_expected_out")
    if len(ix.accounts) < SWAP_V3_MIN_ACCOUNTS:
        r.append("ix_accounts_short")
        return min_return, r, notes
    g = lambda k: ix.accounts[SWAP_V3_ACC[k]]      # noqa: E731
    if g("payer").pubkey != req.wallet or not g("payer").is_signer:
        r.append("ix_authority")
    if g("source_mint").pubkey != req.input.mint or g("destination_mint").pubkey != req.output.mint:
        r.append("ix_mint")
    for k in ("commission", "platform_fee"):
        if g(k).pubkey != OKX_ROUTER:
            r.append(f"okx_fee_account:{k}")
    for k, a in (("source_program", req.input), ("destination_program", req.output)):
        if g(k).pubkey not in (OKX_ROUTER, a.program):
            r.append("ix_token_program")
    if req.input_account is None:
        r.append("source_unverified")
    elif g("source").pubkey != req.input_account:
        r.append("ix_source")
    if req.output_account is None:
        r.append("recipient_unverified")
    elif g("destination").pubkey != req.output_account:
        r.append("ix_recipient")
    notes.append(f"инструкция {name}; commission_info/platform_fee_rate проверяет валидатор по IDL (ждёт solders)")
    return min_return, r, notes


def normalize_swap_instruction(data, req: QuoteRequest, *, received_at: float, received_mono: float,
                               latency_ms: int | None = None) -> SwapCandidate:
    if not isinstance(data, dict):
        raise SchemaError("data swap-instruction не объект (контракт: instructionLists, addressLookupTableAccount, "
                          "routerResult)")
    rr = data.get("routerResult")
    if not isinstance(rr, dict):
        raise SchemaError("нет routerResult")
    il = data.get("instructionLists")
    if not isinstance(il, list) or not il:
        raise SchemaError("нет instructionLists")
    ixs = [ix_from_json(x, f"instructionLists[{i}]") for i, x in enumerate(il)]
    alt_list = data.get("addressLookupTableAccount")
    if not isinstance(alt_list, list) or not all(is_pubkey(a) for a in alt_list):
        raise SchemaError("addressLookupTableAccount не список адресов")
    r: list[str] = []
    notes: list[str] = []
    from_amt = u64(rr.get("fromTokenAmount"), "fromTokenAmount")
    to_amt = u64(rr.get("toTokenAmount"), "toTokenAmount")
    if from_amt != req.amount_in_raw:
        r.append("echo_mismatch:fromTokenAmount")
    in_mint, out_mint, in_dec, out_dec = _tokens(rr, req, r)
    if rr.get("swapMode") not in (None, "exactIn"):
        r.append("swap_mode")
    onchain, rr_r, rr_n = router_ix(ixs, req, to_amt)
    r += rr_r
    notes += rr_n
    r += json_manifest(ixs, req, router_program=OKX_ROUTER)    # S10/S12: tip-инструкции у OKX нет — System запрещён
    tx = data.get("tx") if isinstance(data.get("tx"), dict) else {}
    mr = tx.get("minReceiveAmount")
    min_json = u64(mr, "tx.minReceiveAmount") if mr not in (None, "") else None
    cu_limit, cu_price, cbp = compute_budget(ixs)
    r += cbp
    prep, pr = preparation_fees(ixs, req, PATH)
    r += pr
    signers = signers_of(ixs, req.wallet)
    if len(signers) > 1:
        r.append("external_signer")
    base, prio = network_components(n_signatures=len(signers), cu_limit=cu_limit,
                                    cu_price_micro=cu_price if cu_price is not None else 0, payer=req.wallet, source=PATH)
    if cu_price and cu_limit is None:
        notes.append("лимит CU в инструкциях не задан — priority неизвестна")
    tf = _trade_fee(rr, req, superseded=True)
    ctl = data.get("createTokenAccountList")
    if isinstance(ctl, list) and ctl:
        notes.append(f"createTokenAccountList: {len(ctl)}")
    slot = rr.get("contextSlot")
    return SwapCandidate(
        provider="okx", path=PATH, adapter_version=ADAPTER_VERSION, request_hash=req.request_hash, side=req.side,
        input_mint=in_mint, output_mint=out_mint, input_program=req.input.program, output_program=req.output.program,
        input_decimals=in_dec, output_decimals=out_dec, amount_in_raw=from_amt, expected_out_raw=to_amt,
        min_out_raw=min_json, onchain_min_out_raw=onchain,
        fees=(base, prio, *prep) + ((tf,) if tf else ()),
        payload=Payload("instructions", "json", ixs=tuple(ixs), alts=tuple((a, ()) for a in alt_list)),
        required_signers=signers, cu_limit=cu_limit, cu_price_micro=cu_price, price_impact_bps=_impact_bps(rr),
        route_fingerprint=_fingerprint(rr), quote_id=str(rr["quoteId"]) if rr.get("quoteId") else None,
        received_at=received_at, received_mono=received_mono,
        source_slot=slot if isinstance(slot, int) and not isinstance(slot, bool) else None, latency_ms=latency_ms,
        reasons=tuple(dict.fromkeys(r)), notes=tuple(notes), response_hash=body_hash(data),
        audit=(("quoteId", str(rr.get("quoteId"))), ("router", str(rr.get("router"))[:80]),
               ("tradeFee", str(rr.get("tradeFee"))), ("contextSlot", str(slot))))


def normalize_swap(data, req: QuoteRequest, *, received_at: float, received_mono: float,
                   latency_ms: int | None = None) -> SwapCandidate:
    """/swap — только сверка (G08): blockhash выбрал OKX, его высоту не дали — чужую высоту не приписываем."""
    item = data[0] if isinstance(data, list) and len(data) == 1 and isinstance(data[0], dict) else None
    if item is None:
        raise SchemaError("data /swap: ожидался список из одного объекта")
    rr, tx = item.get("routerResult"), item.get("tx")
    if not isinstance(rr, dict) or not isinstance(tx, dict):
        raise SchemaError("/swap: нет routerResult/tx")
    r = ["okx_swap_reconcile_only"]
    notes = ["blockhash выбран OKX без lastValidBlockHeight — expiry только по точному хешу, исполнение запрещено"]
    from_amt = u64(rr.get("fromTokenAmount"), "fromTokenAmount")
    to_amt = u64(rr.get("toTokenAmount"), "toTokenAmount")
    if from_amt != req.amount_in_raw:
        r.append("echo_mismatch:fromTokenAmount")
    in_mint, out_mint, in_dec, out_dec = _tokens(rr, req, r)
    payload = None
    try:
        decode_bytes(tx.get("data"), "base58")
        payload = Payload("tx", "base58", data=tx["data"])
    except PayloadError as e:
        r.append("payload_encoding")
        notes.append(str(e)[:80])
    if tx.get("from") not in (None, req.wallet):
        r.append("echo_mismatch:from")
    sd = tx.get("signatureData")
    if sd not in (None, "", [], [""]):
        r.append("okx_signature_data")          # отдельная tip-транзакция: этот режим не поддержан
    mr = tx.get("minReceiveAmount")
    tf = _trade_fee(rr, req, superseded=False)
    return SwapCandidate(
        provider="okx", path=PATH, adapter_version=ADAPTER_VERSION, request_hash=req.request_hash, side=req.side,
        input_mint=in_mint, output_mint=out_mint, input_program=req.input.program, output_program=req.output.program,
        input_decimals=in_dec, output_decimals=out_dec, amount_in_raw=from_amt, expected_out_raw=to_amt,
        min_out_raw=u64(mr, "tx.minReceiveAmount") if mr not in (None, "") else None, onchain_min_out_raw=None,
        fees=(tf,) if tf else (), payload=payload, last_valid_block_height=None, price_impact_bps=_impact_bps(rr),
        route_fingerprint=_fingerprint(rr), quote_id=str(rr["quoteId"]) if rr.get("quoteId") else None,
        received_at=received_at, received_mono=received_mono, latency_ms=latency_ms,
        reasons=tuple(dict.fromkeys(r)), notes=tuple(notes), response_hash=body_hash(data),
        audit=(("slippagePercent", str(tx.get("slippagePercent"))), ("to", str(tx.get("to")))))


def normalize_quote(data, req: QuoteRequest, *, received_at: float, received_mono: float,
                    latency_ms: int | None = None) -> SwapCandidate:
    """/quote — превью без кошелька: ни инструкций, ни порога; в выбор не идёт."""
    item = data[0] if isinstance(data, list) and len(data) == 1 and isinstance(data[0], dict) else None
    if item is None:
        raise SchemaError("data /quote: ожидался список из одного объекта")
    r = ["capability:okx_quote_preview", "no_min_out"]
    from_amt = u64(item.get("fromTokenAmount"), "fromTokenAmount")
    to_amt = u64(item.get("toTokenAmount"), "toTokenAmount")
    if from_amt != req.amount_in_raw:
        r.append("echo_mismatch:fromTokenAmount")
    in_mint, out_mint, in_dec, out_dec = _tokens(item, req, r)
    tf = _trade_fee(item, req, superseded=False)
    return SwapCandidate(
        provider="okx", path=PATH, adapter_version=ADAPTER_VERSION, request_hash=req.request_hash, side=req.side,
        input_mint=in_mint, output_mint=out_mint, input_program=req.input.program, output_program=req.output.program,
        input_decimals=in_dec, output_decimals=out_dec, amount_in_raw=from_amt, expected_out_raw=to_amt,
        min_out_raw=None, onchain_min_out_raw=None, fees=(tf,) if tf else (), price_impact_bps=_impact_bps(item),
        route_fingerprint=_fingerprint(item), received_at=received_at, received_mono=received_mono,
        latency_ms=latency_ms, reasons=tuple(dict.fromkeys(r)), response_hash=body_hash(data))


def reconcile(built: SwapCandidate, swap: SwapCandidate) -> dict:
    """Сверка /swap-instruction и /swap одного запроса: разницу показываем, исполняется только build."""
    s = lambda a, b: None if a is None or b is None else b - a     # noqa: E731
    return dict(same_request=built.request_hash == swap.request_hash,
                expected_out_delta=s(built.expected_out_raw, swap.expected_out_raw),
                min_out_delta=s(built.effective_min_out, swap.min_out_raw),
                same_route=built.route_fingerprint == swap.route_fingerprint)


class OkxSolSpot:
    group = "okx"
    paths = (PATH,)

    def __init__(self, okx: OkxDex, *, policy: RoutingPolicy = RoutingPolicy(), limits: RouteLimits = RouteLimits(),
                 tools: SolanaTools = SolanaTools(), gate: RateGate | None = None,
                 clock: Callable[[], float] = time.monotonic, wall: Callable[[], float] = time.time,
                 http_timeout: float = float(config.TICK_HTTP_TIMEOUT)):
        self.okx = okx
        self.gate = gate or RateGate(0.0)       # интервал держит OkxDex (общий файл темпа); здесь — очередь и 1 в полёте
        self.policy, self.limits, self.tools = policy, limits, tools
        self.clock, self.wall, self.http_timeout = clock, wall, http_timeout

    def _params(self, req: QuoteRequest, *, wallet: bool) -> dict:
        p = {"chainIndex": CHAIN_INDEX, "amount": str(req.amount_in_raw), "fromTokenAddress": req.input.mint,
             "toTokenAddress": req.output.mint, "swapMode": "exactIn"}
        if wallet:
            p["userWalletAddress"] = req.wallet
            p["slippagePercent"] = slippage_percent(req.slippage_bps)
            if self.limits.max_price_impact_bps is not None:
                p["priceImpactProtectionPercent"] = OkxDex._pct(Decimal(self.limits.max_price_impact_bps) / 100)
        return p

    def _call(self, path: str, params: dict, req: QuoteRequest):
        if not self.okx.enabled():
            return None, {}, Unavailable("okx", PATH, "no_credentials", "без ключа OKX полного сравнения нет", self.clock())
        if self.clock() >= req.deadline_mono:
            return None, {}, Unavailable("okx", PATH, "deadline", "срок сбора прошёл до запроса", self.clock())
        with self.gate.slot(req.purpose, req.deadline_mono) as ok:
            if not ok:
                return None, {}, Unavailable("okx", PATH, "deadline", "очередь OKX не успевает к сроку", self.clock())
            t0 = self.clock()
            try:
                data = self.okx.request("GET", path, params=params,
                                        timeout=max(0.5, min(self.http_timeout, req.deadline_mono - t0)))
            except QuotaExhausted as e:
                return None, {}, Unavailable("okx", PATH, "quota", str(e)[:120], self.clock())
            except BannedError as e:
                return None, {}, Unavailable("okx", PATH, "rate_limited", str(e)[:120], self.clock())
            except PermanentHTTPError as e:
                return None, {}, Unavailable("okx", PATH, "auth", str(e)[:120], self.clock())
            except NoLiquidity as e:
                return None, {}, Unavailable("okx", PATH, "no_route", str(e)[:120], self.clock())
            except Unsupported as e:
                return None, {}, Unavailable("okx", PATH, "unsupported", str(e)[:120], self.clock())
            except Exception as e:      # noqa — сеть/ошибка ответа: путь недоступен в этом сборе
                return None, {}, Unavailable("okx", PATH, "error", type(e).__name__, self.clock())
            t1 = self.clock()
        if t1 > req.deadline_mono:
            return None, {}, Unavailable("okx", PATH, "deadline", "ответ пришёл после срока сбора", t1)
        return data, dict(received_at=self.wall(), received_mono=t1, latency_ms=int((t1 - t0) * 1000)), None

    def quote(self, req: QuoteRequest) -> SwapCandidate | Unavailable:
        data, meta, un = self._call(QUOTE_PATH, self._params(req, wallet=False), req)
        if un:
            return un
        try:
            return normalize_quote(data, req, **meta)
        except (SchemaError, PayloadError, FeeError) as e:
            return Unavailable("okx", PATH, "schema", str(e)[:120], self.clock())

    def build(self, req: QuoteRequest) -> SwapCandidate | Unavailable:
        data, meta, un = self._call(SWAP_IX_PATH, self._params(req, wallet=True), req)
        if un:
            return un
        try:
            return normalize_swap_instruction(data, req, **meta)
        except (SchemaError, PayloadError, FeeError) as e:
            return Unavailable("okx", PATH, "schema", str(e)[:120], self.clock())

    def swap_reconcile(self, req: QuoteRequest) -> SwapCandidate | Unavailable:
        """Готовая транзакция /swap для сверки с build (M1, G08). Отдельный запрос в общем бюджете 1 RPS."""
        data, meta, un = self._call(SWAP_PATH, self._params(req, wallet=True), req)
        if un:
            return un
        try:
            return normalize_swap(data, req, **meta)
        except (SchemaError, PayloadError, FeeError) as e:
            return Unavailable("okx", PATH, "schema", str(e)[:120], self.clock())

    def finalize(self, c: SwapCandidate, req: QuoteRequest) -> SwapCandidate:
        """Своя сборка со своим blockhash (лимит CU — из инструкций OKX) → проверка → симуляция."""
        if c.hard_reasons or c.payload is None or c.payload.kind != "instructions":
            return c
        t = self.tools
        if not t.can_build():
            return c.with_notes("сборка/симуляция не подключены (ждут solders) — кандидат только для показа")
        if self.clock() >= req.deadline_mono:
            return c.with_reasons("deadline")
        try:
            bh, lvbh = t.chain.latest_blockhash()
            tx = t.assembler.assemble(payer=req.wallet, ixs=c.payload.ixs, alts=c.payload.alts, recent_blockhash=bh,
                                      last_valid_block_height=lvbh, cu_limit=None)
            vr = t.validator.validate(tx, c, req) if t.validator is not None else None
            s = t.simulator.simulate(tx)
        except Exception as e:      # noqa
            return c.with_reasons(f"simulation_error:{type(e).__name__}")
        r = []
        if not s.ok:
            r.append(f"simulation_failed:{str(s.err or '?')[:60]}")
        elif c.cu_limit is not None and (s.units_consumed is None or s.units_consumed > c.cu_limit):
            r.append("cu_exceeded")
        if not tx.signers or any(x != req.wallet for x in tx.signers):
            r.append("external_signer")
        if tx.recent_blockhash != bh:
            r.append("blockhash_changed")
        r += [f"validator:{x}" for x in (vr or ())]
        base, prio = network_components(n_signatures=max(1, len(tx.signers)), cu_limit=c.cu_limit,
                                        cu_price_micro=c.cu_price_micro or 0, payer=req.wallet, source=PATH)
        fees = (base, prio) + tuple(f for f in c.fees if f.kind not in ("network_base", "network_priority"))
        return replace(c, payload=tx.payload, message_hash=tx.message_hash, required_signers=tx.signers,
                       recent_blockhash=bh, last_valid_block_height=lvbh, fees=fees, simulation_ok=s.ok,
                       sim_units=s.units_consumed, sim_slot=s.slot, built_mono=self.clock(),
                       validated=None if vr is None else not vr).with_reasons(*r)

    def candidates(self, req: QuoteRequest) -> list:
        if PATH not in self.policy.paths:
            return []
        x = self.build(req)
        return [self.finalize(x, req) if isinstance(x, SwapCandidate) else x]
