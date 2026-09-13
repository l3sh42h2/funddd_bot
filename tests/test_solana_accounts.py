"""Mint и token-счета: ANSEM (Token-2022, metadata-only) и USDC (classic) из снимка публичного RPC 13.09,
сырые байты против jsonParsed, allowlist расширений (S03–S05), замороженная идентичность (S02, S06), счета
сделки (S08), rent у узла, а не константой."""
import base64, copy, json, struct
import pytest
from funding_bot.trade.solana import ANSEM_MINT, TOKEN_2022_PROGRAM, TOKEN_PROGRAM, USDC_MINT
from funding_bot.trade.solana import accounts as A
from solana_helpers import fx, fx_doc

OWNER = "EH1KQLnYQoJUn4ofX1TKPiagtFbtEsrE3ChkY24eRXae"      # чужой публичный кошелёк из чека jup_buy
ANSEM_ATA = "4WNw6fF5eGdep7nroX9oAqcZCDdSEiT4EnF7ZiNiy8k8"
USDC_ATA = "68n6nUMcaSYEFxXJmvuC5K117WVKxKaCRvfoSgu1UBJA"
CASH_ACC = "FxuqW7exk6oxTJVvr652jY1mtyfwhRqdaASs4LsebgcH"   # CASH (Token-2022) с closeAuthority и transferHook


def value(name):
    r = fx(name)["result"]
    return copy.deepcopy(r["value"]), r["context"]["slot"]


def token_value(addr, enc="jsonParsed"):
    params = fx_doc(f"token_accounts_{enc}.json")["params"][0]
    r = fx(f"token_accounts_{enc}.json")["result"]
    return copy.deepcopy(r["value"][params.index(addr)]), r["context"]["slot"]


class FakeRpc:
    def __init__(self, by_enc: dict):
        self.by_enc = by_enc
        self.calls = []

    def account_info(self, pubkey, *, encoding="jsonParsed", commitment="confirmed"):
        self.calls.append((pubkey, encoding))
        v, slot = self.by_enc[(pubkey, encoding)]
        return copy.deepcopy(v), slot

    def rent_exempt_lamports(self, size):
        return fx(f"rent_{size}.json")["result"]


def ansem():
    v, slot = value("mint_ansem_jsonParsed.json")
    return A.parse_mint(ANSEM_MINT, v, slot)


# --- mint ----------------------------------------------------------------------------------------
def test_ansem_token2022_metadata_only_allowed():         # S03
    info = ansem()
    assert info.address == ANSEM_MINT and info.program == TOKEN_2022_PROGRAM and info.decimals == 6
    assert info.mint_authority is None and info.freeze_authority is None and info.is_initialized
    assert info.ext_names == ("metadataPointer", "tokenMetadata")
    assert A.mint_policy(info) == [] and A.mint_notes(info) == []


def test_usdc_classic_freeze_authority_is_note_not_refusal():
    v, slot = value("mint_usdc_jsonParsed.json")
    info = A.parse_mint(USDC_MINT, v, slot)
    assert info.program == TOKEN_PROGRAM and info.decimals == 6 and info.extensions == ()
    assert A.mint_policy(info) == []
    assert any("freeze authority" in n for n in A.mint_notes(info))
    vb, _ = value("mint_usdc_base64.json")
    raw = A.parse_mint_raw(A.account_bytes(vb), TOKEN_PROGRAM)
    assert raw["extensions"] == () and A.raw_diff_mint(info, raw) == ()


def test_raw_bytes_agree_with_jsonparsed():
    info = ansem()
    vb, _ = value("mint_ansem_base64.json")
    data = A.account_bytes(vb)
    raw = A.parse_mint_raw(data, TOKEN_2022_PROGRAM)
    assert len(data) == 401 and data[165] == 1
    assert [t for t, _ in raw["extensions"]] == [18, 19]          # MetadataPointer, TokenMetadata
    assert raw["decimals"] == 6 and raw["mint_authority"] is None and raw["freeze_authority"] is None
    assert A.raw_diff_mint(info, raw) == ()
    assert A._metadata_pointer_raw(raw["extensions"]) == {"authority": None, "metadataAddress": ANSEM_MINT}


def test_read_mint_crosschecks_two_encodings():
    rpc = FakeRpc({(ANSEM_MINT, "jsonParsed"): value("mint_ansem_jsonParsed.json"),
                   (ANSEM_MINT, "base64"): value("mint_ansem_base64.json")})
    info = A.read_mint(rpc, ANSEM_MINT)
    assert info.raw_diff == () and A.mint_policy(info) == []
    assert [e for _, e in rpc.calls] == ["jsonParsed", "base64"]
    # байты говорят decimals 9, а jsonParsed — 6: отказ, а не выбор «удобной» версии
    vb, slot = value("mint_ansem_base64.json")
    data = bytearray(A.account_bytes(vb))
    data[44] = 9
    vb["data"][0] = base64.b64encode(bytes(data)).decode()
    rpc.by_enc[(ANSEM_MINT, "base64")] = (vb, slot)
    bad = A.read_mint(rpc, ANSEM_MINT)
    assert bad.raw_diff and any("расходятся" in w for w in A.mint_policy(bad))


