"""Реестр инструментов связки SOL × HL (ТЗ SOL×HL §2.1, §2.2, §4).

Приёмка (уровень реестра и единиц; движок, БД и кабинет — после интеграции): S01, S02, S03–S06 (срез mint против
записи), S09, H01, V01, V03, X11 (правило версии), U01–U06 (формула), G05/G06 (защитный допуск).
Контрольные значения независимы от кода: векторы base58 из draft-msporny-base58, байты программы SPL Token, срез RPC
mint ANSEM из evidence ТЗ (13.09 10:50Z), USDC из config.OKX_DEX_STABLES, числа — из матрицы приёмки. Сети нет."""
import copy, dataclasses, json
from decimal import Decimal as D
import pytest
from funding_bot import config
from funding_bot.trade import instruments as I, tconfig
from sol_hl_fixtures import ANSEM, DATA

NOW = I.ts_epoch("2026-09-21T00:00:00+00:00")


def _example() -> dict:
    return json.loads((DATA / "instruments.example.json").read_text(encoding="utf-8"))


def _entry(**paths) -> dict:
    """Запись примера с правками по пути «a.b.c»; значение _DEL — убрать ключ."""
    e = copy.deepcopy(_example()["instruments"][0])
    for path, v in paths.items():
        *head, last = path.split("__")
        d = e
        for h in head:
            d = d[h]
        if v is _DEL:
            d.pop(last)
        else:
            d[last] = v
    return e


_DEL = object()


def _reg(*entries) -> I.Registry:
    return I.parse_registry(json.dumps({"schema_version": 1, "instruments": list(entries)}))


def _live(**paths) -> dict:
    base = dict(enabled_for_live=True, spot__genesis_hash=I.SOLANA_MAINNET_GENESIS, units__status="accepted",
                perp__account_id="mainnet:0xmaster:0xsub:para:usdc", identity__status="reviewed_override",
                identity__reviewed_by="owner", identity__reviewed_at="2026-09-20T10:00:00+00:00",
                identity__review_reason="решение владельца (синтетика теста)",
                identity__expires_at="2026-10-20T10:00:00+00:00")
    base.update(paths)
    return _entry(**base)


def _mint_value() -> dict:
    return json.loads((DATA / "ansem_solana_mint.json").read_text(encoding="utf-8"))["rpc"]["result"]["value"]


# ================================ base58 и адреса ================================
def test_base58_independent_vectors():
    assert I.b58encode(b"Hello World!") == "2NEpo7TZRRrLZSi2U"
    assert I.b58encode(b"The quick brown fox jumps over the lazy dog.") == \
        "USm3fpXnKG5EUBx2ndxBDMPVciP5hGey2Jh4NDv6gmeo1LkMeiKrLJUUBk6Z"
    assert I.b58encode(bytes.fromhex("0000287fb4cd")) == "11233QC4"
    assert I.b58decode("11233QC4") == bytes.fromhex("0000287fb4cd")
    assert I.b58encode(bytes(32)) == "1" * 32 and I.b58decode("1" * 32) == bytes(32)   # System Program
    # SPL Token program id — известные 32 байта
    assert list(I.b58decode(I.TOKEN_PROGRAM)) == [6, 221, 246, 225, 215, 101, 161, 147, 217, 203, 225, 70, 206, 235,
                                                  121, 172, 28, 180, 133, 237, 95, 91, 55, 145, 58, 140, 245, 133, 126,
                                                  255, 0, 169]
    for bad in ("0abc", "Oabc", "Iabc", "labc", "", "a b"):
        with pytest.raises(ValueError) as ei:
            I.b58decode(bad)
        assert bad.strip() == "" or bad not in str(ei.value)          # текст ошибки не повторяет строку


