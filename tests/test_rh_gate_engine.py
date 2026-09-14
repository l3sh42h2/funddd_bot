"""Связка rh_okx_gate (FATCOIN, владелец 13.09): спот OKX DEX в сети Robinhood (USDG, 6 знаков; газ — ETH) × перп Gate
(контракт = 100 токенов). Сквозной вход $400 и полный выход на «боевых» фейках с воротами; ноги сделки RH — только
из реестра своей связки: ноги BSC × Aster здесь «ядовитые» (любое обращение — Touched, BaseException: исполнитель её
не проглотит), так что план, исполнение, сверка на старте и оценка их тронуть не могут. Путь BSC × Aster — прежний
(весь остальной набор тестов)."""
from __future__ import annotations
from decimal import Decimal as D, ROUND_FLOOR
from types import SimpleNamespace
import pytest
from funding_bot.trade import engine as eng, marks, owner, reconcile, store, tconfig
from funding_bot.trade.engine import Conns, Desk, Engine, Legs, deal_book
from funding_bot.trade.gate_trade import GateTrade
from funding_bot.trade.runtime import EvmGateFactory, ProfileDown, RuntimeRegistry, legs_of, profile_of_deal
from funding_bot.trade.sim import SimPerp, SimSpot
from funding_bot.trade.store import DealState, DexTxState
from funding_bot.trade.types import Book, DexQuote, Filters, PerpInstrument
from funding_bot.tg import views
from funding_bot.tg.sender import html_ok
import test_trade_engine as fx

USDG = "0x5fc5360d0400a0fd4f2af552add042d716f1d168"
FAT = "0x12d5ee7917ca430073c3a638ee1e6f0648a98a01"
SYM = "FATCOIN_USDT"
M = D(100)
PXT = D("0.0016")                       # токен на DEX, $
PXC = PXT * M                           # контракт Gate (100 токенов), $
E6, E18 = 10 ** 6, 10 ** 18
GFILT = Filters(tick=D("0.0001"), step=D(1), min_qty=D(1), max_qty_limit=D(1000000), max_qty_market=D(100000),
                min_notional=D(1), tifs=frozenset({"GTC", "IOC"}))
TABLE_RH = {"ts": 0, "sf_rows": [{"base": "FATCOIN", "spot_ex": "okxdex", "perp_ex": "gate", "spot": f"4663:{FAT}",
                                  "perp": SYM, "ident": "same", "ident_ev": "fixture:verified", "mismatch": False, "period": 4,
                                  "spot_label": "okx·rh"}]}


def rh_toml(profile_mode: str = "live", mode: str = "live", enabled: str = "true", wallet: str = fx.WALLET) -> str:
    t = "schema_version = 2\n" + fx.live_toml().replace('mode = "live"\n', f'mode = "{mode}"\n', 1)
    return t + f'''
[profiles.rh_okx_gate]
enabled = {enabled}
mode = "{profile_mode}"
[wallets.rh_gate]
evm_address = "{wallet}"
[perp.gate]
leverage = 1
margin_type = "ISOLATED"
max_slip_bps = 30
touch_frac_max = 0.5
liq_alert_pct = 20
allow_contract_multiplier = true
'''


class Touched(BaseException):
    """Сделку RH коснулись ногами BSC × Aster."""


class _Poison:
    def __init__(self, name: str, **attrs):
        self.__dict__.update(attrs, _name=name)

    def __getattr__(self, n):
        raise Touched(f"{self._name}.{n}")


POISON = Legs(_Poison("bsc.spot", chain="bsc", wallet=fx.WALLET.lower()), _Poison("aster.perp", venue="aster"), False,
              lambda: None, can_send=True)


class MarketRH:
    """Пул FATCOIN/USDG: цена p·(1 ± (c0 + k·$)); USDG — 6 знаков, токен — 18."""

    def __init__(self):
        self.px, self.c0, self.k = PXT, D("0.0001"), D("4e-7")

    def quote(self, t_in: str, t_out: str, amount: int) -> DexQuote:
        if t_in.lower() == USDG:
            a = D(amount) / E6
            p = self.px * (1 + self.c0 + self.k * a)
            out = int((a / p * E18).to_integral_value(ROUND_FLOOR))
            return DexQuote("robinhood", t_in.lower(), t_out.lower(), int(amount), out, 6, 18, None, 0.01, None, False,
                            0.0)
        a = D(amount) / E18
        p = self.px * (1 - self.c0 - self.k * a * self.px)
        out = int((a * p * E6).to_integral_value(ROUND_FLOOR))
        return DexQuote("robinhood", t_in.lower(), t_out.lower(), int(amount), out, 18, 6, None, 0.01, None, False, 0.0)


