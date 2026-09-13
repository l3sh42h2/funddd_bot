"""Мост роутер → Solana-ядро с solders: сборка, полное сообщение, манифест, эффекты симуляции, итог для подписи.
(R09, R14, S10–S13 на уровне PayloadValidator/Simulator/TxAssembler роутера.)"""
import base64, dataclasses
from decimal import Decimal
import pytest
from funding_bot.trade import sol_route_validator as rv
from funding_bot.trade import spot_router as sr
from funding_bot.trade.solana import MAINNET_GENESIS, wire
from funding_bot.trade.solana import message as M
from funding_bot.trade.solana import validate as V
from funding_bot.trade.solana.decoders import JUPITER_PROGRAM, OKX_ROUTER
import sol_tx_helpers as H

USDC = sr.AssetRef(H.USDC_MINT, H.TOKEN_PROGRAM, 6, "USDC")
ANSEM = sr.AssetRef(H.ANSEM_MINT, H.TOKEN_2022_PROGRAM, 6, "ANSEM")
LIM = sr.RouteLimits(max_network_fee_lamports_per_tx=200_000, max_tip_lamports_per_tx=None)


def req(**kw):
    base = dict(side="entry", input=USDC, output=ANSEM, amount_in_raw=H.AMOUNT_IN, wallet=H.WALLET, slippage_bps=H.SLIP,
                genesis_hash=MAINNET_GENESIS, deadline_mono=1e12, input_account=H.USDC_ATA, output_account=H.ANSEM_ATA,
                account_rent=((H.ANSEM_MINT, H.ANSEM_RENT), (H.USDC_MINT, 0)))
    base.update(kw)
    return sr.QuoteRequest(**base)


def cand(r, **kw):
    base = dict(provider="jupiter", path="jupiter_build_v2", adapter_version="t", request_hash=r.request_hash,
                side="entry", input_mint=H.USDC_MINT, output_mint=H.ANSEM_MINT, input_program=H.TOKEN_PROGRAM,
                output_program=H.TOKEN_2022_PROGRAM, input_decimals=6, output_decimals=6, amount_in_raw=H.AMOUNT_IN,
                expected_out_raw=H.QUOTED, min_out_raw=H.MIN_OUT, onchain_min_out_raw=H.MIN_OUT)
    base.update(kw)
    return sr.SwapCandidate(**base)


def to_ix(t):
    pid, metas, data = t
    return sr.Ix(pid, tuple(sr.AccountMeta(k, s, w) for k, s, w in metas), data)


class SimRpc:
    """SolanaRpc-подобный: getMultipleAccounts (pre) и simulateTransaction по сценарию."""

    def __init__(self, sim_pre=None, alt=None):
        self.sim, self.pre = sim_pre or H.good_sim()
        self.alt = alt or H.AltRpc(real=False)
        self.sim_calls = []

    def multiple_accounts(self, keys, encoding="base64", commitment="confirmed"):
        if all(k in self.alt.values for k in keys):
            return self.alt.multiple_accounts(keys, encoding, commitment)
        return [self.pre.get(k) for k in keys], 1999

    def simulate(self, tx_b64, **kw):
        self.sim_calls.append(kw)
        assert kw["sig_verify"] is False and kw["replace_recent_blockhash"] is False
        return self.sim


def wired(rpc, **kw):
    cache = M.AltCache(rpc)
    effects = rv.EffectsSimulator(rpc)
    v = rv.RouteTxValidator(M.RpcMessageResolver(cache), V.ManifestValidator(), limits=LIM,
                            native_budget_lamports=5_000_000, effects=effects, **kw)
    return rv.SoldersAssembler(cache), effects, v


def assemble(asm, router=None, alts=(), cu=300_000):
    ixs = [to_ix(x) for x in (H.cu_price(100_000), H.ata_create(), router or H.route_v2())]
    return asm.assemble(payer=H.WALLET, ixs=ixs, alts=alts, recent_blockhash=H.BH, last_valid_block_height=500,
                        cu_limit=cu)