def test_solana_address_is_case_sensitive():
    """S02: вариант mint с другим регистром — другие байты (или не адрес вовсе), не тот же актив."""
    assert I.is_sol_address(ANSEM) and I.sol_address(ANSEM) == ANSEM
    variant = "9CRCn9rGT8V2imeM2BaKs13yhMEais3ruM3rPvTGpump"           # одна буква в другом регистре
    assert I.is_sol_address(variant) and I.b58decode(variant) != I.b58decode(ANSEM)
    assert not I.is_sol_address(ANSEM.swapcase())                       # 'I' не из алфавита
    assert not I.is_sol_address(" " + ANSEM) and not I.is_sol_address(ANSEM + "1")
    reg = _reg(_entry())
    assert reg.by_asset("sol", ANSEM)[0].spot.mint == ANSEM
    assert reg.by_asset("sol", variant) == () and reg.by_asset("sol", ANSEM.lower()) == ()
    with pytest.raises(I.RegistryError):
        reg.resolve(I.SOL_HL, variant)
    assert I.canonical_asset_address("sol", ANSEM) == ANSEM             # регистр не тронут
    assert I.canonical_asset_address("solana-mainnet", ANSEM) == ANSEM
    assert I.canonical_asset_address("bsc", "0xE4Ebf0815d0980E5a03f7D675F86dc5079fB8919") == \
        "0xe4ebf0815d0980e5a03f7d675f86dc5079fb8919"                    # EVM — как раньше, нижний регистр
    with pytest.raises(ValueError):
        I.canonical_asset_address("solana", ANSEM.lower() + "x")
    with pytest.raises(KeyError):
        I.canonical_asset_address("eth", "0xE4Ebf0815d0980E5a03f7D675F86dc5079fB8919")


def test_network_names_and_chain_index_aliases():
    assert I.canonical_network("SOL") == I.canonical_network("solana") == "solana-mainnet"
    with pytest.raises(KeyError):
        I.canonical_network("501")                     # chainIndex OKX — id провайдера, не наш id сети
    assert tconfig.canonical_chain("sol") == tconfig.canonical_chain(" SOL ") == "solana"
    assert tconfig.chain_index("sol") == tconfig.chain_index("solana") == tconfig.chain_index("501") == "501"
    assert tconfig.chain_index("bsc") == tconfig.chain_index("56") == "56"          # BSC — как было
    for bad in ("eth", "", "solana-mainnet"):
        with pytest.raises(KeyError):
            tconfig.chain_index(bad)


# ================================ запись и файл ================================
def test_example_registry_loads_honestly():
    """Пример ТЗ: Token-2022 с метаданными принимается схемой (S03), live честно закрыт (identity pending)."""
    reg = I.load_registry(DATA / "instruments.example.json")
    s = reg.get("ansem_sol_para_v1")
    assert s.spot.mint == ANSEM and s.spot.token_program == I.TOKEN_2022_PROGRAM and s.spot.decimals == 6
    assert s.spot.extensions == ("metadataPointer", "tokenMetadata")
    assert (s.quote.mint, s.quote.decimals) == config.OKX_DEX_STABLES["501"]         # независимый источник USDC
    assert s.quote.symbol == "USDC" and s.quote.token_program == I.TOKEN_PROGRAM
    assert s.perp_key == ("mainnet", "hyperliquid", "para", "para:ANSEM") and s.perp.coin == "ANSEM"
    assert s.perp.asset_id == 180025 and s.perp.sz_decimals == 0 and s.perp.margin_mode == "noCross"
    assert (s.units.fs, s.units.fp) == (D(1), D(1))
    assert s.identity.status == "pending_underlying_evidence" and s.snapshot.must_refresh_before_live
    bl = " | ".join(I.entry_blockers(s, NOW))
    for needle in ("enabled_for_live", "pending_underlying_evidence", "expires_at", "Fs/Fp", "genesis_hash",
                   "account_id"):
        assert needle in bl
    assert "расширения" not in bl                      # metadata-only Token-2022 — не причина отказа
    assert I.protective_blockers(s) == []
    assert I.load_registry(DATA / "no_such.json").specs == ()         # нет файла — пустой реестр, не ошибка