def _with_ext(name, state=None):
    v, slot = value("mint_ansem_jsonParsed.json")
    v["data"]["parsed"]["info"]["extensions"].append({"extension": name, "state": state or {}})
    return A.parse_mint(ANSEM_MINT, v, slot)


def test_transfer_affecting_and_unknown_extensions_refused():   # S04, S05
    why = A.mint_policy(_with_ext("transferHook", {"programId": USDC_MINT, "authority": None}))
    assert any("TransferHook" in w for w in why)
    why = A.mint_policy(_with_ext("transferFeeConfig", {"newerTransferFee": {"transferFeeBasisPoints": 0}}))
    assert any("TransferFee" in w for w in why)                 # даже с 0 бп — «налог 0» не предполагаем
    for name in ("permanentDelegate", "defaultAccountState", "nonTransferable", "confidentialTransferMint",
                 "interestBearingConfig", "scaledUiAmountConfig", "pausableConfig", "mintCloseAuthority",
                 "someFutureExtension", "unparseableExtension"):
        assert A.mint_policy(_with_ext(name)), name
    # сырые TLV: неизвестный тип 99 и TransferFeeConfig (1) — видны и без jsonParsed
    vb, _ = value("mint_ansem_base64.json")
    data = A.account_bytes(vb)
    tail = struct.pack("<HH", 99, 2) + b"\1\2" + struct.pack("<HH", 1, 0)
    raw = A.parse_mint_raw(data + tail, TOKEN_2022_PROGRAM)
    assert A._ext_raw_names(raw["extensions"])[-2:] == ["unknown#99", "transferFeeConfig"]
    assert A.raw_diff_mint(ansem(), raw)                          # jsonParsed их не показал — расхождение
    with pytest.raises(A.AccountError):
        A.parse_mint_raw(data + struct.pack("<HH", 99, 10) + b"\1", TOKEN_2022_PROGRAM)   # обрыв TLV


def test_mint_parse_refusals():
    v, slot = value("mint_ansem_jsonParsed.json")
    with pytest.raises(A.AccountError):
        A.parse_mint(ANSEM_MINT, None, slot)
    for mut in (lambda x: x.__setitem__("owner", "11111111111111111111111111111111"),
                lambda x: x["data"]["parsed"].__setitem__("type", "account"),
                lambda x: x["data"]["parsed"]["info"].__setitem__("supply", 997.0),
                lambda x: x["data"]["parsed"]["info"].__setitem__("decimals", "6")):
        vv = copy.deepcopy(v)
        mut(vv)
        with pytest.raises(A.AccountError):
            A.parse_mint(ANSEM_MINT, vv, slot)
    vv = copy.deepcopy(v)
    vv["owner"] = TOKEN_PROGRAM                                   # у классического Token расширений нет
    with pytest.raises(A.AccountError):
        A.parse_mint(ANSEM_MINT, vv, slot)
    with pytest.raises(A.AccountError):
        A.parse_mint_raw(b"\0" * 81, TOKEN_2022_PROGRAM)
    with pytest.raises(A.AccountError):
        A.parse_mint_raw(b"\0" * 82 + b"\0" * 10, TOKEN_PROGRAM)


def test_identity_frozen_and_changes_detected():           # S06, S02
    info = ansem()
    frozen = A.mint_identity(info)
    assert frozen["mint"] == ANSEM_MINT and frozen["program"] == TOKEN_2022_PROGRAM
    assert A.identity_diff(frozen, info) == []
    v, slot = value("mint_ansem_jsonParsed.json")
    for mut, field in ((lambda i: i.__setitem__("decimals", 9), "decimals"),
                       (lambda i: i.__setitem__("freezeAuthority", USDC_MINT), "freeze_authority"),
                       (lambda i: i.__setitem__("mintAuthority", USDC_MINT), "mint_authority"),
                       (lambda i: i["extensions"][0]["state"].__setitem__("authority", USDC_MINT),
                        "metadata_pointer_authority")):
        vv = copy.deepcopy(v)
        mut(vv["data"]["parsed"]["info"])
        diff = A.identity_diff(frozen, A.parse_mint(ANSEM_MINT, vv, slot))
        assert any(d.startswith(field) for d in diff), field
    vv = copy.deepcopy(v)
    vv["owner"] = TOKEN_PROGRAM
    vv["data"]["parsed"]["info"].pop("extensions")
    assert any(d.startswith("program") for d in A.identity_diff(frozen, A.parse_mint(ANSEM_MINT, vv, slot)))
    variant = dict(frozen, mint=ANSEM_MINT.replace("pump", "PUMP"))  # другой регистр — другой mint
    assert any(d.startswith("mint") for d in A.identity_diff(variant, info))
    assert A.identity_diff({"decimals": 6}, info)                  # незамороженные mint/program — тоже отказ


