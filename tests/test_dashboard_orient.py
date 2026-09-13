"""Правки владельца 13.09 на главной странице — скрипт страницы в настоящем JS-движке (JavaScriptCore):
1) «0.0338 фандосы округляем до 0.xxx знаков тут (текущий 1ч)… во всех остальных местах X.XX» — ставки в час 3 знака,
   окна, курсовой, комиссия — 2;
2) «во вкладке futures futures первой биржей пиши где лонг, второй где шорт» — строка разворачивается по знаку текущего
   спреда, все числа строки — «шорт − лонг»; table.json (A|B, числа A − B) не меняется."""
import json, os, re, subprocess
import pytest
from funding_bot import calc, config, dashboard
from test_client_collector import JSC

NOW = 1_789_000_000_000


def _ff(base, va, vb, rate_a, iv_a, rate_b, iv_b, px_a, px_b, ws_a, ws_b, mismatch=False):
    item = dict(key=f"{va}:{base}|{vb}:{base}", base=base, va=va, vb=vb, sa=base, sb=base, mismatch=mismatch,
                ident="different" if mismatch else "same", ident_why="индекс другой" if mismatch else None)
    prem = lambda r, iv, px: {"rate": r, "interval_h": iv, "mark": px}
    book = lambda px: {"bid": px * 0.9999, "ask": px * 1.0001}
    return calc.build_ff_row(item, {"url": f"https://{va}/{base}"}, {"url": f"https://{vb}/{base}"},
                             prem(rate_a, iv_a, px_a), prem(rate_b, iv_b, px_b), book(px_a), book(px_b),
                             ws_a, ws_b, NOW)


def _table():
    ff = [
        # шорт A: aster платит 0.0338 %/ч, binance 0 → «binance ▲ | aster ▼», текущий +0.034 %, курсовой = aster / binance − 1
        _ff("ABC", "aster", "binance", 0.002704, 8, 0.0, 8, 10.1, 10.0,
            {24: (0.006, 3), 72: (0.001, 9)}, {24: (0.0005, 3), 72: (0.004, 9)}),
        # шорт B: hyperliquid дороже в час → «aster ▲ | hyperliquid ▼» (порядок A|B), текущий = час(hl) − час(aster)
        _ff("XYZ", "aster", "hyperliquid", 0.00008, 8, 0.00005, 1, 20.0, 20.2,
            {24: (0.0002, 3), 72: (0.0009, 9)}, {24: (0.0082, 24), 72: (0.0001, 72)}),
        # вторая и третья комбинации ABC (у второй ноги разные интервалы — «Период (a|b)» разворачивается вместе с ногами)
        _ff("ABC", "aster", "hyperliquid", 0.002704, 8, 0.0001, 1, 10.1, 10.05,
            {24: (0.006, 3)}, {24: (0.0024, 24)}),
        _ff("ABC", "binance", "hyperliquid", 0.0, 8, 0.0002, 1, 10.0, 10.05,
            {24: (0.0005, 3)}, {24: (0.0048, 24)}),
        # спред 0 и нет ставки — направления нет: A|B без стрелок
        _ff("QQQ", "aster", "binance", 0.0008, 8, 0.0008, 8, 5.0, 5.0, {24: (0.003, 3)}, {24: (0.009, 3)}),
        _ff("NNN", "aster", "binance", 0.0008, 8, None, 8, 5.0, 5.0, {24: (0.003, 3)}, {24: (0.012, 3)}),
        # «не тот актив»: сделки нет — A|B, «—»
        _ff("MMM", "aster", "binance", 0.004, 8, 0.0, 8, 3.0, 1.0, {24: (0.02, 3)}, {24: (0.0, 3)}, mismatch=True),
    ]
    ff[0]["windows"]["24"]["incomplete"] = True                          # «!» у окна переезжает вместе с числом
    sf = [calc.build_sf_row(dict(key="binance_spot:ABCUSDT|aster:ABCUSDT", base="ABC", perp_ex="aster", perp="ABCUSDT",
                                 spot_ex="binance_spot", spot="ABCUSDT"), None, {"rate": 0.002704, "interval_h": 8, "mark": 10.1},
                             {"bid": 10.09, "ask": 10.11}, {"bid": 9.99, "ask": 10.01}, {24: (0.0061, 3)}, NOW)]
    return dict(ts=NOW // 1000, tick_ts=NOW // 1000, ff_rows=ff, sf_rows=sf, health={}, notes={},
                windows=[str(w) for w in config.WINDOWS_H], venues=["aster", "binance", "hyperliquid"],
                spot_venues=["binance_spot"])


