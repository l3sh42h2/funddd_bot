"""Разбор чеков getTransaction: реальные свопы ANSEM через Jupiter из публичного RPC 13.09 (USDC→ANSEM,
ANSEM→USDC, CASH→ANSEM с созданием нашего ATA и чужим получателем выхода, неудачный своп с удержанной fee) и
синтетика по матрице: S09, S14–S18. Ожидаемые суммы — из самих pre/post балансов фикстуры, выписанные числами."""
import copy, struct
import pytest
from funding_bot.trade.solana import (ANSEM_MINT, NATIVE_MINT, SYSTEM_PROGRAM, TOKEN_2022_PROGRAM, TOKEN_PROGRAM,
                                      USDC_MINT, b58)
from funding_bot.trade.solana.receipt import (ReceiptError, ReceiptIncomplete, parse_receipt, plan_mismatches,
                                              wire_of)
from solana_helpers import fx

T22, TK = TOKEN_2022_PROGRAM, TOKEN_PROGRAM
CASH = "CASHx9KJUStyftLFWGvEVf59SGeG9sh5FfcnZMVPCASH"
JUP = "JUP6LkbZbjS1jKKwapdHNy74zcZ3tLUZoi5QNyVTaV4"
BUY_ANSEM_ATA, BUY_USDC_ATA = "4WNw6fF5eGdep7nroX9oAqcZCDdSEiT4EnF7ZiNiy8k8", "68n6nUMcaSYEFxXJmvuC5K117WVKxKaCRvfoSgu1UBJA"
SELL_ANSEM_ATA, SELL_USDC_ATA = "F9r5KY45RJFbRfcgvs9GwzcubQP6Z2tEeyx4uqp9e8Kv", "4eZim6VcF85Qd1PNkeUwt86qnyaH9naor2Q9PSkpYwiK"
TIP = "96gYZGLnJYVFmbjzopPSU6QiEV5fGqZNyN9nmNhvrZU5"


def tx(nm):
    return copy.deepcopy(fx(f"tx_{nm}.json")["result"])


def keys(t):
    la = t["meta"]["loadedAddresses"]
    return t["transaction"]["message"]["accountKeys"] + la["writable"] + la["readonly"]


def set_amount(t, which, address, amount):
    i = keys(t).index(address)
    for b in t["meta"][which]:
        if b["accountIndex"] == i:
            b["uiTokenAmount"]["amount"] = str(amount)
            return
    raise KeyError(address)


def swap(rc, wallet, in_mint, in_prog, out_mint, out_prog, accounts=None):
    return rc.swap_amounts(wallet=wallet, in_mint=in_mint, in_program=in_prog, out_mint=out_mint,
                           out_program=out_prog, accounts=accounts)


# --- реальные чеки -------------------------------------------------------------------------------
def test_buy_usdc_to_ansem_exact_raw():                  # S09: последняя raw-единица, не uiAmount
    t = tx("jup_buy")
    rc = parse_receipt(t, expect_signature=t["transaction"]["signatures"][0])
    w = rc.fee_payer
    a = swap(rc, w, USDC_MINT, TK, ANSEM_MINT, T22)
    assert (a.ok, a.in_raw, a.out_raw) == (True, 50_000_000, 301_749_539)
    assert [x.address for x in a.out_flow.accounts] == [BUY_ANSEM_ATA]
    assert [x.address for x in a.in_flow.accounts] == [BUY_USDC_ATA]
    n = rc.native(w)
    assert (n.fee_paid_by_wallet, n.rent_deposit_lamports, n.rent_refund_lamports, n.external_lamports) == \
        (20304, 0, 0, 0)
    assert n.wallet_delta == -20304 and n.nonrefundable_lamports == 20304
    # временный счёт создан и закрыт внутри транзакции: переводы SOL есть, а из домена кошелька ничего не ушло
    assert n.transfers_out and n.external_lamports == 0
    assert rc.version == 0 and len(rc.loaded_writable) + len(rc.loaded_readonly) == 36
    assert JUP in rc.top_programs and rc.ok and rc.err is None


