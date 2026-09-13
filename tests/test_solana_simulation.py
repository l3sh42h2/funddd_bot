"""Эффекты simulateTransaction до подписи (S13; read_solana §2.3; SOLANA_ROUTERS §6).

Настоящие ответы — симуляция mainnet-свопов ANSEM публичным RPC 13.09 (sim_jup_buy.json — успех с вложенными
инструкциями jsonParsed и частично разобранными; sim_ata_create.json — ошибка). Синтетика — sol_tx_helpers.good_sim."""
import base64, json, struct
import pytest
from funding_bot.trade.solana import SYSTEM_PROGRAM, TOKEN_PROGRAM, USDC_MINT, b58
from funding_bot.trade.solana import message as M
from funding_bot.trade.solana import validate as V
import sol_tx_helpers as H

MV = V.ManifestValidator()
RM = H.resolved(H.build(H.std_ixs()))
IT = H.intent()
PLAN = MV.validate(RM, IT)


def sim_check(sim, pre, it=IT, plan=PLAN, msg=RM):
    return MV.check_simulation(sim, it, pre, msg=msg, plan=plan)


def test_good_simulation_passes_and_reports():
    assert PLAN.ok
    v = sim_check(*H.good_sim())
    assert v.ok and v.reasons == () and v.message_hash == PLAN.message_hash
    assert v.details["input_spent"] == H.AMOUNT_IN and v.details["output_received"] == H.MIN_OUT + 5
    assert v.details["nested_programs"] == [H.POOL[5]]                     # AMM вне allowlist — наблюдение, не отказ
    assert V.sim_accounts(IT) == (H.WALLET, H.USDC_ATA, H.ANSEM_ATA)


# --- недоступная симуляция — не успех; ошибка — отдельно от «нет ликвидности» ---------------------------------------
@pytest.mark.parametrize("sim,reason", [
    (None, "simulation_unavailable"), ({}, "simulation_unavailable"), ("oops", "simulation_unavailable"),
])
def test_s13_unavailable_is_not_success(sim, reason):
    assert sim_check(sim, {}).reasons == (reason,)


def test_s13_missing_accounts_or_inner():
    sim, pre = H.good_sim()
    assert "simulation_accounts_missing" in sim_check(dict(sim, accounts=sim["accounts"][:2]), pre).reasons
    assert "simulation_inner_missing" in sim_check(dict(sim, innerInstructions=None), pre).reasons
    assert "simulation_replaced_blockhash" in sim_check(dict(sim, replacementBlockhash={"blockhash": H.BH}), pre).reasons


def test_s13_error_classified_not_liquidity():
    sim, pre = H.good_sim(err={"InstructionError": [3, {"Custom": 6001}]})
    assert sim_check(sim, pre).reasons == ("simulation_failed:min_out_not_reached",)
    sim, pre = H.good_sim(err={"InstructionError": [2, {"Custom": 6001}]})     # не роутер — общий код
    r = sim_check(sim, pre).reasons
    assert r[0].startswith("simulation_failed:{") and "no_route" not in r[0]
    real = json.loads((H.DATA / "sim_ata_create.json").read_text())["sim"]["response"]["result"]["value"]
    assert sim_check(real, {}).reasons == ('simulation_failed:"AccountNotFound"',)


def test_s13_static_failure_propagates():
    bad_plan = MV.validate(RM, H.intent(min_out_raw=H.MIN_OUT + 1))
    assert "static_validation_failed" in sim_check(*H.good_sim(), plan=bad_plan).reasons


# --- существенное отличие расхода/получателя --------------------------------------------------------------------------
def test_s13_output_below_min_or_to_foreign_owner():
    assert "sim_output_below_min" in sim_check(*H.good_sim(got=H.MIN_OUT - 1)).reasons
    sim, pre = H.good_sim()
    sim["accounts"][2] = H.token_account(H.ANSEM_MINT, H.FOREIGN, H.MIN_OUT + 5, program=H.TOKEN_2022_PROGRAM)
    assert "sim_output_account" in sim_check(sim, pre).reasons
    sim["accounts"][2] = None
    assert "sim_output_missing" in sim_check(sim, pre).reasons


def test_s13_existing_output_balance_not_counted_as_fill():
    pre_out = H.token_account(H.ANSEM_MINT, H.WALLET, 100, program=H.TOKEN_2022_PROGRAM)
    v = sim_check(*H.good_sim(out_pre=pre_out, got=H.MIN_OUT + 50))
    assert "sim_output_below_min" in v.reasons and v.details["output_received"] == H.MIN_OUT - 50


