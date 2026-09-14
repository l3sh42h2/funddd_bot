"""Terra C2/C3 regression tests for the venue-neutral execution ordering."""
from decimal import Decimal as D
import json
import copy

import pytest

from funding_bot.trade import store
from funding_bot.trade.coordinator import HedgeAction, HedgeProgram, LifecycleCoordinator, TwoLegProgram


@pytest.fixture
def journal(tmp_path):
    con = store.connect(tmp_path / "trade.db")
    did = store.create_deal(con, coin="FIX", chain="bsc", token="fixture", token_dec=6,
                            perp_venue="fake", symbol="FIXUSDT", leg_usd=D(1), owner_json="{}", sim=True)
    iid, _ = store.create_intent(con, deal_id=did, kind="entry", spec={}, plan={})
    yield con, iid
    con.close()


def test_two_leg_lifecycle_never_hedges_before_leading_result_is_applied(journal):
    con, iid = journal
    events = []
    program = TwoLegProgram(
        leading=lambda: events.append("lead") or "proven-lead",
        apply_leading=lambda result: events.append(("apply-lead", result)),
        hedge=lambda: events.append("hedge") or "proven-hedge",
        apply_hedge=lambda result: events.append(("apply-hedge", result)),
        finish=lambda lead, hedge: events.append(("finish", lead, hedge)))
    LifecycleCoordinator(con).run_two_leg(program)
    assert events == ["lead", ("apply-lead", "proven-lead"), "hedge",
                      ("apply-hedge", "proven-hedge"), ("finish", "proven-lead", "proven-hedge")]


def test_leading_failure_does_not_reach_common_hedge_or_finish(journal):
    con, iid = journal
    events = []
    program = TwoLegProgram(
        leading=lambda: (_ for _ in ()).throw(RuntimeError("native outcome unknown")),
        apply_leading=lambda _: events.append("apply-lead"), hedge=lambda: events.append("hedge"),
        apply_hedge=lambda _: events.append("apply-hedge"), finish=lambda *_: events.append("finish"))
    with pytest.raises(RuntimeError, match="outcome unknown"):
        LifecycleCoordinator(con).run_two_leg(program)
    assert events == []


def test_rehedge_uses_one_durable_zero_input_clip_and_no_terminal_after_failure(journal):
    con, iid = journal
    events = []
    action = HedgeAction("BUY", D("2"), True)
    program = HedgeProgram(
        intent_id=iid, prepare=lambda: action,
        submit=lambda clip_id, got: events.append(("submit", clip_id, got)) or "proven",
        apply=lambda clip_id, got, result: events.append(("apply", clip_id, got, result)),
        verify=lambda: events.append(("verify",)), finish=lambda got: events.append(("finish", got)))
    LifecycleCoordinator(con).run_hedge(program)
    clips = store.clips_of(con, iid)
    assert len(clips) == 1 and clips[0]["planned_in"] == "0"
    assert [event[0] for event in events] == ["submit", "apply", "verify", "finish"]

    events.clear()
    second_iid, _ = store.create_intent(con, deal_id=store.get_intent(con, iid)["deal_id"], kind="rehedge",
                                        spec={}, plan={})
    failing = HedgeProgram(
        intent_id=second_iid, prepare=lambda: action,
        submit=lambda *_: (_ for _ in ()).throw(RuntimeError("perp unknown")),
        apply=lambda *_: events.append("apply"), verify=lambda: events.append("verify"),
        finish=lambda *_: events.append("finish"))
    with pytest.raises(RuntimeError, match="perp unknown"):
        LifecycleCoordinator(con).run_hedge(failing)
    assert events == []


def test_rehedge_refuses_unbounded_policy_before_creating_a_clip(journal):
    con, iid = journal
    invalid = HedgeProgram(intent_id=iid, prepare=lambda: HedgeAction("SELL", D(0), False),
                           submit=lambda *_: None, apply=lambda *_: None,
                           verify=lambda: None, finish=lambda *_: None)
    with pytest.raises(ValueError, match="invalid bounded"):
        LifecycleCoordinator(con).run_hedge(invalid)
    assert store.clips_of(con, iid) == []


