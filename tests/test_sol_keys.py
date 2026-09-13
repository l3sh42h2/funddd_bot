"""Ключи связки SOL × HL (ТЗ SOL×HL, CONNECTION §2.2–2.3). Приёмка: S19, M08, H05 (ключи/режим), M01/M02 (готовность
по профилю), часть H04 (чтения по счёту, агент — только подпись: адреса разведены).

Ключи — векторы RFC 8032 (Ed25519) и скаляры 5/6/7 (secp256k1): никаких реальных ключей и сети."""
import copy, dataclasses, json, logging, os, pickle
import pytest
from funding_bot.trade import instruments as I, keys, owner
from sol_hl_fixtures import (PUB1, PUB2, SECRET_FORMS, SEED1, SEED2, SIG1_EMPTY, SIG2_72, SOL_ADDR, SOL_SECRET_B58,
                             SpyEnv, evm_addr, fake_key, sol_toml, write)

SOL = owner.SOL_HL
HELIUS = "https://mainnet.helius-rpc.com/?api-key=00000000-1111-2222-3333-444444444444"
QN = "https://example-name.solana-mainnet.quiknode.pro/0123456789abcdef0123456789abcdef/"
SOL_NAMES = {"SOLANA_SECRET_B58", "SOLANA_KEYPAIR_FILE", "SOLANA_RPC_URL", "SOLANA_RPC_SECONDARY_URL", "SOLANA_WS_URL",
             "HL_AGENT_PRIVATE_KEY", "JUPITER_API_KEY", "OKX_DEX_API_KEY", "OKX_DEX_SECRET", "OKX_DEX_PASSPHRASE"}
ASTER_NAMES = {"DEX_EVM_KEY", "ASTER_SIGNER_KEY", "ASTER_SIGNER_ADDRESS", "ASTER_USER", "DEX_EVM_ADDRESS"}


@pytest.fixture(autouse=True)
def _clean_redaction():
    keys._reset_redaction_for_tests()
    yield
    keys._reset_redaction_for_tests()


def _cfg(tmp_path, mode="live", pmode="live", enabled="true", **over):
    o = {"": {"mode": f'"{mode}"'}, "profiles.sol_best_hyperliquid": {"mode": f'"{pmode}"', "enabled": enabled}}
    for k, v in over.items():
        o.setdefault(k.replace("__", "."), {}).update(v)
    return owner.load(write(tmp_path, sol_toml(o)))


def _env(**over):
    e = SpyEnv(SOLANA_SECRET_B58=SOL_SECRET_B58, HL_AGENT_PRIVATE_KEY=fake_key(5), SOLANA_RPC_URL=HELIUS,
               SOLANA_RPC_SECONDARY_URL=QN, JUPITER_API_KEY="jup-test-key-0123456789",
               OKX_DEX_API_KEY="okx-key-0123456789", OKX_DEX_SECRET="okx-secret-0123456789",
               OKX_DEX_PASSPHRASE="okx-pass-0123456789")
    for k, v in over.items():
        if v is None:
            dict.pop(e, k, None)
        else:
            dict.__setitem__(e, k, v)
    return e


def _clean(text: str) -> None:
    for f in SECRET_FORMS + (fake_key(5)[2:],):
        assert f not in text


_KF = iter(range(10**6))


def _keyfile(tmp_path, data, mode=0o600):
    p = tmp_path / f"sol_hl_{next(_KF)}.json"
    p.write_text(data if isinstance(data, str) else json.dumps(data), encoding="utf-8")
    os.chmod(p, mode)
    return str(p)


