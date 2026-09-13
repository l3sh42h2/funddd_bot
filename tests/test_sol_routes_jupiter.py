"""Jupiter Swap V2 на живых ответах без ключа (13.09, USDC↔ANSEM на 100 USDC, tests/data/sol_routes) и их порче:
нормализация Order/Build, сверка инструкции роутера с on-chain IDL, цикл CU за протоколами (фейки Solana-ядра),
HTTP без ключа, сокрытие ключа, кодировки (R09, R14, G13, G14)."""
import base64, copy, hashlib, json, pathlib, struct, time
from decimal import Decimal as D
import pytest
from funding_bot.trade import fees as F, spot_router as sr, jupiter_spot as js

DATA = pathlib.Path(__file__).parent / "data" / "sol_routes"
FX = json.loads((DATA / "jupiter_v2_live_20260913.json").read_text())
IDL = json.loads((DATA / "idl_jupiter_v6.json").read_text())
TAKER = FX["taker"]
USDC = sr.AssetRef("EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v", sr.TOKEN_PROGRAM, 6, "USDC")
ANSEM = sr.AssetRef("9cRCn9rGT8V2imeM2BaKs13yhMEais3ruM3rPvTGpump", sr.TOKEN_2022_PROGRAM, 6, "ANSEM")
GENESIS = "5eykt4UsFv8P8NJdTREpY1vzqKqZKvdpKuc147dw2N9d"
USDC_ATA = "BhTkNtrZ1GGs5eQuXdexgXDAc7AvrN8rrJ4TkFoZzfhV"     # счета синтетического taker в ответе Jupiter
ANSEM_ATA = "ByZkEK8o2tvKLnDPdi8F5GLNnoKsDVBVTVvXDUo5Bae5"
ATTACKER = sr.b58encode(hashlib.sha256(b"attacker").digest())
TIP = sr.b58encode(hashlib.sha256(b"tip account").digest())
BH = sr.b58encode(hashlib.sha256(b"blockhash").digest())
SELL_AMT = int(FX["calls"]["build_sell_taker"]["body"]["inAmount"])


def body(name):
    return copy.deepcopy(FX["calls"][name]["body"])


def mkreq(side="entry", amount=None, accounts=True, rent=None, **kw):
    a_in, a_out = (USDC, ANSEM) if side == "entry" else (ANSEM, USDC)
    acc = {}
    if accounts:
        acc = dict(input_account=USDC_ATA if side == "entry" else ANSEM_ATA,
                   output_account=ANSEM_ATA if side == "entry" else USDC_ATA)
    return sr.QuoteRequest(side=side, input=a_in, output=a_out,
                           amount_in_raw=amount or (100_000_000 if side == "entry" else SELL_AMT), wallet=TAKER,
                           slippage_bps=50, genesis_hash=GENESIS, deadline_mono=time.monotonic() + 30,
                           account_rent=rent if rent is not None else ((a_out.mint, 0),), **acc, **kw)


def nb(b, r, policy=sr.RoutingPolicy()):
    return js.normalize_build(b, r, policy=policy, received_at=time.time(), received_mono=time.monotonic())


def no(b, r, policy=sr.RoutingPolicy()):
    return js.normalize_order(b, r, policy=policy, received_at=time.time(), received_mono=time.monotonic())


def patch_swap(b, **fields):
    """Испортить аргументы shared_accounts_route_v2 в данных инструкции (смещения — по IDL, id: u8 первым)."""
    raw = bytearray(base64.b64decode(b["swapInstruction"]["data"]))
    offs = {"in_amount": (9, "<Q"), "quoted_out": (17, "<Q"), "slippage": (25, "<H"), "platform_fee": (27, "<H"),
            "positive_slippage": (29, "<H")}
    for k, v in fields.items():
        if k == "disc":
            raw[:8] = v
        else:
            struct.pack_into(offs[k][1], raw, offs[k][0], v)
    b["swapInstruction"]["data"] = base64.b64encode(bytes(raw)).decode()
    return b