def test_s13_input_overspent_delegate_or_closed():
    assert "sim_input_overspent" in sim_check(*H.good_sim(spent=H.AMOUNT_IN + 1)).reasons
    sim, pre = H.good_sim()
    sim["accounts"][1] = H.token_account(USDC_MINT, H.WALLET, H.AMOUNT_IN, delegate=H.FOREIGN)
    assert "sim_input_authority" in sim_check(sim, pre).reasons
    sim["accounts"][1] = H.token_account(USDC_MINT, H.WALLET, H.AMOUNT_IN, close=H.FOREIGN)
    assert "sim_input_authority" in sim_check(sim, pre).reasons
    sim["accounts"][1] = None
    assert "sim_input_closed" in sim_check(sim, pre).reasons


def test_s13_wallet_spend_over_plan_and_protected_changed():
    assert "sim_native_spend" in sim_check(*H.good_sim(drop=PLAN.details["native_total"] + 1)).reasons
    prot = H.key("our-bonk-ata")
    it = H.intent(protected_accounts=(prot,))
    plan = MV.validate(RM, it)
    sim, pre = H.good_sim()
    pre[prot] = H.token_account(H.key("bonk"), H.WALLET, 7)
    sim["accounts"].append(H.token_account(H.key("bonk"), H.WALLET, 6))
    assert f"sim_protected_changed:{prot}" in sim_check(sim, pre, it=it, plan=plan).reasons
    sim["accounts"][3] = pre[prot]
    assert sim_check(sim, pre, it=it, plan=plan).ok


# --- вложенные инструкции (CPI) ---------------------------------------------------------------------------------------
def test_s13_nested_approve_or_set_authority_by_wallet():
    ap = H.parsed("spl-token", "approve", {"source": H.USDC_ATA, "delegate": H.FOREIGN, "owner": H.WALLET,
                                             "amount": "1"})
    assert "inner_approve" in sim_check(*H.good_sim(extra_inner=[ap])).reasons
    sa = H.parsed("spl-token-2022", "setAuthority", {"account": H.ANSEM_ATA, "authorityType": "accountOwner",
                                                      "newAuthority": H.FOREIGN, "authority": H.WALLET})
    assert "inner_setAuthority" in sim_check(*H.good_sim(extra_inner=[sa])).reasons
    close = H.parsed("spl-token", "closeAccount", {"account": H.USDC_ATA, "destination": H.FOREIGN,
                                                   "owner": H.WALLET})
    assert "inner_closeAccount" in sim_check(*H.good_sim(extra_inner=[close])).reasons


def test_s13_nested_transfer_from_other_wallet_account():
    tr = H.parsed("spl-token-2022", "transferChecked", {"source": H.ANSEM_ATA, "destination": H.FOREIGN,
                                                        "authority": H.WALLET, "tokenAmount": {"amount": "5"}})
    assert f"inner_transfer_from_wallet:{H.ANSEM_ATA}" in sim_check(*H.good_sim(extra_inner=[tr])).reasons
    more = H.parsed("spl-token", "transfer", {"source": H.USDC_ATA, "destination": H.FOREIGN, "authority": H.WALLET,
                                              "amount": "1"})
    assert "inner_transfer_over_amount" in sim_check(*H.good_sim(extra_inner=[more])).reasons


def test_s13_nested_raw_forms_decoded():
    """Частично разобранная (адреса) и компилированная (индексы) формы: Token/System разбираются из байтов."""
    approve = b58.b58encode(bytes([4]) + struct.pack("<Q", 10 ** 9))
    partial = {"programId": TOKEN_PROGRAM, "accounts": [H.USDC_ATA, H.FOREIGN, H.WALLET], "data": approve,
               "stackHeight": 3}
    assert "inner_approve" in sim_check(*H.good_sim(extra_inner=[partial])).reasons
    keys = RM.keys
    compiled = {"programIdIndex": keys.index(TOKEN_PROGRAM),
                "accounts": [keys.index(H.USDC_ATA), keys.index(H.WALLET), keys.index(H.WALLET)],
                "data": approve, "stackHeight": 2}
    assert "inner_approve" in sim_check(*H.good_sim(extra_inner=[compiled])).reasons
    assert "inner_unparsed_ours:compiled_without_keys" not in sim_check(*H.good_sim(extra_inner=[compiled])).reasons
    assign = b58.b58encode(struct.pack("<I", 1) + b58.pubkey_bytes(H.FOREIGN))
    sysx = {"programId": SYSTEM_PROGRAM, "accounts": [H.WALLET], "data": assign, "stackHeight": 2}
    assert "inner_system_on_wallet" in sim_check(*H.good_sim(extra_inner=[sysx])).reasons
    garbage = {"programId": TOKEN_PROGRAM, "accounts": [H.USDC_ATA, H.WALLET], "data": b58.b58encode(b"\xee"),
               "stackHeight": 2}
    assert "inner_unparsed_ours:token_tag:238" in sim_check(*H.good_sim(extra_inner=[garbage])).reasons