def test_roundtrip_and_identity_hash():
    s = I.parse_instrument(_entry())
    assert s.to_dict() == _example()["instruments"][0]                 # форма файла сохраняется
    assert I.spec_from_json(s.to_json()) == s
    # решение владельца и срез — не смена инструмента: identity_hash тот же, record_hash другой
    reviewed = I.parse_instrument(_live(enabled_for_live=False, spot__genesis_hash=None, perp__account_id=None))
    assert reviewed.identity_hash == s.identity_hash and reviewed.record_hash != s.record_hash
    for path, v in (("spot__mint", "9CRCn9rGT8V2imeM2BaKs13yhMEais3ruM3rPvTGpump"), ("spot__decimals", 9),
                    ("spot__token_program", I.TOKEN_PROGRAM), ("units__perp_units_in_base", "1000"),
                    ("perp__fullcoin", "para:ANSEM2"), ("perp__account_id", "mainnet:0xa:0xb:para:usdc"),
                    ("spot__observed_extensions", ["metadataPointer"])):
        if path == "spot__token_program":
            other = I.parse_instrument(_entry(spot__token_program=v, spot__observed_extensions=[]))
        else:
            other = I.parse_instrument(_entry(**{path: v}))
        assert other.identity_hash != s.identity_hash, path
        with pytest.raises(I.RegistryError, match="без новой версии"):
            I.check_successor(s, other)                                 # X11: подмена без версии — отказ
        I.check_successor(s, dataclasses.replace(other, version=2))    # новой версией — можно
    with pytest.raises(I.RegistryError, match="меньше"):
        I.check_successor(dataclasses.replace(s, version=3), s)
    # «1» и «1.0» — одно число и один хеш
    assert I.parse_instrument(_entry(units__spot_units_in_base="1.0")).identity_hash == s.identity_hash


@pytest.mark.parametrize("paths, needle", [
    (dict(spot__decimalz=6), "неизвестный ключ decimalz"),
    (dict(spot__mint=_DEL), "нет ключа mint"),
    (dict(spot__network="solana-devnet"), "spot.network"),
    (dict(spot__genesis_hash=I.TOKEN_PROGRAM), "не genesis"),
    (dict(spot__token_program="11111111111111111111111111111111"), "token_program"),
    (dict(spot__decimals=-1), "decimals"),
    (dict(spot__decimals=True), "decimals"),
    (dict(spot__mint="0" + ANSEM[1:]), "base58"),
    (dict(spot__token_program=I.TOKEN_PROGRAM), "расширений не бывает"),
    (dict(quote__mint=I.TOKEN_2022_PROGRAM), "не котировочный"),
    (dict(quote__decimals=9), "decimals/программа"),
    (dict(units__spot_units_in_base="0"), "> 0"),
    (dict(units__perp_units_in_base="abc"), "не число"),
    (dict(units__status="ok"), "units.status"),
    (dict(perp__fullcoin="ANSEM"), "fullcoin"),
    (dict(perp__fullcoin="xyz:ANSEM"), "fullcoin"),
    (dict(perp__dex="PARA"), "perp.dex"),
    (dict(perp__venue="aster"), "perp.venue"),
    (dict(profile_id="bsc_okx_aster"), "profile_id"),
    (dict(identity__status="same"), "identity.status"),
    (dict(identity__status="reviewed_override"), "reviewed_override без"),
    (dict(identity__status="verified_source"), "verified_source без"),
    (dict(identity__evidence_hashes=["sha256:" + "0" * 64]), "хешей на"),
    (dict(identity__expires_at="2026-10-20T10:00:00"), "часовым поясом"),
    (dict(display_symbol="AN-SEM"), "display_symbol"),
    (dict(enabled_for_live="true"), "true или false"),
])
def test_registry_is_strict(paths, needle):
    with pytest.raises(I.RegistryError) as ei:
        _reg(_entry(**paths))
    assert needle in str(ei.value)


def test_registry_file_level_strictness():
    good = json.dumps({"schema_version": 1, "instruments": [_entry()]})
    assert len(I.parse_registry(good).specs) == 1
    with pytest.raises(I.RegistryError, match="повтор ключа"):
        I.parse_registry(good.replace('"version": 1,', '"version": 1, "version": 2,', 1))
    with pytest.raises(I.RegistryError, match="недопустимое число"):
        I.parse_registry(good.replace('"version": 1,', '"version": NaN,', 1))
    with pytest.raises(I.RegistryError, match="строка числа"):            # float JSON в множитель не пускаем
        I.parse_registry(good.replace('"spot_units_in_base": "1"', '"spot_units_in_base": 1.0', 1))
    with pytest.raises(I.RegistryError, match="повтор ansem_sol_para_v1 v1"):
        _reg(_entry(), _entry())
    with pytest.raises(I.RegistryError, match="schema_version"):
        I.parse_registry(json.dumps({"schema_version": 2, "instruments": []}))
    with pytest.raises(I.RegistryError, match="имя реестра"):
        I.registry_path("../owner.toml")
    assert I.registry_path().parent == config.RUNTIME and I.registry_path("x.json").name == "x.json"