def test_engine_generic_marker_never_falls_back_to_legacy_submit(tmp_path):
    """A persisted generic plan without its scoped context is a zero-send refusal."""
    from test_trade_engine import OWNER, live_env
    env = live_env(tmp_path)
    proposal = env.desk.propose_entry("AIW3", "okx·bsc", "aster", D(100), chat=OWNER)
    assert store.approve_intent(env.con, proposal.intent_id, proposal.nonce)
    intent = store.get_intent(env.con, proposal.intent_id)
    spec = json.loads(intent["spec_json"])
    spec["generic_operation_v1"] = True
    env.con.execute("UPDATE intents SET spec_json=? WHERE id=?", (store.jdump(spec), proposal.intent_id))
    env.engine.execute(proposal.intent_id)
    assert env.spot.swaps == [] and env.perp.calls == []
    assert store.get_intent(env.con, proposal.intent_id)["status"] == store.IntentStatus.FAILED


class _CexPerp:
    """Minimal native CEX futures adapter; deliberately has no HL account API."""

    def __init__(self, venue, clock):
        from funding_bot.trade.types import Book, Filters, PerpInstrument
        self.venue, self.clock = venue, clock
        self.account_id = f"{venue}.mainnet.fixture"
        self._loaded_mode = "live"
        self._position, self.orders, self.setup_calls, self.calls = D(0), {}, [], []
        self.script = []
        self._filters = Filters(D("0.0001"), D(1), D(1), D("1000000"), D("1000000"), D(1), frozenset({"ioc"}))
        self._book = Book(((D("0.1667"), D("5000")),), ((D("0.1672"), D("5000")),), clock())
        self._instrument = PerpInstrument("ANSEM_USDT", "ANSEM", "ANSEM", D(1), "USDC", None)

    def instrument(self, symbol):
        assert symbol == self._instrument.symbol
        return self._instrument

    def filters(self, symbol):
        assert symbol == self._instrument.symbol
        return self._filters

    def book(self, symbol, limit=20):
        assert symbol == self._instrument.symbol
        # Every live quote is stamped on read; a restart must not make a
        # previously loaded CEX book appear stale.
        return type(self._book)(self._book.bids, self._book.asks, self.clock())

    def available_margin(self):
        return D(1000)

    def position(self, symbol):
        assert symbol == self._instrument.symbol
        return self._position

    def setup(self, symbol, leverage, margin_type):
        assert (symbol, leverage, margin_type) == (self._instrument.symbol, 1, "ISOLATED")
        self.setup_calls.append((symbol, leverage, margin_type))

    def ioc(self, symbol, side, quantity, price, client_id, reduce_only, *, hedge=False, on_signed=None):
        from funding_bot.trade.types import PerpFill
        assert symbol == self._instrument.symbol and side in {"BUY", "SELL"} and hedge is True
        if on_signed is not None:
            on_signed(len(self.orders) + 1)
        self.calls.append((side, quantity, reduce_only, client_id))
        behavior = self.script.pop(0) if self.script else "filled"
        if behavior == "unknown":
            fill = PerpFill(client_id, None, "UNKNOWN", D(0), D(0), D(0), len(self.orders) + 1)
        elif behavior == "reject":
            fill = PerpFill(client_id, None, "REJECTED", D(0), D(0), D(0), len(self.orders) + 1)
        else:
            filled = behavior if isinstance(behavior, D) else quantity
            self._position += filled if side == "BUY" else -filled
            fill = PerpFill(client_id, len(self.orders) + 1, "FILLED", filled, price, filled * price,
                            len(self.orders) + 1)
        self.orders[client_id] = fill
        return fill

    def query(self, symbol, client_id):
        return self.orders.get(client_id) or self.settle_unknown(symbol, client_id, pos_before=None, since_ms=0)

    def settle_unknown(self, symbol, client_id, **_):
        from funding_bot.trade.types import PerpFill
        return self.orders.get(client_id) or PerpFill(client_id, None, "NOT_FOUND", D(0), D(0), D(0), 0)


