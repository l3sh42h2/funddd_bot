"""Проверка до подписи по полному сообщению (S10, S11, S12, R09, R14; SOLANA_ROUTERS §6 и §8 п.3, п.10).

Синтетика — сообщения solders из инструкций по on-chain IDL (sol_tx_helpers); реальные — mainnet-свопы ANSEM через
Jupiter (tests/data/solana): там сама жизнь дала «плохие» случаи — чужой получатель и platform fee (tx_ata_create),
второй подписант и positive slippage (tx_jup_buy), durable nonce и V1-маршрут (tx_jup_sell)."""
import dataclasses, struct
import pytest
from funding_bot.trade.solana import (ANSEM_MINT, NATIVE_MINT, SYSTEM_PROGRAM, TOKEN_2022_PROGRAM, TOKEN_PROGRAM,
                                      USDC_MINT)
from funding_bot.trade.solana import message as M
from funding_bot.trade.solana import validate as V
from funding_bot.trade.solana.borsh import BorshError, IdlDecoder, encode_args
from funding_bot.trade.solana.decoders import JUPITER_PROGRAM, OKX_ROUTER, jupiter_idl, okx_idl
import sol_tx_helpers as H

MV = V.ManifestValidator()


def check(ixs, it=None, alts=(), payer=H.WALLET):
    return MV.validate(H.resolved(H.build(ixs, alts=alts, payer=payer), alts), it or H.intent())


def test_positive_paths_all_routers():
    for r in (H.route_v2(), H.shared_route_v2(), H.okx_swap(), H.okx_swap(name="swap_v3_with_cpi_event"),
              H.okx_swap(name="swap_tob_v3")):
        v = check(H.std_ixs(r))
        assert v.ok and v.reasons == () and v.manifest_version.startswith("sol_tx_manifest_v1+jup:a31edf")
    v = check(H.std_ixs())
    d = v.details
    assert d["router"] == "jupiter:route_v2" and d["onchain_min_out"] == H.MIN_OUT
    assert d["fee_lamports"] == 5000 + 30_000 and d["rent_lamports"] == H.ANSEM_RENT
    assert d["native_total"] == 35_000 + H.ANSEM_RENT and v.message_hash


# --- S10: программа, получатель, подписант; подготовка счетов и оплата — тоже ---------------------------------------
def test_s10_unknown_program_rejected():
    memo = ("MemoSq4gqABAXKb96qnH8TysNcWxMyWCqXgDLGmfcHr", [(H.WALLET, True, False)], b"hi")
    v = check(H.std_ixs() + [memo])
    assert not v.ok and any(r.startswith("ix_not_in_manifest:4:MemoSq4g") for r in v.reasons)
    other = (H.key("evil-program"), [(H.USDC_ATA, False, True)], b"\x01")
    assert any("program_unknown" in r for r in check(H.std_ixs() + [other]).reasons)


def test_s10_foreign_recipient_and_extra_signer():
    assert "ix_recipient" in check(H.std_ixs(H.route_v2(dst=H.FOREIGN))).reasons
    assert "ix_recipient" in check(H.std_ixs(H.route_v2(dest_opt=H.FOREIGN))).reasons      # optional получатель V2
    assert "ix_recipient" in check(H.std_ixs(H.shared_route_v2(dst=H.FOREIGN))).reasons
    assert "ix_recipient" in check(H.std_ixs(H.okx_swap(dst=H.FOREIGN))).reasons
    rem = [(p, False, True) for p in H.POOL[:3]] + [(H.key("rfq-maker"), True, False)]      # как JupiterZ RFQ
    v = check(H.std_ixs(H.route_v2(remaining=rem)))
    assert f"extra_signer:{H.key('rfq-maker')}" in v.reasons


def test_s10_preparation_and_fee_payment_checked():
    v = check([H.ata_create(owner=H.FOREIGN, account=H.ANSEM_ATA)] + H.std_ixs()[3:])
    assert "ata_owner" in v.reasons
    other_ata = H.ata_create(account=H.key("some-ata"))
    assert "ata_not_intent_account" in check([other_ata, H.route_v2()]).reasons
    payer_evil = H.ata_create(payer=H.FOREIGN)
    v = check([payer_evil, H.route_v2()])
    assert "ata_payer" in v.reasons and f"extra_signer:{H.FOREIGN}" in v.reasons
    wrong_prog = H.ata_create(prog=TOKEN_PROGRAM)                        # classic-ATA вместо Token-2022 (S07)
    assert "ata_mint_or_program" in check([wrong_prog, H.route_v2()]).reasons
    v = check(H.std_ixs(), payer=H.FOREIGN)                               # плательщик сети — не наш кошелёк
    assert "fee_payer" in v.reasons and f"extra_signer:{H.FOREIGN}" in v.reasons
    assert "rent_unknown:" + H.ANSEM_ATA in check(H.std_ixs(), H.intent(account_rent=())).reasons


