"""Ручной вход владельца (18.09): manual_positions.toml → опрос публичного Lighter account API → карточка в
/cabinet. Ничего не пишет в trade.db; сеть — только в poll_once/poll_lighter_account. Здесь сеть подделана."""
from __future__ import annotations
import json
from decimal import Decimal as D
import pytest
from funding_bot import cli, manual_positions as mp

TOML = """
[[position]]
id = "ansem-1"
coin = "ANSEM"
opened = 1789700000
note = "тест"
[position.perp]
venue = "lighter"
l1_address = "0xE4Ebf0815d0980E5a03f7D675F86dc5079fB8919"
symbol = "ANSEM"
[position.spot]
venue = "solana"
wallet = "2KqfMRtMtRYyHNZjxW9521ZfT2T2DMEQv1JM3BdKJv37"
mint = "9cRCn9rGT8V2imeM2BaKs13yhMEais3ruM3rPvTGpump"
"""


class FakeResp:
    def __init__(self, body, status=200):
        self._body, self.status = body, status

    def raise_for_status(self):
        if self.status >= 400:
            import requests
            raise requests.HTTPError(f"{self.status}")

    def json(self):
        return self._body


class FakeSession:
    def __init__(self, body=None, exc=None):
        self.body, self.exc, self.calls = body, exc, []

    def get(self, url, params=None, timeout=None, headers=None):
        self.calls.append((url, params))
        if self.exc is not None:
            raise self.exc
        return FakeResp(self.body)


ACCOUNT_OK = {"accounts": [{"positions": [
    # Реальный API Lighter: "position" — величина БЕЗ ЗНАКА, сторона — отдельное поле "sign" (-1 шорт, 1 лонг).
    # Проверено 18.09 независимым ревью против живого API — сторона по знаку "position" была бы багом.
    {"symbol": "ANSEM", "sign": -1, "position": "26040.994142", "avg_entry_price": "0.281",
     "total_funding_paid_out": "5.1354"},
    {"symbol": "OTHER", "sign": 1, "position": "10"},
]}]}


# --- load_config -----------------------------------------------------------------------------------------------
def test_load_config_missing_file_returns_empty(tmp_path):
    assert mp.load_config(tmp_path / "nope.toml") == []


def test_load_config_parses_positions(tmp_path):
    p = tmp_path / "manual_positions.toml"
    p.write_text(TOML)
    out = mp.load_config(p)
    assert len(out) == 1
    assert out[0]["id"] == "ansem-1" and out[0]["coin"] == "ANSEM"
    assert out[0]["perp"]["l1_address"].startswith("0xE4Eb")


def test_load_config_skips_entries_without_id_or_coin(tmp_path):
    p = tmp_path / "manual_positions.toml"
    p.write_text('[[position]]\nnote = "нет id и coin"\n')
    assert mp.load_config(p) == []


def test_load_config_broken_toml_returns_empty_not_raises(tmp_path):
    p = tmp_path / "manual_positions.toml"
    p.write_text("this is not [ valid toml")
    assert mp.load_config(p) == []


# --- poll_lighter_account ----------------------------------------------------------------------------------------
def test_poll_lighter_account_found_short_position():
    s = FakeSession(body=ACCOUNT_OK)
    out = mp.poll_lighter_account("0xE4Eb...", "ANSEM", session=s)
    # Числа наружу — строками (atomic_json не сериализует Decimal); deal_views() читает их обратно через _dv().
    assert out["side"] == "short"
    assert D(out["size"]) == D("26040.994142")
    assert D(out["mark"]) == D("0.281")
    assert D(out["funding_total"]) == D("5.1354")
    assert s.calls[0][1] == {"by": "l1_address", "value": "0xE4Eb..."}


def test_poll_lighter_account_long_side_from_positive_sign():
    s = FakeSession(body=ACCOUNT_OK)
    out = mp.poll_lighter_account("0xE4Eb...", "OTHER", session=s)
    assert out["side"] == "long" and D(out["size"]) == D("10")


def test_poll_lighter_account_side_ignores_position_string_sign():
    """Регрессия P1 (независимое ревью 18.09): даже если "position" пришла бы со знаком, сторону решает только
    "sign" — величина всегда берётся по модулю."""
    body = {"accounts": [{"positions": [{"symbol": "X", "sign": 1, "position": "-5"}]}]}
    out = mp.poll_lighter_account("0xE4Eb...", "X", session=FakeSession(body=body))
    assert out["side"] == "long" and D(out["size"]) == D("5")


def test_poll_lighter_account_missing_sign_field_is_unknown_side():
    body = {"accounts": [{"positions": [{"symbol": "X", "position": "5"}]}]}
    out = mp.poll_lighter_account("0xE4Eb...", "X", session=FakeSession(body=body))
    assert out["side"] is None


def test_poll_lighter_account_non_dict_account_entry_does_not_crash():
    s = FakeSession(body={"accounts": ["not-a-dict"]})
    out = mp.poll_lighter_account("0xE4Eb...", "ANSEM", session=s)
    assert out["error"] == "аккаунт не найден"


def test_poll_lighter_account_null_position_entry_does_not_crash():
    body = {"accounts": [{"positions": [None, {"symbol": "ANSEM", "sign": -1, "position": "1"}]}]}
    out = mp.poll_lighter_account("0xE4Eb...", "ANSEM", session=FakeSession(body=body))
    assert out["side"] == "short"


def test_poll_lighter_account_missing_symbol():
    s = FakeSession(body=ACCOUNT_OK)
    out = mp.poll_lighter_account("0xE4Eb...", "NOPE", session=s)
    assert "error" in out and "NOPE" in out["error"]