def test_full_path_ok_one_simulation_and_results_for_signing():
    rpc = SimRpc()
    asm, sim, v = wired(rpc)
    r = req()
    tx = assemble(asm)
    assert v.validate(tx, cand(r), r) == ()
    s = sim.simulate(tx)                                   # адаптер после validate: та же симуляция из кэша
    assert s.ok and s.units_consumed == 150_000 and len(rpc.sim_calls) == 1
    assert rpc.sim_calls[0]["accounts"] == [H.WALLET, H.USDC_ATA, H.ANSEM_ATA] and rpc.sim_calls[0]["inner"] is True
    st, ef = v.results_for(tx.message_hash)
    assert st.ok and ef.ok and st.message_hash == ef.message_hash == tx.message_hash
    assert isinstance(v, sr.PayloadValidator) and isinstance(sim, sr.Simulator) and isinstance(asm, sr.TxAssembler)


def test_intent_takes_strictest_min_out_and_provider_router():
    r = req()
    it, why = rv.swap_intent(cand(r, min_out_raw=H.MIN_OUT + 7, onchain_min_out_raw=H.MIN_OUT), r, LIM,
                             cu_limit_cap=None, cu_price_cap_micro_lamports=None, native_budget_lamports=1)
    assert why == () and it.min_out_raw == H.MIN_OUT + 7 and it.router_program == JUPITER_PROGRAM
    assert dict(it.account_rent) == {H.ANSEM_ATA: H.ANSEM_RENT, H.USDC_ATA: 0}
    assert it.max_network_fee_lamports == 200_000 and it.tip_cap_lamports is None
    it, _ = rv.swap_intent(cand(r, provider="okx", path="okx_solana_v6"), r, LIM, cu_limit_cap=None,
                           cu_price_cap_micro_lamports=None, native_budget_lamports=1)
    assert it.router_program == OKX_ROUTER


def test_r09_json_promised_more_than_message_holds():
    rpc = SimRpc()
    asm, _, v = wired(rpc)
    r = req()
    tx = assemble(asm, H.route_v2(slip=200))              # собранная инструкция держит порог ниже JSON
    assert "ix_min_out" in v.validate(tx, cand(r), r)
    assert rpc.sim_calls == []                             # до симуляции и подписи не дошло


def test_r14_tip_only_to_policy_recipients():
    tip_to = H.key("tip-account")
    rpc = SimRpc()
    asm, _, v = wired(rpc)
    r = req()
    ixs = [to_ix(x) for x in (H.cu_price(100_000), H.ata_create(), H.route_v2(), H.sys_transfer(H.WALLET, tip_to, 10_000))]
    tx = asm.assemble(payer=H.WALLET, ixs=ixs, alts=(), recent_blockhash=H.BH, last_valid_block_height=500,
                      cu_limit=300_000)
    assert f"system_transfer:{tip_to}" in v.validate(tx, cand(r), r)
    lim = dataclasses.replace(LIM, max_tip_lamports_per_tx=10_000)
    v2 = rv.RouteTxValidator(v.resolver, v.validator, limits=lim, native_budget_lamports=5_000_000,
                             tip_recipients=(tip_to,), effects=rv.EffectsSimulator(rpc))
    assert v2.validate(tx, cand(r), r) == ()


def test_s13_effects_refusal_and_unavailable():
    sim, pre = H.good_sim(got=H.MIN_OUT - 1)
    asm, effects, v = wired(SimRpc((sim, pre)))
    r = req()
    tx = assemble(asm)
    assert "sim_output_below_min" in v.validate(tx, cand(r), r) and v.results_for(tx.message_hash) is None
    assert not effects.simulate(tx).ok                     # результат той же симуляции: эффекты не прошли
    no_eff = rv.RouteTxValidator(v.resolver, v.validator, limits=LIM, native_budget_lamports=5_000_000)
    assert no_eff.validate(tx, cand(r), r) == ("simulation_effects_unavailable",)

    class Down(SimRpc):
        def simulate(self, tx_b64, **kw):
            raise TimeoutError("rpc")
    asm2, _, v3 = wired(Down())
    tx2 = assemble(asm2)
    assert v3.validate(tx2, cand(r), r) == ("simulation_unavailable:TimeoutError",)


