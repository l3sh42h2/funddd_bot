"""Общее для тестов проверки транзакций до подписи (test_solana_message/validate/simulation, test_sol_route_validator).

Синтетические сообщения собираются solders (SoldersMessageBuilder) из инструкций, закодированных по on-chain IDL
(borsh.encode_args), с настоящим route_plan из mainnet-свопа ANSEM (tx_jup_buy_b64.json). Кошелёк — открытый
вектор RFC 8032 (solana_helpers.RFC_PUB), остальные адреса — sha256 от меток: это не ключи и не кошельки.
Фикстуры ALT и simulateTransaction — публичный read-only mainnet-beta 13.09 (tests/data/solana/fetch_alt.py,
fetch_sim.py)."""
import base64, glob, hashlib, json, struct
from pathlib import Path
from funding_bot.trade.solana import (ALT_PROGRAM, ANSEM_MINT, ATA_PROGRAM, COMPUTE_BUDGET_PROGRAM, NATIVE_MINT,
                                      SYSTEM_PROGRAM, TOKEN_2022_PROGRAM, TOKEN_PROGRAM, USDC_MINT, b58, wire)
from funding_bot.trade.solana import message as M
from funding_bot.trade.solana.accounts import ata
from funding_bot.trade.solana.borsh import encode_args
from funding_bot.trade.solana.decoders import JUPITER_PROGRAM, OKX_ROUTER, jupiter_idl, okx_idl, pda
from funding_bot.trade.solana.validate import SwapIntent
from solana_helpers import RFC_PUB

DATA = Path(__file__).parent / "data" / "solana"
U64_MAX = 2 ** 64 - 1


def key(label: str) -> str:
    return b58.b58encode(hashlib.sha256(label.encode()).digest())