# --- IDL: константы модуля против on-chain IDL (ожидания не из тестируемого кода) ------------------------------
def test_idl_constants_match_onchain_idl():
    assert hashlib.sha256((DATA / "idl_jupiter_v6.json").read_bytes()).hexdigest() == js.IDL_SHA256
    ix = {i["name"]: i for i in IDL["instructions"]}
    assert bytes(ix["route_v2"]["discriminator"]) == js.ROUTE_V2
    assert bytes(ix["shared_accounts_route_v2"]["discriminator"]) == js.SHARED_ROUTE_V2
    for disc, name in js.OTHER_ROUTE_IX.items():
        assert bytes(ix[name]["discriminator"]) == disc
    args = lambda n: [(a["name"], a["type"]) for a in ix[n]["args"]]          # noqa: E731
    prefix = [("in_amount", "u64"), ("quoted_out_amount", "u64"), ("slippage_bps", "u16"), ("platform_fee_bps", "u16"),
              ("positive_slippage_bps", "u16")]
    assert args("route_v2")[:5] == prefix and args("shared_accounts_route_v2")[:6] == [("id", "u8")] + prefix
    names = {"authority": "user_transfer_authority", "source_mint": "source_mint", "destination_mint": "destination_mint",
             "source_program": "source_token_program", "destination_program": "destination_token_program"}
    route = [a["name"] for a in ix["route_v2"]["accounts"]]
    shared = [a["name"] for a in ix["shared_accounts_route_v2"]["accounts"]]
    for k, v in {**names, "source": "user_source_token_account", "destination": "user_destination_token_account",
                 "destination_opt": "destination_token_account"}.items():
        assert route[js.ROUTE_V2_ACC[k]] == v
    for k, v in {**names, "source": "source_token_account", "destination": "destination_token_account"}.items():
        assert shared[js.SHARED_ROUTE_V2_ACC[k]] == v
    assert IDL["address"] == js.JUP_PROGRAM


# --- Build V2 на живых ответах --------------------------------------------------------------------------------
def test_build_live_buy_normalizes_with_onchain_threshold():
    b = body("build_buy_taker")
    c = nb(b, mkreq())
    assert c.reasons == ()
    assert (c.expected_out_raw, c.min_out_raw, c.onchain_min_out_raw) == (602_914_237, 599_899_666, 599_899_666)
    assert 602_914_237 * 9950 // 10000 == 599_899_665          # floor на единицу меньше: программа считает вверх
    assert c.payload.kind == "instructions" and c.payload.encoding == "json" and len(c.payload.alts) == 4
    assert c.required_signers == (TAKER,)
    assert base64.b64decode("A0MGAAAAAAAA") == b"\x03" + (1603).to_bytes(8, "little") and c.cu_price_micro == 1603
    net = {f.kind: f for f in c.fees}
    assert net["network_base"].amount_raw == 5000
    assert net["network_priority"].amount_raw == 2245 and net["network_priority"].estimated   # верхняя граница 1.4M
    assert net["rent_deposit"].amount_raw == 0                  # счёт ANSEM есть (данные Solana-ядра)
    assert c.provider_time == pytest.approx(1789304832.303, abs=0.01)
    assert c.message_hash is None and c.simulation_ok is None   # подписывать ещё нечего


def test_build_live_sell_normalizes_token2022_source():
    c = nb(body("build_sell_taker"), mkreq("exit"))
    assert c.reasons == ()
    assert (c.expected_out_raw, c.onchain_min_out_raw, c.min_out_raw) == (99_766_507, 99_267_675, 99_267_675)
    assert c.input_program == sr.TOKEN_2022_PROGRAM


def test_build_without_expected_accounts_is_live_only_unverified():
    c = nb(body("build_buy_taker"), mkreq(accounts=False, rent=()))
    assert set(c.reasons) == {"source_unverified", "recipient_unverified"}
    assert all(sr.live_only(x) for x in c.reasons)
    assert {f.kind: f.amount_raw for f in c.fees}["rent_deposit"] is None     # rent неизвестен, не 0


@pytest.mark.parametrize("patch,reason", [
    (dict(in_amount=100_000_001), "ix_in_amount"),
    (dict(quoted_out=590_000_000), "ix_quoted_out"),
    (dict(slippage=500), "ix_slippage"),
    (dict(platform_fee=10), "ix_platform_fee"),
    (dict(positive_slippage=50), "ix_positive_slippage"),
    (dict(disc=bytes([53, 96, 229, 202, 216, 187, 250, 24])), "jup_ix"),        # exact_out_route_v2
])
def test_r09_router_args_tampering_rejected(patch, reason):
    c = nb(patch_swap(body("build_buy_taker"), **patch), mkreq())
    assert reason in c.reasons and c.hard_reasons