class RHSpot(fx.LiveSpot):
    chain = "robinhood"
    ci = '4663'

    def __init__(self, market, env):
        super().__init__(market, env)
        self.stable, self.stable_dec = USDG, 6
        self.bal = {USDG: 1000 * E6, FAT: 0}

    def decimals(self, token, hint=None):
        return 6 if token.lower() == USDG else 18

    def balances(self, token):
        return {"stable": self.bal[USDG], "token": self.bal.get(token.lower(), 0), "native": self.native}


def gate_book() -> Book:
    bids = tuple((PXC - D("0.0001") * i, D(50000)) for i in range(10))
    asks = tuple((PXC + D("0.0002") + D("0.0001") * i, D(50000)) for i in range(10))
    return Book(bids, asks, 0.0)


class GatePerp(fx.LivePerp):
    venue = "gate"

    def __init__(self, env):
        super().__init__(env)
        self.b = gate_book()

    def filters(self, s):
        return GFILT

    def funding(self, s):
        return PXC, D("0.0004"), 1_757_700_000_000

    def instrument(self, s):
        return PerpInstrument(s, "FATCOIN", "FATCOIN", M, "USDT", "PERPETUAL")

    def sigma_1s(self, s):
        return D("0.0003")


def rh_env(tmp_path, toml: str | None = None, *, factory: bool = True):
    p = tmp_path / "owner.toml"
    p.write_text(toml if toml is not None else rh_toml())
    loader = lambda: owner.load(p)                                    # noqa: E731
    db = tmp_path / "trade.db"
    conns = Conns(db)
    e = SimpleNamespace(conns=conns, con=conns.get(), con2=store.connect(db), loader=loader, path=p)
    e.mode_state = lambda: (loader().profile_mode(owner.RH_GATE), store.is_paused(e.con2))
    e.market = MarketRH()
    e.spot, e.perp = RHSpot(e.market, e), GatePerp(e)
    e.legs_rh = Legs(e.spot, e.perp, False, lambda: D(2500), can_send=True)
    e.legs_sim = Legs(SimSpot(e.spot, native_px=lambda: D(2500)), SimPerp(e.perp), True, lambda: D(2500))
    e.built = []

    def rh(sim):
        e.built.append(sim)
        return e.legs_sim if sim else e.legs_rh
    e.legs = RuntimeRegistry(lambda sim: POISON, {owner.RH_GATE: rh} if factory else {})
    e.desk = Desk(conns, e.legs, owner_loader=loader, table_loader=lambda: TABLE_RH, keys_mode="live")
    e.hooks = fx.RecHooks()
    e.engine = Engine(conns, e.legs, e.desk, e.hooks, owner_loader=loader, keys_mode="live", sleep=lambda s: None,
                      clip_gap_s=0)
    return e


def _texts_clean(texts):
    for t in texts:
        assert html_ok(t) and not fx.RAW_NUM.search(t) and not fx.ASCII_MINUS.search(t), t
        assert "Aster" not in t and "BNB" not in t and "USDT в кошельке" not in t, t


def _open(e, usd=D(400)):
    p = e.desk.propose_entry("FATCOIN", "okx·rh", "gate", usd, chat=fx.OWNER)
    fx.run_approved(e, p)
    return p


