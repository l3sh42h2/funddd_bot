"""OKX V6 Solana (chainIndex 501) поверх существующего транспорта OkxDex на подставной сессии. Ответы OKX —
СИНТЕТИЧЕСКИЕ по документации/ТЗ (ключа нет): swap-instruction с инструкцией swap_v3, собранной по on-chain IDL.
Живой контракт подтвердить фикстурой с ключом на VPS (M1). Плюс сквозной выбор роутером: живой Jupiter + OKX."""
import base64, copy, hashlib, hmac, json, pathlib, struct, time
from urllib.parse import parse_qsl, urlsplit
from decimal import Decimal as D
import pytest
from funding_bot.okxdex import OkxDex, BASE as OKX_BASE
from funding_bot.trade import fees as F, spot_router as sr, okx_sol_spot as ok, jupiter_spot as js
from funding_bot.trade.types import Book

DATA = pathlib.Path(__file__).parent / "data" / "sol_routes"
IDL = json.loads((DATA / "idl_okx_router.json").read_text())
DISC = {i["name"]: bytes(i["discriminator"]) for i in IDL["instructions"]}
USDC = sr.AssetRef("EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v", sr.TOKEN_PROGRAM, 6, "USDC")
ANSEM = sr.AssetRef("9cRCn9rGT8V2imeM2BaKs13yhMEais3ruM3rPvTGpump", sr.TOKEN_2022_PROGRAM, 6, "ANSEM")
GENESIS = "5eykt4UsFv8P8NJdTREpY1vzqKqZKvdpKuc147dw2N9d"
_k = lambda s: sr.b58encode(hashlib.sha256(s.encode()).digest())      # noqa: E731
W, IN_ATA, OUT_ATA, SA, SRC_SA, DST_SA, ALT1, ATTACKER = map(_k, ("w", "in", "out", "sa", "ssa", "dsa", "alt", "evil"))
CB = sr.COMPUTE_BUDGET_PROGRAM
BH = _k("blockhash")


def acc(p, s=False, w=False):
    return {"pubkey": p, "isSigner": s, "isWritable": w}


def ixj(program, accounts, data: bytes):
    return {"programId": program, "accounts": accounts, "data": base64.b64encode(data).decode()}


def swap_data(name="swap_v3", amount_in=100_000_000, expect=601_000_000, min_return=598_000_000):
    """SwapArgs по IDL: amount_in, expect_amount_out, min_return, amounts: Vec<u64>, routes: Vec<Vec<Route>>
    (Route{dexes: Vec<Dex>, weights: bytes}), затем commission_info u32, platform_fee_rate u16, order_id u64."""
    route = struct.pack("<I", 1) + b"\x00" + struct.pack("<I", 1) + b"\x64"          # Dex::SplTokenSwap, вес 100
    routes = struct.pack("<I", 1) + struct.pack("<I", 1) + route
    return (DISC[name] + struct.pack("<QQQ", amount_in, expect, min_return) + struct.pack("<IQ", 1, amount_in) + routes
            + struct.pack("<IHQ", 0, 0, 7))


def swap_accounts(wallet=W, src=IN_ATA, dst=OUT_ATA, commission=ok.OKX_ROUTER, platform=ok.OKX_ROUTER):
    return [acc(wallet, True, True), acc(src, w=True), acc(dst, w=True), acc(USDC.mint), acc(ANSEM.mint),
            acc(commission, w=True), acc(platform, w=True), acc(SA, w=True), acc(SRC_SA, w=True), acc(DST_SA, w=True),
            acc(sr.TOKEN_PROGRAM), acc(sr.TOKEN_2022_PROGRAM), acc(sr.ATA_PROGRAM), acc(sr.SYSTEM_PROGRAM)]