WALLET = b58.b58encode(RFC_PUB)
USDC_ATA = ata(WALLET, USDC_MINT, TOKEN_PROGRAM)
ANSEM_ATA = ata(WALLET, ANSEM_MINT, TOKEN_2022_PROGRAM)
WSOL_ATA = ata(WALLET, NATIVE_MINT, TOKEN_PROGRAM)
BH = "4sGjMW1sUnHzSxGspuhpqLDx6wiyjNtZAMdL4VZHirAn"
FOREIGN = key("foreign-recipient")
POOL = [key(f"amm-{i}") for i in range(6)]
JUP_EVENT = "D8cy77BBepLMngZx6ZukaTff5hCt1HrWyKk3Hnd9oitf"
AMOUNT_IN = 150_000_000                    # 150 USDC — пилот
QUOTED = 1_000_000_000
SLIP = 50
MIN_OUT = -(-QUOTED * (10000 - SLIP) // 10000)
ANSEM_RENT = 2_074_080                     # rent 170 байт (rent_170.json)


def fx_result(name: str):
    return json.loads((DATA / name).read_text())["response"]["result"]


def fixture_tx(name: str) -> wire.WireTransaction:
    return wire.parse_transaction(base64.b64decode(fx_result(name)["transaction"][0]))


def real_route_plan() -> list:
    """route_plan настоящего route_v2 (USDC→ANSEM, 13.09): 3 шага, варианты enum Swap с полями и без."""
    m = fixture_tx("tx_jup_buy_b64.json").message
    for ix in m.instructions:
        if m.static_keys[ix.program_index] == JUPITER_PROGRAM:
            return jupiter_idl().decode_ix(ix.data)[1]["route_plan"]
    raise AssertionError("в фикстуре нет route_v2")


class AltRpc:
    """getMultipleAccounts(base64) по сохранённым ответам getAccountInfo (alt_*.json) и/или подставным значениям."""

    def __init__(self, values: dict | None = None, slot: int | None = None, real: bool = True):
        self.values, self.calls = {}, []
        self.slot = slot
        if real:
            for f in glob.glob(str(DATA / "alt_*.json")):
                d = json.loads(Path(f).read_text())
                self.values[d["params"][0]] = (d["response"]["result"]["value"], d["response"]["result"]["context"]["slot"])
        for k, v in (values or {}).items():
            self.values[k] = (v, slot or 1000)

    def multiple_accounts(self, keys, encoding="base64", commitment="confirmed"):
        assert encoding == "base64"
        self.calls.append(tuple(keys))
        vals = [self.values[k][0] if k in self.values else None for k in keys]
        slot = self.slot if self.slot is not None else max([self.values[k][1] for k in keys if k in self.values] or [0])
        return vals, slot


def alt_value(addresses, *, deact=U64_MAX, last_ext=0, start=0, authority=None, owner=ALT_PROGRAM, typ=1) -> dict:
    meta = struct.pack("<IQQB", typ, deact, last_ext, start)
    meta += (b"\1" + b58.pubkey_bytes(authority)) if authority else b"\0" * 33
    meta += b"\0\0"
    raw = meta + b"".join(b58.pubkey_bytes(a) for a in addresses)
    return {"data": [base64.b64encode(raw).decode(), "base64"], "owner": owner, "executable": False,
            "lamports": 1_000_000, "rentEpoch": U64_MAX, "space": len(raw)}


def snap(address: str, addresses, slot: int = 1000) -> M.AltSnapshot:
    return M.parse_alt(address, alt_value(addresses), slot)


# --- инструкции (program_id, [(key, signer, writable)], data) --------------------------------------------------------
def cu_limit(units: int):
    return (COMPUTE_BUDGET_PROGRAM, [], b"\x02" + struct.pack("<I", units))


def cu_price(micro: int):
    return (COMPUTE_BUDGET_PROGRAM, [], b"\x03" + struct.pack("<Q", micro))


def ata_create(account=ANSEM_ATA, *, payer=WALLET, owner=WALLET, mint=ANSEM_MINT, prog=TOKEN_2022_PROGRAM, tag=b"\x01"):
    return (ATA_PROGRAM, [(payer, True, True), (account, False, True), (owner, False, False), (mint, False, False),
                          (SYSTEM_PROGRAM, False, False), (prog, False, False)], tag)


def sys_transfer(src, dst, lamports):
    return (SYSTEM_PROGRAM, [(src, True, True), (dst, False, True)], struct.pack("<IQ", 2, lamports))


def token_ix(tag: int, metas, extra: bytes = b"", prog=TOKEN_PROGRAM):
    return (prog, metas, bytes([tag]) + extra)


def route_v2(*, in_amount=AMOUNT_IN, quoted=QUOTED, slip=SLIP, pfee=0, pos=0, authority=WALLET, src=USDC_ATA,
             dst=ANSEM_ATA, src_mint=USDC_MINT, dst_mint=ANSEM_MINT, src_prog=TOKEN_PROGRAM, dst_prog=TOKEN_2022_PROGRAM,
             dest_opt=JUPITER_PROGRAM, plan=None, remaining=None, name="route_v2"):
    data = encode_args(jupiter_idl(), name, {
        "in_amount": in_amount, "quoted_out_amount": quoted, "slippage_bps": slip, "platform_fee_bps": pfee,
        "positive_slippage_bps": pos, "route_plan": plan if plan is not None else real_route_plan()})
    metas = [(authority, True, False), (src, False, True), (dst, False, True), (src_mint, False, False),
             (dst_mint, False, False), (src_prog, False, False), (dst_prog, False, False), (dest_opt, False, True),
             (JUP_EVENT, False, False), (JUPITER_PROGRAM, False, False)]
    rem = remaining if remaining is not None else [(p, False, True) for p in POOL[:4]] + [(authority, True, False),
                                                                                     (src, False, True)]
    return (JUPITER_PROGRAM, metas + rem, data)


def shared_route_v2(*, rid=6, in_amount=AMOUNT_IN, quoted=QUOTED, slip=SLIP, pfee=0, pos=0, dst=ANSEM_ATA,
                    prog_src=None, prog_dst=None, authority_pda=None):
    data = encode_args(jupiter_idl(), "shared_accounts_route_v2", {
        "id": rid, "in_amount": in_amount, "quoted_out_amount": quoted, "slippage_bps": slip, "platform_fee_bps": pfee,
        "positive_slippage_bps": pos, "route_plan": real_route_plan()})
    pa = authority_pda or pda([b"authority", bytes([rid])], JUPITER_PROGRAM)
    metas = [(pa, False, False), (WALLET, True, False), (USDC_ATA, False, True),
             (prog_src or ata(pa, USDC_MINT, TOKEN_PROGRAM), False, True),
             (prog_dst or ata(pa, ANSEM_MINT, TOKEN_2022_PROGRAM), False, True), (dst, False, True),
             (USDC_MINT, False, False), (ANSEM_MINT, False, False), (TOKEN_PROGRAM, False, False),
             (TOKEN_2022_PROGRAM, False, False), (JUP_EVENT, False, False), (JUPITER_PROGRAM, False, False)]
    return (JUPITER_PROGRAM, metas + [(p, False, True) for p in POOL[:3]], data)


def okx_swap(*, name="swap_v3", amount_in=AMOUNT_IN, expect=QUOTED, min_return=MIN_OUT, amounts=None, commission=0,
             pfee=0, trim=0, src=USDC_ATA, dst=ANSEM_ATA, commission_acc=OKX_ROUTER, pfee_acc=OKX_ROUTER, payer=WALLET):
    args = {"amount_in": amount_in, "expect_amount_out": expect, "min_return": min_return,
            "amounts": amounts if amounts is not None else [amount_in],
            "routes": [[{"dexes": [{"PumpfunammBuy": None}], "weights": bytes([100])}]]}
    a = {"args": args, "commission_info": commission, "platform_fee_rate": pfee, "order_id": 7}
    if name.startswith("swap_tob"):
        a["trim_rate"] = trim
    if name == "swap_tob_v3_enhanced":
        a["charge_rate"] = 0
    data = encode_args(okx_idl(), name, a)
    sa = pda([b"okx_sa"], OKX_ROUTER)
    metas = [(payer, True, True), (src, False, True), (dst, False, True), (USDC_MINT, False, False),
             (ANSEM_MINT, False, False), (commission_acc, False, True), (pfee_acc, False, True), (sa, False, True),
             (ata(sa, USDC_MINT, TOKEN_PROGRAM), False, True), (ata(sa, ANSEM_MINT, TOKEN_2022_PROGRAM), False, True),
             (TOKEN_PROGRAM, False, False), (TOKEN_2022_PROGRAM, False, False), (ATA_PROGRAM, False, False),
             (SYSTEM_PROGRAM, False, False)]
    if name == "swap_v3_with_cpi_event":
        metas += [(pda([b"__event_authority"], OKX_ROUTER), False, False), (OKX_ROUTER, False, False)]
    return (OKX_ROUTER, metas + [(p, False, True) for p in POOL[:3]], data)


def std_ixs(router=None):
    return [cu_limit(300_000), cu_price(100_000), ata_create(), router or route_v2()]


def build(ixs, alts=(), payer=WALLET, blockhash=BH) -> bytes:
    return M.SoldersMessageBuilder().build_v0(payer=payer, instructions=ixs, recent_blockhash=blockhash, alts=alts)


def resolved(raw: bytes, alts=()) -> M.ResolvedMessage:
    return M.resolve_with(wire.parse_message(raw), {s.address: s for s in alts})


def intent(**over) -> SwapIntent:
    base = dict(wallet=WALLET, input_mint=USDC_MINT, input_program=TOKEN_PROGRAM, input_account=USDC_ATA,
                output_mint=ANSEM_MINT, output_program=TOKEN_2022_PROGRAM, output_account=ANSEM_ATA,
                amount_in_raw=AMOUNT_IN, min_out_raw=MIN_OUT, cu_limit_cap=None, cu_price_cap_micro_lamports=None,
                tip_cap_lamports=None, native_budget_lamports=5_000_000, max_network_fee_lamports=200_000,
                account_rent=((ANSEM_ATA, ANSEM_RENT), (USDC_ATA, 0)))
    base.update(over)
    return SwapIntent(**base)


# --- счета для симуляции ---------------------------------------------------------------------------------------------
def token_account(mint, owner, amount, *, program=TOKEN_PROGRAM, delegate=None, close=None, lamports=2_039_280) -> dict:
    def copt(k):
        return struct.pack("<I", 1) + b58.pubkey_bytes(k) if k else b"\0" * 36
    raw = (b58.pubkey_bytes(mint) + b58.pubkey_bytes(owner) + struct.pack("<Q", amount) + copt(delegate) + b"\x01"
           + b"\0" * 12 + struct.pack("<Q", 0) + copt(close))
    assert len(raw) == 165
    if program == TOKEN_2022_PROGRAM:
        raw += b"\x02" + struct.pack("<HH", 7, 0)            # AccountType::Account + ImmutableOwner (TLV, пусто)
    return {"data": [base64.b64encode(raw).decode(), "base64"], "owner": program, "executable": False,
            "lamports": lamports, "rentEpoch": U64_MAX, "space": len(raw)}


def wallet_account(lamports: int) -> dict:
    return {"data": ["", "base64"], "owner": SYSTEM_PROGRAM, "executable": False, "lamports": lamports,
            "rentEpoch": U64_MAX, "space": 0}


def parsed(program: str, typ: str, info: dict, stack=2) -> dict:
    pid = {"spl-token": TOKEN_PROGRAM, "spl-token-2022": TOKEN_2022_PROGRAM, "system": SYSTEM_PROGRAM}[program]
    return {"parsed": {"type": typ, "info": info}, "program": program, "programId": pid, "stackHeight": stack}


def good_sim(*, spent=AMOUNT_IN, got=MIN_OUT + 5, drop=100_000, units=150_000, extra_inner=(), err=None,
             out_pre=None, input_pre=AMOUNT_IN * 2, wallet_pre=50_000_000, router_index=3):
    """(sim, pre) — успешный своп: списан вход, получен выход, одна вложенная пара переводов."""
    pre = {WALLET: wallet_account(wallet_pre), USDC_ATA: token_account(USDC_MINT, WALLET, input_pre),
           ANSEM_ATA: out_pre}
    post = [wallet_account(wallet_pre - drop), token_account(USDC_MINT, WALLET, input_pre - spent),
            token_account(ANSEM_MINT, WALLET, got, program=TOKEN_2022_PROGRAM)]
    inner = [{"index": router_index, "instructions": [
        parsed("spl-token", "transferChecked", {"source": USDC_ATA, "destination": POOL[0], "authority": WALLET,
                                                "mint": USDC_MINT, "tokenAmount": {"amount": str(spent)}}),
        {"programId": POOL[5], "accounts": [POOL[0], POOL[1]], "data": "3Bxs", "stackHeight": 2},
        parsed("spl-token-2022", "transferChecked", {"source": POOL[1], "destination": ANSEM_ATA, "authority": POOL[2],
                                                     "mint": ANSEM_MINT, "tokenAmount": {"amount": str(got)}}),
        *extra_inner]}]
    sim = {"context_slot": 2000, "err": err, "logs": ["Program log: ok"], "accounts": post, "unitsConsumed": units,
           "innerInstructions": inner, "returnData": None}
    return sim, pre
