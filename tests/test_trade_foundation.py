"""Фундамент фазы 2: строгий owner.toml, ворота ключей, маскировка, trade.db и запись-до.

Ключи в тестах — заведомо фальшивые скаляры 1, 2, 3… (никаких реальных ключей и сетевых вызовов)."""
import copy, logging, pickle, sqlite3
from decimal import Decimal
from pathlib import Path
import pytest
from funding_bot.trade import keys, owner, store, tconfig
from funding_bot.trade.types import PerpFill, PerpLeg, SpotLeg

ROOT = Path(__file__).resolve().parents[1]
EXAMPLE = ROOT / "deploy" / "owner.toml.example"
WALLET = "0xE4Ebf0815d0980E5a03f7D675F86dc5079fB8919"


def _fake_key(n: int) -> str:
    return "0x" + f"{n:064x}"


def _addr(n: int) -> str:
    acct = pytest.importorskip("eth_account").Account
    return acct.from_key(bytes.fromhex(f"{n:064x}")).address


def _write(tmp_path, text: str) -> Path:
    p = tmp_path / "owner.toml"
    p.write_text(text, encoding="utf-8")
    return p


@pytest.fixture(autouse=True)
def _clean_redaction():
    keys._reset_redaction_for_tests()
    yield
    keys._reset_redaction_for_tests()


# ================================ owner.toml ================================
def test_example_loads_with_owner_values_and_lists_live_blockers():
    cfg = owner.load(EXAMPLE)
    assert cfg.mode == "dry" and cfg.owner_id is None
    assert cfg.get("wallets.bsc") == WALLET
    assert cfg.get("dex.slippage_pct") == Decimal("3") and isinstance(cfg.get("dex.slippage_pct"), Decimal)
    assert cfg.get("perp.aster.leverage") == 1 and cfg.get("perp.aster.margin_type") == "ISOLATED"
    assert cfg.get("limits.deal_max_usd_per_leg") == Decimal(500) and cfg.get("limits.max_open_deals") == 1
    assert cfg.get("exec.clip_max_usd") == "auto" and cfg.get("limits.daily_loss_stop_usd") == "off"
    # решения владельца 12.09: DEX-ключи заданы, α/β/лимиты исполнения — "auto" (для live «auto» = задано)
    assert cfg.get("dex.impact_cap_pct") == 3 and cfg.get("dex.approve_policy") == "unlimited"
    assert cfg.get("dex.broadcast") == "public" and cfg.get("dex.allow_tax_tokens") is False
    assert cfg.get("dex.native_reserve") == Decimal("0.001")
    assert cfg.get("exec.refill_wait_max_s") == 30 and cfg.get("exec.plan_cost_drift_pct") == Decimal("0.5")
    for k in ("perp.aster.max_slip_bps", "perp.aster.touch_frac_max", "perp.aster.liq_alert_pct", "exec.clips_max",
              "exec.unhedged_usd_max", "exec.exec_time_max_s"):
        assert cfg.get(k) == "auto", k
    miss = cfg.live_missing("aster")
    assert miss == ["telegram.owner_id", "wallets.aster_user", "wallets.aster_signer"]
    # заданное владельцем, «auto» и «пусто = осмысленно» не блокирует live
    for k in ("wallets.bsc", "dex.slippage_pct", "perp.aster.leverage", "limits.daily_loss_stop_usd",
              "limits.daily_loss_basis", "dex.clip_slippage_pct", "perp.aster.maker_allowed",
              "exec.auto_unwind_naked_after_s", "limits.min_entry_funding_pct_h", "limits.min_entry_basis_bps",
              "perp.aster.max_slip_bps", "exec.unhedged_usd_max", "exec.clips_max"):
        assert k not in miss
    with pytest.raises(owner.OwnerMissing) as ei:
        cfg.require("dex.slippage_pct", "telegram.owner_id", "wallets.aster_user")
    assert ei.value.key == "telegram.owner_id"
    assert ei.value.keys == ("telegram.owner_id", "wallets.aster_user")
    assert "telegram.owner_id" in str(ei.value)
    assert cfg.require("dex.slippage_pct", "exec.clip_max_usd") == (Decimal("3"), "auto")


def test_numeric_daily_stop_requires_its_definition(tmp_path):
    cfg = owner.load(_write(tmp_path, "[limits]\ndaily_loss_stop_usd = 25.5\n"))
    assert cfg.get("limits.daily_loss_stop_usd") == Decimal("25.5")
    assert "limits.daily_loss_basis" in cfg.live_required("aster")


@pytest.mark.parametrize("sec, key", [("perp.aster", "max_slip_bps"), ("perp.aster", "touch_frac_max"),
                                      ("perp.aster", "liq_alert_pct"), ("exec", "clip_max_usd"), ("exec", "clips_max"),
                                      ("exec", "unhedged_usd_max"), ("exec", "exec_time_max_s")])