def response(*, wallet=W, in_ata=IN_ATA, out_ata=OUT_ATA, swap=None, accounts=None, to_amount="601000000",
             min_receive="598000000", to_dec="6", to_mint=None, extra_ixs=(), cu=(300_000, 5_000)):
    ixs = []
    if cu:
        ixs += [ixj(CB, [], b"\x02" + struct.pack("<I", cu[0])), ixj(CB, [], b"\x03" + struct.pack("<Q", cu[1]))]
    ixs.append(ixj(sr.ATA_PROGRAM, [acc(wallet, True, True), acc(out_ata, w=True), acc(wallet), acc(ANSEM.mint),
                                    acc(sr.SYSTEM_PROGRAM), acc(sr.TOKEN_2022_PROGRAM)], b"\x01"))
    ixs += list(extra_ixs)
    ixs.append(ixj(ok.OKX_ROUTER, accounts or swap_accounts(wallet, in_ata, out_ata), swap or swap_data()))
    rr = {"chainIndex": "501", "contextSlot": 446_700_000, "fromTokenAmount": "100000000", "toTokenAmount": to_amount,
          "tradeFee": "0.0021", "estimateGasFee": "300000", "priceImpactPercent": "-0.05", "quoteId": "q-1",
          "swapMode": "exactIn", "router": f"{USDC.mint}--{ANSEM.mint}",
          "fromToken": {"tokenContractAddress": USDC.mint, "decimal": "6", "taxRate": "0", "isHoneyPot": False,
                        "tokenSymbol": "USDC"},
          "toToken": {"tokenContractAddress": to_mint or ANSEM.mint, "decimal": to_dec, "taxRate": "0",
                      "isHoneyPot": False, "tokenSymbol": "ANSEM"},
          "dexRouterList": [{"dexProtocol": {"dexName": "Meteora DLMM", "percent": "100"}, "fromTokenIndex": "0",
                             "toTokenIndex": "1"}]}
    d = {"addressLookupTableAccount": [ALT1], "instructionLists": ixs, "routerResult": rr}
    if min_receive is not None:
        d["tx"] = {"minReceiveAmount": min_receive}
    return {"code": "0", "msg": "", "data": d}


def mkreq(wallet=W, in_ata=IN_ATA, out_ata=OUT_ATA, deadline=30.0, **kw):
    return sr.QuoteRequest(side="entry", input=USDC, output=ANSEM, amount_in_raw=100_000_000, wallet=wallet,
                           slippage_bps=50, genesis_hash=GENESIS, deadline_mono=time.monotonic() + deadline,
                           input_account=in_ata, output_account=out_ata, account_rent=((ANSEM.mint, 0),), **kw)


def norm(resp, r=None):
    return ok.normalize_swap_instruction(resp["data"], r or mkreq(), received_at=time.time(),
                                         received_mono=time.monotonic())


class _R:
    def __init__(self, body, code=200):
        self.status_code, self._b, self.text = code, body, json.dumps(body)

    def json(self):
        return self._b


class _S:
    def __init__(self, handler):
        self.handler, self.calls, self.headers = handler, [], {}

    def request(self, method, url, data=None, headers=None, timeout=None):
        self.calls.append(dict(method=method, url=url, data=data, headers=headers, timeout=timeout))
        return self.handler(method, url, data)


def okx(handler, key="k"):
    return OkxDex(session=_S(handler), key=key, secret="s3cr3t", passphrase="p", rps=1000,
                  clock=lambda: 1_789_000_000.123, pace_path=False)


# --- IDL --------------------------------------------------------------------------------------------------------
def test_idl_constants_match_onchain_idl():
    assert hashlib.sha256((DATA / "idl_okx_router.json").read_bytes()).hexdigest() == ok.IDL_SHA256
    assert IDL["address"] == ok.OKX_ROUTER
    tables = [ok.SWAP_ALLOWED, ok.SWAP_UNCONFIRMED, ok.SWAP_FORBIDDEN, ok.OKX_PREP]
    for t in tables:
        for disc, name in t.items():
            assert DISC[name] == disc
    classified = {n for t in tables for n in t.values()}
    assert classified == set(DISC)                      # каждая инструкция роутера разложена: новая — будет замечена
    ix = {i["name"]: i for i in IDL["instructions"]}
    accs = [a["name"] for a in ix["swap_v3"]["accounts"]]
    assert len(accs) == ok.SWAP_V3_MIN_ACCOUNTS
    want = {"payer": "payer", "source": "source_token_account", "destination": "destination_token_account",
            "source_mint": "source_mint", "destination_mint": "destination_mint", "commission": "commission_account",
            "platform_fee": "platform_fee_account", "source_program": "source_token_program",
            "destination_program": "destination_token_program"}
    assert {k: accs[v] for k, v in ok.SWAP_V3_ACC.items()} == want
    assert [a["name"] for a in ix["swap_v3_with_cpi_event"]["accounts"]][:14] == accs
    swap_args = next(t for t in IDL["types"] if t["name"] == "SwapArgs")["type"]["fields"]
    assert [(f["name"], f["type"]) for f in swap_args[:3]] == [("amount_in", "u64"), ("expect_amount_out", "u64"),
                                                               ("min_return", "u64")]
    assert [a["name"] for a in ix["swap_v3"]["args"]] == ["args", "commission_info", "platform_fee_rate", "order_id"]


