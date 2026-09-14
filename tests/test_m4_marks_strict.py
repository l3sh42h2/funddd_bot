"""M4: missing execution money stays unknown through marks and the read-only cache."""
from decimal import Decimal as D

import pytest

from funding_bot.core import readmodel
from funding_bot.trade import marks, store

import test_marks as legacy


@pytest.fixture
def con(tmp_path):
    db = store.connect(tmp_path / "trade.db")
    yield db
    db.close()


def _entry_clip(con) -> int:
    return int(con.execute(
        "SELECT c.id FROM clips c JOIN intents i ON i.id=c.intent_id "
        "WHERE i.deal_id='DQA9Q' AND i.kind='entry'"
    ).fetchone()[0])


def test_known_journal_keeps_exact_legacy_amounts(con):
    deal = legacy.dqa9q(con)
    j = marks.journal(con, deal)

    assert j.missing_flows == ()
    assert j.spot_flow == D("-200")
    assert j.perp_flow == D("200.63886")
    assert marks.final_mark(con, deal, legacy.legs_of(), now=legacy.NOW).pnl_now == legacy.BASE


@pytest.mark.parametrize(
    ("sql", "missing"),
    [
        ("UPDATE clips SET dex_in=NULL WHERE id=?", "clip:"),
        ("UPDATE perp_orders SET cum_quote=NULL WHERE side='SELL'", "order:"),
    ],
)
def test_missing_executed_quote_makes_mark_and_final_unknown(con, sql, missing):
    deal = legacy.dqa9q(con)
    args = (_entry_clip(con),) if "id=?" in sql else ()
    con.execute(sql, args)

    j = marks.journal(con, deal)
    assert j.missing_flows and j.missing_flows[0].startswith(missing)
    assert (j.spot_flow if missing == "clip:" else j.perp_flow) is None

    active = marks.mark_deal(con, deal, legacy.legs_of(), now=legacy.NOW)
    final = marks.final_mark(con, deal, legacy.legs_of(), now=legacy.NOW)
    for m in (active, final):
        assert m.pnl_now is None
        assert m.flags["accounting_complete"] is False
        assert m.flags["missing_flows"] == list(j.missing_flows)


def test_active_pre_migration_cache_is_invalidated_by_missing_quote(con):
    deal = legacy.dqa9q(con)
    store.add_mark(con, deal["id"], legacy.NOW - 5, pnl_now=D("9"), pnl_exit=D("8"), exit_cost=D("1"), flags={})
    con.execute("UPDATE perp_orders SET cum_quote=NULL WHERE side='SELL'")

    view = readmodel._pnl_view(con, dict(deal), legacy.NOW)

    assert view["mark"]["pnl_now"] is None
    assert view["mark"]["pnl_exit"] is None
    assert view["mark"]["exit_cost"] is None
    assert view["mark"]["flags"]["accounting_complete"] is False
    assert view["mark"]["flags"]["missing_flows"] == ["order:fb-DQA9Q-e01-c1-a1:quote"]


def test_closed_pre_migration_cache_and_fallback_cannot_report_zero(con):
    legacy.dqa9q(con)
    legacy._close_dqa9q(con)
    deal = dict(store.get_deal(con, "DQA9Q"))
    store.add_mark(con, deal["id"], legacy.NOW, pnl_now=D("7"), flags={"final": True})
    con.execute("UPDATE perp_orders SET cum_quote=NULL WHERE side='SELL'")

    cached = readmodel._pnl_view(con, deal, legacy.NOW)
    con.execute("DELETE FROM deal_marks")
    fallback = readmodel._pnl_view(con, deal, legacy.NOW)

    assert cached == {"final": True, "total": None, "no_gas": False, "incomplete": True}
    assert fallback == cached