def test_auto_word_is_accepted_and_counts_as_set(tmp_path, sec, key):
    """«auto» (владелец 12.09) — задано: число подбирает бот. Копия сделки хранит слово, опечатка — ошибка."""
    full = f"{sec}.{key}"
    extra = "\n[exec]\nplan_cost_drift_pct = 0.5\n" if sec != "exec" else "plan_cost_drift_pct = 0.5\n"
    cfg = owner.load(_write(tmp_path, f'[{sec}]\n{key} = "auto"\n{extra}'))
    assert cfg.get(full) == "auto" and cfg.is_set(full) and full not in cfg.live_missing("aster")
    assert owner.OwnerCfg.from_frozen(cfg.frozen_json()).get(full) == "auto"
    for typo in ('"avto"', '"AUTO"', '"авто"'):
        with pytest.raises(owner.OwnerConfigError) as ei:
            owner.load(_write(tmp_path, f"[{sec}]\n{key} = {typo}\n"))
        assert full in str(ei.value) and "auto" in str(ei.value)
    with pytest.raises(owner.OwnerConfigError, match="целое"):
        owner.load(_write(tmp_path, "[exec]\nclips_max = 2.5\n"))


def test_unhedged_auto_without_cost_drift_is_missing_for_live(tmp_path):
    """unhedged_usd_max = "auto" = plan_cost_drift_pct % клипа: без допуска числа нет — для live это пусто."""
    cfg = owner.load(_write(tmp_path, '[exec]\nunhedged_usd_max = "auto"\n'))
    miss = cfg.live_missing("aster")
    assert "exec.unhedged_usd_max" in miss and "exec.plan_cost_drift_pct" in miss
    with pytest.raises(owner.OwnerMissing) as ei:
        cfg.require_live("aster")
    assert "exec.unhedged_usd_max" in ei.value.keys
    ok = owner.load(_write(tmp_path, '[exec]\nunhedged_usd_max = "auto"\nplan_cost_drift_pct = 0.5\n'))
    assert "exec.unhedged_usd_max" not in ok.live_missing("aster")


@pytest.mark.parametrize("text, needle", [
    ("[dex]\nslipage_pct = 3\n", "dex.slipage_pct"),                         # опечатка не молчит
    ("[risk]\nx = 1\n", "risk"),
    ("[perp.bybit]\nleverage = 1\n", "perp.bybit"),
    ("[perp.aster]\nleverage = 1\nlevrage = 2\n", "perp.aster.levrage"),
    ("[dex]\nslippage_pct = \"3\"\n", "без кавычек"),                        # число строкой
    ("[dex]\nslippage_pct = true\n", "dex.slippage_pct"),                     # bool не число
    ("[dex]\nslippage_pct = 0\n", "dex.slippage_pct"),
    ("[dex]\nslippage_pct = 101\n", "dex.slippage_pct"),
    ("[dex]\nslippage_pct = nan\n", "конечное"),
    ("[perp.aster]\nleverage = 1.5\n", "целое"),
    ("[perp.aster]\ntouch_frac_max = 1.5\n", "touch_frac_max"),
    ("[perp.aster]\nmargin_type = \"isolated\"\n", "ISOLATED"),               # регистр значим
    ("[dex]\nallow_tax_tokens = \"true\"\n", "true или false"),
    ("[exec]\nclip_max_usd = \"авто\"\n", "auto"),
    ("[wallets]\nbsc = \"0xE4Ebf0815d0980E5a03f7D675F86dc5079fB891\"\n", "40 hex"),
    ("[wallets]\nbsc = \"0xe4Ebf0815d0980E5a03f7D675F86dc5079fB8919\"\n", "EIP-55"),   # одна буква регистра
    ("[telegram]\nowner_id =\n", "\"\""),                                    # TOML без значения
    ("[dex]\nslippage_pct = 1\nclip_slippage_pct = 2\n", "потолок"),
    ("mode = \"prod\"\n", "mode"),
])
def test_owner_loader_is_strict(tmp_path, text, needle):
    with pytest.raises(owner.OwnerConfigError) as ei:
        owner.load(_write(tmp_path, text))
    assert needle in str(ei.value)


def test_signer_equal_to_user_is_refused_at_load(tmp_path):
    a = WALLET
    with pytest.raises(owner.OwnerConfigError, match="мастер-ключ"):
        owner.load(_write(tmp_path, f'[wallets]\naster_user = "{a}"\naster_signer = "{a.lower()}"\n'))


def test_decimals_exact_and_empty_is_none(tmp_path):
    cfg = owner.load(_write(tmp_path, '[dex]\nslippage_pct = 0.1\nimpact_cap_pct = ""\n[perp.aster]\nmaker_allowed = false\n'))
    assert cfg.get("dex.slippage_pct") == Decimal("0.1")          # не 0.1000000000000000055…
    assert not cfg.is_set("dex.impact_cap_pct") and cfg.get("dex.impact_cap_pct", "x") == "x"
    assert cfg.get("perp.aster.maker_allowed") is False and cfg.is_set("perp.aster.maker_allowed")
    with pytest.raises(KeyError):
        cfg.get("dex.no_such_key")                                   # опечатка в коде ≠ «пусто»


