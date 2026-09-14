"""M3 contract tests use native result shapes, no keys or network requests."""
from dataclasses import replace
from decimal import Decimal as D
from types import SimpleNamespace as NS
import sqlite3
import pytest
from funding_bot.trade.adapters.contracts import *
from funding_bot.trade.adapters.registry import production_registry, AdapterRegistry
from funding_bot.trade.adapters.native import Bindings, NativeAdapter
from funding_bot.trade.adapters.attempts import AttemptJournal
from funding_bot.trade.adapters import outcomes
from funding_bot.trade.types import PerpFill, SwapResult
from funding_bot.trade.sol_exec import SwapOutcome


def spec(adapter='aster', *, leg='p', direction='short', **kw):
    spot = adapter in {'okx_evm', 'sol_best'}
    family = ('evm' if adapter == 'okx_evm' else 'solana') if spot else None
    cap = Capabilities('dex' if spot or adapter == 'hyperliquid' else 'cex',
                       'spot' if spot else 'perpetual', family, short=not spot, reduce_only=not spot)
    values = dict(leg_id=leg, role='inventory' if spot else 'hedge', direction=direction,
                  adapter_id=adapter, venue=adapter, account='account:' + adapter, instrument='CaseSensitive4',
                  asset_id='proof:asset4', identity_evidence='verified-source', multiplier=D(1), step=D('.01'),
                  tick=D('.001'), quote_currency='USDC', settlement_currency='USDC', capabilities=cap,
                  network='genesis:Case' if spot else None, decimals=6 if spot else None, metadata_revision='r1')
    values.update(kw)
    return LegSpec(**values)


class Context:
    def __init__(self, path=':memory:'):
        self.journal = AttemptJournal(sqlite3.connect(path))
        self.sent = []
        self.allow = True
        self.fail_after_send = False
        self.last = {}

    def for_leg(self, s):
        def quote(spec, a, bounds):
            return Quote(a, 200, D(2), D(1), 'USDC', spec.asset_id, '{}')
        def authorize(spec, a):
            if not self.allow:
                raise AdapterError(ErrorKind.CONFIG, 'readonly')
        def submit(spec, prepared):
            self.sent.append(prepared.attempt_id)
            if self.fail_after_send:
                raise TimeoutError()
            if spec.adapter_id == 'okx_evm':
                result = SwapResult('tx', 'ok', 1000000, 1000000, 1, None, 42, 3)
            elif spec.adapter_id == 'sol_best':
                result = SwapOutcome('ok', prepared.attempt_id, 'sig', 1000000, 1000000, 'finalized')
            else:
                result = PerpFill(prepared.attempt_id, 1, 'FILLED', D(1), D(2), D(2), 1)
            self.last[prepared.attempt_id] = (result, prepared.quote.action.side)
            return result
        return Bindings(None, self.journal, authorize, quote, submit, lambda spec, ref: self.last[ref],
                        lambda spec: Observation(D(1), 100, 'native', 'authoritative'),
                        lambda spec, cursor: ExecutionPage((), cursor, True), clock=lambda: 100)


@pytest.mark.parametrize('spot', ['okx_evm', 'sol_best'])
@pytest.mark.parametrize('perp', ['aster', 'gate', 'hyperliquid'])
def test_all_existing_native_families_compose_without_peer_methods(spot, perp):
    ctx = Context()
    pair = production_registry().compose(spec(spot, leg='s', direction='long'), spec(perp), ctx)
    for index, leg in enumerate((pair.first, pair.second)):
        for side in ('BUY', 'SELL'):
            action = Action(f'a{index}{side}', leg.describe().leg_id, side, D(1))
            prepared = leg.prepare(f't{index}{side}', leg.quote(action, {}))
            result = leg.submit(prepared)
            assert result.status == Status.SETTLED and result.executed_quantity == D(1)
            assert leg.resolve(prepared.attempt_id) == result
    assert len(ctx.sent) == 4


