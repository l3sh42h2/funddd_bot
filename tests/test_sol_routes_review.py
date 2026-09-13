"""Находки ревью потока routes 13.09: RT-01 (манифест v1 на уровне JSON, S10/S12), RT-02 (возраст стакана — часами
оценки, свежий стакан на каждый раунд select), RT-05 (получатель tip — только из allowlist, R14), RT-06 (маржа шорта
HL на кандидата, §5.1 п.5, SOLANA_ROUTERS сценарий 7) и мост PayloadValidator → TransactionValidator.
Живой ответ Jupiter /build 13.09 + по одной добавленной инструкции; OKX — синтетика по IDL. Числа маржи и лимитов —
синтетические, не значения владельца."""
import base64, copy, hashlib, json, pathlib, struct, time
from dataclasses import replace
from decimal import Decimal as D, localcontext
import pytest
from funding_bot.trade import fees as F, spot_router as sr, jupiter_spot as js, okx_sol_spot as ok
from funding_bot.trade import sol_route_validator as rv
from funding_bot.trade.solana import wire
from funding_bot.trade.solana.message import ResolvedMessage
from funding_bot.trade.types import Book

DATA = pathlib.Path(__file__).parent / "data" / "sol_routes"
FX = json.loads((DATA / "jupiter_v2_live_20260913.json").read_text())
TAKER = FX["taker"]
USDC = sr.AssetRef("EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v", sr.TOKEN_PROGRAM, 6, "USDC")
ANSEM = sr.AssetRef("9cRCn9rGT8V2imeM2BaKs13yhMEais3ruM3rPvTGpump", sr.TOKEN_2022_PROGRAM, 6, "ANSEM")
GENESIS = "5eykt4UsFv8P8NJdTREpY1vzqKqZKvdpKuc147dw2N9d"
USDC_ATA = "BhTkNtrZ1GGs5eQuXdexgXDAc7AvrN8rrJ4TkFoZzfhV"     # счета синтетического taker в живом ответе Jupiter
ANSEM_ATA = "ByZkEK8o2tvKLnDPdi8F5GLNnoKsDVBVTVvXDUo5Bae5"
_k = lambda s: sr.b58encode(hashlib.sha256(s.encode()).digest())      # noqa: E731
ATTACKER, UNKNOWN, TIP, BH, POOL = map(_k, ("rv-attacker", "rv-unknown-program", "rv-tip", "rv-blockhash", "rv-pool"))
OKX_SWAP_V3 = bytes([240, 224, 38, 33, 176, 31, 241, 175])     # swap_v3 по IDL (сверка с IDL — test_sol_routes_okx)
OKX = "okx_solana_v6"
LIM = sr.RouteLimits(max_quote_age_ms=5000, collection_deadline_ms=3000, min_blockhash_validity_heights=60,
                     max_network_fee_lamports_per_tx=200_000, max_spot_slippage_bps=100, max_price_impact_bps=150,
                     max_native_price_age_ms=10_000, max_book_age_ms=2000, max_rent_locked_lamports=3_000_000,
                     max_tip_lamports_per_tx=2_000_000)
POL = sr.RoutingPolicy(require_both_providers_for_entry=False)
WALL = 1_789_000_000.0


def body(name="build_buy_taker"):
    return copy.deepcopy(FX["calls"][name]["body"])


def mkreq(side="entry", **kw):
    kw.setdefault("deadline_mono", time.monotonic() + 30)
    entry = side == "entry"
    return sr.QuoteRequest(side=side, input=USDC if entry else ANSEM, output=ANSEM if entry else USDC,
                           amount_in_raw=100_000_000, wallet=TAKER, slippage_bps=50, genesis_hash=GENESIS,
                           input_account=USDC_ATA if entry else ANSEM_ATA, output_account=ANSEM_ATA if entry else USDC_ATA,
                           account_rent=(((ANSEM if entry else USDC).mint, 0),), **kw)


def ixj(prog, accs, data: bytes):
    return {"programId": prog, "accounts": [{"pubkey": p, "isSigner": s, "isWritable": w} for p, s, w in accs],
            "data": base64.b64encode(data).decode()}