def test_missing_file_is_all_empty_dry(tmp_path):
    cfg = owner.load(tmp_path / "nope.toml")
    assert cfg.mode == "dry" and cfg.sha256 is None
    assert all(v is None for v in cfg.values.values())
    assert "wallets.bsc" in cfg.live_missing()


def test_reread_on_every_call_and_frozen_copy(tmp_path):
    p = _write(tmp_path, '[dex]\nslippage_pct = 3\n')
    first = owner.load(p)
    p.write_text('mode = "readonly"\n[dex]\nslippage_pct = 2.5\n', encoding="utf-8")
    second = owner.load(p)
    assert first.get("dex.slippage_pct") == 3 and second.get("dex.slippage_pct") == Decimal("2.5")
    assert second.mode == "readonly" and first.sha256 != second.sha256
    with pytest.raises(TypeError):
        second.values["dex.slippage_pct"] = 1                       # снимок неизменяем
    fj = first.frozen_json()
    assert '"dex.slippage_pct":"3"' in fj                            # Decimal строкой, без float
    back = owner.OwnerCfg.from_frozen(fj)
    assert back.get("dex.slippage_pct") == Decimal(3) and back.sha256 == first.sha256
    assert dict(back.values) == dict(first.values)


# ================================ keys ================================
class _SpyEnv(dict):
    """Окружение, которое запоминает любое обращение."""

    def __init__(self, *a, **kw):
        super().__init__(*a, **kw)
        self.touched = []

    def __getitem__(self, k):
        self.touched.append(("get", k)); return super().__getitem__(k)

    def get(self, k, d=None):
        self.touched.append(("get", k)); return super().get(k, d)

    def pop(self, k, *d):
        self.touched.append(("pop", k)); return super().pop(k, *d)

    def __contains__(self, k):
        self.touched.append(("in", k)); return super().__contains__(k)

    def keys(self):
        self.touched.append(("keys", None)); return super().keys()

    def items(self):
        self.touched.append(("items", None)); return super().items()


def _cfg(tmp_path, mode="live", bsc=None, user=None, signer=None):
    bsc = bsc or _addr(1)
    user = user or _addr(3)
    signer = signer or _addr(2)
    return owner.load(_write(tmp_path, f'mode = "{mode}"\n[wallets]\nbsc = "{bsc}"\naster_user = "{user}"\n'
                                       f'aster_signer = "{signer}"\n'))


def _env(**over):
    e = _SpyEnv(DEX_EVM_KEY=_fake_key(1), ASTER_SIGNER_KEY=_fake_key(2), ASTER_SIGNER_ADDRESS=_addr(2),
                ASTER_USER=_addr(3), DEX_EVM_ADDRESS=_addr(1))
    for k, v in over.items():
        if v is None:
            dict.pop(e, k, None)
        else:
            dict.__setitem__(e, k, v)
    return e


def test_dry_never_touches_environment(tmp_path, monkeypatch):
    cfg = owner.load(_write(tmp_path, f'[wallets]\nbsc = "{WALLET}"\n'))       # mode пуст → dry
    spy = _env()
    with pytest.raises(keys.KeysForbidden):
        keys.load(cfg, environ=spy)
    with pytest.raises(keys.KeysForbidden):
        keys.load(cfg, mode="live", environ=spy)          # запрос не повышает режим файла
    assert spy.touched == []
    monkeypatch.setattr(keys.os, "environ", spy)          # и окружение процесса по умолчанию — тоже не трогается
    with pytest.raises(keys.KeysForbidden):
        keys.load(cfg)
    assert spy.touched == [] and "DEX_EVM_KEY" in dict(spy)


def test_empty_wallet_value_is_a_keys_error_and_env_is_still_scrubbed(tmp_path):
    """Пустой wallets.* в readonly/live — ошибка настройки (KeysError → трейдер выходит с кодом 78 и пишет
    владельцу), а не «сеть» с молчаливым рестартом каждые 5 с."""
    cfg = owner.load(_write(tmp_path, f'mode = "readonly"\n[wallets]\nbsc = "{_addr(1)}"\naster_user = ""\n'
                                      f'aster_signer = "{_addr(2)}"\n'))
    env = _env()
    with pytest.raises(keys.KeysError, match="aster_user"):
        keys.load(cfg, environ=env)
    assert "DEX_EVM_KEY" not in dict(env) and "ASTER_SIGNER_KEY" not in dict(env)