def test_durable_no_resubmit_no_new_attempt_for_same_action(tmp_path):
    path = str(tmp_path / 'attempts.db')
    ctx = Context(path)
    leg = production_registry().build(spec(), ctx)
    p = leg.prepare('attempt', leg.quote(Action('action', 'p', 'SELL', D(1)), {}))
    ctx.fail_after_send = True
    assert leg.submit(p).status == Status.UNKNOWN
    ctx.journal.con.close()
    restarted = Context(path)
    leg2 = production_registry().build(spec(), restarted)
    with pytest.raises(AdapterError):
        leg2.submit(p)
    with pytest.raises(AdapterError):
        leg2.prepare('replacement', p.quote)
    assert restarted.sent == []


def test_changed_payload_and_permissions_refused_before_send():
    ctx = Context()
    leg = production_registry().build(spec(), ctx)
    quote = leg.quote(Action('a', 'p', 'SELL', D(1)), {})
    p = leg.prepare('one', quote)
    with pytest.raises(AdapterError):
        leg.prepare('one', replace(quote, max_spend=D(3)))
    ctx.allow = False
    with pytest.raises(AdapterError):
        leg.submit(p)
    assert not ctx.sent
    ctx.allow = True
    assert leg.submit(p).terminal


@pytest.mark.parametrize('change', [dict(multiplier=None), dict(multiplier=D('NaN')),
    dict(step=D(0)), dict(direction='short', adapter='sol_best'), dict(identity_evidence='')])
def test_invalid_contract(change):
    adapter = change.pop('adapter', 'aster')
    with pytest.raises(AdapterError):
        spec(adapter, **change)


def test_cancelled_partial_unknown_and_solana_finality():
    fill = PerpFill('c', 4, 'CANCELED', D('.3'), D(2), D('.6'), 1)
    result = outcomes.perpetual(fill, spec().scope)
    assert result.status == Status.CANCELLED and result.executed_quantity == D('.3')
    assert result.terminal
    result = outcomes.perpetual(replace(fill, status='UNKNOWN'), spec().scope)
    assert result.executed_quantity == D('.3') and not result.terminal
    sol = spec('sol_best', direction='long')
    result = outcomes.sol_swap(SwapOutcome('ok', 'a', 'sig', 10, 20, 'confirmed'), sol, 'BUY')
    assert result.status == Status.UNKNOWN and result.provisional
    result = outcomes.sol_swap(SwapOutcome('ok', 'a', 'sig', 10, 20, 'finalized'), sol, 'BUY')
    assert result.status == Status.SETTLED and not result.provisional


class Synthetic(NativeAdapter):
    def __init__(self, spec, context):
        self.market_kind = spec.capabilities.market_kind
        self.network_family = spec.capabilities.network_family
        super().__init__(spec, context.for_leg(spec))

    def normalize(self, native, side):
        return outcomes.perpetual(native, self.spec.scope)


def test_five_new_adapters_only_registration_changes():
    registry = production_registry()
    ctx = Context()
    caps = [Capabilities('cex', 'spot'), Capabilities('cex', 'perpetual', short=True, reduce_only=True),
            Capabilities('dex', 'spot', 'evm'), Capabilities('dex', 'spot', 'solana'),
            Capabilities('dex', 'perpetual', short=True, reduce_only=True)]
    legs = []
    for i, cap in enumerate(caps):
        name = f'new{i}'
        registry.register(name, Synthetic)
        legs.append(spec(name, leg=name, direction='long', capabilities=cap,
                         network='NetworkCase' if cap.network_family else None))
    for first in legs:
        second = replace(legs[-1], leg_id='other', account='other', direction='short')
        pair = registry.compose(first, second, ctx)
        assert pair.first.describe() == first
        for side in ('BUY', 'SELL'):
            a = Action(first.leg_id + side, first.leg_id, side, D(1))
            assert pair.first.submit(pair.first.prepare(a.action_id, pair.first.quote(a, {}))).terminal
        assert first.exposure(D(3)) == D(3)
    # CEX spot and perp/perp carry no fake blockchain field.
    assert legs[0].network is None and legs[1].network is None
    assert not hasattr(legs[0], 'wallet')
    with pytest.raises(AdapterError):
        registry.compose(legs[0], replace(second, asset_id='wrong'), ctx)
    with pytest.raises(AdapterError):
        Action('bad', legs[0].leg_id, 'SELL', D(1), reduce_only=True).validate(legs[0])


