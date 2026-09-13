"""Владелец 13.09: «количество сделок устанавливаю я» — лимит числа открытых сделок не обязателен. Нет ключа
limits.max_open_deals — live не отказывает и вход не ограничен числом сделок; ключ задан — ограничение как раньше."""
from decimal import Decimal as D

from funding_bot.trade import store
from funding_bot.trade.store import DealState

import test_trade_engine as fx


def _open_other_deal(e, deal_id: str) -> None:
    row = dict(store.get_deal(e.con, deal_id))
    row.update(id="DOTHER", state="OPEN", token="0x" + "b2" * 20, symbol="BBBUSDT")
    e.con.execute(f"INSERT INTO deals({','.join(row)}) VALUES({','.join('?' * len(row))})", tuple(row.values()))
    e.con.commit()


def test_max_open_deals_is_not_required_for_live(tmp_path):
    e = fx.live_env(tmp_path)
    e.path.write_text(fx.live_toml().replace("max_open_deals = 1\n", ""))
    cfg = e.loader()
    assert cfg.get("limits.max_open_deals") is None
    assert "limits.max_open_deals" not in cfg.live_required()
    assert cfg.live_missing() == []


def test_without_the_key_entry_is_not_limited_by_open_deals(tmp_path):
    e = fx.live_env(tmp_path)
    e.path.write_text(fx.live_toml().replace("max_open_deals = 1\n", ""))
    p = e.desk.propose_entry("AIW3", "okx·bsc", "aster", D(200), chat=fx.OWNER)
    _open_other_deal(e, p.deal_id)                 # до кнопки открылась другая сделка — лимита нет, вход идёт
    fx.run_approved(e, p)
    assert store.get_deal(e.con, p.deal_id)["state"] == DealState.OPEN
    assert not any("max_open_deals" in h for h in e.hooks.reports)