# ================================ ключ Solana и подпись ================================
def test_solana_key_rfc8032_vectors_and_contract(tmp_path):
    """Ed25519 — векторы RFC 8032 §7.1; загруженный ключ удовлетворяет протоколу sign.SolanaSigner (стык с потоком
    Solana: транзакцию он подписывает только через sign.sign_validated по закреплённому сообщению)."""
    from funding_bot.trade.solana.sign import SolanaSigner
    k1, k2 = keys.SolanaKey(SEED1), keys.SolanaKey(SEED2)
    assert k1.public_key() == I.b58encode(PUB1) and k2.address == I.b58encode(PUB2)
    assert k1.sign_message(b"") == SIG1_EMPTY and k2.sign_message(bytes([0x72])) == SIG2_72
    with pytest.raises(TypeError):
        k1.sign_message("текст")                                       # подписываются только байты
    with pytest.raises(keys.KeysError):
        keys.SolanaKey(SEED1 + PUB1)                                    # только seed 32 байта
    k = keys.load_sol_hl(_cfg(tmp_path), environ=_env())
    assert isinstance(k.sol, SolanaSigner) and k.sol.public_key() == SOL_ADDR
    assert k.sol.sign_message(b"") == SIG1_EMPTY
    k2 = keys.load_sol_hl(_cfg(tmp_path, wallets__sol_hl={"solana_address": f'"{I.b58encode(PUB2)}"'}),
                          environ=_env(SOLANA_SECRET_B58=I.b58encode(SEED2 + PUB2)))
    assert k2.sol.sign_message(bytes([0x72])) == SIG2_72
    for f in (pickle.dumps, copy.copy, copy.deepcopy):
        with pytest.raises(TypeError):
            f(k.sol)
    assert repr(k.sol) == str(k.sol) == f"{k.sol}" == "<key>" and not hasattr(k.sol, "__dict__")
    _clean(repr(k))


# ================================ режимы ================================
def test_dry_never_touches_environment(tmp_path, monkeypatch):
    cfg = _cfg(tmp_path, mode="dry")
    spy = _env()
    with pytest.raises(keys.KeysForbidden):
        keys.load_sol_hl(cfg, environ=spy)
    with pytest.raises(keys.KeysForbidden):
        keys.load_sol_hl(cfg, mode="live", environ=spy)               # запрос не повышает режим
    r = keys.profile_readiness(cfg, SOL, environ=spy)
    assert r.env_issues is None and not r.live_ready and spy.touched == []
    monkeypatch.setattr(keys.os, "environ", spy)
    with pytest.raises(keys.KeysForbidden):
        keys.load_sol_hl(cfg)
    assert spy.touched == []


def test_readonly_does_not_load_private_keys(tmp_path):
    """H05: readonly — публичные адреса и ключи API; приватные не разбираются (мусор в них не мешает), стираются."""
    env = _env(SOLANA_SECRET_B58="мусор-не-base58", HL_AGENT_PRIVATE_KEY="0x1234",
               SOLANA_KEYPAIR_FILE="/nonexistent/sol.json")
    k = keys.load_sol_hl(_cfg(tmp_path, pmode="readonly"), environ=env)
    assert k.mode == "readonly" and k.sol is None and k.hl is None
    assert k.solana_address == SOL_ADDR and k.hl_account == evm_addr(6) and k.hl_agent_address == evm_addr(5)
    assert "SOLANA_SECRET_B58" not in dict(env) and "HL_AGENT_PRIVATE_KEY" not in dict(env)
    assert k.jupiter.reveal() == "jup-test-key-0123456789" and k.okx.secret.reveal() == "okx-secret-0123456789"
    assert k.rpc_primary.display == "https://mainnet.helius-rpc.com/…" and k.rpc_ws is None
    assert not (env.names() & ASTER_NAMES)                             # M02: Aster не трогается
    k.gate("readonly", "signed_read")
    with pytest.raises(keys.ModeForbidden):
        k.gate("live", "send")                                         # повышение — только перезапуском
    # live в файле, readonly по запросу — то же
    k2 = keys.load_sol_hl(_cfg(tmp_path), mode="readonly", environ=_env())
    assert k2.mode == "readonly" and k2.sol is None
    # выключенная связка: не выше readonly даже при live в файле
    k3 = keys.load_sol_hl(_cfg(tmp_path, enabled="false"), environ=_env())
    assert k3.mode == "readonly" and k3.sol is None


def test_readonly_needs_public_addresses(tmp_path):
    cfg = _cfg(tmp_path, pmode="readonly", wallets__sol_hl={"solana_address": '""'})
    env = _env()
    with pytest.raises(keys.KeysError, match="wallets.sol_hl.solana_address"):
        keys.load_sol_hl(cfg, environ=env)
    assert "SOLANA_SECRET_B58" not in dict(env)                        # стёрт и при отказе