def test_rh_evm_credentials_do_not_read_aster_or_require_bsc(monkeypatch):
    from funding_bot.trade.adapters.credentials import CredentialProvider
    from funding_bot.trade import keys as K
    calls = []
    monkeypatch.setattr(K, '_account_cls', lambda: object())
    monkeypatch.setattr(K, '_from_key', lambda cls, raw, name: calls.append(name) or NS(address='0xabc'))
    env = {'DEX_EVM_KEY': 'test-evm', 'ASTER_SIGNER_KEY': 'untouched',
           'GATE_API_KEY': 'test-key', 'GATE_API_SECRET': 'test-secret'}
    provider = CredentialProvider(env)
    k = provider.evm('live', '0xAbC')
    assert k.evm is not None and calls == ['DEX_EVM_KEY']
    assert provider.evm('live', '0xabc') is k
    assert env['ASTER_SIGNER_KEY'] == 'untouched' and 'DEX_EVM_KEY' not in env
    assert len(provider.gate()) == 2
    with pytest.raises(K.KeyMismatch):
        provider.evm('live', '0xdifferent')


def test_solana_and_hl_credentials_independent(monkeypatch):
    from funding_bot.trade.adapters.credentials import CredentialProvider
    from funding_bot.trade import keys as K
    class Cfg:
        def env_name(self, name):
            return name
        def require(self, *names):
            return tuple('address:' + name for name in names)
        def get(self, name):
            return 'address:' + name
    monkeypatch.setattr(K, '_api_creds', lambda *a: {})
    monkeypatch.setattr(K, '_load_solana', lambda *a: 'SOL_SIGNER')
    monkeypatch.setattr(K, '_load_hl_agent', lambda *a: 'HL_SIGNER')
    env = {'spot.solana.secret_b58_env': 'sol-secret', 'perp.hyperliquid.agent_key_env': 'hl-secret'}
    provider = CredentialProvider(env)
    sol = provider.solana(Cfg(), 'live')
    assert sol.sol == 'SOL_SIGNER' and not hasattr(sol, 'hl')
    assert env['perp.hyperliquid.agent_key_env'] == 'hl-secret'
    hl = provider.hyperliquid(Cfg(), 'live')
    assert hl.hl == 'HL_SIGNER' and not hasattr(hl, 'sol')
    assert provider.solana(Cfg(), 'live') is sol


def test_rh_factory_works_with_no_legacy_runtime(tmp_path, monkeypatch):
    from funding_bot.trade.runtime import EvmGateFactory
    from funding_bot.trade.adapters.credentials import CredentialProvider
    from funding_bot.trade.engine import CfgHolder, Conns
    from funding_bot.trade import keys as K, owner
    import test_rh_gate_engine as rh
    import test_trade_engine as te
    p = tmp_path / 'owner.toml'
    p.write_text(rh.rh_toml())
    cfg = owner.load(p)
    monkeypatch.setattr(K, '_account_cls', lambda: object())
    monkeypatch.setattr(K, '_from_key', lambda *a: NS(address=te.WALLET))
    provider = CredentialProvider({'DEX_EVM_KEY': 'fake'})
    factory = EvmGateFactory(lambda: cfg, Conns(tmp_path / 'trade.db'), CfgHolder(lambda: cfg), None,
                            credentials=provider, keys_mode='live', okx=NS(), rpc=NS(), gate=NS(venue='gate'))
    legs = factory(False)
    assert legs.can_send and legs.spot.chain == 'robinhood' and legs.perp.venue == 'gate'