def _cex_toml(profile, venue):
    import sol_hl_fixtures as F
    base = F.base_sections()
    limits = copy.deepcopy(base["limits.sol_best_hyperliquid"])
    emergency = copy.deepcopy(base["emergency.sol_best_hyperliquid"])
    observability = copy.deepcopy(base["observability.sol_best_hyperliquid"])
    return F.sol_toml({
        f"profiles.{profile}": {
            "enabled": "true", "mode": '"live"', "spot_chain": '"solana-mainnet"', "spot_policy": '"auto"',
            "perp_venue": f'"{venue}"', "instrument_registry": '"instruments.json"',
            "allowed_instruments": '["ansem_sol_cex_v1"]', "max_active_execution_clips": "1"},
        "wallets.solana": {"solana_address": f'"{F.SOL_ADDR}"'},
        f"perp.{venue}": {"leverage": "1"},
        f"limits.{profile}": limits,
        f"emergency.{profile}": emergency,
        f"observability.{profile}": observability,
    }, drop=("profiles.sol_best_hyperliquid", "wallets.sol_hl", "perp.hyperliquid",
             "limits.sol_best_hyperliquid", "emergency.sol_best_hyperliquid",
             "observability.sol_best_hyperliquid"))


def _cex_registry(profile, venue, account_id):
    import sol_c2_world as W
    doc = json.loads(W.registry_text())
    rec = doc["instruments"][0]
    rec.update(instrument_id="ansem_sol_cex_v1", profile_id=profile)
    rec["perp"].update(network="mainnet", venue=venue, dex="", fullcoin="ANSEM_USDT", account_id=account_id)
    return json.dumps(doc)


def _cex_world(tmp_path, profile, venue):
    import sol_c2_world as W
    from funding_bot.trade.fees import NATIVE_SOL, PriceObs
    from funding_bot.trade.runtime import RuntimeRegistry, SolLegs
    from funding_bot.trade.solana import USDC_MINT

    w = W.make_world(tmp_path)
    native = _CexPerp(venue, w.clock)
    w.path.write_text(_cex_toml(profile, venue))
    w.registry = __import__("funding_bot.trade.instruments", fromlist=["parse_registry"]).parse_registry(
        _cex_registry(profile, venue, native.account_id))
    legs = SolLegs(w.spot, native, w.router, False, native.account_id, W.WALLET,
                   lambda: D(150), lambda: PriceObs(NATIVE_SOL, USDC_MINT, D(150), w.clock(), "test"),
                   lambda: W.FEE, can_send=True, profile=profile, block_height=w.live.block_height)
    w.reg = RuntimeRegistry(lambda sim: None, {profile: lambda sim: legs})
    W.restart(w)
    return w, native


@pytest.mark.parametrize(("profile", "venue"), [("sol_best_gate", "gate"), ("sol_best_aster", "aster")])
def test_sol_cex_profile_runs_actual_desk_engine_without_hyperliquid_native_api(tmp_path, profile, venue):
    """Same SOL business path uses only the frozen CEX-native contract surface."""
    import sol_c2_world as W
    from funding_bot.tg.parse import ProfileEntry

    w, native = _cex_world(tmp_path, profile, venue)

    assert not any(hasattr(native, name) for name in ("identity", "margin", "agent_role", "http"))
    frozen_config = w.path.read_text()
    assert "wallets.sol_hl" not in frozen_config and "perp.hyperliquid" not in frozen_config
    cmd = ProfileEntry("ANSEM", "auto", "solana", venue, None, D(30), profile)
    proposal = w.desk.propose_profile_entry(cmd, chat=None)
    W.approve_run(w, proposal)

    deal = store.get_deal(w.con, proposal.deal_id)
    assert deal["state"] == store.DealState.OPEN, (store.get_intent(w.con, proposal.intent_id)["err"], w.hooks.reports)
    assert deal["perp_venue"] == venue and native.position("ANSEM_USDT") == D(-903)
    assert native.setup_calls == [("ANSEM_USDT", 1, "ISOLATED")]
    assert len(native.orders) == 1 and w.venue.calls == []


