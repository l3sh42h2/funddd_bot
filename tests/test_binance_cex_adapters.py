"""Binance зарегистрирован в production_registry() и проходит полный dry-run вход+выход через ОБЩИЙ адаптерный
слой (contracts/native/registry/futures_bindings/cex_bindings) на фейковом транспорте — настоящие BinanceTrade
и BinanceSpotTrade, а не синтетический Transport из docs/migration/examples (в отличие от
tests/test_generic_adapter_matrix.py, который проверяет контракт на fake-примерах). Это архитектурный аналог
tests/test_rh_gate_engine.py (полный dry-run FATCOIN), но на уровне adapters/: у Binance пока нет профиля
owner.toml/Desk-обвязки (см. PATCHNOTES/binance-perp-spot-adapters-20260918.md — открытый вопрос), поэтому здесь
собирается и проверяется именно то, что реально подключено — registry.compose() двух настоящих ног, — а не
воображаемый Desk-путь.

Никаких реальных сетевых вызовов: обе ноги — фейковые requests.Session из test_binance_trade.py /
test_binance_spot_trade.py (переиспользованы напрямую, как test_m4_signing_fence.py делает с FakeAster/FakeGate).
"""
from __future__ import annotations
import sqlite3
from decimal import Decimal as D

import pytest

from funding_bot.trade import binance_trade as bt
from funding_bot.trade import binance_spot_trade as bst
from funding_bot.trade.adapters import cex_bindings, futures_bindings, registry as registry_mod
from funding_bot.trade.adapters.attempts import AttemptJournal
from funding_bot.trade.adapters.context import AdapterContext
from funding_bot.trade.adapters.contracts import Action, Capabilities, LegSpec, Status

from test_binance_trade import FakeBinance, EI as PERP_EI, KEY as PKEY, SECRET as PSECRET
from test_binance_spot_trade import FakeBinanceSpot, EI_ROW as SPOT_EI_ROW, KEY as SKEY, SECRET as SSECRET

SYM = "AIW3USDT"


def test_binance_and_binance_spot_are_registered():
    reg = registry_mod.production_registry()
    assert {"binance", "binance_spot"} <= set(reg._factories)


class _PerpJournal:
    """Минимальный тестовый journal+attempt_lookup+on_signed для futures_bindings.bind (не native_journal.PerpJournal
    — этот тест не завязан на deal/clip, см. докстринг файла): хранит leg_id/symbol/side по attempt_id."""

    def __init__(self):
        self._rows: dict[str, dict] = {}

    def prepare(self, prepared):
        import json
        data = json.loads(prepared.quote.native)
        self._rows[prepared.attempt_id] = {"leg_id": prepared.quote.action.leg_id, "symbol": data["symbol"],
                                           "side": prepared.quote.action.side, "state": "PREPARED"}

    def claim(self, prepared):
        self._rows[prepared.attempt_id]["state"] = "CLAIMED"

    def lookup(self, ref):
        return self._rows[ref]

    def on_signed(self, attempt_id, nonce):
        pass


@pytest.fixture
def perp_fake():
    return FakeBinance()


@pytest.fixture
def spot_fake():
    f = FakeBinanceSpot()
    f.balances_rows = [{"asset": "USDT", "free": "1000", "locked": "0"}, {"asset": "AIW3", "free": "0", "locked": "0"}]
    return f


def _perp_native(fake):
    return bt.BinanceTrade(PKEY, PSECRET, mode_state=lambda: ("live", False), session=fake, sleep=lambda s: None)


def _spot_native(fake):
    return bst.BinanceSpotTrade(SKEY, SSECRET, mode_state=lambda: ("live", False), session=fake, sleep=lambda s: None)


def _specs(spot_account: str, perp_account: str):
    """account — ровно то, что вернёт native.history_account() (futures_bindings.executions сверяет identity()
    == spec.account буквально): не произвольная метка, а доказанная принадлежность счёта."""
    common = dict(asset_id="AIW3", identity_evidence="test-evidence", multiplier=D(1), step=D(1),
                 tick=D("0.0001"), quote_currency="USDT", settlement_currency="USDT", metadata_revision="v1")
    spot = LegSpec("t:spot", "inventory", "long", "binance_spot", "binance_spot", spot_account, SYM,
                   capabilities=Capabilities("cex", "spot"), decimals=8, quote_decimals=8, **common)
    perp = LegSpec("t:perp", "hedge", "short", "binance", "binance", perp_account, SYM,
                   capabilities=Capabilities("cex", "perpetual", short=True, reduce_only=True),
                   margin_currency="USDT", **common)
    return spot, perp


def _compose(perp_fake, spot_fake):
    perp_native, spot_native = _perp_native(perp_fake), _spot_native(spot_fake)
    perp_native.filters(SYM)     # прогреть кэш фильтров (как это делал бы движок до планирования)
    spot_native.filters(SYM)
    spot_spec, perp_spec = _specs(spot_native.history_account(), perp_native.history_account())
    con = sqlite3.connect(":memory:")
    pj = _PerpJournal()
    perp_bindings = futures_bindings.bind(perp_native, journal=pj, authorize=lambda spec, action: None,
                                         attempt_lookup=pj.lookup, on_signed=pj.on_signed)
    spot_bindings = cex_bindings.bind(spot_native, journal=AttemptJournal(con), authorize=lambda spec, action: None)
    context = AdapterContext()
    context.add(spot_spec, spot_bindings)
    context.add(perp_spec, perp_bindings)
    reg = registry_mod.production_registry()
    pair = reg.compose(spot_spec, perp_spec, context)
    return pair, perp_native, spot_native