def test_s10_real_ata_create_swap_to_foreign_account():
    """Настоящий mainnet-своп: ATA кошелька создан, но выход идёт на чужой счёт; внутри platform fee 85 бп."""
    res = M.RpcMessageResolver(M.AltCache(H.AltRpc()))
    rm = res.resolve(H.fixture_tx("tx_ata_create_b64.json"))
    wallet, own_ata = "Ep4TUukQdi2X2Z1mKH54vYKHfoPtnSTi3TSArvGPuydR", "EHWN9tUeAzWUjYPpkk69yeoLPRbtVh5hEFuX8o9xpigY"
    it = H.intent(wallet=wallet, input_mint="CASHx9KJUStyftLFWGvEVf59SGeG9sh5FfcnZMVPCASH",
                  input_program=TOKEN_2022_PROGRAM, input_account="FxuqW7exk6oxTJVvr652jY1mtyfwhRqdaASs4LsebgcH",
                  output_account=own_ata, amount_in_raw=1_447_784, min_out_raw=8_324_336, max_network_fee_lamports=10 ** 6,
                  account_rent=((own_ata, 2_074_080),))
    v = MV.validate(rm, it)
    assert "ix_recipient" in v.reasons and "platform_fee" in v.reasons and not v.ok


def test_s10_real_rfq_second_signer_and_positive_slippage():
    res = M.RpcMessageResolver(M.AltCache(H.AltRpc()))
    rm = res.resolve(H.fixture_tx("tx_jup_buy_b64.json"))
    it = H.intent(wallet="EH1KQLnYQoJUn4ofX1TKPiagtFbtEsrE3ChkY24eRXae",
                  input_account="68n6nUMcaSYEFxXJmvuC5K117WVKxKaCRvfoSgu1UBJA",
                  output_account="4WNw6fF5eGdep7nroX9oAqcZCDdSEiT4EnF7ZiNiy8k8", amount_in_raw=50_000_000,
                  min_out_raw=301_447_789, account_rent=())
    v = MV.validate(rm, it)
    assert v.reasons == ("extra_signer:sighWH8KaiT7QhtV4w29ReVF8kG6D5yG3EQP1KYyGVF", "positive_slippage")
    assert v.details["temp_accounts"] == ["4bEjD3jb9og4jDhC6D1kW48Tf8PwEDbSS42btahffFCm"]   # wSOL создан и закрыт
    assert v.details["fee_lamports"] == 20_304                          # = meta.fee чека этой транзакции
    assert H.fx_result("tx_jup_buy_b64.json")["meta"]["fee"] == 20_304


# --- S11: ALT ---------------------------------------------------------------------------------------------------------
def test_s11_foreign_recipient_hidden_in_alt():
    t = H.key("alt-hidden")
    good = H.snap(t, H.POOL[:4] + [H.ANSEM_ATA])
    raw = H.build(H.std_ixs(H.route_v2()), alts=[good])
    assert check(H.std_ixs(H.route_v2()), alts=[good]).ok
    evil = H.snap(t, H.POOL[:4] + [H.FOREIGN])                          # те же байты, таблица говорит другое
    v = MV.validate(M.resolve_with(M.wire.parse_message(raw), {t: evil}), H.intent())
    assert not v.ok and "ix_recipient" in v.reasons and "ata_not_intent_account" in v.reasons


def test_s11_protected_account_via_alt_and_deactivated_snapshot():
    t = H.key("alt-p")
    prot = H.key("our-other-ata")
    rem = [(p, False, True) for p in H.POOL[:3]] + [(prot, False, True)]
    s = H.snap(t, H.POOL[:3] + [prot])
    v = check(H.std_ixs(H.route_v2(remaining=rem)), H.intent(protected_accounts=(prot,)), alts=[s])
    assert f"protected_account_writable:{prot}" in v.reasons and f"protected_account_in_route:{prot}" in v.reasons
    rm = H.resolved(H.build(H.std_ixs(), alts=[H.snap(t, H.POOL)]), [H.snap(t, H.POOL)])
    rm = dataclasses.replace(rm, alts=(dataclasses.replace(rm.alts[0], deactivation_slot=9),))
    assert f"alt_deactivated:{t}" in MV.validate(rm, H.intent()).reasons