# ==== 1. вход $400 и полный выход ================================================================================
def test_rh_gate_entry_400_and_full_exit(tmp_path):
    e = rh_env(tmp_path)
    p = e.desk.propose_entry("FATCOIN", "okx·rh", "gate", D(400), chat=fx.OWNER)
    assert not p.html.startswith(views.SIM_PREFIX), "связка в live, ключи live — план живой"
    assert not p.plan.missing_owner_keys, p.plan.missing_owner_keys
    fx.run_approved(e, p)
    deal = store.get_deal(e.con, p.deal_id)
    assert deal["state"] == DealState.OPEN and deal["sim"] == 0, (deal["state"], e.hooks.reports)
    assert (deal["chain"], deal["perp_venue"], deal["symbol"]) == ("robinhood", "gate", SYM)
    assert profile_of_deal(deal) == owner.RH_GATE
    bk = deal_book(e.con, p.deal_id)
    assert bk.known and bk.m == M and bk.short > 0
    assert abs(bk.tokens(18) - bk.short * M) < M, "хедж в токенах: контракты × 100, остаток меньше контракта"
    assert e.perp.pos == -bk.short and e.perp.setups == [(SYM, 1, "ISOLATED")]
    spent = sum(a for t_in, _o, a, _q in e.spot.swaps if t_in.lower() == USDG)
    assert 0 < D(spent) / E6 <= D(400)
    assert all(q % 1 == 0 for q in (c["qty"] for c in e.perp.calls)), "заявки Gate — целые контракты"
    assert e.spot.approvals and e.spot.approvals[0][0].lower() == USDG

    x = e.desk.propose_exit(p.deal_id, None, False, chat=fx.OWNER)
    fx.run_approved(e, x)
    deal = store.get_deal(e.con, p.deal_id)
    bk = deal_book(e.con, p.deal_id)
    assert deal["state"] == DealState.CLOSED and bk.tokens_raw == 0 and bk.short == 0
    assert e.perp.pos == 0 and e.spot.bal[FAT] == 0
    rows = e.con.execute("SELECT venue, reduce_only, side FROM perp_orders ORDER BY id").fetchall()
    assert {r[0] for r in rows} == {"gate"}
    assert [r[2] for r in rows if r[1]] and all(r[2] == "BUY" for r in rows if r[1])
    _texts_clean([p.html, x.html, *e.hooks.reports, *(h for _i, h in e.hooks.progresses)])
    assert e.built and set(e.built) == {False}, "ноги — из реестра связки RH; симуляционные не собирались"


def test_rh_plan_is_simulation_unless_profile_is_live(tmp_path):
    """Общий mode = live не делает живой связку, которую владелец не перевёл в live (Desk._pair_mode)."""
    for i, toml in enumerate((rh_toml(profile_mode="readonly"), rh_toml(mode="dry"),
                              rh_toml(enabled="false"))):
        (tmp_path / str(i)).mkdir()
        e = rh_env(tmp_path / str(i), toml)
        p = e.desk.propose_entry("FATCOIN", "okx·rh", "gate", D(400), chat=fx.OWNER)
        assert p.html.startswith(views.SIM_PREFIX), toml
        assert e.built == [True]


def test_rh_refused_when_profile_legs_not_connected(tmp_path):
    e = rh_env(tmp_path, factory=False)
    with pytest.raises(eng.Refused) as ei:
        e.desk.propose_entry("FATCOIN", "okx·rh", "gate", D(400), chat=fx.OWNER)
    assert "rh_okx_gate" in ei.value.html and "не подключена" in ei.value.html


def test_pairs_outside_evm_profiles_refused(tmp_path):
    e = rh_env(tmp_path)
    with pytest.raises(eng.Refused) as ei:
        e.desk.find_pair("FATCOIN", "okx·bsc", "gate")
    assert "okx·rh" in ei.value.html
    with pytest.raises(eng.Refused) as ei:
        e.desk.find_pair("FATCOIN", "okx·rh", "aster")
    assert "okx·bsc" in ei.value.html
    with pytest.raises(eng.Refused) as ei:
        e.desk.find_pair("FATCOIN", "okx·rh", "binance")
    assert "пока только" in ei.value.html
    pi = e.desk.find_pair("FATCOIN", "okx", "gate")                    # «okx dex» без сети — сеть из таблицы
    assert (pi.chain, pi.venue, pi.token, pi.spot_label) == ("robinhood", "gate", FAT, "okx·rh")