def hedge(now_wall, *, bids=((D("0.16"), D(10 ** 7)),), asks=None, ts=None, fee=D("0.00045"), **kw):
    kw.setdefault("leverage", D(1))
    kw.setdefault("margin_reserve", D(0))
    kw.setdefault("available_margin", D(10 ** 12))
    return sr.HedgeContext(Book(bids=bids, asks=bids if asks is None else asks, ts=now_wall if ts is None else ts),
                           sr.PairParams(fs=D(1), fp=D(1), step=D(1), perp_fee_rate=fee, min_notional=D(10), **kw),
                           now_wall)


def prices_at(mono):
    return {F.NATIVE_SOL: F.PriceObs(F.NATIVE_SOL, USDC.mint, D(150), mono, "t")}


def nb(b, r):
    return js.normalize_build(b, r, policy=POL, received_at=time.time(), received_mono=time.monotonic())


def legacy_message(payer: str, blockhash: str) -> bytes:
    """Настоящий legacy message: подписант — плательщик, второй ключ — System, без инструкций (для wire)."""
    return (bytes([1, 0, 1, 2]) + sr.b58decode(payer) + sr.b58decode(sr.SYSTEM_PROGRAM) + sr.b58decode(blockhash)
            + bytes([0]))


class Asm:
    def assemble(self, *, payer, ixs, alts, recent_blockhash, last_valid_block_height, cu_limit):
        raw = legacy_message(payer, recent_blockhash)
        return sr.UnsignedTx(sr.Payload("message", "base64", data=base64.b64encode(raw).decode()),
                             wire.message_hash(raw), (payer,), recent_blockhash, last_valid_block_height, cu_limit)

    def wrap(self, payload, lvbh):
        raise AssertionError("готовую транзакцию здесь не исполняем")


class Sim:
    def simulate(self, tx):
        return sr.SimResult(True, None, 100_000, 1)


class Chain:
    def latest_blockhash(self):
        return BH, 424_800_000

    def block_height(self):
        return 424_799_000


class Val:
    """Валидатор-пустышка «всё прошло»: барьер JSON-манифеста не должен от него зависеть."""
    def validate(self, tx, cand, req):
        return ()


def finalize(c, r, validator=None):
    tools = sr.SolanaTools(Asm(), Sim(), Chain(), Val() if validator is None else validator)
    return js.JupiterSpot(session=object(), api_key="", tools=tools, rps=1000, policy=POL).finalize_build(c, r)


def ranked(c, r, *, policy=POL, limits=LIM, h=None):
    now, wall = time.monotonic(), time.time()
    return sr.rank([c], r, policy=policy, limits=limits, prices=prices_at(now), now_mono=now, now_wall=wall,
                   block_height=424_799_000, hedge=h or hedge(wall))[0][0]


def scand(req, out=990_000_000, path=OKX, **kw):
    """Синтетический проверенный кандидат (как в test_sol_routes_router)."""
    base = dict(provider=sr.GROUP_OF[path], path=path, adapter_version="test", request_hash=req.request_hash,
                side=req.side, input_mint=req.input.mint, output_mint=req.output.mint, input_program=req.input.program,
                output_program=req.output.program, input_decimals=req.input.decimals,
                output_decimals=req.output.decimals, amount_in_raw=req.amount_in_raw, expected_out_raw=out,
                min_out_raw=out * 9950 // 10000, onchain_min_out_raw=None, fees=(), message_hash="h",
                last_valid_block_height=2000, price_impact_bps=D(5), received_mono=0.0, received_at=WALL,
                simulation_ok=True, validated=True, response_hash=path)
    base.update(kw)
    return sr.SwapCandidate(**base)


def test_live_build_baseline_passes_with_fakes():
    """Контроль: без добавленных инструкций живой Build с подставными Solana-инструментами пригоден — отказы ниже
    вызваны именно добавленной инструкцией, а не обвязкой теста."""
    r = mkreq()
    c = nb(body(), r)
    assert c.reasons == ()
    x = ranked(finalize(c, r), r)
    assert x.eligible and x.cand.validated and x.cand.simulation_ok


# --- RT-01: манифест v1 на уровне JSON (S10, S12) ----------------------------------------------------------------
def _add(key, ix):
    def f(b):
        if key in ("cleanupInstruction", "tipInstruction"):
            b[key] = ix
        else:
            b[key].append(ix)
    return f