@pytest.mark.parametrize(("profile", "venue"), [("sol_best_gate", "gate"), ("sol_best_aster", "aster")])
def test_sol_cex_quote_currency_mismatch_refuses_before_any_spot_or_perp_send(tmp_path, profile, venue):
    """USDC spot cannot be silently treated as USDT collateral at a CEX."""
    from funding_bot.tg.parse import ProfileEntry
    from funding_bot.trade.types import PerpInstrument

    w, native = _cex_world(tmp_path, profile, venue)
    native._instrument = PerpInstrument("ANSEM_USDT", "ANSEM", "ANSEM", D(1), "USDT", None)
    cmd = ProfileEntry("ANSEM", "auto", "solana", venue, None, D(30), profile)
    with pytest.raises(Exception, match="инструмент ANSEM_USDT изменился"):
        w.desk.propose_profile_entry(cmd, chat=None)
    assert not native.calls and w.venue.calls == []


def _cex_enter(w, profile, venue):
    import sol_c2_world as W
    from funding_bot.tg.parse import ProfileEntry

    prop = w.desk.propose_profile_entry(ProfileEntry("ANSEM", "auto", "solana", venue, None, D(30), profile), chat=None)
    W.approve_run(w, prop)
    return prop, store.get_deal(w.con, prop.deal_id)


@pytest.mark.parametrize(("profile", "venue"), [("sol_best_gate", "gate"), ("sol_best_aster", "aster")])
def test_sol_cex_exit_survives_restart_and_uses_native_reduce_only(tmp_path, profile, venue):
    """Entry and exit use the same Desk/Engine after process reconstruction."""
    import sol_c2_world as W

    w, native = _cex_world(tmp_path, profile, venue)
    _prop, opened = _cex_enter(w, profile, venue)
    W.restart(w)
    opened = store.get_deal(w.con, opened["id"])
    exit_prop = w.desk.propose_exit(opened["id"], None, False, chat=None)
    W.approve_run(w, exit_prop)

    closed = store.get_deal(w.con, opened["id"])
    assert closed["state"] == store.DealState.CLOSED
    assert native.position("ANSEM_USDT") == D(0)
    assert native.calls[-1][:3] == ("BUY", D(903), True)
    assert not any(hasattr(native, name) for name in ("identity", "margin", "agent_role", "http"))


@pytest.mark.parametrize(("profile", "venue", "first_fill", "expected_side", "expected_qty"), [
    ("sol_best_gate", "gate", "reject", "SELL", D(903)),
    ("sol_best_gate", "gate", D(904), "BUY", D(1)),
    ("sol_best_aster", "aster", "reject", "SELL", D(903)),
    ("sol_best_aster", "aster", D(904), "BUY", D(1)),
])
def test_sol_cex_rehedge_corrects_under_and_over_hedge_with_native_adapter(
        tmp_path, profile, venue, first_fill, expected_side, expected_qty):
    """A rejected sell and an overfilled sell both enter the shared rehedge lifecycle."""
    import sol_c2_world as W

    w, native = _cex_world(tmp_path, profile, venue)
    native.script = [first_fill]
    _prop, paused = _cex_enter(w, profile, venue)
    assert store.get_deal(w.con, paused["id"])["state"] == store.DealState.PAUSED

    fix = w.desk.propose_fix("rehedge", paused["id"], chat=None)
    assert fix.plan.perp == venue
    W.approve_run(w, fix)

    repaired = store.get_deal(w.con, paused["id"])
    assert repaired["state"] == store.DealState.OPEN, [
        tuple(row) for row in w.con.execute("SELECT kind, status, err FROM intents WHERE deal_id=? ORDER BY created", (paused["id"],))]
    assert native.position("ANSEM_USDT") == D(-903)
    assert native.calls[-1][:3] == (expected_side, expected_qty, expected_side == "BUY")


@pytest.mark.parametrize(("profile", "venue"), [("sol_best_gate", "gate"), ("sol_best_aster", "aster")])
def test_sol_cex_unknown_hedge_stays_paused_after_restart_without_retry(tmp_path, profile, venue):
    """UNKNOWN stays an observation-only hold across a new Engine instance."""
    import sol_c2_world as W

    w, native = _cex_world(tmp_path, profile, venue)
    native.script = ["unknown"]
    _prop, paused = _cex_enter(w, profile, venue)
    assert store.get_deal(w.con, paused["id"])["state"] == store.DealState.PAUSED
    sent = tuple(native.calls)

    W.restart(w)
    assert w.engine.recover_sol(store.get_deal(w.con, paused["id"]))
    assert tuple(native.calls) == sent
    with pytest.raises(Exception, match="неизвестен"):
        w.desk.propose_fix("rehedge", paused["id"], chat=None)
    assert tuple(native.calls) == sent