def test_barrier_does_not_commit_callers_unrelated_transaction():
    con = sqlite3.connect(':memory:')
    journal = AttemptJournal(con)
    con.execute('CREATE TABLE unrelated (v INT)')
    con.execute('INSERT INTO unrelated VALUES (1)')
    p = Prepared('x', Quote(Action('x', 'p', 'SELL', D(1)), 200, D(2), D(1), 'USD', 'asset'))
    with pytest.raises(AdapterError):
        journal.prepare(p)
    con.rollback()
    assert con.execute('SELECT COUNT(*) FROM unrelated').fetchone()[0] == 0


def test_real_futures_binding_preserves_amounts_and_record_before_send():
    from funding_bot.trade.adapters.futures_bindings import bind
    from funding_bot.trade.adapters.context import AdapterContext
    from funding_bot.trade.types import Filters, PerpInstrument
    journal = AttemptJournal(sqlite3.connect(':memory:'))
    calls = []
    class Perp:
        def instrument(self, symbol):
            return PerpInstrument(symbol, '4', '4', D(1000), 'USDC', 'PERPETUAL')
        def filters(self, symbol):
            return Filters(D('.001'), D('.01'), D('.01'), D(100), D(100), D('.01'), frozenset({'IOC'}))
        def ioc(self, symbol, side, qty, price, cid, ro, *, on_signed):
            assert journal.con.execute('SELECT state FROM core_leg_attempts WHERE attempt_id=?', (cid,)).fetchone()[0] == 'CLAIMED'
            on_signed(7)
            calls.append((symbol, side, qty, price, cid, ro))
            return PerpFill(cid, 1, 'FILLED', qty, price, qty * price, 7)
    s = spec(multiplier=D(1000))
    context = AdapterContext()
    context.add(s, bind(Perp(), journal=journal, authorize=lambda *a: None, attempt_lookup=lambda ref: {},
                        on_signed=lambda *a: calls.append(a), clock=lambda: 100))
    leg = production_registry().build(s, context)
    quote = leg.quote(Action('a', 'p', 'SELL', D(2)), {'price_cap': D(4)})
    result = leg.submit(leg.prepare('native-id', quote))
    assert result.executed_quantity == D(2) and s.exposure(result.executed_quantity) == D(2000)
    assert calls[0] == ('native-id', 7) and calls[1][2:4] == (D(2), D(4))
    with pytest.raises(AdapterError):
        context.for_leg(replace(s, account='wrong'))


def test_evm_new_approval_floor_checked_before_signing():
    from funding_bot.trade.evm_swap import OkxEvmSpot, GuardError
    obj = object.__new__(OkxEvmSpot)
    obj.sender = NS(send_and_wait=lambda *a, **kw: pytest.fail('must not sign'))
    obj._cfg = lambda: object()
    obj.build_swap = lambda *a, **kw: NS(min_receive=99)
    with pytest.raises(GuardError, match='min_receive'):
        obj.swap('in', 'out', 100, 'clip', approved_min_receive=100)


def test_evm_binding_uses_raw_units_and_native_approval_floor():
    from funding_bot.trade.adapters.spot_bindings import evm
    from funding_bot.trade.adapters.context import AdapterContext
    journal = AttemptJournal(sqlite3.connect(':memory:'))
    sent = []
    native = NS(wallet='Wallet', ci=56,
                build_swap=lambda *a, **kw: NS(min_receive=1000000),
                swap=lambda *a, **kw: sent.append((a, kw)) or SwapResult('tx', 'ok', 2000000, 1000000, 1, None, 1, 1))
    s = spec('okx_evm', leg='s', direction='long', network='56', account='wallet')
    ctx = AdapterContext()
    ctx.add(s, evm(native, quote_token='USDC', quote_decimals=6, journal=journal, authorize=lambda *a: None,
                   resolve_row=lambda *a: None, read_executions=lambda *a: ExecutionPage((), None, True),
                   clip_ref=lambda a: 'clip-1', clock=lambda: 100))
    leg = production_registry().build(s, ctx)
    q = leg.quote(Action('a', 's', 'BUY', D(1)), {'spend': D(2)})
    assert leg.submit(leg.prepare('p', q)).executed_quantity == D(1)
    assert sent[0][0][2] == 2000000 and sent[0][1]['approved_min_receive'] == 1000000