def _second_swap(b):
    sw = copy.deepcopy(b["swapInstruction"])
    raw = bytearray(base64.b64decode(sw["data"]))
    struct.pack_into("<Q", raw, 9, 1_000_000_000)                  # shared_accounts_route_v2: in_amount после id:u8
    sw["data"] = base64.b64encode(bytes(raw)).decode()
    b["otherInstructions"].append(sw)


def _swap_moved(b):
    b["otherInstructions"].append(b["swapInstruction"])            # своп Jupiter не там, где swapInstruction
    b["swapInstruction"] = ixj(sr.TOKEN_PROGRAM, [(USDC_ATA, False, True), (ANSEM_ATA, False, True), (TAKER, True, False)],
                               b"\x03" + struct.pack("<Q", 1))


_APPROVE = ixj(sr.TOKEN_PROGRAM, [(USDC_ATA, False, True), (ATTACKER, False, False), (TAKER, True, False)],
               b"\x04" + struct.pack("<Q", 2 ** 64 - 1))
_SET_AUTH = ixj(sr.TOKEN_2022_PROGRAM, [(ANSEM_ATA, False, True), (TAKER, True, False)],
                b"\x06\x02\x01" + sr.b58decode(ATTACKER))
_CLOSE = ixj(sr.TOKEN_PROGRAM, [(USDC_ATA, False, True), (ATTACKER, False, True), (TAKER, True, False)], b"\x09")
_TRANSFER = ixj(sr.TOKEN_PROGRAM, [(USDC_ATA, False, True), (ATTACKER, False, True), (TAKER, True, False)],
                b"\x03" + struct.pack("<Q", 50_000_000))
_UNKNOWN = ixj(UNKNOWN, [(TAKER, True, True)], b"\x01")
_OKX_IN_JUP = ixj(ok.OKX_ROUTER, [(TAKER, True, True)], OKX_SWAP_V3 + b"\x00" * 24)
_HEAP = ixj(sr.COMPUTE_BUDGET_PROGRAM, [], b"\x01" + struct.pack("<I", 256 * 1024))        # RequestHeapFrame
_RECOVER = ixj(sr.ATA_PROGRAM, [(TAKER, True, True)] + [(ATTACKER, False, True)] * 6, b"\x02")   # RecoverNested
_SOL_OUT = ixj(sr.SYSTEM_PROGRAM, [(TAKER, True, True), (ATTACKER, False, True)], struct.pack("<IQ", 2, 1_000))
_TIP_FOREIGN = ixj(sr.SYSTEM_PROGRAM, [(ATTACKER, True, True), (TIP, False, True)], struct.pack("<IQ", 2, 1_000_000))

BUILD_CASES = [
    ("s12_approve_setup", _add("setupInstructions", _APPROVE), "ix_not_in_manifest:Token:4"),
    ("s12_setauthority_token2022_other", _add("otherInstructions", _SET_AUTH), "ix_not_in_manifest:Token2022:6"),
    ("s12_closeaccount_cleanup", _add("cleanupInstruction", _CLOSE), "ix_not_in_manifest:Token:9"),
    ("s12_token_transfer_other", _add("otherInstructions", _TRANSFER), "ix_not_in_manifest:Token:3"),
    ("s10_second_jupiter_swap", _second_swap, "router_ix_count:2"),
    ("s10_unknown_program", _add("otherInstructions", _UNKNOWN), f"ix_not_in_manifest:{UNKNOWN}:01"),
    ("s10_okx_router_inside_jupiter", _add("otherInstructions", _OKX_IN_JUP),
     f"ix_not_in_manifest:{ok.OKX_ROUTER}:{OKX_SWAP_V3.hex()}"),
    ("s10_compute_budget_heap_frame", _add("computeBudgetInstructions", _HEAP), "ix_not_in_manifest:ComputeBudget:1"),
    ("s10_ata_recover_nested", _add("setupInstructions", _RECOVER), "ix_not_in_manifest:ATA:2"),
    ("s12_sol_transfer_outside_tip", _add("otherInstructions", _SOL_OUT), "ix_not_in_manifest:System:2"),
    ("s10_tip_from_foreign_wallet", _add("tipInstruction", _TIP_FOREIGN), "ix_not_in_manifest:System:2"),
    ("s10_swap_instruction_is_not_the_router_ix", _swap_moved, "router_ix_count:1"),
]