def test_chain_venue_helpers():
    assert eng._cmd_cv("okx·rh", "gate") == ("robinhood", "gate")
    assert eng._cmd_cv("okx", "gate") == ("robinhood", "gate")
    assert eng._cmd_cv("okx·bsc", "aster") == ("bsc", "aster")
    assert eng._deal_cv({"chain": "robinhood", "perp_venue": "gate"}) == ("robinhood", "gate")
    # пара не из связок (колонку подменили) — прежняя BSC × Aster: остановит сверка с инструментом, не чужие ключи
    assert eng._deal_cv({"chain": "bsc", "perp_venue": "binance"}) == ("bsc", "aster")
    assert eng._deal_cv({"chain": "nosuch", "perp_venue": "gate"}) == ("bsc", "aster")
    assert views.VENUE_LABEL["gate"] == "Gate" and tconfig.STABLE_SYMBOL["robinhood"] == "USDG"


# ==== 2. сделку RH не трогают ноги BSC: сверка на старте, «позиции», оценка ===========================================
def test_restart_marks_positions_never_touch_bsc_legs(tmp_path):
    e = rh_env(tmp_path)
    p = _open(e)
    h = "0x" + "ab" * 32                       # висящий approve RH на ТОМ ЖЕ адресе, что и кошелёк BSC
    store.dex_tx_signed(e.con2, clip_id=None, kind="approve", chain="robinhood", wallet=fx.WALLET.lower(), nonce=7,
                        to_addr=USDG, value=0, min_receive=0, gas_limit=70_000, gas_price=1, raw_tx="0xf86b",
                        tx_hash=h)
    store.dex_tx_sent(e.con2, h)
    bare = RuntimeRegistry(lambda sim: POISON, {})     # процесс без связки RH: её ноги не собраны
    with pytest.raises(ProfileDown):
        legs_of(bare, store.get_deal(e.con, p.deal_id))
    reconcile.startup(e.con, bare, now=2e9)              # Touched (BaseException) здесь уронил бы тест
    assert store.get_dex_tx(e.con, h)["state"] == DexTxState.SENT, "чек RH в сети BSC не искали"
    assert store.get_deal(e.con, p.deal_id)["state"] in (DealState.OPEN, DealState.PAUSED)
    marks.run_pass(e.con, bare, now=2e9)
    reconcile.positions(e.con, bare, now=2e9)
    # со связкой: ноги — её, висящий approve RH сверяется ногой RH
    e.spot.resolutions[h] = fx.Resolved(DexTxState.MINED_OK, "ok", 0, 0, block=5)
    reconcile.startup(e.con, e.legs, now=2e9 + 1)
    assert store.get_dex_tx(e.con, h)["state"] == DexTxState.MINED_OK
    out, done = marks.run_pass(e.con, e.legs, now=2e9 + 2)
    assert done


# ==== 3. фабрика ног, регистрация в боте, σ Gate =====================================================================
def _factory(tmp_path, toml: str, *, keys=True, evm=True, rt_mode="live", gate=None):
    p = tmp_path / "owner.toml"
    p.write_text(toml)
    loader = lambda: owner.load(p)                                    # noqa: E731
    gates = []
    acct = SimpleNamespace(address=fx.WALLET, sign_transaction=lambda *a, **k: None)
    k = SimpleNamespace(evm=acct if evm else None, evm_address=fx.WALLET,
                        gate=lambda *a, **kw: gates.append((a, kw))) if keys else None
    rt = SimpleNamespace(mode=rt_mode, keys=k)
    f = EvmGateFactory(loader, Conns(tmp_path / "trade.db"), eng.CfgHolder(loader), rt, okx=SimpleNamespace(),
                       rpc=SimpleNamespace(), gate=gate or SimpleNamespace(venue="gate"))
    return f, gates


def test_evm_gate_factory_shares_evm_key_on_robinhood(tmp_path):
    f, gates = _factory(tmp_path, rh_toml())
    live = f(False)
    assert live.spot.chain == "robinhood" and live.spot.chain_id == 4663 and live.spot.sender.chain_id == 4663
    assert live.spot.sender.chain == "robinhood" and live.spot.stable == USDG and live.spot.stable_dec == 6
    assert live.perp.venue == "gate" and live.can_send and not live.sim
    live.spot.sender._gate(False)
    assert gates[-1][0][:2] == ("live", "send")                        # ворота — режим связки rh_okx_gate
    sim = f(True)
    assert sim.sim and sim.spot.chain == "robinhood" and sim.perp.venue == "gate"