def test_live_load_verifies_addresses_and_scrubs_env(tmp_path):
    cfg = _cfg(tmp_path)
    env = _env()
    k = keys.load(cfg, environ=env)
    assert k.mode == "live" and k.evm is not None
    assert k.evm.address == _addr(1) and k.aster.address == _addr(2) and k.aster_user == _addr(3)
    assert "DEX_EVM_KEY" not in dict(env) and "ASTER_SIGNER_KEY" not in dict(env)   # стёрты из окружения
    assert "ASTER_SIGNER_ADDRESS" in dict(env)
    # обёртка не показывает и не отдаёт ключ
    for s in (repr(k), str(k.aster), f"{k.evm}", repr([k.aster])):
        assert f"{1:064x}" not in s and f"{2:064x}" not in s
    assert str(k.aster) == "<key>" and "<key>" in repr(k)
    assert not hasattr(k.aster, "key") and not hasattr(k.aster, "__dict__")
    for f in (pickle.dumps, copy.copy, copy.deepcopy):
        with pytest.raises(TypeError):
            f(k.aster)
    # подпись работает (EIP-712 домен Aster) и восстанавливается в адрес агента
    from eth_account import Account
    from eth_account.messages import encode_typed_data
    msg = encode_typed_data(full_message={
        "types": {"EIP712Domain": [{"name": "name", "type": "string"}, {"name": "version", "type": "string"},
                                   {"name": "chainId", "type": "uint256"}, {"name": "verifyingContract", "type": "address"}],
                  "Message": [{"name": "msg", "type": "string"}]},
        "primaryType": "Message",
        "domain": {"name": "AsterSignTransaction", "version": "1", "chainId": tconfig.ASTER_EIP712_CHAIN_ID,
                   "verifyingContract": "0x" + "00" * 20},
        "message": {"msg": "symbol=AIW3USDT&nonce=1"}})
    assert Account.recover_message(msg, signature=k.aster.sign_message(msg).signature) == _addr(2)


def test_readonly_keeps_only_aster_agent(tmp_path):
    k = keys.load(_cfg(tmp_path, mode="readonly"), environ=_env())
    assert k.mode == "readonly" and k.evm is None and k.evm_address == _addr(1)
    # live в owner.toml → readonly по запросу CLI: понижение разрешено
    k2 = keys.load(_cfg(tmp_path, mode="live"), mode="readonly", environ=_env())
    assert k2.mode == "readonly" and k2.evm is None


@pytest.mark.parametrize("cfg_over, env_over, needle", [
    (dict(bsc=WALLET), {}, "DEX_EVM_KEY даёт адрес"),                        # ключ не от кошелька владельца
    ({}, dict(DEX_EVM_ADDRESS=WALLET), "DEX_EVM_ADDRESS"),
    ({}, dict(ASTER_SIGNER_ADDRESS=WALLET), "ASTER_SIGNER_ADDRESS"),         # derived != ASTER_SIGNER_ADDRESS
    ({}, dict(ASTER_SIGNER_ADDRESS=None), "нет ASTER_SIGNER_ADDRESS"),
    (dict(signer=WALLET), {}, "aster_signer"),
    ({}, dict(ASTER_USER="__derived__"), "мастер-ключ"),                    # derived == ASTER_USER (.env)
    (dict(user="__derived__", signer=WALLET), dict(ASTER_USER=None), "мастер-ключ"),   # derived == aster_user (toml)
    ({}, dict(ASTER_USER=WALLET), "ASTER_USER"),
    ({}, dict(DEX_EVM_KEY=None), "нет DEX_EVM_KEY"),
    ({}, dict(ASTER_SIGNER_KEY="0x1234"), "не 64 hex"),
    ({}, dict(ASTER_SIGNER_KEY="0x" + "00" * 32), "недопустимый"),          # eth_account сам нулевой принимает
    ({}, dict(DEX_EVM_KEY="ff" * 32), "недопустимый"),                       # ≥ порядка кривой
])
def test_key_mismatch_refusals(tmp_path, cfg_over, env_over, needle):
    cfg_over = {k: (_addr(2) if v == "__derived__" else v) for k, v in cfg_over.items()}
    env_over = {k: (_addr(2) if v == "__derived__" else v) for k, v in env_over.items()}
    env = _env(**env_over)
    with pytest.raises(keys.KeysError) as ei:
        keys.load(_cfg(tmp_path, **cfg_over), environ=env)
    msg = str(ei.value)
    assert needle in msg
    assert f"{1:064x}" not in msg and f"{2:064x}" not in msg               # значения ключей не печатаются
    assert "DEX_EVM_KEY" not in dict(env) and "ASTER_SIGNER_KEY" not in dict(env)   # стёрты и при отказе