def test_slippage_percent_is_percent_string():
    assert [ok.slippage_percent(b) for b in (50, 100, 1, 5, 150)] == ["0.5", "1", "0.01", "0.05", "1.5"]


# --- запрос и транспорт ------------------------------------------------------------------------------------------
def test_swap_instruction_request_signed_by_existing_transport():
    d = okx(lambda *a: _R(response()))
    c = ok.OkxSolSpot(d, limits=sr.RouteLimits(max_price_impact_bps=150)).build(mkreq())
    assert isinstance(c, sr.SwapCandidate)
    call = d._s.calls[0]
    u = urlsplit(call["url"])
    assert u.path == ok.SWAP_IX_PATH and call["method"] == "GET" and call["data"] is None
    q = dict(parse_qsl(u.query))
    assert q == {"chainIndex": "501", "amount": "100000000", "fromTokenAddress": USDC.mint, "toTokenAddress": ANSEM.mint,
                 "swapMode": "exactIn", "userWalletAddress": W, "slippagePercent": "0.5",
                 "priceImpactProtectionPercent": "1.5"}
    for forbidden in ("swapReceiverAddress", "closeAuthorityAddress", "feePercent", "maxIn",
                      "fromTokenReferrerWalletAddress", "toTokenReferrerWalletAddress", "positiveSlippagePercent"):
        assert forbidden not in q
    path_q = call["url"].replace(OKX_BASE, "")
    want = base64.b64encode(hmac.new(b"s3cr3t", (call["headers"]["OK-ACCESS-TIMESTAMP"] + "GET" + path_q).encode(),
                                     hashlib.sha256).digest()).decode()
    assert call["headers"]["OK-ACCESS-SIGN"] == want


def test_no_credentials_means_unavailable_without_requests():
    d = okx(lambda *a: _R(response()), key="")
    u = ok.OkxSolSpot(d).build(mkreq())
    assert isinstance(u, sr.Unavailable) and u.reason == "no_credentials" and d._s.calls == []


@pytest.mark.parametrize("resp,reason", [
    (_R({"code": "82000", "msg": "Insufficient liquidity"}), "no_route"),
    (_R({"code": "50011", "msg": "Too Many Requests"}, 429), "rate_limited"),
    (_R({"code": "50113", "msg": "Invalid Sign"}, 401), "auth"),
    (_R({"code": "82104", "msg": "token not supported"}), "unsupported"),
    (_R({"code": "0", "data": [response()["data"]]}), "schema"),      # список вместо объекта — не угадываем
])
def test_errors_are_unavailable(resp, reason):
    u = ok.OkxSolSpot(okx(lambda *a: resp)).build(mkreq())
    assert isinstance(u, sr.Unavailable) and u.reason == reason