def test_evm_gate_factory_refuses_or_downgrades(tmp_path):
    for d in "abcd":
        (tmp_path / d).mkdir()
    f, _ = _factory(tmp_path / "a", rh_toml(wallet="0x" + "12" * 20))
    with pytest.raises(RuntimeError, match="не совпадает"):
        f(False)
    f, _ = _factory(tmp_path / "b", rh_toml(), evm=False)
    with pytest.raises(RuntimeError, match="EVM-ключ"):
        f(False)
    f, _ = _factory(tmp_path / "b", rh_toml(), keys=False)
    assert f(False) is None                                             # ключей нет вовсе — режим dry, боевых ног нет
    f, _ = _factory(tmp_path / "c", rh_toml(profile_mode="readonly"))
    assert f(False).can_send is False                                   # readonly: собрана, но отправлять нельзя
    f, _ = _factory(tmp_path / "d", rh_toml(), rt_mode="dry")
    assert f(False) is None                                             # ключи в dry — боевых ног нет


def test_bot_registers_rh_factory_only_when_enabled(tmp_path):
    from funding_bot.tg.bot import build_trader_legs
    for i, (en, want) in enumerate((("true", True), ("false", False))):
        d = tmp_path / str(i)
        d.mkdir()
        p = d / "owner.toml"
        p.write_text(rh_toml(enabled=en))
        cfg = owner.load(p)
        rt = SimpleNamespace(mode="live", keys=SimpleNamespace(evm=None), sim=None, live=None)
        _rt, reg, *_ = build_trader_legs(cfg, Conns(d / "trade.db"), eng.CfgHolder(lambda: cfg), {},
                                         build=lambda *a, **k: rt)
        assert (owner.RH_GATE in reg.factories) is want


def test_gate_sigma_1s_from_candles():
    g = GateTrade()
    closes = [D("0.16") + (D("0.0002") if i % 2 else 0) for i in range(61)]
    g._public = lambda path, params=None, retries=None: [{"t": i, "c": str(c)} for i, c in enumerate(closes)]
    s = g.sigma_1s(SYM)
    assert s is not None and D(0) < s < D("0.001")
    g._public = lambda path, params=None, retries=None: [{"t": 1, "c": "0.16"}] * 5
    assert g.sigma_1s(SYM) is None                                       # мало свечей — неизвестно, а не 0


def test_evm_gate_factory_readonly_without_evm_key_and_clock(tmp_path):
    for d in "ab":
        (tmp_path / d).mkdir()
    f, _ = _factory(tmp_path / "a", rh_toml(profile_mode="readonly"), evm=False, rt_mode="readonly")
    ro = f(False)
    assert ro.spot.sender is None and ro.can_send is False and ro.spot.chain == "robinhood"   # только чтение
    def bad_clock():
        raise RuntimeError("часы расходятся с Gate")
    f, _ = _factory(tmp_path / "b", rh_toml(), gate=SimpleNamespace(venue="gate", check_clock=bad_clock))
    with pytest.raises(RuntimeError, match="часы"):
        f(False)


def test_guard_uses_profile_mode_not_general_mode(tmp_path):
    """Связку перевели в readonly посреди сделки — отправки останавливает guard («режим readonly»), общий mode = live."""
    e = rh_env(tmp_path)
    p = _open(e)
    x = e.desk.propose_exit(p.deal_id, None, False, chat=fx.OWNER)
    e.path.write_text(rh_toml(profile_mode="readonly"))
    n = fx.sends(e)
    fx.run_approved(e, x)
    assert fx.sends(e) == n, "после перевода связки в readonly ни свопа, ни заявки"
    assert store.get_deal(e.con, p.deal_id)["state"] != DealState.CLOSED
    assert any("readonly" in h for h in [*e.hooks.reports, *(h for _i, h in e.hooks.progresses)]), e.hooks.reports


def test_position_zero_right_after_fill_is_reread_not_halted(tmp_path):
    """Gate сразу после первого филла коротко отвечает «позиции нет» (0) — сверка перечитывает, сделка открывается."""
    e = rh_env(tmp_path)
    real, lied = e.perp.position, []

    def flaky(s):
        r = real(s)
        if r != 0 and not lied:
            lied.append(r)
            return D(0)
        return r
    e.perp.position = flaky
    p = _open(e)
    assert lied, "сценарий не сработал"
    assert store.get_deal(e.con, p.deal_id)["state"] == DealState.OPEN