# ================================ live ================================
def test_live_base58_secret_and_agent(tmp_path):
    env = _env()
    k = keys.load_sol_hl(_cfg(tmp_path), environ=env)
    assert k.mode == "live" and k.sol.public_key() == SOL_ADDR and k.hl.address == evm_addr(5)
    assert "SOLANA_SECRET_B58" not in dict(env) and "HL_AGENT_PRIVATE_KEY" not in dict(env)
    assert not (env.names() & ASTER_NAMES)
    k.gate("live", "send")
    with pytest.raises(keys.ModeForbidden):
        k.gate("readonly", "send")
    with pytest.raises(keys.ModeForbidden, match="пауза"):
        k.gate("live", "send", paused=True)
    k.gate("live", "send", paused=True, hedge=True)
    _clean(repr(k))
    for obj in (k, k.sol, k.hl, k.jupiter, k.rpc_primary, k.okx):
        with pytest.raises(TypeError):
            copy.deepcopy(obj)
    for obj in (k.sol, k.hl, k.jupiter, k.rpc_primary):
        with pytest.raises(TypeError):
            pickle.dumps(obj)
    with pytest.raises(TypeError):
        dataclasses.asdict(k)                                          # план с ключами не сериализуется
    with pytest.raises(TypeError):
        json.dumps({"k": k.sol})
    assert "jup-test-key" not in repr(k) and "okx-secret" not in repr(k) and "api-key" not in repr(k)


def test_live_keypair_file(tmp_path):
    arr = list(SEED1 + PUB1)
    path = _keyfile(tmp_path, arr)
    k = keys.load_sol_hl(_cfg(tmp_path), environ=_env(SOLANA_SECRET_B58=None, SOLANA_KEYPAIR_FILE=path))
    assert k.sol.public_key() == SOL_ADDR
    bad = [
        (_keyfile(tmp_path, arr, 0o644), "0600"),
        (_keyfile(tmp_path, arr, 0o640), "0600"),
        (_keyfile(tmp_path, arr[:63]), "вместо 64"),
        (_keyfile(tmp_path, arr[:32]), "seed без публичной половины"),
        (_keyfile(tmp_path, arr[:63] + [256]), "0..255"),
        (_keyfile(tmp_path, [True] + arr[1:]), "0..255"),
        (_keyfile(tmp_path, "{not json"), "JSON"),
        (_keyfile(tmp_path, list(SEED1 + PUB2)), "публичная половина"),
        ("sol_hl.json", "абсолютный путь"),
        (str(tmp_path / "nope.json"), "недоступен"),
        (str(tmp_path), "не обычный файл"),
    ]
    for p, needle in bad:
        with pytest.raises(keys.KeysError) as ei:
            keys.load_sol_hl(_cfg(tmp_path), environ=_env(SOLANA_SECRET_B58=None, SOLANA_KEYPAIR_FILE=p))
        assert needle in str(ei.value), (p, str(ei.value))
        _clean(str(ei.value))
        assert str(arr[:8])[1:-1] not in str(ei.value)
    with pytest.raises(keys.KeysError, match="оставьте один"):
        keys.load_sol_hl(_cfg(tmp_path), environ=_env(SOLANA_KEYPAIR_FILE=path))
    with pytest.raises(keys.KeysError, match="нет SOLANA_SECRET_B58 или SOLANA_KEYPAIR_FILE"):
        keys.load_sol_hl(_cfg(tmp_path), environ=_env(SOLANA_SECRET_B58=None))