STUB = """
var __els = {};
function __el(){ return {innerHTML:'', textContent:'', hidden:false, value:'', checked:false, dataset:{},
                         classList:{toggle(){}, add(){}, remove(){}}}; }
var document = {hidden:false, querySelector(s){ if(!__els[s]) __els[s] = __el(); return __els[s]; }, querySelectorAll(s){ return []; },
                addEventListener(){}};
var localStorage = {getItem(){ return null; }, setItem(){}};
function setInterval(){}
function setTimeout(){ return 0; }
function clearTimeout(){}
var console = {error(){}, log(){}};
"""

CHECKS = """
var out = {};
GAP_HIDE = Infinity;          // проверки порядка и «скрыть ≠» — без правила 7 % (MMM ×3); его проверяет test_dashboard_rating
const ORIG = JSON.stringify(T.ff_rows);
// строки таблицы: ячейки без тегов; ссылки ячейки «Биржа» — по порядку
const cells = h => h.split(/<tr/).slice(1).map(tr => tr.split(/<td/).slice(1)
  .map(c => c.replace(/^[^>]*>/, '').replace(/<[^>]+>/g, '').replace(/\\s+/g, ' ').trim()));
const hrefs = h => h.split(/<tr/).slice(1).map(tr => (tr.match(/href="[^"]+"/g) || []).map(x => x.slice(6, -1)));
ui.min1d = ''; ui.mode = 'ff'; ui.ff = {sort:'spread', dir:-1}; ui.open.ff = ['ABC']; render();
out.ffHtml = __els['#rows'].innerHTML; out.ff = cells(out.ffHtml); out.ffHref = hrefs(out.ffHtml);
out.view = filtered().map(r => [r.base, r.va, r.vb, r.lng, r.spread, r.gap, (r.windows['24'] || {}).spread,
                                (r.windows['72'] || {}).spread, r.rate_h_a, r.rate_h_b, r.iv_a, r.iv_b, r.urls[0]]);
// сортировка со знаком — по тому, что на экране
const K = (key, dir) => { ui.ff = {sort:key, dir:dir}; return filtered().map(r => r.key); };
out.sort = {sd: K('spread', -1), sa: K('spread', 1), w24a: K('w24', 1), w72d: K('w72', -1)};
ui.ff = {sort:'w24', dir:-1}; out.w24 = filtered().map(r => r.mismatch ? 'M' : r.windows['24'].spread);
ui.ff = {sort:'spread', dir:-1};
// фильтры: «1d >» по модулю, галочки бирж, «скрыть не тот актив», поиск, «N спредов»
ui.min1d = '0.5'; out.d1 = filtered().map(r => r.key); ui.min1d = '';
setPick('pp', '*', false); setPick('pp', 'aster', true); setPick('pp', 'binance', true); out.pick = filtered().map(r => r.key);
setPick('pp', '*', true);
ui.hideMis = true; out.hide = filtered().map(r => r.key); ui.hideMis = false;
ui.q = 'xy'; out.q = filtered().map(r => r.key); ui.q = '';
ui.open.ff = []; render(); out.closed = cells(__els['#rows'].innerHTML); out.closedHtml = __els['#rows'].innerHTML;
ui.mode = 'sf'; ui.sf = {sort:'spread', dir:-1}; render(); out.sf = cells(__els['#rows'].innerHTML);
out.same = JSON.stringify(T.ff_rows) === ORIG;             // сама таблица не тронута: её же отдаёт следующий опрос
out.pageErr = pageErr;
print(JSON.stringify(out));
"""