def test_poll_lighter_account_no_accounts():
    s = FakeSession(body={"accounts": []})
    out = mp.poll_lighter_account("0xE4Eb...", "ANSEM", session=s)
    assert out["error"] == "аккаунт не найден"


def test_poll_lighter_account_network_error_is_caught():
    import requests
    s = FakeSession(exc=requests.ConnectionError("boom"))
    out = mp.poll_lighter_account("0xE4Eb...", "ANSEM", session=s)
    assert "error" in out and "ConnectionError" in out["error"]


def test_poll_lighter_account_bad_json_is_caught():
    class BadJsonSession(FakeSession):
        def get(self, *a, **kw):
            r = FakeResp(None)
            r.json = lambda: (_ for _ in ()).throw(ValueError("bad json"))
            return r
    out = mp.poll_lighter_account("0xE4Eb...", "ANSEM", session=BadJsonSession())
    assert "error" in out


# --- poll_once / load_live ----------------------------------------------------------------------------------------
def test_poll_once_writes_live_json(tmp_path):
    positions = mp.load_config(_write_toml(tmp_path))
    live_path = tmp_path / "live.json"
    s = FakeSession(body=ACCOUNT_OK)
    out = mp.poll_once(positions, session=s, now=1789750000.0, path=live_path)
    assert out["ansem-1"]["side"] == "short"
    doc = json.loads(live_path.read_text())
    assert doc["schema_version"] == mp.SCHEMA and doc["ts"] == 1789750000.0
    assert doc["positions"]["ansem-1"]["funding_total"] == "5.1354"


def test_poll_once_flags_incomplete_perp_config(tmp_path):
    live_path = tmp_path / "live.json"
    out = mp.poll_once([{"id": "x", "perp": {"venue": "hyperliquid"}}], session=FakeSession(body={}),
                        now=1.0, path=live_path)
    assert "error" in out["x"]


def test_load_live_rejects_wrong_schema(tmp_path):
    p = tmp_path / "live.json"
    p.write_text(json.dumps({"schema_version": 999, "ts": 1.0, "positions": {}}))
    assert mp.load_live(p) == {}


def test_load_live_missing_file_returns_empty(tmp_path):
    assert mp.load_live(tmp_path / "nope.json") == {}


# --- deal_views ------------------------------------------------------------------------------------------------
def _write_toml(tmp_path, text=TOML):
    p = tmp_path / "manual_positions.toml"
    p.write_text(text)
    return p


def test_deal_views_empty_without_config(tmp_path):
    assert mp.deal_views(now=1.0, config_path=tmp_path / "nope.toml", live_path=tmp_path / "nope.json") == []


def test_deal_views_merges_config_and_fresh_live(tmp_path):
    cfg = _write_toml(tmp_path)
    live = tmp_path / "live.json"
    live.write_text(json.dumps({"schema_version": mp.SCHEMA, "ts": 1789750000.0, "positions": {
        "ansem-1": {"side": "short", "size": "26040.994142", "mark": "0.281", "funding_total": "5.1354"}}}))
    [v] = mp.deal_views(now=1789750010.0, config_path=cfg, live_path=live)
    assert v["manual"] is True and v["state"] == "MANUAL" and v["coin"] == "ANSEM"
    assert v["perp"]["side"] == "short" and v["perp"]["address"].startswith("0xE4Eb")
    assert v["perp"]["size"] == D("26040.994142") and v["perp"]["mark"] == D("0.281")
    assert v["spot"]["mint"].startswith("9cRCn9rG")
    assert v["funding"]["total"] == D("5.1354") and v["funding"]["ccy"] == "USDC"
    assert v["stale"] is False and v["error"] is None
    assert v["opened"] == 1789700000.0


def test_deal_views_stale_when_live_too_old(tmp_path):
    cfg = _write_toml(tmp_path)
    live = tmp_path / "live.json"
    live.write_text(json.dumps({"schema_version": mp.SCHEMA, "ts": 1789750000.0, "positions": {}}))
    [v] = mp.deal_views(now=1789750000.0 + mp.LIVE_STALE_S + 1, config_path=cfg, live_path=live)
    assert v["stale"] is True


def test_deal_views_stale_when_live_file_missing(tmp_path):
    cfg = _write_toml(tmp_path)
    [v] = mp.deal_views(now=1789750000.0, config_path=cfg, live_path=tmp_path / "nope.json")
    assert v["stale"] is True and v["funding"]["total"] is None


def test_deal_views_surfaces_per_position_error(tmp_path):
    cfg = _write_toml(tmp_path)
    live = tmp_path / "live.json"
    live.write_text(json.dumps({"schema_version": mp.SCHEMA, "ts": 1789750000.0,
                                "positions": {"ansem-1": {"error": "рынок не найден"}}}))
    [v] = mp.deal_views(now=1789750000.0, config_path=cfg, live_path=live)
    assert v["error"] == "рынок не найден"


# --- CLI (manual-poll): код возврата — как у audit, крон должен видеть сбой ------------------------------------
def test_cli_manual_poll_exit_0_when_no_errors(monkeypatch):
    monkeypatch.setattr(mp, "poll_once", lambda: {"a": {"side": "long"}})
    assert cli.main(["manual-poll"]) == 0


def test_cli_manual_poll_exit_1_when_any_position_errors(monkeypatch, capsys):
    monkeypatch.setattr(mp, "poll_once", lambda: {"a": {"error": "boom"}, "b": {"side": "short"}})
    assert cli.main(["manual-poll"]) == 1
    assert "boom" in capsys.readouterr().err