@pytest.mark.parametrize("env_over, needle, exc", [
    (dict(SOLANA_SECRET_B58=I.b58encode(SEED1 + PUB2)), "публичная половина", keys.KeysError),
    (dict(SOLANA_SECRET_B58=I.b58encode(SEED2 + PUB2)), "даёт адрес", keys.KeyMismatch),
    (dict(SOLANA_SECRET_B58=I.b58encode(SEED1)), "64-байтный", keys.KeysError),
    (dict(SOLANA_SECRET_B58="0x" + (SEED1 + PUB1).hex()), "не base58", keys.KeysError),
    (dict(SOLANA_SECRET_B58=(SEED1 + PUB1).hex()), "не base58", keys.KeysError),       # hex с «0» — не base58
    (dict(SOLANA_SECRET_B58=json.dumps(list(SEED1 + PUB1))), "JSON-массив", keys.KeysError),
    # S19: ключ EVM/HL (64 hex) в слоте Solana — отказ по роли
    (dict(SOLANA_SECRET_B58=fake_key(1)), "ключ EVM/HL", keys.KeysError),
    (dict(SOLANA_SECRET_B58=fake_key(1)[2:]), "ключ EVM/HL", keys.KeysError),
    # S19: секрет Solana в слоте агента HL — отказ по формату; ключ мастера — отказ по роли
    (dict(HL_AGENT_PRIVATE_KEY=SOL_SECRET_B58), "не 64 hex", keys.KeysError),
    (dict(HL_AGENT_PRIVATE_KEY=json.dumps(list(SEED1 + PUB1))), "не 64 hex", keys.KeysError),
    (dict(HL_AGENT_PRIVATE_KEY=fake_key(6)), "мастер-ключ", keys.KeyMismatch),
    (dict(HL_AGENT_PRIVATE_KEY=fake_key(7)), "hl_agent_address", keys.KeyMismatch),
    (dict(HL_AGENT_PRIVATE_KEY=None), "нет HL_AGENT_PRIVATE_KEY", keys.KeysError),
    (dict(HL_AGENT_PRIVATE_KEY="0x" + SEED1.hex()), "одна seed на две роли", keys.KeyMismatch),
    (dict(OKX_DEX_SECRET=None), "OKX: задан не весь набор — нет OKX_DEX_SECRET", keys.KeysError),
    (dict(SOLANA_RPC_URL="http://mainnet.example/?api-key=zz"), "SOLANA_RPC_URL: нужен URL https", keys.KeysError),
    (dict(SOLANA_RPC_SECONDARY_URL=HELIUS), "один и тот же URL", keys.KeysError),
    (dict(SOLANA_WS_URL="https://x.example"), "wss", keys.KeysError),
])
def test_live_refusals_never_print_secrets(tmp_path, env_over, needle, exc):
    env = _env(**env_over)
    with pytest.raises(exc) as ei:
        keys.load_sol_hl(_cfg(tmp_path), environ=env)
    msg = str(ei.value)
    assert needle in msg, msg
    _clean(msg)
    assert "api-key=zz" not in msg and "okx-secret" not in msg
    assert "SOLANA_SECRET_B58" not in dict(env) and "HL_AGENT_PRIVATE_KEY" not in dict(env)


def test_solana_secret_in_legacy_evm_slot_is_refused(tmp_path):
    """S19: старый загрузчик не примет секрет Solana ни как ключ BSC, ни как ключ Aster — по формату, без значения."""
    cfg = owner.load(write(tmp_path, f'mode = "live"\n[wallets]\nbsc = "{evm_addr(1)}"\naster_user = "{evm_addr(3)}"\n'
                                     f'aster_signer = "{evm_addr(2)}"\n'))
    for name in ("DEX_EVM_KEY", "ASTER_SIGNER_KEY"):
        env = SpyEnv(DEX_EVM_KEY=fake_key(1), ASTER_SIGNER_KEY=fake_key(2), ASTER_SIGNER_ADDRESS=evm_addr(2))
        dict.__setitem__(env, name, SOL_SECRET_B58)
        with pytest.raises(keys.KeysError, match=f"{name}: не 64 hex") as ei:
            keys.load(cfg, environ=env)
        _clean(str(ei.value))


def test_legacy_and_sol_loaders_do_not_touch_each_other(tmp_path):
    """M01/M02: старый загрузчик не читает переменные Solana/HL, новый — Aster/EVM."""
    legacy = owner.load(write(tmp_path, f'mode = "live"\n[wallets]\nbsc = "{evm_addr(1)}"\naster_user = "{evm_addr(3)}"\n'
                                        f'aster_signer = "{evm_addr(2)}"\n', "legacy.toml"))
    env = _env(DEX_EVM_KEY=fake_key(1), ASTER_SIGNER_KEY=fake_key(2), ASTER_SIGNER_ADDRESS=evm_addr(2))
    keys.load(legacy, environ=env)
    assert not (env.names() & SOL_NAMES) and "SOLANA_SECRET_B58" in dict(env)
    env2 = _env(DEX_EVM_KEY=fake_key(1), ASTER_SIGNER_KEY=fake_key(2))
    keys.load_sol_hl(_cfg(tmp_path), environ=env2)
    assert not (env2.names() & ASTER_NAMES) and "DEX_EVM_KEY" in dict(env2)
    # тот же ключ сначала как EVM, потом как агент HL — «одна seed на две роли»
    env3 = _env(HL_AGENT_PRIVATE_KEY=fake_key(1))
    with pytest.raises(keys.KeyMismatch, match="уже загружен как DEX_EVM_KEY"):
        keys.load_sol_hl(_cfg(tmp_path), environ=env3)