def test_gate_modes_pause_and_hedge(tmp_path):
    keys.gate("dry", "public")
    with pytest.raises(keys.ModeForbidden):
        keys.gate("dry", "signed_read")
    keys.gate("readonly", "signed_read")
    with pytest.raises(keys.ModeForbidden):
        keys.gate("readonly", "send")
    keys.gate("live", "send")
    with pytest.raises(keys.ModeForbidden, match="пауза"):
        keys.gate("live", "send", paused=True)
    keys.gate("live", "send", paused=True, hedge=True)       # хедж уже исполненной ноги на «стоп» (Q7)
    assert not keys.mode_allows(None, "signed_read") and keys.mode_allows("live", "signed_read")
    k = keys.load(_cfg(tmp_path), environ=_env())
    k.gate("live", "send")
    with pytest.raises(keys.ModeForbidden):
        k.gate("readonly", "send")                           # владелец понизил режим — отправок нет
    kr = keys.load(_cfg(tmp_path, mode="readonly"), environ=_env())
    with pytest.raises(keys.ModeForbidden):
        kr.gate("live", "send")                              # повышение только перезапуском


def test_redact_tokens_keys_and_hashes():
    tok = "https://api.telegram.org/bot123456789:AAHdqTcvCH1vGWJxfSeofSAs0K5PALDsaw-x/getUpdates"
    assert keys.redact(tok) == "https://api.telegram.org/bot<redacted>/getUpdates"
    h = "ab" * 32
    assert keys.redact(f"key={h}") == "key=<hex64>"
    assert keys.redact(f"0x{h.upper()} end") == "<hex64> end"
    raw_tx = "0x02f8" + "cd" * 60                                     # длиннее 64 — подписанная tx, не ключ
    assert keys.redact(raw_tx) == raw_tx
    txh = "0x" + "ef" * 32
    keys.mark_public(txh)
    assert keys.redact(f"tx {txh}") == f"tx {txh}"                     # хэш транзакции помечен публичным
    keys._remember_secret("12" * 32)
    keys.mark_public("0x" + "12" * 32)                                # секрет публичным не становится
    assert "12" * 32 not in keys.redact("x" + "12" * 32 + "y")        # и внутри слова — тоже
    assert keys.redact_secrets(f"err {'12' * 32} {txh} bot1:abc") == f"err <key> {txh} bot<redacted>"


def test_log_formatter_redacts():
    lg = logging.getLogger("fb.test.redact")
    h = logging.Handler()
    out = []
    h.emit = lambda r: out.append(h.format(r))
    lg.addHandler(h)
    try:
        keys.install_log_redaction(lg)
        keys.install_log_redaction(lg)                               # повторно — без двойной обёртки
        assert isinstance(h.formatter, keys.RedactFormatter) and not isinstance(h.formatter.inner, keys.RedactFormatter)
        lg.warning("poll failed %s", "https://api.telegram.org/bot42:SeCrEt_-x/getUpdates")
    finally:
        lg.removeHandler(h)
    assert out and "SeCrEt" not in out[0] and "bot<redacted>" in out[0]


# ================================ tconfig / types ================================
def test_tconfig_allowlists_and_rpc(monkeypatch):
    assert tconfig.router_allowed("bsc", "0x5994814F2c4040B863a0125a45de152a8c2a4dec")
    assert tconfig.router_allowed("56", "0x5994814f2c4040b863a0125a45de152a8c2a4dec")
    assert not tconfig.router_allowed("bsc", "0x6015126d7d23648c2e4466693b8deab005ffaba8")
    assert tconfig.spender_allowed("bsc", "0x2c34A2Fb1d0b4f55de51E1d0bDEfaDDce6b7cDD6")
    assert not tconfig.spender_allowed("bsc", None)
    with pytest.raises(KeyError):
        tconfig.router_allowed("eth", "0x5994814f2c4040b863a0125a45de152a8c2a4dec")
    assert tconfig.bsc_rpc_urls({}) == tconfig.BSC_RPC_DEFAULTS
    assert tconfig.bsc_rpc_urls({"BSC_RPC_URLS": " https://a , ,https://b"}) == ("https://a", "https://b")
    assert tconfig.aster_send_user({}) is True and tconfig.aster_send_user({"ASTER_SEND_USER": "0"}) is False
    assert tconfig.PLAN_TTL_S == 60 and tconfig.STALE_CMD_S == 90 and tconfig.POLL_S == 25
    assert (tconfig.TX_LOOKUP_S, tconfig.TX_BUMP_S, tconfig.TX_CANCEL_S) == (15, 30, 90)


def test_types_protocols_are_structural():
    class P:
        venue = "aster"
        def filters(self, s): ...
        def book(self, s, limit=20): ...
        def funding(self, s): ...
        def position(self, s): ...
        def available_margin(self): ...
        def setup(self, s, l, m): ...
        def ioc(self, *a): ...
        def query(self, s, c): ...
        def fills(self, s, f): ...
        def funding_income(self, s, t): ...
    assert isinstance(P(), PerpLeg) and not isinstance(P(), SpotLeg)