def test_s13_nested_sol_to_foreign_only_net_leak_refused():
    """SEC-1 (осознанная правка ожиданий 13.09): SOL маршрута на чужой счёт меряется чистым уходом (просадка кошелька −
    план), а не суммой переводов и не «в пределах бюджета»: вернулся в той же транзакции — не расход; ушёл сверх
    плана — отказ с получателем, даже если бюджет (сеть + кап rent) позволяет."""
    rent = H.parsed("system", "transfer", {"source": H.WALLET, "destination": H.key("amm-user-acc"),
                                           "lamports": 1_346_200})
    v = sim_check(*H.good_sim(extra_inner=[rent]))                          # просадка 100 000 ≤ плана: вернулся
    assert v.ok and v.details["sol_out_foreign"] == 1_346_200 and v.details["sol_out_foreign_net"] == 0
    it = H.intent(native_budget_lamports=PLAN.details["native_total"] + 1_346_200 * 2)
    plan = MV.validate(RM, it)
    leak = sim_check(*H.good_sim(extra_inner=[rent], drop=PLAN.details["native_total"] + 1_346_200), it=it, plan=plan)
    assert leak.reasons == (f"route_foreign_sol:{H.key('amm-user-acc')}",)
    assert leak.details["sol_out_foreign_net"] == 1_346_200
    own = H.parsed("system", "createAccount", {"source": H.WALLET, "newAccount": H.ANSEM_ATA, "lamports": H.ANSEM_RENT,
                                               "space": 170, "owner": H.TOKEN_2022_PROGRAM})
    assert sim_check(*H.good_sim(extra_inner=[own]), it=it, plan=plan).details["sol_out_foreign"] == 0


def test_s13_fee_and_units_against_plan():
    sim, pre = H.good_sim()
    assert "sim_fee_over_plan" in sim_check(dict(sim, fee=PLAN.details["fee_lamports"] + 1), pre).reasons
    assert "sim_units_over_limit" in sim_check(*H.good_sim(units=300_001)).reasons


def test_s13_real_simulation_of_mainnet_swap():
    """Настоящий simulateTransaction свопа USDC→ANSEM: из выхода кошелька ушла доля positive slippage, SOL на счёт
    AMM вне котировки, blockhash подменён узлом — отказ с понятными причинами, AMM-программы записаны."""
    doc = json.loads((H.DATA / "sim_jup_buy.json").read_text())
    sim = doc["sim"]["response"]["result"]["value"]
    pre = dict(zip(doc["addresses"], doc["pre"]["response"]["result"]["value"]))
    rm = M.RpcMessageResolver(M.AltCache(H.AltRpc())).resolve(H.fixture_tx("tx_jup_buy_b64.json"))
    it = H.intent(wallet=doc["addresses"][0], input_account=doc["addresses"][1], output_account=doc["addresses"][2],
                  amount_in_raw=50_000_000, min_out_raw=301_447_789, account_rent=(), native_budget_lamports=200_000)
    plan = MV.validate(rm, it)
    v = MV.check_simulation(sim, it, pre, msg=rm, plan=plan)
    assert "static_validation_failed" in v.reasons and "simulation_replaced_blockhash" in v.reasons
    assert f"inner_transfer_from_wallet:{doc['addresses'][2]}" in v.reasons
    # SEC-1 (осознанная правка 13.09): 1 346 200 на счёт AMM возвращаются в той же транзакции (промежуточный SOL
    # маршрута) — кошелёк потерял ровно план (сеть 20 304): чистого ухода нет, это не расход и не отказ
    assert v.details["sol_out_foreign"] == 1_346_200 and v.details["sol_out_foreign_net"] == 0
    assert not [x for x in v.reasons if x.startswith(("sim_native_spend", "route_foreign_sol"))]
    assert v.details["input_spent"] == 50_000_000 and v.details["inner_spent"] == 50_000_000
    assert v.details["wallet_lamports_drop"] == 20_304 and "pAMMBay6oceH9fJKBRHGP5D4bD4sWpmSwMn52FMfXEA" in v.details["nested_programs"]


def test_refusing_simulation_default():
    assert not V.REFUSING.check_simulation({}, IT, {}).ok
