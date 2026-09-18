"""Владелец 18.09: «спред в стакане (и на споте, и на фьючах) больше 3-дневного заработка — не показывать монету,
пусть будет тоггл». Тоггл «скрыть спред > 3 дн» — по умолчанию выключен (ничего не скрывается, как раньше);
включённый прячет строки, где calc.spread_book (сумма round-trip спредов обеих ног) больше |текущая часовая ставка
строки| × 72 (дашборд.py: `spread_h` у futures/futures, `rate_h` у spot/futures — «пересчитай период ставки в 3 дня»).
Строка без годного спреда хоть на одной ноге (spread_book is None) — тоггл её не трогает."""
import json, os, subprocess
import pytest
from funding_bot import calc, config, dashboard
from test_client_collector import JSC
from test_dashboard_orient import STUB

NOW = 1_789_000_000_000


def _sf(key, rate, iv, perp_book, spot_book):
    """spot/futures: rate_h = rate/iv; income3d = |rate_h| * 72."""
    item = dict(key=key, base=key.split(":")[-1], perp_ex="binance", perp="X", spot_ex="binance_spot", spot="X")
    return calc.build_sf_row(item, {"interval_h": iv}, {"rate": rate, "interval_h": iv, "mark": 100.0},
                             perp_book, spot_book, {}, NOW)


def _ff(key, rate_a, rate_b, iv, book_a, book_b):
    item = dict(key=key, base=key.split(":")[-1], va="aster", vb="binance", sa="X", sb="X", ident="same")
    prem = lambda r: {"rate": r, "interval_h": iv, "mark": 100.0}
    return calc.build_ff_row(item, None, None, prem(rate_a), prem(rate_b), book_a, book_b, {}, {}, NOW)


def _table():
    # rate_h = 0.0008 / 8 = 0.0001 (0.01 %/ч) → доход за 3 дня = 0.0001 * 72 = 0.0072 (0.72 %) для всех строк ниже
    sf = [
        # LOW: спред 0.0002 + 0.0001 = 0.0003 (0.03 %) < 0.72 % — видна всегда
        _sf("sp:LOW", 0.0008, 8, {"bid": 99.99, "ask": 100.01}, {"bid": 99.995, "ask": 100.005}),
        # HIGH: спред 0.20 + 0.02 = 0.22 (22 %) > 0.72 % — скрыта только при включённом тоггле
        _sf("sp:HIGH", 0.0008, 8, {"bid": 90.0, "ask": 110.0}, {"bid": 99.0, "ask": 101.0}),
        # UNK: спот-книги нет (не свежая площадка) → spread_book = None — тоггл не трогает, видна всегда
        _sf("sp:UNK", 0.0008, 8, {"bid": 90.0, "ask": 110.0}, None),
    ]
    ff = [
        _ff("ff:LOW", 0.0008, 0.0, 8, {"bid": 9.999, "ask": 10.001}, {"bid": 9.999, "ask": 10.001}),
        _ff("ff:HIGH", 0.0008, 0.0, 8, {"bid": 9.0, "ask": 11.0}, {"bid": 9.9, "ask": 10.1}),
        _ff("ff:UNK", 0.0008, 0.0, 8, {"bid": 9.0, "ask": 11.0}, None),
    ]
    return dict(ts=NOW // 1000, tick_ts=NOW // 1000, ff_rows=ff, sf_rows=sf, health={}, notes={},
                windows=[str(w) for w in config.WINDOWS_H], venues=["aster", "binance"], spot_venues=["binance_spot"])


CHECKS = """
var out = {};
const ORIG = JSON.stringify(T);
out.rowsAt = {};
for(const [mode, off, on] of [['sf', 'off_sf', 'on_sf'], ['ff', 'off_ff', 'on_ff']]){
  ui.mode = mode; ui.min1d = ''; GAP_HIDE = Infinity;
  ui.hideSpread3d = false; out.rowsAt[off] = filtered().map(r => r.key);
  ui.hideSpread3d = true; out.rowsAt[on] = filtered().map(r => r.key);
}
// сама таблица не тронута (клиентский фильтр, как «курсовой > 7 %»)
out.same = JSON.stringify(T) === ORIG;
// дефолт — выключен: без явного ui.hideSpread3d страница ничего не должна скрывать по этому правилу
ui.hideSpread3d = undefined; ui.mode = 'sf'; out.defaultShowsAll = filtered().map(r => r.key).length === 3;
out.pageErr = pageErr;
print(JSON.stringify(out));
"""


def _run(tmp_path, table):
    """Своя копия (не test_dashboard_orient._run!): та функция замкнута на CHECKS своего модуля — импортировать
    её сюда означало бы прогнать чужой скрипт проверки, а не наш."""
    src = tmp_path / "page.js"
    src.write_text(STUB + dashboard.page_script(table) + CHECKS)
    res = subprocess.run([JSC, str(src)], capture_output=True, text=True, timeout=30)
    assert res.returncode == 0, res.stderr + res.stdout
    return json.loads(res.stdout.strip().splitlines()[-1])


@pytest.mark.skipif(not os.path.exists(JSC), reason="JavaScriptCore есть только на Маке")
def test_spread_book_computed_and_toggle_hides_only_high_spread_rows(tmp_path):
    tbl = _table()
    sf = {r["key"]: r for r in tbl["sf_rows"]}
    ff = {r["key"]: r for r in tbl["ff_rows"]}
    # backend: spread_book посчитан и совпадает с ручным расчётом (см. docstring файла)
    assert abs(sf["sp:LOW"]["spread_book"] - 0.0003) < 1e-9
    assert abs(sf["sp:HIGH"]["spread_book"] - 0.22) < 1e-9
    assert sf["sp:UNK"]["spread_book"] is None
    assert abs(ff["ff:LOW"]["spread_book"] - 0.0004) < 1e-9
    assert abs(ff["ff:HIGH"]["spread_book"] - 0.22) < 1e-9
    assert ff["ff:UNK"]["spread_book"] is None

    out = _run(tmp_path, tbl)
    assert out["pageErr"] == "" and out["same"] and out["defaultShowsAll"]
    for off_key, on_key, prefix in [("off_sf", "on_sf", "sp"), ("off_ff", "on_ff", "ff")]:
        off, on = set(out["rowsAt"][off_key]), set(out["rowsAt"][on_key])
        assert off == {f"{prefix}:LOW", f"{prefix}:HIGH", f"{prefix}:UNK"}       # тоггл выключен — ничего не скрыто
        assert on == {f"{prefix}:LOW", f"{prefix}:UNK"}                          # тоггл включён — HIGH скрыта, UNK осталась