# ================================ допуск ================================
def test_entry_gate_and_identity_expiry():
    """V03: истёкшее доказательство соответствия закрывает вход, свежесть котировок его не заменяет."""
    live = I.parse_instrument(_live())
    assert I.entry_blockers(live, NOW) == []
    late = I.ts_epoch("2026-10-20T10:00:01+00:00")
    assert any("срок истёк" in b for b in I.entry_blockers(live, late))
    assert any("enabled_for_live" in b for b in I.entry_blockers(I.parse_instrument(_live(enabled_for_live=False)), NOW))
    assert any("Fs/Fp" in b for b in I.entry_blockers(
        I.parse_instrument(_live(units__status="candidate_pending_mapping_acceptance")), NOW))
    assert any("расширения" in b for b in I.entry_blockers(live, NOW, allowed_mint_extensions=("tokenMetadata",)))
    # G05: для защитного действия по открытой позиции истёкший срок не мешает; G06: отозванное соответствие — мешает
    assert I.protective_blockers(live) == []
    revoked = I.parse_instrument(_live(identity__status="revoked"))
    assert I.protective_blockers(revoked) and I.entry_blockers(revoked, NOW)
    assert I.protective_blockers(I.parse_instrument(_live(units__status="revoked")))


def test_mint_snapshot_against_record():
    """S03–S06 на уровне реестра: свежий jsonParsed mint сверяется с записью; неизвестное расширение ≠ «налог 0»."""
    s = I.parse_instrument(_entry())
    v = _mint_value()
    assert I.mint_mismatches(s, v) == []
    for ext in ("transferHook", "transferFeeConfig", "permanentDelegate", "defaultAccountState", "fooBar"):
        w = copy.deepcopy(v)
        w["data"]["parsed"]["info"]["extensions"].append({"extension": ext, "state": {}})
        out = " | ".join(I.mint_mismatches(s, w))
        assert ext in out and "неподдержанные" in out and "изменились" in out
    w = copy.deepcopy(v)
    w["data"]["parsed"]["info"]["decimals"] = 9
    assert any("decimals 9" in x for x in I.mint_mismatches(s, w))
    w = copy.deepcopy(v)
    w["owner"] = I.TOKEN_PROGRAM
    assert any("программа" in x for x in I.mint_mismatches(s, w))
    w = copy.deepcopy(v)
    w["data"]["parsed"]["info"]["extensions"] = w["data"]["parsed"]["info"]["extensions"][:1]
    assert any("изменились" in x for x in I.mint_mismatches(s, w))
    w = copy.deepcopy(v)
    w["data"]["parsed"]["info"]["isInitialized"] = False
    assert any("инициализирован" in x for x in I.mint_mismatches(s, w))
    w = copy.deepcopy(v)
    w["data"]["parsed"]["info"]["extensions"].append({"state": {}})
    assert any("без имени" in x for x in I.mint_mismatches(s, w))
    assert I.mint_mismatches(s, None) == ["mint: аккаунт не найден"]
    assert I.mint_mismatches(s, {"owner": I.TOKEN_2022_PROGRAM, "data": ["base64", "AAAA"]})


def test_resolve_exact_ambiguous_and_versions():
    """V01/H01: ANSEM на двух dex — выбор, не первая строка; para:ANSEM — точное имя HIP-3; регистр значим."""
    para = _entry()
    xyz = _entry(instrument_id="ansem_sol_xyz_v1", perp__dex="xyz", perp__fullcoin="xyz:ANSEM")
    para2 = _entry(version=2, perp__account_id="mainnet:0xm:0xm:para:usdc")
    reg = _reg(para, xyz, para2)
    assert reg.get("ansem_sol_para_v1").version == 2 and reg.get("ansem_sol_para_v1", 1).version == 1
    with pytest.raises(I.AmbiguousInstrument) as ei:
        reg.resolve(I.SOL_HL, "ANSEM")
    assert ei.value.ids == ("ansem_sol_para_v1", "ansem_sol_xyz_v1")
    with pytest.raises(I.AmbiguousInstrument):
        reg.resolve(I.SOL_HL, ANSEM)                                    # один mint, два перпа
    assert reg.resolve(I.SOL_HL, "ANSEM", perp_dex="para").version == 2
    assert reg.resolve(I.SOL_HL, "para:ANSEM").perp.dex == "para"
    assert reg.resolve(I.SOL_HL, "ansem_sol_xyz_v1").perp.fullcoin == "xyz:ANSEM"
    assert reg.resolve(I.SOL_HL, "ANSEM", allowed=("ansem_sol_xyz_v1",)).perp.dex == "xyz"
    with pytest.raises(I.RegistryError, match="другим регистром") as ei:
        reg.resolve(I.SOL_HL, "ansem")                                  # подсказка, но не автовыбор
    assert not isinstance(ei.value, I.AmbiguousInstrument)
    with pytest.raises(I.RegistryError, match="нет записи"):
        reg.resolve(I.SOL_HL, "PARA:ANSEM2")
    assert reg.by_perp("mainnet", "hyperliquid", "para", "para:ANSEM")[0].version == 2
    assert reg.by_perp("mainnet", "hyperliquid", "", "ANSEM") == ()   # основной dex HL — не para:ANSEM
    revoked = _reg(_entry(identity__status="revoked"))
    with pytest.raises(I.RegistryError):
        revoked.resolve(I.SOL_HL, "ANSEM")