# ================================ готовность по профилю ================================
def test_readiness_per_profile(tmp_path):
    cfg = _cfg(tmp_path)
    r = keys.profile_readiness(cfg, SOL, environ=_env())
    assert r.live_ready and r.env_issues == () and r.blockers == () and r.enabled
    r = keys.profile_readiness(cfg, SOL, environ=_env(JUPITER_API_KEY=None, SOLANA_RPC_SECONDARY_URL=None))
    assert not r.live_ready and set(r.env_issues) == {"JUPITER_API_KEY", "SOLANA_RPC_SECONDARY_URL"}
    r = keys.profile_readiness(cfg, SOL, environ=_env(SOLANA_KEYPAIR_FILE="/x/sol.json"))
    assert any("заданы оба" in x for x in r.env_issues)
    r = keys.profile_readiness(cfg, SOL, environ=_env(SOLANA_SECRET_B58=None))
    assert "SOLANA_SECRET_B58 или SOLANA_KEYPAIR_FILE" in r.env_issues
    okx_only = _cfg(tmp_path, routing__solana={"paths": '["okx_solana_v6"]'})
    assert keys.profile_readiness(okx_only, SOL, environ=_env(JUPITER_API_KEY=None)).live_ready
    # M02: для связки Solana ключи Aster не нужны; M01: для старой — не нужны ключи Solana
    spy = _env()
    keys.profile_readiness(cfg, SOL, environ=spy)
    assert not (spy.names() & ASTER_NAMES)
    leg = keys.profile_readiness(cfg, owner.LEGACY_PROFILE, environ=spy)
    assert set(leg.env_issues) == {"DEX_EVM_KEY", "ASTER_SIGNER_KEY", "ASTER_SIGNER_ADDRESS"}
    assert not leg.live_ready
    legacy = owner.load(write(tmp_path, 'mode = "readonly"\n', "legacy.toml"))
    spy2 = SpyEnv()
    r = keys.profile_readiness(legacy, owner.LEGACY_PROFILE, environ=spy2)
    assert not (spy2.names() & SOL_NAMES)
    r = keys.profile_readiness(legacy, SOL, environ=spy2)
    assert r.mode == "dry" and r.env_issues is None and "выключена" in " ".join(r.blockers)


# ================================ маскировка (M08) ================================
def test_redaction_covers_every_secret_form(tmp_path):
    keys.load_sol_hl(_cfg(tmp_path), environ=_env(SOLANA_RPC_SECONDARY_URL=QN))
    full = SEED1 + PUB1
    text = " ".join([SEED1.hex(), full.hex(), full.hex().upper(), SOL_SECRET_B58, I.b58encode(SEED1),
                     json.dumps(list(full)), json.dumps(list(full), separators=(",", ":")), str(list(SEED1)),
                     "jup-test-key-0123456789", "okx-pass-0123456789", HELIUS, QN, fake_key(5)])
    for fn in (keys.redact_secrets, keys.redact):
        out = fn(text)
        _clean(out)
        for leak in ("157, 97, 177", "157,97,177", "jup-test-key", "okx-pass", "44444444-", "0123456789abcdef0123"):
            assert leak not in out, (fn.__name__, leak)
    # публичное остаётся: адрес кошелька, mint, помеченная подпись и хэш
    sig = I.b58encode(SIG1_EMPTY)
    keys.mark_public(sig)
    kept = f"addr {SOL_ADDR} mint {I.TOKEN_2022_PROGRAM} sig {sig}"
    assert keys.redact(kept) == kept == keys.redact_secrets(kept)