def test_solana_binding_routes_without_hl_account_methods():
    from funding_bot.trade.adapters.spot_bindings import solana
    from funding_bot.trade.adapters.context import AdapterContext
    from funding_bot.trade.spot_router import AssetRef
    from funding_bot.trade.solana import TOKEN_PROGRAM, USDC_MINT, MAINNET_GENESIS
    import sol_hl_fixtures as f
    journal = AttemptJournal(sqlite3.connect(':memory:'))
    sent = []
    native = NS(wallet=f.SOL_ADDR, genesis=MAINNET_GENESIS,
                chain=NS(block_height=lambda: 100), pending=lambda con: [], account_rent=lambda *a: 0,
                swap=lambda *a, **kw: sent.append((a, kw)) or SwapOutcome('ok', 'native-attempt', 'sig',
                                                                         2000000, 1000000, 'finalized'))
    candidate = NS(effective_min_out=1000000, message_hash='validated-hash')
    router = NS(clock=lambda: 100, select=lambda *a, **kw: NS(winner=candidate), presign_check=lambda *a, **kw: ())
    s = spec('sol_best', leg='sol', direction='long', instrument=f.ANSEM, account=f.SOL_ADDR, network=MAINNET_GENESIS)
    ctx = AdapterContext()
    ctx.add(s, solana(native, router, token=AssetRef(f.ANSEM, TOKEN_PROGRAM, 6),
                     quote_asset=AssetRef(USDC_MINT, TOKEN_PROGRAM, 6), journal=journal, authorize=lambda *a: None,
                     con=journal.con, prices=lambda: {}, hedge=lambda: None, resolve_ref=lambda *a: None,
                     read_executions=lambda *a: ExecutionPage((), None, True), apply=lambda r: None,
                     clip_ref=lambda ref: 'legacy-clip', slippage_bps=50, min_validity_heights=10, clock=lambda: 100))
    leg = production_registry().build(s, ctx)
    quote = leg.quote(Action('buy-sol', 'sol', 'BUY', D(1)), {'spend': D(2)})
    assert leg.submit(leg.prepare('prepared-sol', quote)).status == Status.SETTLED
    assert sent[0][1]['logical_action_id'] == 'buy-sol'
    assert sent[0][0][2].output.mint == f.ANSEM


def test_legacy_mapping_preserves_hash_and_separates_quote_currencies():
    from funding_bot.trade.adapters.mapping import map_legacy
    from funding_bot.trade.types import InstrumentSpec, Filters
    inst = InstrumentSpec(chain='robinhood', token='0x' + 'ab' * 20, token_dec=18, perp_venue='gate',
                          perp_symbol='TOKEN_USDT', units_per_contract=D(1000), spot_units_per_token=D(1),
                          quote_asset='USDT', ident_ev='verified:test-source', verified=True)
    raw, original_hash = inst.to_json(), inst.inst_hash()
    deal = {'id': 'historical', 'inst_json': raw}
    filt = Filters(D('.01'), D(1), D(1), D(1000), D(1000), D(1), frozenset({'IOC'}))
    first, second = map_legacy(deal, spot_account='evm-wallet', perp_account='gate-account', filters=filt,
                               spot_quote='USDG', network='4663', spot_tick=D('.0001'), metadata_revision='r1')
    assert deal['inst_json'] == raw and InstrumentSpec.from_json(raw).inst_hash() == original_hash
    assert first.legacy_hash == second.legacy_hash == original_hash
    assert first.quote_currency == 'USDG' and second.quote_currency == 'USDT'
    assert first.exposure(D(1000)) == second.exposure(D(1))