# --- S12: authority, approve, посторонние переводы -------------------------------------------------------------------
@pytest.mark.parametrize("ix,reason", [
    (H.token_ix(4, [(H.USDC_ATA, False, True), (H.FOREIGN, False, False), (H.WALLET, True, False)],
                struct.pack("<Q", 10 ** 12)), "token_ix_forbidden:approve"),
    (H.token_ix(6, [(H.ANSEM_ATA, False, True), (H.WALLET, True, False)], bytes([2, 1]) + H.b58.pubkey_bytes(H.FOREIGN),
                prog=TOKEN_2022_PROGRAM), "token_ix_forbidden:setAuthority"),
    (H.token_ix(3, [(H.USDC_ATA, False, True), (H.FOREIGN, False, True), (H.WALLET, True, False)],
                struct.pack("<Q", 1)), "token_ix_forbidden:transfer"),
    (H.token_ix(12, [(H.key("our-bonk"), False, True), (H.key("bonk"), False, False), (H.FOREIGN, False, True),
                     (H.WALLET, True, False)], struct.pack("<QB", 5, 5)), "token_ix_forbidden:transferChecked"),
    (H.token_ix(9, [(H.USDC_ATA, False, True), (H.FOREIGN, False, True), (H.WALLET, True, False)]),
     "token_close_forbidden"),
    (H.token_ix(8, [(H.USDC_ATA, False, True), (USDC_MINT, False, True), (H.WALLET, True, False)],
                struct.pack("<Q", 1)), "token_ix_forbidden:burn"),
])
def test_s12_extra_token_instruction_rejected_despite_good_price(ix, reason):
    v = check(H.std_ixs() + [ix])
    assert reason in v.reasons and not v.ok


def test_s12_second_swap_and_router_prep_rejected():
    assert "router_ix_count:2" in check(H.std_ixs() + [H.route_v2()]).reasons
    assert "router_ix_count:2" in check(H.std_ixs() + [H.okx_swap()]).reasons
    assert "router_program:OKX" in check(H.std_ixs(H.okx_swap()), H.intent(router_program=JUPITER_PROGRAM)).reasons
    prep = (OKX_ROUTER, [(H.WALLET, True, True), (H.WALLET, False, False), (H.WSOL_ATA, False, True),
                         (NATIVE_MINT, False, False), (TOKEN_PROGRAM, False, False), (SYSTEM_PROGRAM, False, False)],
            encode_args(okx_idl(), "create_token_account", {"bump": 255}))
    assert "okx_prep_not_needed:create_token_account" in check(H.std_ixs(H.okx_swap()) + [prep]).reasons


def test_s12_temp_wsol_only_when_closed_to_wallet():
    create = H.ata_create(account=H.WSOL_ATA, mint=NATIVE_MINT, prog=TOKEN_PROGRAM)
    close_ok = H.token_ix(9, [(H.WSOL_ATA, False, True), (H.WALLET, False, True), (H.WALLET, True, False)])
    assert check(H.std_ixs() + [create, close_ok]).ok               # как в настоящем tx_jup_buy: создан и закрыт
    close_bad = H.token_ix(9, [(H.WSOL_ATA, False, True), (H.FOREIGN, False, True), (H.WALLET, True, False)])
    v = check(H.std_ixs() + [create, close_bad])
    assert "token_close_forbidden" in v.reasons and "ata_not_intent_account" in v.reasons


def test_s12_jupiter_forbidden_instructions():
    ex = encode_args(jupiter_idl(), "exact_out_route_v2", {
        "out_amount": 1, "quoted_in_amount": 1, "slippage_bps": 1, "platform_fee_bps": 0,
        "positive_slippage_bps": 0, "route_plan": H.real_route_plan()})
    ix = list(H.route_v2())
    ix[2] = ex
    v = check(H.std_ixs(tuple(ix)))
    assert "jupiter_ix_forbidden:exact_out_route_v2" in v.reasons
    res = M.RpcMessageResolver(M.AltCache(H.AltRpc()))
    rm = res.resolve(H.fixture_tx("tx_jup_sell_b64.json"))
    wallet = "CKuBTzPwzrCisqQMQfAjNn6jKcfny8ny4rJVN3cxLjcf"
    it = H.intent(wallet=wallet, input_mint=ANSEM_MINT, input_program=TOKEN_2022_PROGRAM,
                  input_account="F9r5KY45RJFbRfcgvs9GwzcubQP6Z2tEeyx4uqp9e8Kv", output_mint=USDC_MINT,
                  output_program=TOKEN_PROGRAM, output_account="4eZim6VcF85Qd1PNkeUwt86qnyaH9naor2Q9PSkpYwiK",
                  amount_in_raw=51_882_704, min_out_raw=8_402_912,
                  account_rent=(("4eZim6VcF85Qd1PNkeUwt86qnyaH9naor2Q9PSkpYwiK", 0),))
    v = MV.validate(rm, it)
    assert "durable_nonce" in v.reasons and "jupiter_ix_forbidden:shared_accounts_route" in v.reasons


