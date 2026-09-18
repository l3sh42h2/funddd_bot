"""Ручной вход владельца в /cabinet (18.09): карточка приходит из manual_positions.deal_views() через отдельный
инжектируемый Cabinet.manual_positions_loader — trade.db тут вообще не участвует. Пароли фейковые (как в
test_cabinet.py)."""
from __future__ import annotations
from decimal import Decimal as D
from urllib.parse import urlencode
from funding_bot import cabinet

LOGIN, PW = "owner-test", "fake-test-password-9Qx"
HASH = cabinet.make_hash(PW)
T0 = 1_789_750_000.0


def make_cab(tmp_path, manual_loader, deals=(), env=None):
    env = {"CABINET_LOGIN": LOGIN, "CABINET_PASS_HASH": HASH} if env is None else env
    snap = {"now": T0, "deals": list(deals), "drafts": 0, "err": None}
    return cabinet.Cabinet(environ=env, clock=lambda: T0, snapshot_loader=lambda: snap,
                           manual_positions_loader=manual_loader)


def login_ok(cab) -> str:
    body = urlencode({"login": LOGIN, "password": PW}).encode()
    r = cab.handle(cabinet.Req("POST", "/cabinet/login", {}, "127.0.0.1", body))
    assert r.code == 303, r.body
    return [v for k, v in r.headers if k.lower() == "set-cookie"][0].split(";")[0]


def page_of(cab) -> str:
    ck = login_ok(cab)
    r = cab.handle(cabinet.Req("GET", "/cabinet", {"Cookie": ck}, "127.0.0.1"))
    assert r.code == 200
    return r.body.decode()


def manual_view(**over) -> dict:
    v = {
        "manual": True, "state": "MANUAL", "id": "ansem-1", "coin": "ANSEM", "opened": 1789700000.0,
        "note": "вход руками 18.09",
        "perp": {"venue": "Lighter", "address": "0xE4Ebf0815d0980E5a03f7D675F86dc5079fB8919", "symbol": "ANSEM",
                 "side": "short", "size": D("26040.994142"), "mark": D("0.281")},
        "spot": {"venue": "Solana", "wallet": "2KqfMRtMtRYyHNZjxW9521ZfT2T2DMEQv1JM3BdKJv37",
                  "mint": "9cRCn9rGT8V2imeM2BaKs13yhMEais3ruM3rPvTGpump"},
        "funding": {"total": D("5.1354"), "ccy": "USDC"},
        "stale": False, "error": None, "live_ts": T0,
    }
    v.update(over)
    return v


def test_manual_card_renders_legs_funding_and_no_pnl_claim(tmp_path):
    cab = make_cab(tmp_path, lambda now: [manual_view()])
    page = page_of(cab)
    assert "✋ Ручной вход" in page and ">ANSEM<" in page and ">ansem-1<" in page
    assert "0xE4…8919" in page and "2Kqf…Jv37" in page and "9cRC…pump" in page
    assert "шорт" in page and "26" in page
    assert "$0.281" in page                                        # марк
    assert "фандинг с входа (счётчик Lighter)" in page
    assert "PnL: нет подтверждённой оценки" in page and "cost basis спота не отслеживается" in page
    assert "вход руками 18.09" in page


def test_manual_card_funding_amount_and_sign(tmp_path):
    cab = make_cab(tmp_path, lambda now: [manual_view()])
    page = page_of(cab)
    # _pnl_usd → fmt_num(x, 2, sign=True): 5.1354 -> "+5.14"
    assert '<b class="mono g">+5.14 USDC</b>' in page


def test_manual_card_negative_funding_is_red(tmp_path):
    cab = make_cab(tmp_path, lambda now: [manual_view(funding={"total": D("-1.5"), "ccy": "USDC"})])
    page = page_of(cab)
    assert '<b class="mono r">−1.50 USDC</b>' in page


def test_manual_card_shows_error_instead_of_stale(tmp_path):
    cab = make_cab(tmp_path, lambda now: [manual_view(error="рынок ANSEM не найден в позициях аккаунта", stale=True)])
    page = page_of(cab)
    assert '<span class="unk">рынок ANSEM не найден в позициях аккаунта</span>' in page
    assert "устарело" not in page      # ошибка важнее, "устарело" не дублируется


def test_manual_card_shows_stale_when_no_error(tmp_path):
    cab = make_cab(tmp_path, lambda now: [manual_view(stale=True, live_ts=T0 - 1000)])
    page = page_of(cab)
    assert "устарело" in page


def test_manual_card_missing_funding_shows_dash(tmp_path):
    cab = make_cab(tmp_path, lambda now: [manual_view(funding={"total": None, "ccy": "USDC"})])
    page = page_of(cab)
    assert 'фандинг с входа (счётчик Lighter)</span><b class="mono ">—</b>' in page


def test_manual_position_escapes_html_in_note_and_ids(tmp_path):
    xss = '<script>alert(1)</script>'
    cab = make_cab(tmp_path, lambda now: [manual_view(coin=xss, note=xss, id=xss)])
    page = page_of(cab)
    assert "<script>alert(1)</script>" not in page
    assert "&lt;script&gt;alert(1)&lt;/script&gt;" in page


def test_summary_counts_manual_separately_from_bot_deals(tmp_path):
    deal = {"id": "DAAA", "coin": "AIW3", "chain": "bsc", "venue": "aster", "symbol": "AIW3USDT", "leg_usd": D(500),
            "state": "OPEN", "reason": None, "sim": False, "opened": T0, "closed": None, "spot_unknown": False,
            "perp_unknown": False, "last": None, "row_key": None, "live": None, "liq": None, "hist": None,
            "pnl": None}
    cab = make_cab(tmp_path, lambda now: [manual_view()], deals=[deal])
    ck = login_ok(cab)
    body = cab.handle(cabinet.Req("GET", "/cabinet/deals.json", {"Cookie": ck}, "127.0.0.1")).body
    import json
    summary = json.loads(body)["summary"]
    assert summary.startswith("сделок 1 · открыто 1") and "ручных 1" in summary


def test_manual_positions_loader_exception_does_not_crash_page(tmp_path):
    def boom(now):
        raise RuntimeError("сеть недоступна")
    cab = make_cab(tmp_path, boom)
    page = page_of(cab)
    assert "внутренняя ошибка" not in page and "сделок пока нет" in page


def test_no_manual_positions_means_no_extra_card(tmp_path):
    cab = make_cab(tmp_path, lambda now: [])
    page = page_of(cab)
    assert "Ручной вход" not in page and "сделок пока нет" in page