# ================================ store ================================
SPEC_COLUMNS = {
    "flags": "k v",
    "tg_updates": "update_id ts user_id chat_id text verdict",
    "deals": "id created state reason coin chain token token_dec perp_venue symbol leg_usd owner_json sim carry dust updated "
             "inst_json perp_scope",
    "intents": "id deal_id kind spec_json plan_json nonce status created expires approved chat msg_id err",
    "clips": "id intent_id seq state planned_in dex_in dex_out perp_qty perp_quote basis_bps carry_in carry_out "
             "recovery created updated",
    "dex_txs": "id clip_id kind chain wallet nonce to_addr value min_receive gas_limit gas_price raw_tx tx_hash state "
               "block status gas_used eff_gas_price amount_in amount_out err sent_ts resolved_ts",
    "perp_orders": "id clip_id client_id venue symbol side reduce_only tif price qty sign_nonce state order_id "
                   "executed_qty avg_price cum_quote err_code err sent_ts resolved_ts",
    "perp_fills": "venue trade_id order_id price qty quote_qty commission_abs commission_asset maker realized_pnl ts",
    "funding_income": "venue tran_id symbol income ts",
    "exec_events": "ts deal_id intent_id clip_id kind json",
    "deal_marks": "deal_id ts px_dex px_perp pnl_now pnl_exit exit_cost funding fees gas flags_json",
}


@pytest.fixture
def db(tmp_path):
    con = store.connect(tmp_path / "trade.db")
    yield con
    con.close()


def test_schema_matches_spec_and_wal(db, tmp_path):
    for table, cols in SPEC_COLUMNS.items():
        assert [r[1] for r in db.execute(f"PRAGMA table_info({table})")] == cols.split(), table
    assert db.execute("PRAGMA journal_mode").fetchone()[0] == "wal"
    assert db.execute("PRAGMA synchronous").fetchone()[0] == 2            # FULL
    store.connect(tmp_path / "trade.db").close()                           # повторное открытие безвредно
    assert tconfig.TRADE_DB_PATH.name == "trade.db"


def _deal(db, coin="AIW3", token="0x37E94fc028903E74275478b65160D6d2c0e8880b", symbol="AIW3USDT"):
    return store.create_deal(db, coin=coin, chain="bsc", token=token, token_dec=18, perp_venue="aster", symbol=symbol,
                             leg_usd=Decimal("500"), owner_json="{}", sim=True)


def test_amounts_are_text_and_float_is_refused(db):
    did = _deal(db)
    row = db.execute("SELECT leg_usd, typeof(leg_usd), carry FROM deals WHERE id=?", (did,)).fetchone()
    assert tuple(row) == ("500", "text", "0")
    assert store.amt(Decimal("1E+3")) == "1000" and store.amt(10 ** 30) == "1" + "0" * 30
    with pytest.raises(TypeError):
        store.amt(0.1)
    with pytest.raises(TypeError):
        store.set_deal_state(db, did, "DRAFT", carry=0.5)                   # float в сумму не пролезает
    with pytest.raises(ValueError):
        store.amt(Decimal("NaN"))


def test_deal_states_and_one_active_per_token(db):
    a, b = _deal(db), _deal(db)                                            # два черновика на один токен — можно
    assert store.set_deal_state(db, a, "ENTERING", expect="DRAFT")
    with pytest.raises(store.StoreBusy):
        store.set_deal_state(db, b, "ENTERING")                            # токен уже в работе
    assert not store.set_deal_state(db, a, "OPEN", expect="DRAFT")         # CAS: состояние уже другое
    assert store.set_deal_state(db, a, "PAUSED", reason="HEDGE_DEFICIT")
    assert store.get_deal(db, a)["reason"] == "HEDGE_DEFICIT"
    for s in ("ENTERING", "OPEN", "EXITING", "CLOSED"):
        assert store.set_deal_state(db, a, s)
    with pytest.raises(store.BadTransition):
        store.set_deal_state(db, a, "OPEN")                                # из CLOSED пути нет
    assert store.set_deal_state(db, b, "ENTERING")                         # токен освободился
    c = _deal(db, coin="X", token="0x" + "11" * 20, symbol="AIW3USDT")
    with pytest.raises(store.StoreBusy):
        store.set_deal_state(db, c, "ENTERING")                            # тот же символ перпа
    assert [d["id"] for d in store.active_deals(db)] == [b]