@pytest.mark.parametrize("mutate,reason", [c[1:] for c in BUILD_CASES], ids=[c[0] for c in BUILD_CASES])
def test_rt01_build_extra_instruction_rejected_at_json_level(mutate, reason):
    r = mkreq()
    b = body()
    mutate(b)
    c = nb(b, r)
    assert reason in c.reasons and not sr.live_only(reason) and c.hard_reasons
    f = finalize(c, r)                   # валидатор «()» барьер не снимает: до сборки и симуляции дело не доходит
    assert f.simulation_ok is None and f.message_hash is None and f.validated is None
    x = ranked(f, r)
    assert not x.eligible and not x.previewable


OKX_BASE_ACCS = ([(TAKER, True, True), (USDC_ATA, False, True), (ANSEM_ATA, False, True), (USDC.mint, False, False),
                  (ANSEM.mint, False, False), (ok.OKX_ROUTER, False, True), (ok.OKX_ROUTER, False, True)]
                 + [(POOL, False, True)] * 3
                 + [(sr.TOKEN_PROGRAM, False, False), (sr.TOKEN_2022_PROGRAM, False, False),
                    (sr.ATA_PROGRAM, False, False), (sr.SYSTEM_PROGRAM, False, False)])


def okx_swap(amount_in=100_000_000):
    return ixj(ok.OKX_ROUTER, OKX_BASE_ACCS,
               OKX_SWAP_V3 + struct.pack("<QQQ", amount_in, 601_000_000, 598_000_000) + b"\x00" * 12)


def okx_data(extra=()):
    ixs = [ixj(sr.COMPUTE_BUDGET_PROGRAM, [], b"\x02" + struct.pack("<I", 300_000)),
           ixj(sr.COMPUTE_BUDGET_PROGRAM, [], b"\x03" + struct.pack("<Q", 1000)),
           ixj(sr.ATA_PROGRAM, [(TAKER, True, True), (ANSEM_ATA, False, True), (TAKER, False, False),
                                (ANSEM.mint, False, False), (sr.SYSTEM_PROGRAM, False, False),
                                (sr.TOKEN_2022_PROGRAM, False, False)], b"\x01"),
           okx_swap()] + list(extra)
    tok = lambda a: {"tokenContractAddress": a.mint, "decimal": "6", "taxRate": "0", "isHoneyPot": False}  # noqa: E731
    return {"instructionLists": ixs, "addressLookupTableAccount": [],
            "routerResult": {"fromTokenAmount": "100000000", "toTokenAmount": "601000000", "swapMode": "exactIn",
                             "priceImpactPercent": "0.1", "tradeFee": "0.01", "fromToken": tok(USDC), "toToken": tok(ANSEM)}}


def okx_norm(data, r=None):
    return ok.normalize_swap_instruction(data, r or mkreq(), received_at=time.time(), received_mono=time.monotonic())


OKX_CASES = [
    ("s12_approve", _APPROVE, "ix_not_in_manifest:Token:4"),
    ("s12_setauthority_token2022", _SET_AUTH, "ix_not_in_manifest:Token2022:6"),
    ("s10_jupiter_swap_inside_okx", body()["swapInstruction"],
     f"ix_not_in_manifest:{js.JUP_PROGRAM}:{js.SHARED_ROUTE_V2.hex()}"),
    ("s10_unknown_program", ixj(UNKNOWN, [(TAKER, True, True)], b"\x09"), f"ix_not_in_manifest:{UNKNOWN}:09"),
    ("s12_sol_transfer_no_tip_in_okx", _SOL_OUT, "ix_not_in_manifest:System:2"),
    ("s10_second_okx_swap", okx_swap(1_000_000_000), "router_ix_count:2"),
    ("s10_compute_budget_heap_frame", _HEAP, "ix_not_in_manifest:ComputeBudget:1"),
]


def test_okx_baseline_passes():
    assert okx_norm(okx_data()).reasons == ()


@pytest.mark.parametrize("extra,reason", [c[1:] for c in OKX_CASES], ids=[c[0] for c in OKX_CASES])
def test_rt01_okx_extra_instruction_rejected_at_json_level(extra, reason):
    c = okx_norm(okx_data((extra,)))
    assert reason in c.reasons and c.hard_reasons
    spot = ok.OkxSolSpot(object(), tools=sr.SolanaTools(Asm(), Sim(), Chain(), Val()))
    f = spot.finalize(c, mkreq())
    assert f.simulation_ok is None and f.message_hash is None        # до сборки не дошло