# --- нормализация swap-instruction --------------------------------------------------------------------------------
def test_normalize_swap_instruction_ok():
    c = norm(response())
    assert c.reasons == ()
    assert (c.expected_out_raw, c.min_out_raw, c.onchain_min_out_raw) == (601_000_000, 598_000_000, 598_000_000)
    assert (c.cu_limit, c.cu_price_micro, c.required_signers) == (300_000, 5_000, (W,))
    f = {x.kind: x for x in c.fees}
    assert f["network_base"].amount_raw == 5000
    assert f["network_priority"].amount_raw == 1500 and not f["network_priority"].estimated   # от запрошенного лимита
    assert f["rent_deposit"].amount_raw == 0
    assert f["network_estimate_usd"].superseded and f["network_estimate_usd"].amount_raw == 21
    assert c.price_impact_bps == D("-5") and c.quote_id == "q-1" and c.source_slot == 446_700_000
    assert c.payload.kind == "instructions" and c.payload.alts == ((ALT1, ()),)
    assert c.last_valid_block_height is None                              # высоту даёт только своя сборка
    prices = {F.NATIVE_SOL: F.PriceObs(F.NATIVE_SOL, USDC.mint, D(100), 0.0, "t"),
              F.USD: F.PriceObs(F.USD, USDC.mint, D(1), 0.0, "t")}
    v = F.value_external(c.fees, wallet=W, unit=USDC.mint, prices=prices, now_mono=0.0, max_price_age_ms=1000)
    assert v.total == D("0.00065")                                        # 6500 лампортов × 100; tradeFee не прибавлен


def test_r09_json_min_receive_differs_from_instruction():
    c = norm(response(min_receive="599000000"))
    assert (c.min_out_raw, c.onchain_min_out_raw) == (599_000_000, 598_000_000)
    r = sr.evaluate(c, mkreq(), policy=sr.RoutingPolicy(), limits=sr.RouteLimits(), prices={}, now_mono=time.monotonic(),
                    now_wall=time.time())
    assert "min_out_mismatch" in r.cand.reasons and not r.eligible and not r.previewable


@pytest.mark.parametrize("kw,reason", [
    (dict(swap=swap_data("swap_tob_v3_with_receiver")), "okx_ix_forbidden:swap_tob_v3_with_receiver"),
    (dict(swap=swap_data("proxy_swap")), "okx_ix_forbidden:proxy_swap"),
    (dict(swap=swap_data("swap_tob_v3")), "okx_ix_unconfirmed:swap_tob_v3"),
    (dict(swap=b"\x01" * 40), "okx_ix_unknown:0101010101010101"),
    (dict(swap=swap_data(amount_in=99_000_000)), "ix_in_amount"),
    (dict(swap=swap_data(expect=610_000_000)), "ix_expected_out"),
    (dict(accounts=swap_accounts(commission=ATTACKER)), "okx_fee_account:commission"),
    (dict(accounts=swap_accounts(platform=ATTACKER)), "okx_fee_account:platform_fee"),
    (dict(accounts=swap_accounts(wallet=ATTACKER)), "ix_authority"),
    (dict(accounts=swap_accounts(dst=ATTACKER)), "ix_recipient"),
    (dict(to_dec="9"), "decimals_mismatch:to"),
    (dict(to_mint=ANSEM.mint.lower()), "echo_mismatch:toToken"),          # регистр mint значим
    (dict(to_mint=ok.NATIVE_PLACEHOLDER), "native_placeholder:to"),
    (dict(extra_ixs=[ixj(ok.OKX_ROUTER, swap_accounts(), swap_data())]), "okx_router_ix_count:2"),
    (dict(extra_ixs=[ixj(sr.SYSTEM_PROGRAM, [acc(W, True, True), acc(ATTACKER, w=True)], struct.pack("<IQ", 2, 10))]),
     "system_transfer_unexpected"),
    (dict(extra_ixs=[ixj(ok.OKX_ROUTER, [acc(W, True, True)], DISC["wrap_unwrap_v3"])]), "okx_ix_prep:wrap_unwrap_v3"),
    (dict(extra_ixs=[ixj(CB, [], b"\x03" + struct.pack("<Q", 9))]), "cu_conflict"),
])
def test_tampered_or_unsupported_payload_rejected(kw, reason):
    assert reason in norm(response(**kw)).reasons


def test_transfer_tax_and_honeypot_rejected():
    resp = response()
    resp["data"]["routerResult"]["toToken"].update(taxRate="0.01", isHoneyPot="true")
    c = norm(resp)
    assert {"transfer_tax:to", "honeypot:to"} <= set(c.reasons)


def test_price_unknown_without_cu_limit():
    c = norm(response(cu=None, extra_ixs=[ixj(CB, [], b"\x03" + struct.pack("<Q", 7))]))
    assert {f.kind: f.amount_raw for f in c.fees}["network_priority"] is None       # R05: неизвестно, не 0