def test_execution_pages_keep_scoped_trade_ids_and_completeness():
    from funding_bot.trade.adapters.futures_bindings import bind
    calls = []
    native = NS(fills=lambda symbol, start: calls.append(start) or [{'trade_id': 7, 'qty': D(1)}])
    b = bind(native, journal=None, authorize=None, attempt_lookup=None, on_signed=None)
    page = b.executions(spec('gate'), None)
    assert calls == [0] and page.cursor == '8' and page.complete
    assert page.executions[0]['dedup_key'] == (*spec('gate').scope, 7)


def test_unknown_zero_placeholder_is_not_proven_flat():
    result = outcomes.perpetual(PerpFill('x', None, 'UNKNOWN', D(0), D(0), D(0), 0), spec().scope)
    assert result.executed_quantity is None and not result.terminal and not result.fees_complete


def test_nonfinal_solana_failure_remains_unknown():
    result = outcomes.sol_swap(SwapOutcome('failed', 'a', 'sig', 0, 0, 'confirmed'),
                               spec('sol_best', direction='long'), 'BUY')
    assert result.status == Status.UNKNOWN and result.provisional


def test_prepared_scope_cannot_be_rebound_to_another_account():
    ctx = Context()
    leg = production_registry().build(spec(), ctx)
    p = leg.prepare('scope-attempt', leg.quote(Action('scope-action', 'p', 'SELL', D(1)), {}))
    altered = production_registry().build(spec(account='other-account'), ctx)
    with pytest.raises(AdapterError):
        altered.submit(p)
    # Changing the object hash cannot bypass the persisted spec binding either.
    with pytest.raises(AdapterError):
        altered.submit(replace(p, spec_hash=altered.describe().fingerprint))
    assert not ctx.sent


def test_aster_initialization_failure_does_not_block_gate(tmp_path, monkeypatch):
    from funding_bot.trade import assembly, owner
    from funding_bot.trade.engine import Conns, CfgHolder
    from funding_bot.trade.runtime import ProfileDown
    import test_rh_gate_engine as rh
    p = tmp_path / 'owner.toml'
    p.write_text(rh.rh_toml())
    cfg = owner.load(p)
    def bad_clock():
        raise RuntimeError('clock unavailable')
    def build(cfg, conns, **kw):
        if kw.get('mode') == 'dry':
            return NS(mode='dry', keys=None, sim='dry', live=None)
        return NS(mode='live', keys=object(), sim='sim', live=NS(perp=NS(check_clock=bad_clock)))
    monkeypatch.setattr(assembly, 'build_runtime', build)
    rt, registry, *_ = assembly.build_trader_legs(cfg, Conns(tmp_path / 'trade.db'), CfgHolder(lambda: cfg), {})
    assert owner.RH_GATE in registry.factories and rt.mode == 'dry'
    assert registry.health()[owner.LEGACY_PROFILE] == 'clock unavailable'
    with pytest.raises(ProfileDown):
        registry(False)


def test_single_aster_clock_refusal_retains_configuration_error(tmp_path, monkeypatch):
    from funding_bot.trade import assembly, owner
    from funding_bot.trade.engine import Conns, CfgHolder
    from funding_bot.trade.keys import KeysError
    from funding_bot.trade.aster_trade import AsterError
    import test_trade_engine as te
    p = tmp_path / 'owner.toml'
    p.write_text(te.live_toml())
    cfg = owner.load(p)
    def bad_clock():
        raise AsterError('clock skew')
    monkeypatch.setattr(assembly, 'build_runtime', lambda *a, **kw:
                        NS(mode='live', keys=object(), sim=None, live=NS(perp=NS(check_clock=bad_clock))))
    with pytest.raises(KeysError, match='clock skew'):
        assembly.build_trader_legs(cfg, Conns(tmp_path / 'trade.db'), CfgHolder(lambda: cfg), {})