def test_sell_ansem_to_usdc_deal_accounts():
    rc = parse_receipt(tx("jup_sell"))
    w = rc.fee_payer
    for accs in (None, {SELL_ANSEM_ATA, SELL_USDC_ATA}):
        a = swap(rc, w, ANSEM_MINT, T22, USDC_MINT, TK, accs)
        assert (a.in_raw, a.out_raw) == (51_882_704, 8_574_400)
    n = rc.native(w)
    assert n.fee_paid_by_wallet == 105002 and n.wallet_delta == -105002 and n.external_lamports == 0


def test_created_ata_and_foreign_recipient():            # создание счёта — не выход; чужой получатель — не наш
    rc = parse_receipt(tx("ata_create"))
    w = rc.fee_payer
    a = swap(rc, w, CASH, T22, ANSEM_MINT, T22)
    assert (a.ok, a.in_raw, a.out_raw) == (True, 1_447_784, 0)
    created = [x for x in a.out_flow.accounts if x.created]
    assert len(created) == 1 and created[0].post_raw == 0 and created[0].post_lamports == 1_513_840
    n = rc.native(w)
    assert (n.fee_paid_by_wallet, n.rent_deposit_lamports, n.external_lamports) == (605_000, 1_513_840, 0)
    assert n.wallet_delta == -(605_000 + 1_513_840)
    assert n.nonrefundable_lamports == 605_000            # rent своего ATA — депозит, не расход
    assert [(x.kind, x.dest, x.lamports) for x in n.transfers_out] == [("create_account", created[0].address,
                                                                        1_513_840)]
    foreign = [x for x in rc.token_accounts if x.mint == ANSEM_MINT and x.delta > 0]
    assert len(foreign) == 1 and foreign[0].owner != w and foreign[0].delta == 8_660_478
    assert rc.mint_flow(ANSEM_MINT, owner=foreign[0].owner, program=T22).delta_raw == 8_660_478


def test_failed_swap_keeps_fee_moves_nothing():          # S16
    rc = parse_receipt(tx("failed_jup"))
    w = rc.fee_payer
    assert not rc.ok and rc.err == {"InstructionError": [3, {"Custom": 6001}]}
    a = swap(rc, w, NATIVE_MINT, TK, ANSEM_MINT, T22)
    assert (a.ok, a.in_raw, a.out_raw) == (False, 0, 0)
    n = rc.native(w)
    assert n.fee_paid_by_wallet == 5125 and n.wallet_delta == -5125 and n.transfers_out == ()
    # неудачная транзакция с переводами SOL в инструкциях: они откатаны — не tip и не расход
    rc2 = parse_receipt(tx("failed"))
    n2 = rc2.native(rc2.fee_payer, tip_accounts={x for x in keys(tx("failed"))})
    assert n2.transfers_out == () and n2.tip_lamports == 0 and n2.fee_paid_by_wallet == 105_000
    # «неуспешная, а токены сдвинулись» — разбору не верим
    t = tx("failed_jup")
    acc = next(x for x in rc.token_accounts if x.mint == ANSEM_MINT and x.owner == w)
    set_amount(t, "postTokenBalances", acc.address, acc.post_raw + 1)
    with pytest.raises(ReceiptError, match="неуспешная"):
        swap(parse_receipt(t), w, NATIVE_MINT, TK, ANSEM_MINT, T22)


# --- синтетика по матрице ------------------------------------------------------------------------
def test_refund_counts_actual_input():                   # S14: ушло 1000, роутер вернул 10, получено 500
    t = tx("jup_buy")
    set_amount(t, "preTokenBalances", BUY_USDC_ATA, 1000)
    set_amount(t, "postTokenBalances", BUY_USDC_ATA, 10)
    set_amount(t, "preTokenBalances", BUY_ANSEM_ATA, 0)
    set_amount(t, "postTokenBalances", BUY_ANSEM_ATA, 500)
    rc = parse_receipt(t)
    a = swap(rc, rc.fee_payer, USDC_MINT, TK, ANSEM_MINT, T22)
    assert (a.in_raw, a.out_raw) == (990, 500)