def test_decoder_unknown_discriminator_truncated_or_trailing_args():
    base = H.route_v2()
    for data, code in ((b"\x99" * 8 + base[2][8:], "idl_unknown_ix"), (base[2][:30], "idl_args"),
                       (base[2] + b"\x00", "idl_args")):
        v = check(H.std_ixs((base[0], base[1], data)))
        assert any(r.startswith("ix_not_in_manifest:3:Jupiter:" + code) for r in v.reasons)


# --- R09: сборка изменила minOut / mint / получателя / сумму ---------------------------------------------------------
@pytest.mark.parametrize("router,reason", [
    (H.route_v2(slip=300), "ix_min_out"),                                  # порог в аргументах ниже нашего
    (H.route_v2(quoted=H.QUOTED - 10), "ix_min_out"),
    (H.route_v2(dst_mint=H.key("other-mint")), "ix_mint"),
    (H.route_v2(in_amount=H.AMOUNT_IN + 1), "ix_in_amount"),
    (H.route_v2(pfee=20), "platform_fee"),
    (H.route_v2(pos=100), "positive_slippage"),
    (H.route_v2(dst_prog=TOKEN_PROGRAM), "ix_token_program"),
    (H.route_v2(authority=H.FOREIGN), "ix_authority"),
    (H.okx_swap(min_return=H.MIN_OUT - 1), "ix_min_out"),
    (H.okx_swap(amounts=[H.AMOUNT_IN - 1]), "okx_amounts_sum"),
    (H.okx_swap(amount_in=H.AMOUNT_IN + 5, amounts=[H.AMOUNT_IN + 5]), "ix_in_amount"),
    (H.okx_swap(commission=10), "okx_commission"),
    (H.okx_swap(pfee=5), "okx_platform_fee"),
    (H.okx_swap(name="swap_tob_v3", trim=1), "okx_trim"),
    (H.okx_swap(commission_acc=H.FOREIGN), "okx_fee_account:commission_account"),
    (H.okx_swap(name="swap_tob_v3_enhanced"), "okx_ix_forbidden:swap_tob_v3_enhanced"),
    (H.shared_route_v2(authority_pda=H.key("fake-pda")), "jupiter_program_authority"),
    (H.shared_route_v2(prog_dst=H.ANSEM_ATA), "jupiter_program_account:program_destination_token_account"),
])
def test_r09_built_transaction_differs_from_quote(router, reason):
    v = check(H.std_ixs(router))
    assert reason in v.reasons and not v.ok


def test_r09_min_out_exact_boundary():
    assert check(H.std_ixs(), H.intent(min_out_raw=H.MIN_OUT)).ok
    assert "ix_min_out" in check(H.std_ixs(), H.intent(min_out_raw=H.MIN_OUT + 1)).reasons
    assert check(H.std_ixs(H.okx_swap()), H.intent(min_out_raw=H.MIN_OUT)).ok
    assert "ix_min_out" in check(H.std_ixs(H.okx_swap()), H.intent(min_out_raw=H.MIN_OUT + 1)).reasons


def test_blockhash_must_be_the_one_with_known_height():
    assert check(H.std_ixs(), H.intent(recent_blockhash=H.BH)).ok
    assert "blockhash_mismatch" in check(H.std_ixs(), H.intent(recent_blockhash=H.key("other-bh"))).reasons