def _run(tmp_path, table):
    src = tmp_path / "page.js"
    src.write_text(STUB + dashboard.page_script(table) + CHECKS)
    res = subprocess.run([JSC, str(src)], capture_output=True, text=True, timeout=30)
    assert res.returncode == 0, res.stderr + res.stdout
    return json.loads(res.stdout.strip().splitlines()[-1])


@pytest.mark.skipif(not os.path.exists(JSC), reason="JavaScriptCore есть только на Маке")
def test_ff_long_first_short_second_and_decimals(tmp_path):
    tbl = _table()
    ff = {r["key"]: r for r in tbl["ff_rows"]}
    # table.json как был: A|B в порядке config.PERP_VENUES, числа A − B (их читают audit, tg, trade)
    abc = ff["aster:ABC|binance:ABC"]
    assert abc["side"] == "short_a" and abc["spread"] > 0 and abc["va"] == "aster" and abc["windows"]["72"]["spread"] < 0
    assert ff["aster:XYZ|hyperliquid:XYZ"]["side"] == "short_b" and ff["aster:QQQ|binance:QQQ"]["side"] is None
    out = _run(tmp_path, tbl)
    assert out["pageErr"] == "" and out["same"]

    # копии строк в сторону сделки:
    # [монета, первая, вторая, первая — лонг?, текущий, курсовой, 1 день, 3 дня, ставка первой, второй, интервалы, ссылка первой]
    v = {(r[0], r[1], r[2]): r for r in out["view"]}
    a = v[("ABC", "binance", "aster")]                        # шорт A → aster второй
    assert a[3] is True and abs(a[4] - 0.002704 / 8) < 1e-15 and abs(a[5] - abc["gap"]) < 1e-15   # aster / binance − 1
    assert abs(a[6] - (0.006 - 0.0005)) < 1e-15 and abs(a[7] - (0.001 - 0.004)) < 1e-15      # окно шорт − лонг, бывает минус
    assert a[8] == 0.0 and abs(a[9] - 0.002704 / 8) < 1e-15 and a[12] == "https://binance/ABC"
    x = v[("XYZ", "aster", "hyperliquid")]                    # шорт B → порядок A|B как лежит, знаки — наоборот
    assert x[3] is True and abs(x[4] - (0.00005 - 0.00001)) < 1e-15
    assert abs(x[6] - (0.0082 - 0.0002)) < 1e-15 and abs(x[5] - (20.2 / 20.0 - 1)) < 1e-12        # цена(шорт) / цена(лонг) − 1
    assert (x[10], x[11]) == (8, 1) and v[("ABC", "hyperliquid", "aster")][10:12] == [1, 8]
    q = v[("QQQ", "aster", "binance")]; n = v[("NNN", "aster", "binance")]; m = v[("MMM", "aster", "binance")]
    assert q[3] is False and q[4] == 0 and abs(q[6] - (0.009 - 0.003)) < 1e-15                       # второй − первый
    assert n[3] is False and n[4] is None and m[3] is False and abs(m[6] - (0.0 - 0.02)) < 1e-15
    assert abs(m[5] - (1.0 / 3.0 - 1)) < 1e-12                                                    # и курсовой: второй / первый − 1
    assert all(r[4] >= 0 for r in out["view"] if r[3])                                   # у строк со стрелками текущий ≥ 0

    # экран: первой — лонг ▲, второй — шорт ▼; ставки «лонг | шорт»; 3 знака у ставок в час, 2 — у остального
    # ячейки: [монета?] Биржа | Фандинг 1ч | Текущий 1ч | Курсовой | Период | Комиссия | 4 ч | 1 день | 3 дня | 1 неделя | 1 месяц
    r0, r1, r2 = out["ff"][:3]                                # раскрытая ABC: лучшая по текущему со знаком — первой
    assert r0[0] == "ABC3 спреда ⌃"
    assert r0[1:] == ["binance ▲ | aster ▼", "0.000% | +0.034%", "+0.034%", "+1.00%10.0000 | 10.1000", "8", "0.18%",
                      "—", "+0.55%!", "-0.30%", "—", "—"]                               # «!» — у своего окна
    assert r1[:5] == ["hyperliquid ▲ | aster ▼", "+0.010% | +0.034%", "+0.024%", "+0.50%10.0500 | 10.1000", "8 (1|8)"]
    assert r2[:3] == ["binance ▲ | hyperliquid ▼", "0.000% | +0.020%", "+0.020%"]
    assert out["ffHref"][:3] == [["https://binance/ABC", "https://aster/ABC"], ["https://hyperliquid/ABC", "https://aster/ABC"],
                                 ["https://binance/ABC", "https://hyperliquid/ABC"]]
    assert out["ff"][3] == ["XYZ", "aster ▲ | hyperliquid ▼", "+0.001% | +0.005%", "+0.004%", "+1.00%20.0000 | 20.2000",
                            "8 (8|1)", "0.17%", "—", "+0.80%", "-0.08%", "—", "—"]
    assert out["ff"][4][:4] == ["QQQ", "aster | binance", "+0.010% | +0.010%", "0.000%"] and out["ff"][4][8] == "+0.60%"
    assert out["ff"][5][:4] == ["NNN", "aster | binance", "+0.010% | —", "—"] and out["ff"][5][8] == "+0.90%"
    assert out["ff"][6][:2] == ["MMM", "aster | binance ≠"] and out["ff"][6][3:5] == ["—", "-66.67%3.0000 | 1.0000"]
    # ни одного процента с 4 знаками; окна — ровно 2
    assert not re.search(r"\d\.\d{4}%", out["ffHtml"])
    for r in out["ff"]:
        for c in r[-5:]:
            assert c == "—" or re.fullmatch(r"[+-]?\d+\.\d\d%!?", c), c

    # сортировка со знаком по показанным числам; пустые и «≠» — в конце при любом направлении
    s = out["sort"]
    assert s["sd"] == ["aster:ABC|binance:ABC", "aster:ABC|hyperliquid:ABC", "binance:ABC|hyperliquid:ABC",
                       "aster:XYZ|hyperliquid:XYZ", "aster:QQQ|binance:QQQ", "aster:NNN|binance:NNN", "aster:MMM|binance:MMM"]
    assert s["sa"] == ["aster:QQQ|binance:QQQ", "aster:XYZ|hyperliquid:XYZ", "binance:ABC|hyperliquid:ABC",
                       "aster:ABC|hyperliquid:ABC", "aster:ABC|binance:ABC", "aster:NNN|binance:NNN", "aster:MMM|binance:MMM"]
    w = out["w24"][:-1]
    assert w == sorted(w, reverse=True) and out["w24"][-1] == "M" and s["w24a"][0] == "aster:ABC|hyperliquid:ABC"
    assert s["w72d"][:2] == ["aster:XYZ|hyperliquid:XYZ", "aster:ABC|binance:ABC"]      # −0.08 % выше −0.30 %
    # фильтры и группы работают на развёрнутых строках
    assert out["d1"] == ["aster:ABC|binance:ABC", "aster:XYZ|hyperliquid:XYZ", "aster:QQQ|binance:QQQ",
                         "aster:NNN|binance:NNN", "aster:MMM|binance:MMM"]                  # |1 день| > 0.5 %
    assert out["pick"] == ["aster:ABC|binance:ABC", "aster:QQQ|binance:QQQ", "aster:NNN|binance:NNN", "aster:MMM|binance:MMM"]
    assert out["hide"] == s["sd"][:-1]
    assert out["q"] == ["aster:XYZ|hyperliquid:XYZ"]
    assert "3 спреда ⌄" in out["closedHtml"] and len(out["closed"]) == 5 and out["closed"][0][1] == "binance ▲ | aster ▼"
    # spot/futures: текущий 3 знака, курсовой, комиссия и окна — 2
    assert out["sf"][0] == ["ABC", "binance ▲ | aster ▼", "+0.034%", "+1.00%10.0000 | 10.1000", "8", "0.28%",
                            "—", "+0.61%", "—", "—", "—"]