def test_assembler_cu_limit_once_and_alt_from_network():
    t = H.key("alt-asm")
    alt = H.AltRpc({t: H.alt_value(H.POOL + [H.ANSEM_ATA])}, slot=1000, real=False)
    rpc = SimRpc(alt=alt)
    asm, _, v = wired(rpc)
    tx = assemble(asm, alts=((t, ()),))
    m = wire.parse_message(tx.payload.raw_bytes())
    assert m.lookups and m.lookups[0].table == t and tx.cu_limit == 300_000
    cb = [ix for ix in m.instructions if m.static_keys[ix.program_index] == sr.COMPUTE_BUDGET_PROGRAM]
    assert [ix.data[0] for ix in cb].count(2) == 1
    r = req()
    assert v.validate(tx, cand(r), r) == ()
    with pytest.raises(sr.PayloadError):                   # провайдер «прислал» другое содержимое таблицы
        assemble(asm, alts=((t, (H.FOREIGN,)),))
    with pytest.raises(sr.PayloadError):                   # второй SetComputeUnitLimit не добавляем
        asm.assemble(payer=H.WALLET, ixs=[to_ix(H.cu_limit(1000)), to_ix(H.route_v2())], alts=(), recent_blockhash=H.BH,
                     last_valid_block_height=1, cu_limit=5000)


def test_wrap_real_order_like_tx_refused_for_second_signer():
    raw = base64.b64encode(H.fixture_tx("tx_jup_buy_b64.json").raw).decode()
    rpc = SimRpc(alt=H.AltRpc())
    asm, _, v = wired(rpc)
    tx = asm.wrap(sr.Payload("tx", "base64", data=raw), None)
    assert tx.signers[1] == "sighWH8KaiT7QhtV4w29ReVF8kG6D5yG3EQP1KYyGVF" and tx.cu_limit == 309_098
    r = req(wallet="EH1KQLnYQoJUn4ofX1TKPiagtFbtEsrE3ChkY24eRXae",
            input_account="68n6nUMcaSYEFxXJmvuC5K117WVKxKaCRvfoSgu1UBJA",
            output_account="4WNw6fF5eGdep7nroX9oAqcZCDdSEiT4EnF7ZiNiy8k8", amount_in_raw=50_000_000)
    why = v.validate(tx, cand(r, amount_in_raw=50_000_000, min_out_raw=301_447_789, onchain_min_out_raw=None), r)
    assert "extra_signer:sighWH8KaiT7QhtV4w29ReVF8kG6D5yG3EQP1KYyGVF" in why and "positive_slippage" in why


def test_resolver_errors_are_reasons_not_crashes():
    t = H.key("alt-missing")
    rpc = SimRpc(alt=H.AltRpc({t: H.alt_value(H.POOL + [H.ANSEM_ATA])}, slot=1000, real=False))
    asm, _, v = wired(rpc)
    tx = assemble(asm, alts=((t, ()),))
    r = req()
    v.resolver.alts._rows.clear()
    rpc.alt.values[t] = (H.alt_value(H.POOL + [H.ANSEM_ATA], deact=5), 1000)
    assert v.validate(tx, cand(r), r) == ("alt_resolver_error:alt_deactivated",)


def test_solana_tools_factory():
    rpc = SimRpc()
    tools, v = rv.solana_tools(rpc, object(), limits=LIM, policy=sr.RoutingPolicy(), native_budget_lamports=5_000_000)
    assert tools.can_build() and tools.validator is v and isinstance(tools.simulator, rv.EffectsSimulator)
    r = req()
    tx = tools.assembler.assemble(payer=H.WALLET, ixs=[to_ix(x) for x in (H.ata_create(), H.route_v2())], alts=(),
                                  recent_blockhash=H.BH, last_valid_block_height=1, cu_limit=300_000)
    assert v.validate(tx, cand(r), r) == ()