# --- R14: сеть, tip, отдельная fee-инструкция ------------------------------------------------------------------------
def test_r14_priority_fee_over_cap():
    ixs = [H.cu_limit(1_400_000), H.cu_price(200_000), H.ata_create(), H.route_v2()]     # 280 000 + 5000
    assert "network_fee_over_cap" in check(ixs).reasons
    assert "cu_price_over_cap" in check(H.std_ixs(), H.intent(cu_price_cap_micro_lamports=99_999)).reasons
    assert "cu_limit_over_cap" in check(H.std_ixs(), H.intent(cu_limit_cap=299_999)).reasons
    assert "limit_missing:max_network_fee_lamports_per_tx" in check(H.std_ixs(),
                                                                    H.intent(max_network_fee_lamports=None)).reasons


def test_r14_default_cu_limit_counts_as_upper_bound():
    v = check([H.cu_price(1_000_000), H.ata_create(), H.route_v2()])       # без SetComputeUnitLimit
    assert v.details["cu_limit"] is None and v.details["cu_limit_effective"] == 400_000
    assert v.details["priority_lamports"] == 400_000 and "network_fee_over_cap" in v.reasons


def test_r14_cu_conflicts_and_heap_frame():
    assert "cu_conflict" in check(H.std_ixs() + [H.cu_price(1)]).reasons
    heap = ("ComputeBudget111111111111111111111111111111", [], b"\x01" + struct.pack("<I", 256 * 1024))
    assert any(r.endswith("ComputeBudget:RequestHeapFrame") for r in check(H.std_ixs() + [heap]).reasons)


def test_r14_tip_recipients_and_cap():
    tip_to = H.key("tip-account")
    tip = H.sys_transfer(H.WALLET, tip_to, 1_000_000)
    assert f"system_transfer:{tip_to}" in check(H.std_ixs() + [tip]).reasons            # неизвестный получатель
    it = H.intent(tip_recipients=(tip_to,))
    assert "limit_missing:max_tip_lamports_per_tx" in check(H.std_ixs() + [tip], it).reasons
    assert "tip_over_cap" in check(H.std_ixs() + [tip], dataclasses.replace(it, tip_cap_lamports=999_999)).reasons
    ok = check(H.std_ixs() + [tip], dataclasses.replace(it, tip_cap_lamports=1_000_000))
    assert ok.ok and ok.details["tip_lamports"] == 1_000_000
    assert ok.details["native_total"] == 35_000 + H.ANSEM_RENT + 1_000_000


def test_r14_native_budget_and_foreign_sol_transfer():
    assert "native_budget_exceeded" in check(H.std_ixs(), H.intent(native_budget_lamports=2_000_000)).reasons
    assert "limit_missing:native_budget_lamports" in check(H.std_ixs(), H.intent(native_budget_lamports=None)).reasons
    fee_ix = H.sys_transfer(H.WALLET, H.key("provider-fee"), 5000)       # отдельная fee-инструкция мимо котировки
    assert f"system_transfer:{H.key('provider-fee')}" in check(H.std_ixs() + [fee_ix]).reasons
    nonce = (SYSTEM_PROGRAM, [(H.key("nonce"), False, True), (H.key("SysvarRecent"), False, False),
                              (H.WALLET, True, False)], struct.pack("<I", 4))
    assert "durable_nonce" in check([nonce] + H.std_ixs()).reasons


# --- манифест и IDL ---------------------------------------------------------------------------------------------------
def test_idl_pinned_and_same_as_adapters(tmp_path):
    from funding_bot.trade import jupiter_spot, okx_sol_spot
    assert jupiter_idl().sha256 == jupiter_spot.IDL_SHA256 and okx_idl().sha256 == okx_sol_spot.IDL_SHA256
    routes = H.DATA.parent / "sol_routes"
    assert (routes / "idl_jupiter_v6.json").read_bytes() == (H.Path(V.__file__).with_name("idl") / "jupiter_v6.json").read_bytes()
    p = tmp_path / "idl.json"
    p.write_bytes((routes / "idl_jupiter_v6.json").read_bytes() + b" ")
    with pytest.raises(BorshError):
        IdlDecoder.load(p, expect_sha256=jupiter_spot.IDL_SHA256)


def test_intent_sanity():
    v = check(H.std_ixs(), H.intent(output_account=H.USDC_ATA))
    assert v.reasons == ("intent_invalid:accounts",)
    assert "intent_invalid:min_out_raw" in check(H.std_ixs(), H.intent(min_out_raw=0)).reasons
    assert "intent_invalid:protected_accounts" in check(H.std_ixs(), H.intent(protected_accounts=(H.USDC_ATA,))).reasons


def test_refusing_default_unchanged():
    v = V.REFUSING.validate(None, H.intent())
    assert not v.ok and v.manifest_version is None