def test_intent_approval_is_one_shot(db, tmp_path):
    did = _deal(db)
    iid, nonce = store.create_intent(db, deal_id=did, kind="entry", spec={"usd": Decimal("500")},
                                     plan={"clips": [1]}, now=1000.0)
    assert iid.startswith("E") and len(nonce) == 8
    assert store.get_intent(db, iid)["spec_json"] == '{"usd":"500"}'
    assert not store.approve_intent(db, iid, "deadbeef", now=1001.0)      # чужой nonce
    other = store.connect(tmp_path / "trade.db")                           # второе устройство / поток
    assert store.approve_intent(db, iid, nonce, now=1001.0)
    assert not store.approve_intent(other, iid, nonce, now=1001.5)        # двойное нажатие — одно одобрение
    other.close()
    assert store.busy_intents(db) == 1
    i2, n2 = store.create_intent(db, deal_id=did, kind="exit", spec={}, plan={}, now=1002.0)
    assert i2.startswith("X")
    with pytest.raises(store.StoreBusy):
        store.approve_intent(db, i2, n2, now=1003.0)                       # одно идущее намерение на всю БД
    i3, n3 = store.create_intent(db, deal_id=did, kind="entry", spec={}, plan={}, now=1000.0)
    assert not store.approve_intent(db, i3, n3, now=1061.0)                # истекло (PLAN_TTL_S = 60)
    assert set(store.expire_intents(db, now=1070.0)) == {i2, i3}
    assert store.set_intent_status(db, iid, "running", expect="approved")
    with pytest.raises(store.BadTransition):
        store.set_intent_status(db, iid, "proposed")
    assert store.interrupt_unfinished(db) == [iid]
    assert store.get_intent(db, iid)["status"] == "interrupted" and store.busy_intents(db) == 0


def test_write_ahead_dex_tx_visible_before_broadcast(db, tmp_path):
    txh = "0x" + "AB" * 32
    tid = store.dex_tx_signed(db, clip_id=1, kind="swap", chain="bsc", wallet=WALLET, nonce=43,
                              to_addr="0x5994814f2c4040b863a0125a45de152a8c2a4dec", value=0, min_receive=10 ** 21,
                              gas_limit=2_600_000, gas_price=50_000_000, raw_tx="0x02f8aa", tx_hash=txh)
    # «упали сразу после подписи»: другой процесс уже видит хэш, nonce и сырые байты
    other = store.connect(tmp_path / "trade.db")
    seen = store.get_dex_tx(other, txh)
    other.close()
    assert seen["id"] == tid and seen["state"] == "SIGNED" and seen["raw_tx"] == "0x02f8aa" and seen["nonce"] == 43
    assert seen["min_receive"] == str(10 ** 21) and seen["wallet"] == WALLET.lower()
    assert [r["tx_hash"] for r in store.dex_txs_unresolved(db)] == [txh.lower()]
    with pytest.raises(sqlite3.IntegrityError):
        store.dex_tx_signed(db, clip_id=1, kind="swap", chain="bsc", wallet=WALLET, nonce=43, to_addr="0x1",
                            value=0, min_receive=None, gas_limit=1, gas_price=1, raw_tx="0x", tx_hash=txh)
    with pytest.raises(ValueError):
        store.dex_tx_signed(db, clip_id=1, kind="transfer", chain="bsc", wallet=WALLET, nonce=44, to_addr="0x1",
                            value=0, min_receive=None, gas_limit=1, gas_price=1, raw_tx="0x", tx_hash="0x" + "01" * 32)
    assert store.dex_tx_sent(db, txh, now=5.0)
    assert not store.dex_tx_sent(db, txh)                                  # уже SENT
    assert store.dex_tx_resolve(db, txh, "MINED_OK", block=100, status=1, gas_used=1_108_626,
                                eff_gas_price=80_000_000, amount_in=500 * 10 ** 18, amount_out=12_000 * 10 ** 18)
    r = store.get_dex_tx(db, txh)
    assert r["state"] == "MINED_OK" and r["amount_out"] == str(12_000 * 10 ** 18) and r["sent_ts"] == 5.0
    assert r["resolved_ts"] is not None and store.dex_txs_unresolved(db) == []
    with pytest.raises(store.BadTransition):
        store.dex_tx_resolve(db, txh, "SENT")
    assert len(store.dex_txs_on_nonce(db, "bsc", WALLET, 43)) == 1