def test_full_dry_run_entry_and_exit_on_fakes(perp_fake, spot_fake):
    pair, perp_native, spot_native = _compose(perp_fake, spot_fake)
    spot_adapter, perp_adapter = pair.first, pair.second

    # --- ВХОД: спот BUY 100 AIW3 @≤0.05, перп SELL 100 (открыть шорт-хедж, reduce_only=False) ---
    spot_action = Action("e-spot-1", "t:spot", "BUY", D(100))
    spot_quote = spot_adapter.quote(spot_action, {"price_cap": D("0.05")})
    spot_prepared = spot_adapter.prepare("e-spot-1", spot_quote)
    spot_result = spot_adapter.submit(spot_prepared)
    assert spot_result.status == Status.SETTLED and spot_result.executed_quantity == D(100)
    assert spot_result.spot_input_raw.amount == D(100) * D("0.05")     # USDT потрачено
    assert spot_result.spot_output_raw.amount == D(100)                 # AIW3 получено

    perp_action = Action("e-perp-1", "t:perp", "SELL", D(100), reduce_only=False)
    perp_quote = perp_adapter.quote(perp_action, {"price_cap": D("0.06")})     # notional 6 >= min_notional 5
    perp_prepared = perp_adapter.prepare("e-perp-1", perp_quote)
    perp_result = perp_adapter.submit(perp_prepared)
    assert perp_result.status == Status.SETTLED and perp_result.executed_quantity == D(100)
    assert perp_native.position(SYM) == D(-100)          # шорт открыт на фейковой площадке
    assert spot_native.balance("AIW3") == D(100)          # спот куплен на фейковой площадке

    # --- ВЫХОД: спот SELL 100 AIW3, перп BUY 100 reduce_only=True (закрыть шорт) ---
    exit_spot_action = Action("x-spot-1", "t:spot", "SELL", D(100))
    exit_spot_quote = spot_adapter.quote(exit_spot_action, {"price_cap": D("0.0501")})
    exit_spot_result = spot_adapter.submit(spot_adapter.prepare("x-spot-1", exit_spot_quote))
    assert exit_spot_result.status == Status.SETTLED and exit_spot_result.executed_quantity == D(100)

    exit_perp_action = Action("x-perp-1", "t:perp", "BUY", D(100), reduce_only=True)
    exit_perp_quote = perp_adapter.quote(exit_perp_action, {"price_cap": D("0.0502")})
    exit_perp_result = perp_adapter.submit(perp_adapter.prepare("x-perp-1", exit_perp_quote))
    assert exit_perp_result.status == Status.SETTLED and exit_perp_result.executed_quantity == D(100)

    assert perp_native.position(SYM) == D(0)              # шорт закрыт
    assert spot_native.balance("AIW3") == D(0)             # спот продан обратно
    # обе ноги действительно ушли по сети (не осели в кэше/раннем отказе)
    assert perp_fake.n("POST", "/fapi/v1/order") == 2 and spot_fake.n("POST", "/api/v3/order") == 2


def test_perp_reduce_only_close_exempt_from_min_notional(perp_fake, spot_fake):
    """futures_bindings.bind: close_exempt теперь включает 'binance' — маленький reduce-only, который не
    прошёл бы min_notional как открывающая заявка, не отклоняется локально при закрытии позиции."""
    import dataclasses
    pair, perp_native, _ = _compose(perp_fake, spot_fake)
    perp_adapter = pair.second
    ts, filt = perp_native._filters[SYM]
    perp_native._filters[SYM] = (ts, dataclasses.replace(filt, min_notional=D(1000)))    # заведомо выше 1×0.05
    tiny = Action("tiny-close", "t:perp", "BUY", D(1), reduce_only=True)
    q = perp_adapter.quote(tiny, {"price_cap": D("0.05")})     # notional 0.05 << min_notional, но reduce_only+binance
    assert q.max_spend == D("0.05") and q.min_receive == D(1)  # не бросило AdapterError на min_notional (BUY: value=max_spend)
    open_qty = Action("tiny-open", "t:perp", "BUY", D(1), reduce_only=False)
    with pytest.raises(Exception):
        perp_adapter.quote(open_qty, {"price_cap": D("0.05")})   # не reduce_only — min_notional бьёт как обычно


def test_read_executions_pagination_both_legs(perp_fake, spot_fake):
    pair, perp_native, spot_native = _compose(perp_fake, spot_fake)
    spot_adapter, perp_adapter = pair.first, pair.second
    perp_fake.trades = [{"id": 1, "orderId": 900, "symbol": SYM, "side": "SELL", "price": "0.05", "qty": "100",
                        "quoteQty": "5", "commission": "0.002", "commissionAsset": "USDT", "maker": False,
                        "realizedPnl": "0", "time": 1}]
    spot_fake.trades = [{"id": 1, "orderId": 901, "symbol": SYM, "isBuyer": True, "price": "0.05", "qty": "100",
                        "quoteQty": "5", "commission": "0.0001", "commissionAsset": "AIW3", "isMaker": False, "time": 1}]
    perp_page = perp_adapter.read_executions(None)
    spot_page = spot_adapter.read_executions(None)
    assert perp_page.executions and perp_page.executions[0]["native"]["trade_id"] == 1
    assert spot_page.executions and spot_page.executions[0]["native"]["trade_id"] == 1
    assert perp_page.executions[0]["dedup_key"][:4] == ("binance", None, perp_native.history_account(), None)