def test_rt01_json_manifest_is_the_same_rule_for_both_providers():
    """Одна функция на все списки: чужой роутер для одного провайдера — неизвестная программа, а не «второй роутер»."""
    r = mkreq()
    j = [sr.ix_from_json(x, "t") for x in (_HEAP, _APPROVE)]
    assert sr.json_manifest(j, r, router_program=js.JUP_PROGRAM) == [
        "router_ix_count:0", "ix_not_in_manifest:ComputeBudget:1", "ix_not_in_manifest:Token:4"]
    tip = sr.ix_from_json(ixj(sr.SYSTEM_PROGRAM, [(TAKER, True, True), (TIP, False, True)],
                              struct.pack("<IQ", 2, 1)), "tip")
    sw = sr.ix_from_json(okx_swap(), "sw")
    assert sr.json_manifest([sw, tip], r, router_program=ok.OKX_ROUTER) == ["ix_not_in_manifest:System:2"]  # tip у OKX нет
    assert sr.json_manifest([sw, tip], r, router_program=ok.OKX_ROUTER, tip_ix=tip) == []


# --- мост PayloadValidator (роутер) → TransactionValidator (Solana-ядро) --------------------------------------------
def test_rt01_bridge_validator_protocols_and_refusal_until_solders():
    r = mkreq()
    c = nb(body(), r)
    v = rv.RouteTxValidator(limits=LIM)
    assert isinstance(v, sr.PayloadValidator)
    raw = legacy_message(TAKER, BH)
    tx = sr.UnsignedTx(sr.Payload("message", "base64", data=base64.b64encode(raw).decode()), wire.message_hash(raw),
                       (TAKER,), BH, 424_800_000, 120_000)
    assert v.validate(tx, c, r) == ("alt_resolver_waits_solders",)          # ALT без solders не раскрыть — отказ
    assert v.validate(replace(tx, message_hash="0" * 64), c, r) == ("message_hash_mismatch",)
    assert v.validate(replace(tx, payload=c.payload), c, r) == ("payload_not_assembled",)
    bare = replace(r, input_account=None, output_account=None)
    assert v.validate(tx, c, bare) == ("source_unverified", "recipient_unverified")

    class Resolver:
        def resolve(self, wtx):
            m = wtx.message
            return ResolvedMessage(version=m.version, payer=TAKER, signers=(TAKER,), writable=frozenset({TAKER}),
                                   readonly=frozenset(), keys=m.static_keys, alts=(), instructions=(),
                                   recent_blockhash=BH, message_hash=m.message_hash)
    why = rv.RouteTxValidator(Resolver(), limits=LIM).validate(tx, c, r)
    assert why and "solders" in why[0]                  # манифест и декодеры Solana-ядра ещё не реализованы
    f = finalize(c, r, validator=v)                     # мост подключается в SolanaTools как есть
    assert f.validated is False and "validator:alt_resolver_waits_solders" in f.reasons
    x = ranked(f, r)
    assert not x.eligible and not x.previewable


# --- RT-02: возраст стакана — часами оценки, свежий стакан на раунд --------------------------------------------------
def test_rt02_pair_check_measures_age_at_evaluation():
    r = mkreq()
    h = hedge(WALL, ts=WALL)                            # контекст собран в момент снимка — сам по себе «свежий»
    c = scand(r)
    assert "book_stale" in sr.pair_check(c, r, h, LIM, None, now_wall=WALL + 2.5).reasons
    assert "book_stale" not in sr.pair_check(c, r, h, LIM, None, now_wall=WALL + 1.9).reasons


class _Slow:
    """Провайдер, сбор которого занимает `dt` секунд по подставным часам роутера."""
    group, paths = "okx", (OKX,)

    def __init__(self, clk, dt):
        self.clk, self.dt, self.calls = clk, dt, 0

    def candidates(self, q):
        self.calls += 1
        self.clk["m"] += self.dt
        self.clk["w"] += self.dt
        return [scand(q, received_mono=self.clk["m"], received_at=self.clk["w"])]


def _router(clk, dt):
    p = _Slow(clk, dt)
    return p, sr.SpotRouter([p], policy=POL, limits=LIM, clock=lambda: clk["m"], wall=lambda: clk["w"])