def test_existing_balance_not_counted():                 # S15: было 100 своих, пришло 20 → сделка +20
    t = tx("jup_buy")
    set_amount(t, "preTokenBalances", BUY_ANSEM_ATA, 100)
    set_amount(t, "postTokenBalances", BUY_ANSEM_ATA, 120)
    rc = parse_receipt(t)
    assert swap(rc, rc.fee_payer, USDC_MINT, TK, ANSEM_MINT, T22).out_raw == 20
    # и с созданием счёта в той же транзакции: rent отдельно, в токенах — ровно полученное
    t = tx("ata_create")
    rc0 = parse_receipt(t)
    created = next(x for x in rc0.token_accounts if x.created and x.mint == ANSEM_MINT)
    set_amount(t, "postTokenBalances", created.address, 20)
    rc = parse_receipt(t)
    a = swap(rc, rc.fee_payer, CASH, T22, ANSEM_MINT, T22)
    assert a.out_raw == 20 and rc.native(rc.fee_payer).rent_deposit_lamports == 1_513_840


def _synth(keys_, pre, post, fee, ixs, pre_tok=(), post_tok=(), err=None):
    base = fx("tx_jup_sell.json")["result"]
    return {"slot": 1, "blockTime": 1, "version": "legacy",
            "transaction": {"signatures": [base["transaction"]["signatures"][0]],
                            "message": {"accountKeys": list(keys_), "recentBlockhash":
                                        base["transaction"]["message"]["recentBlockhash"],
                                        "header": {"numRequiredSignatures": 1, "numReadonlySignedAccounts": 0,
                                                   "numReadonlyUnsignedAccounts": 1}, "instructions": ixs}},
            "meta": {"err": err, "fee": fee, "preBalances": pre, "postBalances": post,
                     "preTokenBalances": list(pre_tok), "postTokenBalances": list(post_tok),
                     "innerInstructions": [], "loadedAddresses": {"writable": [], "readonly": []}}}


def _tb(i, mint, owner, prog, amount):
    return {"accountIndex": i, "mint": mint, "owner": owner, "programId": prog,
            "uiTokenAmount": {"amount": str(amount), "decimals": 6}}


def test_fee_includes_priority_tip_separate():           # S17 (лампорты; оценка в USD — fees.py)
    w = fx("tx_jup_sell.json")["result"]["transaction"]["message"]["accountKeys"][0]
    data = b58.b58encode(struct.pack("<IQ", 2, 10_000))
    t = _synth([w, TIP, SYSTEM_PROGRAM], [1_000_000, 0, 1], [960_000, 10_000, 1], 30_000,
               [{"programIdIndex": 2, "accounts": [0, 1], "data": data}])
    n = parse_receipt(t).native(w, tip_accounts={TIP})
    assert (n.fee_paid_by_wallet, n.tip_lamports, n.nonrefundable_lamports) == (30_000, 10_000, 40_000)
    assert n.external_lamports == 10_000                  # сверх fee ушёл ровно tip; priority не прибавлен дважды
    assert parse_receipt(t).native(w).tip_lamports == 0   # без списка tip-счетов перевод виден как external


def test_rent_deposit_and_refund_separate():             # S18
    w = fx("tx_jup_sell.json")["result"]["transaction"]["message"]["accountKeys"][0]
    new, old = SELL_ANSEM_ATA, SELL_USDC_ATA
    t = _synth([w, new, old, TOKEN_PROGRAM], [5_000_000, 0, 2_000_000, 1], [4_995_000, 2_000_000, 0, 1], 5_000, [],
               pre_tok=[_tb(2, USDC_MINT, w, TK, 0)], post_tok=[_tb(1, USDC_MINT, w, TK, 0)])
    n = parse_receipt(t).native(w)
    assert (n.rent_deposit_lamports, n.rent_refund_lamports, n.fee_paid_by_wallet) == (2_000_000, 2_000_000, 5_000)
    assert n.nonrefundable_lamports == 5_000 and n.external_lamports == 0


def test_wsol_trading_sol_not_rent():
    w = fx("tx_jup_sell.json")["result"]["transaction"]["message"]["accountKeys"][0]
    wsol = SELL_USDC_ATA
    # временный wSOL создан с депозитом 1 000 и обёрнутыми 50 000; в конце остался (не закрыт)
    t = _synth([w, wsol, TOKEN_PROGRAM], [1_000_000, 0, 1], [944_000, 51_000, 1], 5_000, [],
               post_tok=[_tb(1, NATIVE_MINT, w, TK, 50_000)])
    n = parse_receipt(t).native(w)
    assert (n.rent_deposit_lamports, n.wsol_delta_raw, n.external_lamports) == (1_000, 50_000, 0)