def test_r09_changed_quoted_out_makes_json_and_instruction_thresholds_differ():
    c = nb(patch_swap(body("build_buy_taker"), quoted_out=590_000_000), mkreq())
    assert c.onchain_min_out_raw == -(-590_000_000 * 9950 // 10000) != c.min_out_raw


@pytest.mark.parametrize("idx,value,reason", [
    (5, ATTACKER, "ix_recipient"),                       # destination_token_account — чужой
    (1, ATTACKER, "ix_authority"),                       # user_transfer_authority — не наш кошелёк
    (7, USDC.mint, "ix_mint"),                           # destination_mint подменён
    (9, sr.TOKEN_PROGRAM, "ix_token_program"),           # classic вместо Token-2022 у ANSEM
    (2, ATTACKER, "ix_source"),
])
def test_r09_router_accounts_tampering_rejected(idx, value, reason):
    b = body("build_buy_taker")
    b["swapInstruction"]["accounts"][idx]["pubkey"] = value
    assert reason in nb(b, mkreq()).reasons


def test_echo_mismatch_and_mint_case_are_different_assets():
    b = body("build_buy_taker")
    b["outputMint"] = ANSEM.mint.lower()                 # S02: вариант регистра — другой актив
    assert "echo_mismatch:outputMint" in nb(b, mkreq()).reasons


def test_r14_tip_and_cu_conflicts_are_visible():
    b = body("build_buy_taker")
    b["tipInstruction"] = {"programId": sr.SYSTEM_PROGRAM, "accounts": [
        {"pubkey": TAKER, "isSigner": True, "isWritable": True}, {"pubkey": TIP, "isSigner": False, "isWritable": True}],
        "data": base64.b64encode(struct.pack("<IQ", 2, 1_000_000)).decode()}
    b["computeBudgetInstructions"].append({"programId": sr.COMPUTE_BUDGET_PROGRAM, "accounts": [],
                                           "data": base64.b64encode(b"\x02" + struct.pack("<I", 400_000)).decode()})
    c = nb(b, mkreq())
    assert "cu_conflict" in c.reasons                    # лимит CU в Build ставим мы
    tips = [f for f in c.fees if f.kind == "tip"]
    assert [(f.amount_raw, f.payer) for f in tips] == [(1_000_000, TAKER)]
    b2 = body("build_buy_taker")
    b2["otherInstructions"].append(b["tipInstruction"])
    assert "system_transfer_unexpected" in nb(b2, mkreq()).reasons


def test_external_signer_and_ata_checks():
    b = body("build_buy_taker")
    b["setupInstructions"][0]["accounts"][0]["pubkey"] = ATTACKER       # платит и подписывает чужой
    c = nb(b, mkreq())
    assert "external_signer" in c.reasons and ATTACKER in c.required_signers
    b = body("build_buy_taker")
    b["setupInstructions"][0]["accounts"][2]["pubkey"] = ATTACKER       # ATA для чужого владельца
    assert "ata_owner" in nb(b, mkreq()).reasons
    r = mkreq(output_account=ATTACKER) if False else sr.QuoteRequest(
        side="entry", input=USDC, output=ANSEM, amount_in_raw=100_000_000, wallet=TAKER, slippage_bps=50,
        genesis_hash=GENESIS, deadline_mono=time.monotonic() + 30, input_account=USDC_ATA, output_account=ATTACKER)
    c = nb(body("build_buy_taker"), r)
    assert {"ata_address", "ix_recipient"} <= set(c.reasons)
    c = nb(body("build_buy_taker"), mkreq(rent=((ANSEM.mint, 2_074_080),)))
    assert {f.kind: f.amount_raw for f in c.fees}["rent_deposit"] == 2_074_080


# --- цикл CU за протоколами Solana-ядра -------------------------------------------------------------------------
class Asm:
    def __init__(self, signers=None):
        self.calls, self.signers = [], signers

    def assemble(self, *, payer, ixs, alts, recent_blockhash, last_valid_block_height, cu_limit):
        self.calls.append(dict(cu_limit=cu_limit, blockhash=recent_blockhash, lvbh=last_valid_block_height, n_ix=len(ixs)))
        return sr.UnsignedTx(payload=sr.Payload("message", "base64", data="AQID"), message_hash=f"h{cu_limit}",
                             signers=self.signers or (payer,), recent_blockhash=recent_blockhash,
                             last_valid_block_height=last_valid_block_height, cu_limit=cu_limit)

    def wrap(self, payload, lvbh):
        return sr.UnsignedTx(payload=payload, message_hash="hw", signers=self.signers or (TAKER,), recent_blockhash=BH,
                             last_valid_block_height=lvbh, cu_limit=None)


class Sim:
    def __init__(self, *results):
        self.results, self.seen = list(results), []

    def simulate(self, tx):
        self.seen.append(tx.message_hash)
        return self.results.pop(0)


class Chain:
    def latest_blockhash(self):
        return BH, 424_800_000

    def block_height(self):
        return 424_799_000


class Val:
    def __init__(self, out=()):
        self.out, self.seen = out, []

    def validate(self, tx, cand, req):
        self.seen.append(tx.message_hash)
        return self.out


def ok(units):
    return sr.SimResult(True, None, units, 446_700_000)


def spot(tools, **kw):
    return js.JupiterSpot(session=object(), api_key="", tools=tools, rps=1000, **kw)


def test_fakes_satisfy_solana_protocols():
    assert isinstance(Asm(), sr.TxAssembler) and isinstance(Sim(), sr.Simulator)
    assert isinstance(Chain(), sr.ChainState) and isinstance(Val(), sr.PayloadValidator)


def test_cu_loop_sets_limit_resimulates_and_recomputes_fee():
    asm, sim, val = Asm(), Sim(ok(100_000), ok(101_000)), Val()
    r = mkreq()
    c = spot(sr.SolanaTools(asm, sim, Chain(), val)).finalize_build(nb(body("build_buy_taker"), r), r)
    assert [x["cu_limit"] for x in asm.calls] == [1_400_000, 120_000]     # симуляция с потолком → 1.2 × 100 000
    assert sim.seen == ["h1400000", "h120000"] and val.seen == ["h120000"]  # проверка и симуляция — финальных байт
    assert (c.cu_limit, c.message_hash, c.simulation_ok, c.validated) == (120_000, "h120000", True, True)
    assert (c.recent_blockhash, c.last_valid_block_height) == (BH, 424_800_000)   # одна пара из своего RPC
    fees = {f.kind: f for f in c.fees}
    assert fees["network_priority"].amount_raw == 193 and not fees["network_priority"].estimated
    assert c.reasons == ()


def test_cu_loop_caps_at_protocol_limit_and_failures_reject():
    asm = Asm()
    r = mkreq()
    c = spot(sr.SolanaTools(asm, Sim(ok(1_300_000), ok(1_300_000)), Chain(), Val())).finalize_build(
        nb(body("build_buy_taker"), r), r)
    assert c.cu_limit == 1_400_000
    c = spot(sr.SolanaTools(Asm(), Sim(sr.SimResult(False, "InstructionError 0x1771", None, 1)), Chain(), Val())
             ).finalize_build(nb(body("build_buy_taker"), r), r)
    assert c.simulation_ok is False and any(x.startswith("simulation_failed:") for x in c.reasons)   # S13
    c = spot(sr.SolanaTools(Asm(), Sim(ok(100_000), ok(130_000)), Chain(), Val())).finalize_build(
        nb(body("build_buy_taker"), r), r)
    assert "cu_exceeded_final" in c.reasons
    c = spot(sr.SolanaTools(Asm(), Sim(ok(100_000), ok(100_000)), Chain(), Val(("recipient_not_ours",)))
             ).finalize_build(nb(body("build_buy_taker"), r), r)
    assert "validator:recipient_not_ours" in c.reasons and c.validated is False
    c = spot(sr.SolanaTools(Asm(), Sim(ok(100_000), ok(100_000)), Chain(), None)).finalize_build(
        nb(body("build_buy_taker"), r), r)
    assert c.validated is None and c.reasons == ()        # без валидатора — «not_validated» добавит роутер (live-only)
    c = spot(sr.SolanaTools(Asm(signers=(TAKER, ATTACKER)), Sim(ok(100_000), ok(100_000)), Chain(), Val())
             ).finalize_build(nb(body("build_buy_taker"), r), r)
    assert "external_signer" in c.reasons


def test_without_solana_tools_candidate_stays_preview():
    r = mkreq()
    c = spot(sr.SolanaTools()).finalize_build(nb(body("build_buy_taker"), r), r)
    assert c.simulation_ok is None and c.message_hash is None and any("ждут solders" in n for n in c.notes)


# --- Order V2 ------------------------------------------------------------------------------------------------
def test_order_live_is_preview_only_and_not_executable():
    c = no(body("order_buy_taker"), mkreq())
    assert {"provider_error:1", "no_transaction", "capability:order_preview_only"} <= set(c.reasons)
    assert c.payload is None and c.request_id == "01a09ae0-f4ee-728b-a00f-7670fc1316b9"
    pf = [f for f in c.fees if f.kind == "platform"]
    assert [(f.amount_raw, f.included_in_input_output, f.asset) for f in pf] == [(100_000, True, USDC.mint)]
    assert not [f for f in c.fees if f.asset == F.NATIVE_SOL]       # без транзакции статей сети нет
    s = no(body("order_sell_taker"), mkreq("exit", amount=602_365_257))
    assert [f.amount_raw for f in s.fees if f.kind == "platform"] == [99_649_774 * 10000 // 9990 - 99_649_774]


def _order_with_tx(**over):
    b = body("order_buy_taker")
    for k in ("errorCode", "errorMessage", "error"):
        b.pop(k, None)
    b.update(transaction=base64.b64encode(b"\x01" + b"\x00" * 64 + b"\x80payload").decode(), signatureFeeLamports=5000,
             signatureFeePayer=TAKER, prioritizationFeeLamports=20_000, prioritizationFeePayer=TAKER,
             rentFeeLamports=0, rentFeePayer=TAKER)
    b.update(over)
    return b


def test_g14_external_payer_blocks_send_until_recovery_capability():
    ext = _order_with_tx(signatureFeePayer=ATTACKER, prioritizationFeePayer=ATTACKER, gasless=True)
    exec_only = sr.RoutingPolicy(order_execution_enabled=True)
    c = no(ext, mkreq(), exec_only)
    assert "external_signer:fee_payer" in c.reasons and c.request_id      # requestId сам по себе не доказательство
    assert "capability:order_preview_only" not in c.reasons
    full = sr.RoutingPolicy(order_execution_enabled=True, external_signer_recovery=True,
                            allow_external_signer_managed_routes=True)
    c = no(ext, mkreq(), full)
    assert not [x for x in c.reasons if x.startswith("external_signer")]
    v = F.value_external(c.fees, wallet=TAKER, unit=USDC.mint, prices={}, now_mono=0, max_price_age_ms=None)
    assert v.total == 0 and set(v.sponsored) == {"network_base", "network_priority"}    # спонсор — не наш расход
    rfq = no(_order_with_tx(router="jupiterz"), mkreq(), exec_only)
    assert "external_signer:rfq" in rfq.reasons
    own = no(_order_with_tx(), mkreq())
    assert "capability:order_preview_only" in own.reasons and not own.hard_reasons


def test_order_unknown_payer_is_unknown_fee_not_free():
    c = no(_order_with_tx(signatureFeePayer=None, prioritizationFeePayer=None), mkreq())
    v = F.value_external(c.fees, wallet=TAKER, unit=USDC.mint, prices={}, now_mono=0, max_price_age_ms=None)
    assert v.total is None and "network_base:payer" in v.unknown


def test_g13_payload_encoding_is_declared_not_guessed():
    b58tx = sr.b58encode(b"\x01" + b"\x00" * 64 + b"\x80xyz")
    assert len(b58tx) % 4 != 0
    c = no(_order_with_tx(transaction=b58tx), mkreq())
    assert "payload_encoding" in c.reasons and c.payload is None           # base58 там, где контракт — base64
    with pytest.raises(sr.PayloadError):
        sr.decode_bytes("AQID+/==", "base58")                              # base64 там, где контракт — base58
    assert sr.decode_bytes("AQID", "base64") == b"\x01\x02\x03"
    assert sr.decode_bytes(sr.b58encode(b"\x00\x01\x02"), "base58") == b"\x00\x01\x02"
    for bad in (("tx", "json", "x"), ("instructions", "base64", "")):
        with pytest.raises(sr.PayloadError):
            sr.Payload(bad[0], bad[1], data=bad[2])
    with pytest.raises(sr.SchemaError):                                    # данные инструкции — только base64
        sr.ix_from_json({"programId": js.JUP_PROGRAM, "accounts": [], "data": "не base64!"}, "t")


def test_rfq_expiry_not_parsed_is_rejected():
    c = no(_order_with_tx(expireAt="1789304900"), mkreq())
    assert "rfq_expiry_unknown" in c.reasons


# --- HTTP ---------------------------------------------------------------------------------------------------
class Resp:
    def __init__(self, status, body=None, text=""):
        self.status_code, self._b, self.text = status, body, text

    def json(self):
        if self._b is None:
            raise ValueError("не JSON")
        return self._b


class Sess:
    def __init__(self, routes):
        self.routes, self.calls = routes, []

    def get(self, url, params=None, headers=None, timeout=None):
        self.calls.append(dict(url=url, params=dict(params or {}), headers=dict(headers or {}), timeout=timeout))
        h = self.routes[url.rsplit("/", 1)[1]]
        if isinstance(h, Exception):
            raise h
        return h


def test_keyless_request_params_are_exact():
    s = Sess({"order": Resp(200, body("order_buy_taker")), "build": Resp(200, body("build_buy_taker"))})
    out = js.JupiterSpot(session=s, api_key="", rps=1000).candidates(mkreq())
    assert [x.path for x in out] == ["jupiter_order_v2", "jupiter_build_v2"]
    for c in s.calls:
        assert "x-api-key" not in c["headers"]
        assert c["params"] == {"inputMint": USDC.mint, "outputMint": ANSEM.mint, "amount": "100000000", "taker": TAKER,
                               "slippageBps": "50"}
    assert [c["url"] for c in s.calls] == [js.BASE + "/order", js.BASE + "/build"]


def test_api_key_goes_only_to_header_and_never_to_text():
    key = "SECRET-KEY-XYZ-123"
    s = Sess({"order": Resp(500, {"error": f"bad key {key}"}), "build": Resp(200, body("build_buy_taker"))})
    j = js.JupiterSpot(session=s, api_key=key, rps=1000)
    assert key not in repr(j) and j.has_key()
    u = j.order(mkreq())
    assert isinstance(u, sr.Unavailable) and u.reason == "http:500" and key not in u.detail and "<key>" in u.detail
    assert s.calls[0]["headers"]["x-api-key"] == key and key not in json.dumps(s.calls[0]["params"])
    j2 = js.JupiterSpot(session=s, rps=1000, environ={"JUPITER_API_KEY": " k-from-env "})
    assert j2.has_key() and j2._key == "k-from-env"


@pytest.mark.parametrize("resp,reason", [
    (Resp(429, {"error": "slow"}), "rate_limited"), (Resp(401, {}), "auth"), (Resp(200, None, "<html>"), "schema"),
    (TimeoutError("read timeout"), "http"), (Resp(200, {"inAmount": "1"}), "schema"),
])
def test_http_failures_are_unavailable_not_zero(resp, reason):
    u = js.JupiterSpot(session=Sess({"build": resp}), api_key="", rps=1000).build(mkreq())
    assert isinstance(u, sr.Unavailable) and u.reason == reason and u.path == "jupiter_build_v2"


def test_deadline_passed_makes_no_request_and_policy_limits_paths():
    s = Sess({"order": Resp(200, body("order_buy_taker")), "build": Resp(200, body("build_buy_taker"))})
    r = sr.QuoteRequest(side="entry", input=USDC, output=ANSEM, amount_in_raw=100_000_000, wallet=TAKER, slippage_bps=50,
                        genesis_hash=GENESIS, deadline_mono=time.monotonic() - 1)
    u = js.JupiterSpot(session=s, api_key="", rps=1000).build(r)
    assert u.reason == "deadline" and s.calls == []
    out = js.JupiterSpot(session=s, api_key="", rps=1000, policy=sr.RoutingPolicy(paths=("jupiter_build_v2",))
                         ).candidates(mkreq())
    assert [x.path for x in out] == ["jupiter_build_v2"] and [c["url"][-5:] for c in s.calls] == ["build"]