# --- token-счета ---------------------------------------------------------------------------------
def test_deal_accounts_real_ok():                          # S08: годные счета проходят
    for addr, mint, prog in ((ANSEM_ATA, ANSEM_MINT, TOKEN_2022_PROGRAM), (USDC_ATA, USDC_MINT, TOKEN_PROGRAM)):
        v, slot = token_value(addr)
        info = A.parse_token_account(addr, v, slot)
        assert (info.owner, info.mint, info.program) == (OWNER, mint, prog)
        vb, _ = token_value(addr, "base64")
        raw = A.parse_token_account_raw(A.account_bytes(vb), prog)
        assert A.raw_diff_account(info, raw) == ()
        assert A.account_policy(info, owner=OWNER, mint=mint, program=prog) == []
    v, slot = token_value(ANSEM_ATA)
    info = A.parse_token_account(ANSEM_ATA, v, slot)
    assert info.ext_names == ("immutableOwner",) and info.space == 170 and info.amount == 14992580832


def test_deal_account_mismatch_refused():                   # S08: чужой/не тот счёт не признаётся
    v, slot = token_value(ANSEM_ATA)
    info = A.parse_token_account(ANSEM_ATA, v, slot)
    assert any("владелец" in w for w in A.account_policy(info, owner=USDC_MINT, mint=ANSEM_MINT,
                                                          program=TOKEN_2022_PROGRAM))
    assert any("mint счёта" in w for w in A.account_policy(info, owner=OWNER, mint=USDC_MINT,
                                                            program=TOKEN_2022_PROGRAM))
    why = A.account_policy(info, owner=OWNER, mint=ANSEM_MINT, program=TOKEN_PROGRAM)
    assert any("программой" in w for w in why) and any("не ATA" in w for w in why)
    for field, val, word in (("state", "frozen", "заморожен"), ("delegate", USDC_MINT, "делегат"),
                             ("closeAuthority", USDC_MINT, "close authority")):
        vv = copy.deepcopy(v)
        vv["data"]["parsed"]["info"][field] = val
        why = A.account_policy(A.parse_token_account(ANSEM_ATA, vv, slot), owner=OWNER, mint=ANSEM_MINT,
                               program=TOKEN_2022_PROGRAM)
        assert any(word in w for w in why), field


def test_real_account_with_close_authority_and_hook_refused():
    v, slot = token_value(CASH_ACC)
    info = A.parse_token_account(CASH_ACC, v, slot)
    vb, _ = token_value(CASH_ACC, "base64")
    raw = A.parse_token_account_raw(A.account_bytes(vb), TOKEN_2022_PROGRAM)
    assert A.raw_diff_account(info, raw) == ()
    assert A._ext_raw_names(raw["extensions"]) == ["immutableOwner", "transferHookAccount"]
    why = A.account_policy(info, owner=info.owner, mint=info.mint, program=TOKEN_2022_PROGRAM)
    assert any("close authority" in w for w in why) and any("TransferHook" in w for w in why)


def test_read_token_account_absent_is_none_not_zero():
    rpc = FakeRpc({(ANSEM_ATA, "jsonParsed"): token_value(ANSEM_ATA), (ANSEM_ATA, "base64"): token_value(ANSEM_ATA,
                                                                                                         "base64"),
                   ("EHWN9tUeAzWUjYPpkk69yeoLPRbtVh5hEFuX8o9xpigY", "jsonParsed"): (None, 1)})
    assert A.read_token_account(rpc, ANSEM_ATA).raw_diff == ()
    assert A.read_token_account(rpc, "EHWN9tUeAzWUjYPpkk69yeoLPRbtVh5hEFuX8o9xpigY") is None


def test_policy_constants_match_instruments_registry():
    """Один источник истины после слияния: allowlist и genesis совпадают с реестром потока profiles."""
    ins = pytest.importorskip("funding_bot.trade.instruments")
    from funding_bot.trade.solana import MAINNET_GENESIS
    assert set(A.MINT_EXT_ALLOW) == set(ins.MINT_EXTENSIONS_V1)
    assert set(A.ACCOUNT_EXT_ALLOW) == set(ins.ACCOUNT_EXTENSIONS_V1)
    assert ins.SOLANA_MAINNET_GENESIS == MAINNET_GENESIS


def test_rent_from_node_matches_real_created_accounts():
    rpc = FakeRpc({})
    # rent нового ANSEM ATA = lamports счёта, созданного в чеке ata_create; USDC ATA = lamports свежего USDC ATA
    assert A.ata_rent_lamports(rpc, TOKEN_2022_PROGRAM, ("metadataPointer", "tokenMetadata")) == 1513840
    assert A.ata_rent_lamports(rpc, TOKEN_PROGRAM) == 1488440
    assert token_value(USDC_ATA)[0]["lamports"] == 1488440
    assert A.ata_space(TOKEN_2022_PROGRAM, ("metadataPointer",)) == 170 and A.ata_space(TOKEN_PROGRAM) == 165
    with pytest.raises(ValueError):
        A.ata_space(TOKEN_2022_PROGRAM, ("transferFeeConfig",))
    with pytest.raises(ValueError):
        A.ata_space("11111111111111111111111111111111")