def test_evm_gate_rehedge_after_restart_uses_the_registered_profile_legs(tmp_path):
    """The existing EVM×Gate alias shares the same corrective lifecycle after restart."""
    import test_rh_gate_engine as RH
    import test_trade_engine as E
    from funding_bot.trade import reconcile
    from funding_bot.trade.engine import Desk, Engine, deal_book

    e = RH.rh_env(tmp_path)
    opened = RH._open(e)
    # A confirmed short disappears outside this process; preserve the journal
    # and native position mismatch that the real restart must repair.
    prefix = f"fb-{opened.deal_id}-"
    client_id, qty = e.con.execute(
        "SELECT client_id, executed_qty FROM perp_orders WHERE substr(client_id, 1, ?)=? AND side='SELL' ORDER BY id LIMIT 1",
        (len(prefix), prefix)).fetchone()
    e.con.execute("UPDATE perp_orders SET executed_qty=? WHERE client_id=?", (store.amt(D(qty) - 1), client_id))
    e.perp.pos += 1

    e.desk = Desk(e.conns, e.legs, owner_loader=e.loader, table_loader=lambda: RH.TABLE_RH, keys_mode="live")
    e.hooks = E.RecHooks()
    e.engine = Engine(e.conns, e.legs, e.desk, e.hooks, owner_loader=e.loader, keys_mode="live",
                      sleep=lambda _: None, clip_gap_s=0)
    reconcile.startup(e.con, e.legs)
    fix = e.desk.propose_fix("rehedge", opened.deal_id, chat=E.OWNER)
    E.run_approved(e, fix)

    book = deal_book(e.con, opened.deal_id)
    # The legacy EVM policy deliberately keeps a deal paused after a
    # restart-time mismatch, even once the invariant is repaired; the proof
    # here is the registered Gate leg and the bounded corrective SELL.
    assert store.get_deal(e.con, opened.deal_id)["state"] == store.DealState.PAUSED
    assert e.perp.calls[-1]["side"] == "SELL" and e.perp.pos == -book.short


def test_evm_gate_partial_exit_resume_after_restart_keeps_the_frozen_root_target(tmp_path):
    """The existing EVM×Gate profile resumes only the unfilled portion after a restart."""
    import json
    import test_rh_gate_engine as RH
    import test_trade_engine as E
    from funding_bot.trade.engine import Desk, Engine, deal_book

    e = RH.rh_env(tmp_path, RH.rh_toml().replace('clip_max_usd = "auto"', 'clip_max_usd = 100'))
    opened = RH._open(e)
    sent = [0]

    def stop_after_first_swap():
        sent[0] += 1
        if sent[0] == 1:
            store.set_paused(e.con2, True)

    e.spot.on_swap = stop_after_first_swap
    partial = e.desk.propose_exit(opened.deal_id, D(300), False, chat=E.OWNER)
    E.run_approved(e, partial)
    e.spot.on_swap = None
    assert store.get_intent(e.con, partial.intent_id)["status"] == store.IntentStatus.PARTIAL
    store.set_paused(e.con, False)

    e.desk = Desk(e.conns, e.legs, owner_loader=e.loader, table_loader=lambda: RH.TABLE_RH, keys_mode="live")
    e.hooks = E.RecHooks()
    e.engine = Engine(e.conns, e.legs, e.desk, e.hooks, owner_loader=e.loader, keys_mode="live",
                      sleep=lambda _: None, clip_gap_s=0)
    resumed = e.desk.propose_resume(opened.deal_id, chat=E.OWNER)
    E.run_approved(e, resumed)

    root_units = json.loads(store.get_intent(e.con, partial.intent_id)["spec_json"])["units"]
    sold = sum(int(row["dex_in"] or 0) for iid in (partial.intent_id, resumed.intent_id)
               for row in store.clips_of(e.con, iid) if row["state"] != store.ClipState.DEX_REVERTED)
    book = deal_book(e.con, opened.deal_id)
    assert sold == root_units and e.perp.pos == -book.short