def test_generic_redaction_patterns():
    sig = I.b58encode(SIG2_72)                                          # 64 байта base58 — форма подписи и секрета
    assert keys.redact(f"tx {sig}") == "tx <b58-64>" and keys.redact_secrets(f"tx {sig}") == f"tx {sig}"
    keys.mark_public(sig)
    assert keys.redact(f"tx {sig}") == f"tx {sig}"
    arr = json.dumps(list(range(40)))
    assert keys.redact(f"bytes {arr}") == "bytes <bytes>" and keys.redact(f"[{', '.join(['300'] * 40)}]") != "<bytes>"
    assert keys.redact("GET https://rpc.example/?api-key=abcd-1234&x=1") == "GET https://rpc.example/?api-key=<redacted>&x=1"
    assert keys.redact_secrets("x-api-key: SeCrEt123 next") == "x-api-key: <redacted> next"
    assert keys.redact_secrets('{"OK-ACCESS-PASSPHRASE": "p4ss"}') == '{"OK-ACCESS-PASSPHRASE": "<redacted>"}'
    assert keys.redact(f"mint {I.b58encode(PUB2)}") == f"mint {I.b58encode(PUB2)}"   # 32 байта — адрес, не прячем


def test_log_formatter_masks_solana_secret(tmp_path):
    keys.load_sol_hl(_cfg(tmp_path), environ=_env())
    lg = logging.getLogger("fb.test.sol.redact")
    h = logging.Handler()
    out = []
    h.emit = lambda r: out.append(h.format(r))
    lg.addHandler(h)
    try:
        keys.install_log_redaction(lg)
        try:
            raise RuntimeError(f"boom {SOL_SECRET_B58} {json.dumps(list(SEED1 + PUB1))} {HELIUS}")
        except RuntimeError:
            lg.exception("send failed")
    finally:
        lg.removeHandler(h)
    assert out
    _clean(out[0])
    assert "44444444" not in out[0] and "157, 97" not in out[0]


# ================================ логин/пароль в URL RPC (PF-1) ================================
@pytest.mark.parametrize("name, url, secret", [
    ("SOLANA_RPC_SECONDARY_URL", "https://alice:Sup3rS3cretPw@rpc.example.com/", "Sup3rS3cretPw"),
    ("SOLANA_RPC_SECONDARY_URL", "https://tok_9f8e7d6c5b4a3210@rpc.example.com", "tok_9f8e7d6c5b4a3210"),  # логин
    ("SOLANA_RPC_URL", "https://u:pw-0123456789@rpc.example.com:8899/p?x=1", "pw-0123456789"),
    ("SOLANA_WS_URL", "wss://alice:Sup3rS3cretPw@ws.example.com/", "Sup3rS3cretPw"),
])
def test_rpc_url_with_userinfo_is_refused_and_masked(tmp_path, name, url, secret):
    """Раньше userinfo становился «хостом»: display/host и repr(SolHlKeys) показывали пароль, redact его не знал."""
    env = _env(**{name: url})
    with pytest.raises(keys.KeysError, match="логин/пароль в URL") as ei:
        keys.load_sol_hl(_cfg(tmp_path), mode="readonly", environ=env)
    msg = str(ei.value)
    assert name in msg and secret not in msg and "example.com" not in msg
    # с момента отказа пароль (и токен-логин) маскируются и по отдельности, и в составе URL
    for fn in (keys.redact_secrets, keys.redact):
        out = fn(f"GET {url} failed; auth={secret}; pw {secret}")
        assert secret not in out, (fn.__name__, out)


def test_rpc_url_at_sign_in_path_is_fine_and_display_never_shows_userinfo(tmp_path):
    url = "https://rpc.example.com/v1/tok@0123456789abcdef"          # «@» в пути — не userinfo
    k = keys.load_sol_hl(_cfg(tmp_path), mode="readonly", environ=_env(SOLANA_RPC_SECONDARY_URL=url))
    assert k.rpc_secondary.host == "rpc.example.com" and k.rpc_secondary.display == "https://rpc.example.com/…"
    assert "0123456789abcdef" not in repr(k) and "0123456789abcdef" not in keys.redact(url)
    raw = keys.SecretUrl("https://alice:Sup3rS3cretPw@rpc.example.com/", "X")   # в обход загрузчика
    assert raw.host == "?" and raw.display == "<url>"
    assert "Sup3rS3cretPw" not in repr(raw) and "alice" not in repr(raw)