def test_rt02_select_static_context_goes_stale_during_collection():
    clk = {"m": 1000.0, "w": WALL}
    p, router = _router(clk, 1.0)
    ok_dec = router.select(mkreq(deadline_mono=clk["m"] + 30), prices=prices_at(clk["m"] + 1), block_height=1000,
                           hedge=hedge(clk["w"]))
    assert ok_dec.winner is not None                    # контроль: быстрый сбор — стакан ещё свеж
    clk = {"m": 1000.0, "w": WALL}
    p, router = _router(clk, 2.5)                       # сбор 2.5 с при max_book_age_ms=2000
    dec = router.select(mkreq(deadline_mono=clk["m"] + 30), prices=prices_at(clk["m"] + 2.5), block_height=1000,
                        hedge=hedge(clk["w"]))
    x = dec.ranked[0]
    assert "book_stale" in x.pair.reasons and "book_stale" in x.cand.reasons
    assert dec.status == "refused" and dec.winner is None
    assert p.calls == 1 and "plan_expired" not in dec.reasons     # тот же стакан повтором сбора не освежить


def test_rt02_select_callback_takes_fresh_book_each_round():
    clk = {"m": 1000.0, "w": WALL}
    p, router = _router(clk, 2.5)
    taken = []

    def fetch():
        taken.append(clk["m"])
        ts = clk["w"] - 5 if len(taken) == 1 else clk["w"]        # первый снимок протух, второй свежий
        return hedge(clk["w"], ts=ts)
    dec = router.select(mkreq(deadline_mono=clk["m"] + 30), prices=prices_at(1005.0), block_height=1000, hedge=fetch)
    assert taken == [1002.5, 1005.0]                    # стакан берётся ПОСЛЕ сбора котировок каждого раунда
    assert p.calls == 2 and dec.round_no == 2 and dec.winner is not None and dec.status == "single_provider"


# --- RT-05: получатель tip — только из allowlist владельца (R14) ---------------------------------------------------
def _with_tip(to, lam=1_000_000, frm=TAKER):
    b = body()
    b["tipInstruction"] = ixj(sr.SYSTEM_PROGRAM, [(frm, True, True), (to, False, True)], struct.pack("<IQ", 2, lam))
    return b


def test_rt05_tip_to_unlisted_recipient_is_refused_even_under_cap():
    r = mkreq()
    c = finalize(nb(_with_tip(ATTACKER), r), r)
    assert [(f.amount_raw, f.recipient) for f in c.fees if f.kind == "tip"] == [(1_000_000, ATTACKER)]
    x = ranked(c, r)                                     # allowlist пуст (по умолчанию) — tip никому
    assert "tip_recipient_unknown" in x.cand.reasons and not x.eligible and not x.previewable
    listed = replace(POL, tip_recipients=(TIP,))
    x = ranked(c, r, policy=listed)                      # в списке другой адрес
    assert "tip_recipient_unknown" in x.cand.reasons and not x.eligible
    good = finalize(nb(_with_tip(TIP), r), r)
    y = ranked(good, r, policy=listed)                   # контроль: получатель из списка и сумма в лимите
    assert y.eligible and "tip_recipient_unknown" not in y.cand.reasons
    assert "tip_over_cap" in ranked(good, r, policy=listed, limits=replace(LIM, max_tip_lamports_per_tx=999_999)).cand.reasons


def test_rt05_unparsed_or_case_changed_recipient_is_unknown():
    r = mkreq()
    listed = replace(POL, tip_recipients=(TIP,))
    b = body()
    b["tipInstruction"] = ixj(sr.SYSTEM_PROGRAM, [(TAKER, True, True), (TIP, False, True)], b"\x02\x00\x00\x00")
    x = ranked(finalize(nb(b, r), r), r, policy=listed)          # tip не разобран: получатель неизвестен
    assert {"tip_unparsed", "tip_recipient_unknown"} <= set(x.cand.reasons)
    swapped = scand(r, fees=(F.lamports("tip", 10_000, TAKER, estimated=False, source="t", recipient=TIP.swapcase()),),
                    received_mono=time.monotonic())
    assert "tip_recipient_unknown" in ranked(swapped, r, policy=listed).cand.reasons   # регистр значим