def test_write_ahead_perp_order_and_fills(db):
    cid = store.client_order_id("D7K2", "entry", 3, 1, 1)
    assert cid == "fb-D7K2-e03-c1-a1"
    with pytest.raises(ValueError):
        store.client_order_id("D7K2 bad", "entry", 3, 1, 1)
    store.perp_order_intent(db, clip_id=7, client_id=cid, venue="aster", symbol="AIW3USDT", side="SELL",
                            reduce_only=False, tif="IOC", price=Decimal("0.04081"), qty=Decimal("2443"))
    assert store.get_perp_order(db, cid)["state"] == "INTENT"
    with pytest.raises(sqlite3.IntegrityError):
        store.perp_order_intent(db, clip_id=7, client_id=cid, venue="aster", symbol="AIW3USDT", side="SELL",
                                reduce_only=False, tif="IOC", price=Decimal("0.04"), qty=Decimal("1"))
    assert store.perp_order_sent(db, cid, sign_nonce=1789217110000000)
    fill = PerpFill(client_id=cid, order_id=99, status="UNKNOWN", qty=Decimal(0), avg_px=Decimal(0),
                    quote=Decimal(0), sign_nonce=1789217110000000, err_code=-1007)
    assert store.record_perp_fill(db, fill, err="timeout")
    assert [r["client_id"] for r in store.perp_orders_unresolved(db)] == [cid]
    with pytest.raises(ValueError):
        store.record_perp_fill(db, PerpFill(cid, None, "NOT_FOUND", Decimal(0), Decimal(0), Decimal(0), 0))
    done = PerpFill(client_id=cid, order_id=99, status="PARTIALLY_FILLED", qty=Decimal("2400"),
                    avg_px=Decimal("0.04085"), quote=Decimal("98.04"), sign_nonce=1789217110000000)
    assert store.record_perp_fill(db, done)
    o = store.get_perp_order(db, cid)
    assert (o["state"], o["executed_qty"], o["avg_price"], o["price"]) == ("PARTIALLY_FILLED", "2400", "0.04085", "0.04081")
    assert store.perp_orders_unresolved(db) == []
    rows = [dict(trade_id=5, order_id=99, price=Decimal("0.04085"), qty=Decimal("2400"), quote_qty=Decimal("98.04"),
                 commission_abs=Decimal("-0.039"), commission_asset="USDT", maker=False, realized_pnl=Decimal(0), ts=1)]
    assert store.add_perp_fills(db, "aster", rows) == 1 and store.add_perp_fills(db, "aster", rows) == 0
    assert db.execute("SELECT commission_abs FROM perp_fills").fetchone()[0] == "0.039"
    assert store.last_trade_id(db, "aster") == 5 and store.last_trade_id(db, "binance") is None
    inc = [dict(tran_id=11, symbol="AIW3USDT", income=Decimal("0.21"), ts=2)]
    assert store.add_funding_income(db, "aster", inc) == 1 and store.add_funding_income(db, "aster", inc) == 0


def test_clips_flags_updates_and_append_only_events(db):
    did = _deal(db)
    iid, _ = store.create_intent(db, deal_id=did, kind="entry", spec={}, plan={})
    cl = store.create_clip(db, iid, 1, 500 * 10 ** 18)
    with pytest.raises(sqlite3.IntegrityError):
        store.create_clip(db, iid, 1, 1)
    assert store.set_clip_state(db, cl, "DEX_SENT", expect="PLANNED")
    assert store.set_clip_state(db, cl, "DEX_OK", dex_out=12_000 * 10 ** 18, carry_in=Decimal("0.4"))
    with pytest.raises(store.BadTransition):
        store.set_clip_state(db, cl, "PLANNED")
    assert store.get_clip(db, cl)["carry_in"] == "0.4" and len(store.clips_of(db, iid)) == 1

    assert not store.is_paused(db)
    store.set_paused(db, True)
    assert store.is_paused(db)
    store.set_tg_offset(db, 10); store.set_tg_offset(db, 7)
    assert store.tg_offset(db) == 10                                       # смещение назад не ходит
    assert store.claim_update(db, 5, 1.0, 1, 1, "вход AIW3 okx·bsc aster 500")
    assert not store.claim_update(db, 5, 1.0, 1, 1, "вход AIW3 okx·bsc aster 500")
    store.set_update_verdict(db, 5, "owner")

    keys._remember_secret("34" * 32)
    store.event(db, "clip_dex", deal_id=did, clip_id=cl, err="boom " + "34" * 32 + " bot9:tok", px=Decimal("0.04"))
    ev = store.events(db, did)
    assert len(ev) == 1 and "34" * 32 not in ev[0]["json"] and "<key>" in ev[0]["json"] and '"px":"0.04"' in ev[0]["json"]
    with pytest.raises(sqlite3.DatabaseError):
        db.execute("UPDATE exec_events SET kind='x'")
    with pytest.raises(sqlite3.DatabaseError):
        db.execute("DELETE FROM exec_events")
    store.add_perp_fills(db, "aster", [dict(trade_id=1, price=Decimal(1), qty=Decimal(1), ts=1)])   # триггер строчный
    with pytest.raises(sqlite3.DatabaseError):
        db.execute("DELETE FROM perp_fills")
    with pytest.raises(sqlite3.DatabaseError):
        db.execute("UPDATE perp_fills SET qty='2'")


def test_tx_rolls_back_on_error(db):
    with pytest.raises(RuntimeError):
        with store.tx(db):
            store.set_flag(db, "x", "1")
            raise RuntimeError("сбой между записью и COMMIT")
    assert store.get_flag(db, "x") is None
    with store.tx(db):
        with store.tx(db):                                                 # вложенный — присоединяется
            store.set_flag(db, "x", "2")
    assert store.get_flag(db, "x") == "2"