# --- /swap: только сверка -----------------------------------------------------------------------------------------
def _swap_resp(tx_data, **tx):
    rr = response()["data"]["routerResult"]
    return [{"routerResult": rr, "tx": {"data": tx_data, "minReceiveAmount": "598000000", "from": W,
                                        "to": ok.OKX_ROUTER, "signatureData": [], "slippagePercent": "0.5", **tx}}]


def test_g08_g13_swap_is_reconcile_only_base58_without_height():
    raw = b"\x01" + b"\x00" * 64 + b"\x80okx"
    c = ok.normalize_swap(_swap_resp(sr.b58encode(raw)), mkreq(), received_at=0.0, received_mono=0.0)
    assert c.payload.encoding == "base58" and c.payload.raw_bytes() == raw
    assert c.last_valid_block_height is None and "okx_swap_reconcile_only" in c.hard_reasons
    raw70 = raw + b"!"                                                      # base64 с '=' — не алфавит base58
    c64 = ok.normalize_swap(_swap_resp(base64.b64encode(raw70).decode()), mkreq(), received_at=0.0, received_mono=0.0)
    assert "payload_encoding" in c64.reasons and c64.payload is None       # base64 там, где контракт — base58
    # Строка base64 без символов вне base58 декодируется «успешно», но в ДРУГИЕ байты: по алфавиту это не поймать.
    # Такое ловит только разбор структуры транзакции — валидатор (ждёт solders); угадывать кодировку не будем.
    sneaky = base64.b64encode(raw).decode()
    assert not set(sneaky) - set(sr.B58_ALPHABET) and sr.decode_bytes(sneaky, "base58") != raw
    sd = ok.normalize_swap(_swap_resp(sr.b58encode(raw), signatureData=["extra-tx"]), mkreq(), received_at=0.0,
                           received_mono=0.0)
    assert "okx_signature_data" in sd.reasons
    built = norm(response())
    rec = ok.reconcile(built, c)
    assert rec == dict(same_request=True, expected_out_delta=0, min_out_delta=0, same_route=True)


def test_swap_reconcile_and_quote_via_transport():
    raw = b"\x01" + b"\x00" * 64 + b"\x80okx"
    paths = []

    def h(method, url, data):
        p = urlsplit(url).path
        paths.append(p)
        if p == ok.QUOTE_PATH:
            return _R({"code": "0", "data": [response()["data"]["routerResult"]]})
        return _R({"code": "0", "data": _swap_resp(sr.b58encode(raw))})
    spot = ok.OkxSolSpot(okx(h))
    s = spot.swap_reconcile(mkreq())
    q = spot.quote(mkreq())
    assert paths == [ok.SWAP_PATH, ok.QUOTE_PATH]
    assert s.payload.encoding == "base58" and {"capability:okx_quote_preview", "no_min_out"} <= set(q.reasons)


# --- своя сборка со своим blockhash (G08) -------------------------------------------------------------------------
class Asm:
    def __init__(self):
        self.calls = []

    def assemble(self, *, payer, ixs, alts, recent_blockhash, last_valid_block_height, cu_limit):
        self.calls.append(dict(bh=recent_blockhash, lvbh=last_valid_block_height, cu_limit=cu_limit, alts=alts))
        return sr.UnsignedTx(payload=sr.Payload("message", "base64", data="AQID"), message_hash="m1", signers=(payer,),
                             recent_blockhash=recent_blockhash, last_valid_block_height=last_valid_block_height,
                             cu_limit=cu_limit)

    def wrap(self, payload, lvbh):
        raise AssertionError("готовую tx OKX не исполняем")


class Sim:
    def __init__(self, units, ok_=True):
        self.units, self.ok = units, ok_

    def simulate(self, tx):
        return sr.SimResult(self.ok, None if self.ok else "err", self.units, 1)


class Chain:
    def latest_blockhash(self):
        return BH, 999

    def block_height(self):
        return 900


class Val:
    def validate(self, tx, cand, req):
        return ()