# ================================ единицы ================================
def _units(fs, fp, dec=0) -> I.InstrumentSpec:
    s = I.parse_instrument(_entry())
    return dataclasses.replace(s, spot=dataclasses.replace(s.spot, decimals=dec), units=I.Units(D(fs), D(fp), "accepted"))


def test_raw_human_exact():
    """U01/S09: сырые ↔ человеческие без float и без потери последней единицы."""
    assert I.raw_to_human(1_234_567, 6) == D("1.234567") and I.human_to_raw(D("1.234567"), 6) == 1_234_567
    assert I.raw_to_human(200_000_000, 6) == D(200)                    # USDC 200, а не 2e-10
    assert I.raw_to_human(1, 9) == D("0.000000001") and I.human_to_raw(D("0.000000001"), 9) == 1
    u64 = 18_446_744_073_709_551_615
    assert I.human_to_raw(I.raw_to_human(u64, 9), 9) == u64 and I.human_to_raw(I.raw_to_human(u64, 0), 0) == u64
    with pytest.raises(ValueError):
        I.human_to_raw(D("1.2345678"), 6)                              # не ложится на шаг — не округляем молча
    for bad in (1.5, "1", -1, True):
        with pytest.raises(ValueError):
            I.raw_to_human(bad, 6)
    with pytest.raises(ValueError):
        I.human_to_raw(1.5, 6)
    with pytest.raises(ValueError):
        I.human_to_raw(D(-1), 6)


def test_target_short_and_delta_formula():
    s = I.parse_instrument(_entry())                                   # ANSEM: Fs = Fp = 1, decimals 6
    assert I.target_short(1_234_567_890, s, D(1)) == 1234 and I.delta_base(1_234_567_890, D(1234), s) == D("0.567890")
    assert I.target_short(734_567_890, s, D(1)) == 734 and I.target_short(34_567_890, s, D(1)) == 34      # §8
    assert I.target_short(100_000, _units(1, 1000), D(1)) == 100 and I.delta_base(100_000, D(100), _units(1, 1000)) == 0  # U02
    assert I.target_short(2550, _units(1, 1000), D(1)) == 2 and I.delta_base(2550, D(2), _units(1, 1000)) == 550      # U03
    assert I.target_short(1950, _units(1, 1000), D(1)) == 1 and I.delta_base(1950, D(1), _units(1, 1000)) == 950     # U04
    u5 = _units(1, 1000)
    assert I.target_short(1950, u5, D("0.1")) == D("1.9") and I.delta_base(1950, D("1.9"), u5) == 50                 # U05
    u6 = _units(2, 1000)
    assert I.target_short(2750, u6, D(1)) == 5 and I.delta_base(2750, D(5), u6) == 500                                 # U06
    assert I.floor_step(D("0.99999999999999999999999999999999999"), D(1)) == 0     # не перепрыгивает целое
    assert I.floor_step(D("1.2345678"), D("0.000001")) == D("1.234567")            # H06 (шаг 10^-6)
    with pytest.raises(ValueError):
        I.floor_step(D(1), D(0))
    with pytest.raises(ValueError):
        I.delta_base(1, D(-1), s)                                       # S — модуль шорта, знак szi — у адаптера