# --- неполные и чужие данные ---------------------------------------------------------------------
def test_incomplete_is_unknown_not_zero():
    with pytest.raises(ReceiptIncomplete):
        parse_receipt(None)
    for mut in (lambda t: t.__setitem__("meta", None),
                lambda t: t["meta"].pop("loadedAddresses"),
                lambda t: t["meta"].__setitem__("preTokenBalances", None)):
        t = tx("jup_buy")
        mut(t)
        with pytest.raises(ReceiptIncomplete):
            parse_receipt(t)
    t = tx("jup_buy")
    i = keys(t).index(BUY_ANSEM_ATA)
    for b in t["meta"]["postTokenBalances"]:
        if b["accountIndex"] == i:
            b.pop("owner")
    rc = parse_receipt(t)
    with pytest.raises(ReceiptIncomplete):
        rc.mint_flow(ANSEM_MINT, owner=rc.fee_payer, program=T22)
    assert rc.mint_flow(USDC_MINT, owner=rc.fee_payer, program=TK).delta_raw == -50_000_000


def test_malformed_receipts_refused():
    base = tx("jup_buy")
    for mut in (lambda t: t["transaction"]["message"]["accountKeys"].__setitem__(0, {"pubkey": "x"}),
                lambda t: t["meta"]["postTokenBalances"][0]["uiTokenAmount"].__setitem__("amount", "1.5"),
                lambda t: t.__setitem__("version", 1),
                lambda t: t["meta"]["postBalances"].pop(),
                lambda t: t["meta"]["postTokenBalances"].append(dict(t["meta"]["postTokenBalances"][0])),
                lambda t: t["meta"]["postTokenBalances"][0].__setitem__("mint", USDC_MINT + "x")):
        t = copy.deepcopy(base)
        mut(t)
        with pytest.raises((ReceiptError, ValueError)):
            parse_receipt(t)
    with pytest.raises(ReceiptError, match="первая подпись"):
        parse_receipt(base, expect_signature=fx("tx_jup_sell.json")["result"]["transaction"]["signatures"][0])


def test_outside_deal_accounts_and_program_checked():
    rc = parse_receipt(tx("jup_buy"))
    w = rc.fee_payer
    with pytest.raises(ReceiptError, match="вне сделки"):
        swap(rc, w, USDC_MINT, TK, ANSEM_MINT, T22, accounts={BUY_USDC_ATA})
    with pytest.raises(ReceiptError, match="программой"):
        rc.mint_flow(ANSEM_MINT, owner=w, program=TK)
    with pytest.raises(ReceiptError):
        swap(rc, w, ANSEM_MINT, T22, USDC_MINT, TK)       # направление перепутано: «вход прибыл»
    assert TIP not in rc.account_keys
    with pytest.raises(ReceiptError):
        rc.native(TIP)                                    # кошелька нет среди ключей транзакции
    variant = ANSEM_MINT.replace("pump", "PUMP")          # S01/S02: другой регистр — не наш mint
    try:
        f = rc.mint_flow(variant, owner=w, program=T22)
        assert f.delta_raw == 0 and f.accounts == ()
    except ValueError:
        pass


def test_plan_mismatches_with_message_hash():
    rc = parse_receipt(tx("jup_sell"))
    wt = wire_of(fx("tx_jup_sell_b64.json")["result"])
    ok = dict(signature=rc.signature, recent_blockhash=rc.recent_blockhash, fee_payer=rc.fee_payer)
    assert plan_mismatches(rc, **ok, message_hash=wt.message.message_hash, wire_tx=wt) == []
    assert plan_mismatches(rc, **ok, message_hash="0" * 64, wire_tx=wt) == ["хеш исполненного сообщения ≠ закреплённому"]
    assert plan_mismatches(rc, **ok, message_hash=wt.message.message_hash) == \
        ["хеш сообщения не сверен: нет байтов транзакции"]
    assert any("blockhash" in m for m in plan_mismatches(rc, **dict(ok, recent_blockhash=USDC_MINT)))
    assert any("плательщик" in m for m in plan_mismatches(rc, **dict(ok, fee_payer=USDC_MINT)))
    other = wire_of(fx("tx_jup_buy_b64.json")["result"])
    assert "байты и чек — разные транзакции" in plan_mismatches(rc, **ok, message_hash=wt.message.message_hash,
                                                               wire_tx=other)
    with pytest.raises(ReceiptError):
        wire_of(tx("jup_sell"))