def test_g08_finalize_uses_own_blockhash_pair():
    asm = Asm()
    spot = ok.OkxSolSpot(okx(lambda *a: _R(response())), tools=sr.SolanaTools(asm, Sim(250_000), Chain(), Val()))
    c = spot.finalize(norm(response()), mkreq())
    assert asm.calls == [dict(bh=BH, lvbh=999, cu_limit=None, alts=((ALT1, ()),))]   # лимит CU — из инструкций OKX
    assert (c.recent_blockhash, c.last_valid_block_height, c.message_hash) == (BH, 999, "m1")
    assert c.simulation_ok and c.validated and c.reasons == ()
    over = ok.OkxSolSpot(okx(lambda *a: _R(response())), tools=sr.SolanaTools(Asm(), Sim(350_000), Chain(), Val()))
    assert "cu_exceeded" in over.finalize(norm(response()), mkreq()).reasons


# --- сквозной выбор: живой Jupiter (без ключа) + синтетический OKX ------------------------------------------------
class _JSess:
    def __init__(self, fx):
        self.fx = fx

    def get(self, url, params=None, headers=None, timeout=None):
        name = {"order": "order_buy_taker", "build": "build_buy_taker"}[url.rsplit("/", 1)[1]]

        class R:
            status_code = 200
            _b = copy.deepcopy(self.fx["calls"][name]["body"])

            def json(self):
                return self._b
        return R()


def test_router_end_to_end_live_jupiter_and_synthetic_okx():
    fx = json.loads((DATA / "jupiter_v2_live_20260913.json").read_text())
    taker = fx["taker"]
    usdc_ata, ansem_ata = "BhTkNtrZ1GGs5eQuXdexgXDAc7AvrN8rrJ4TkFoZzfhV", "ByZkEK8o2tvKLnDPdi8F5GLNnoKsDVBVTVvXDUo5Bae5"
    d = okx(lambda *a: _R(response(wallet=taker, in_ata=usdc_ata, out_ata=ansem_ata)))
    limits = sr.RouteLimits(max_quote_age_ms=10_000, collection_deadline_ms=5_000, min_blockhash_validity_heights=60,
                            max_network_fee_lamports_per_tx=200_000, max_spot_slippage_bps=100, max_price_impact_bps=150,
                            max_native_price_age_ms=60_000, max_book_age_ms=10_000, max_rent_locked_lamports=3_000_000)
    router = sr.SpotRouter([js.JupiterSpot(session=_JSess(fx), api_key="", rps=1000), ok.OkxSolSpot(d, limits=limits)],
                           limits=limits)
    now = time.monotonic()
    prices = {F.NATIVE_SOL: F.PriceObs(F.NATIVE_SOL, USDC.mint, D(150), now, "t"),
              F.USD: F.PriceObs(F.USD, USDC.mint, D(1), now, "t")}
    hedge = sr.HedgeContext(Book(bids=((D("0.1660"), D(2000)), (D("0.1655"), D(10_000))), asks=((D("0.1670"), D(10_000)),),
                                 ts=time.time()),
                            sr.PairParams(D(1), D(1), D(1), D("0.000675"), D(10), leverage=D(1), margin_reserve=D(0),
                                          available_margin=D(10 ** 6)), time.time())   # маржа синтетическая
    r = mkreq(wallet=taker, in_ata=usdc_ata, out_ata=ansem_ata)
    dec = router.select(r, prices=prices, block_height=424_750_000, hedge=hedge)
    assert dec.status == "refused" and dec.winner is None           # без solders никто не собран и не симулирован
    assert dec.preview_winner.path == "jupiter_build_v2"             # 602.91M против 601.00M при сопоставимой сети
    by = {x.cand.path: x for x in dec.ranked}
    assert set(by) == {"jupiter_order_v2", "jupiter_build_v2", "okx_solana_v6"}
    assert "no_transaction" in by["jupiter_order_v2"].cand.reasons and not by["jupiter_order_v2"].previewable
    assert {"not_simulated", "not_validated"} <= set(by["okx_solana_v6"].cand.reasons)
    assert by["jupiter_build_v2"].pair.qty == 602 and by["jupiter_build_v2"].pair.reasons == ()
    rows = dec.records("op-1", 1)
    assert len(rows) == 3 and not any(x["selected"] for x in rows)
    assert [x["path"] for x in rows if x["preview_selected"]] == ["jupiter_build_v2"]
    assert all(x["metric_version"] == sr.METRIC_VERSION for x in rows)