def test_rt05_tip_recipients_config():
    assert sr.RoutingPolicy.from_config({"tip_recipients": [TIP]}).tip_recipients == (TIP,)
    assert sr.RoutingPolicy.from_config({}).tip_recipients == ()
    for bad in ({"tip_recipients": TIP}, {"tip_recipients": ["not-an-address-0OIl"]}):
        with pytest.raises(ValueError):
            sr.RoutingPolicy.from_config(bad)


# --- RT-06: маржа шорта HL на кандидата -----------------------------------------------------------------------------
BOOK_M = dict(bids=((D("1.00"), D(10 ** 7)),), asks=((D("1.01"), D(10 ** 7)),), fee=D("0.000675"))


def dq(x):
    with localcontext() as c:
        c.prec = 50
        return eval(x, {"D": D})       # ожидание — отдельной формулой, не функцией модуля


def test_rt06_margin_params_missing_block_live_but_keep_preview():
    r = mkreq()
    h = hedge(WALL, leverage=None, margin_reserve=None, available_margin=None)
    x = sr.rank([scand(r, received_mono=1000.0)], r, policy=POL, limits=LIM, prices=prices_at(1000.0), now_mono=1000.0,
                now_wall=WALL, block_height=1000, hedge=h)[0][0]
    assert {"limit_missing:hl_leverage", "limit_missing:hl_margin_reserve", "margin_unknown"} <= set(x.cand.reasons)
    assert not x.eligible and x.previewable and x.pair.margin_needed is None


def _rank_m(cands, r, **margin):
    return sr.rank(cands, r, policy=POL, limits=LIM, prices=prices_at(1000.0), now_mono=1000.0, now_wall=WALL,
                   block_height=1000, hedge=hedge(WALL, **BOOK_M, **margin))[0]


def test_rt06_margin_needed_formula_and_insufficient():
    r = mkreq()
    c = scand(r, out=990_000_000, received_mono=1000.0)          # 990 токенов → 990 контрактов, минимум 985
    need = dq("D(990) * D('1.01') / D(2) + D(990) * D('1.00') * D('0.000675')")     # опорная цена — лучший аск
    at_min = dq("D(985) * D('1.01') / D(2) + D(985) * D('1.00') * D('0.000675')")
    x = _rank_m([c], r, leverage=D(2), margin_reserve=D(50), available_margin=need + D(50))[0]
    assert (x.pair.margin_needed, x.pair.margin_at_min_out) == (need, at_min) and x.eligible
    y = _rank_m([c], r, leverage=D(2), margin_reserve=D(50), available_margin=need + D(50) - D("0.000001"))[0]
    assert "margin_insufficient" in y.cand.reasons and not y.eligible and y.previewable
    dec = sr.gate(_rank_m([c], r, leverage=D(2), margin_reserve=D(50), available_margin=need + D(50)), [], r, POL)
    assert dec.records("op", 1)[0]["pair_margin"] == str(need)


def test_rt06_bigger_short_needs_more_margin_than_available():
    """Сценарий 7: лучший по цене кандидат даёт больший шорт, маржи на него нет — выбирается тот, что помещается."""
    r = mkreq()
    small, big = scand(r, out=990_000_000, received_mono=1000.0), scand(r, out=1_000_000_000, path="jupiter_build_v2",
                                                                         received_mono=1000.0)
    rk = _rank_m([small, big], r, leverage=D(2), margin_reserve=D(0), available_margin=D(503))
    by = {x.cand.path: x for x in rk}
    assert by[OKX].pair.margin_needed == dq("D(990) * D('1.01') / D(2) + D(990) * D('0.000675')")
    assert "margin_insufficient" in by["jupiter_build_v2"].cand.reasons           # 505.675 > 503
    dec = sr.gate(rk, [], r, POL)
    assert dec.winner.path == OKX and dec.preview_winner.path == "jupiter_build_v2"


def test_rt06_exit_needs_no_margin_and_params_validated():
    e = mkreq("exit")
    x = _rank_m([scand(e, out=99_000_000, received_mono=1000.0)], e, leverage=None, margin_reserve=None,
                available_margin=None)[0]
    assert not any(k.startswith(("margin", "limit_missing:hl_")) for k in x.cand.reasons) and x.eligible
    base = hedge(WALL).params
    for bad in (dict(leverage=D(0)), dict(available_margin=D(-1)), dict(leverage=2.0), dict(margin_reserve="5")):
        with pytest.raises(ValueError):
            replace(base, **bad)