@pytest.mark.skipif(not os.path.exists(JSC), reason="JavaScriptCore есть только на Маке")
def test_ff_zero_at_shown_precision_has_no_colour(tmp_path):
    """Ревью 13.09: число, что на точности показа ноль, пишется «0.00%» без знака — и без цвета знака (раньше «0.00%»
    выходил красным или зелёным: на живой таблице 12.09 — 412 окон и 56 ставок). Не ноль на этой точности — цвет как был.
    И счёт «N из M» — по числу строк таблицы."""
    ff = [
        # шорт B: ставки 0.00001 и 0.00003 %/ч → «0.000%», текущий +0.00002 %/ч → «0.000%»; 1 день +0.003 % → «0.00%»,
        # 3 дня −1 % → «-1.00%» красный
        _ff("TNY", "aster", "binance", 0.0000008, 8, 0.0000024, 8, 5.0, 5.0,
            {24: (0.00001, 3), 72: (0.01, 9)}, {24: (0.00004, 3), 72: (0.0, 9)}),
        _ff("BIG", "aster", "binance", 0.0008, 8, 0.0, 8, 5.0, 5.0, {24: (0.006, 3)}, {24: (0.0, 3)}),
    ]
    tbl = dict(ts=NOW // 1000, tick_ts=NOW // 1000, ff_rows=ff, sf_rows=[], health={}, notes={},
               windows=[str(w) for w in config.WINDOWS_H], venues=["aster", "binance"], spot_venues=["binance_spot"])
    src = tmp_path / "page.js"
    src.write_text(STUB + dashboard.page_script(tbl) + """
ui.min1d = ''; ui.mode = 'ff'; ui.ff = {sort:'spread', dir:-1}; render();
print(JSON.stringify({h: __els['#rows'].innerHTML, shown: __els['#shown'].textContent, e: pageErr}));""")
    res = subprocess.run([JSC, str(src)], capture_output=True, text=True, timeout=30)
    assert res.returncode == 0, res.stderr + res.stdout
    out = json.loads(res.stdout.strip().splitlines()[-1])
    assert out["e"] == "" and out["shown"] == "2 монеты · 2 из 2"
    tny = [tr for tr in out["h"].split("<tr")[1:] if ">TNY<" in tr][0]
    assert 'aster</a> <span class="up">▲</span> | <a href="https://binance/TNY"' in tny
    assert tny.count('<span class="">0.000%</span>') == 2                       # обе ставки в час
    assert '<b class="mono ">0.000%</b>' in tny                                  # текущий
    assert '<b class="mono ">0.00%</b>' in tny and '<b class="mono r">-1.00%</b>' in tny   # 1 день — ноль; 3 дня — минус
    assert not re.search(r'class="(mono )?[gr]">[+-]?0\.0+%', out["h"])
    big = [tr for tr in out["h"].split("<tr")[1:] if ">BIG<" in tr][0]
    assert '<b class="mono g">+0.010%</b>' in big and '<b class="mono g">+0.60%</b>' in big


def test_page_decimals_rule_in_source():
    """Формат чисел в исходнике страницы: 4 знаков нет нигде, 3 — только у ставок в час."""
    js = dashboard.JS
    assert not re.search(r"spct\([^)]*, 4\)|toFixed\(4\)", js.replace("x.toFixed(4)", ""))   # px() — цены, не проценты
    assert len(re.findall(r"spct\([^)]*, 3\)", js)) == 4 and "spct(r.rate_h_a, 3)" in js and "spct(r.rate_h_b, 3)" in js
    # цвет знака — на той же точности, что и число (ревью 13.09): у каждого spct(x, d) в ячейке — cls(x, d)
    for x, d in re.findall(r"spct\(((?:r|x)\.(?:spread|rate_h_[ab])), (\d)\)", js):
        assert f"cls({x}, {d})" in js, (x, d)
    assert js.count("spct(r.spread, 3)") == 2 and "spct(x.spread, 2)" in js                    # Текущий обеих таблиц; окна
