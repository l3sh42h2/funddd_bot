"""Правки владельца 13.09 (вечер) — скрипт страницы в настоящем JS-движке (JavaScriptCore):
1) «ставь с названием монеты рейтинг надежности, A, B, C (когда наводишь будет видно)» — буква у названия монеты =
   рейтинг ведущей строки группы, расшифровка в подсказке; у раскрытых комбинаций — своя буква в начале «Биржи»; у «?»
   и «≠» буквы нет;
2) «не показывай в таблице токены с курсовым спредом более 7%» — по модулю и по числу на экране, в обоих режимах;
   table.json (T) не меняется."""
import json, os, re, subprocess
import pytest
from funding_bot import calc, config, dashboard
from test_client_collector import JSC
from test_dashboard_orient import STUB

NOW = 1_789_000_000_000
SCALE = "A — биржа сама указала рынок или контракт · B — через первоисточник · C — догадка по тикеру или имени"


def _sf(base, spot_ex, rate, gap, ident="same", ev="index_market", rel=None):
    """spot/futures: спот 100, перп 100 × (1 + gap) по марку; gap None — книги спота нет, курсового нет."""
    item = dict(key=f"{spot_ex}:{base}USDT|aster:{base}USDT", base=base, perp_ex="aster", perp=f"{base}USDT",
                spot_ex=spot_ex, spot=f"{base}USDT", ident=ident, ident_ev=ev, mismatch=ident == "other",
                ident_why=None if ident == "same" else "причина")
    if rel:
        item.update(rel=rel[0], rel_ev=rel[1], rel_d=rel[2] if len(rel) > 2 else None)
    return calc.build_sf_row(item, None, {"rate": rate, "interval_h": 8, "mark": 100.0 * (1 + (gap or 0.0))}, None,
                             None if gap is None else {"bid": 100.0, "ask": 100.0}, {24: (0.01, 3)}, NOW)


def _ff(base, va, vb, rate_a, rate_b, px_a, rel):
    """futures/futures: цена B — 100, A — px_a; в table.json пара A|B и курсовой A / B − 1, страница разворачивает."""
    item = dict(key=f"{va}:{base}|{vb}:{base}", base=base, va=va, vb=vb, sa=base, sb=base, ident="same",
                ident_ev="shared_leg", mismatch=False, ident_why=None, rel=rel[0], rel_ev=rel[1], rel_d=None)
    prem = lambda r, px: {"rate": r, "interval_h": 8, "mark": px}
    return calc.build_ff_row(item, None, None, prem(rate_a, px_a), prem(rate_b, 100.0), None, None,
                             {24: (0.006, 3)}, {24: (0.0, 3)}, NOW)


def _table():
    sf = [
        # AAA: ведущая по «Текущему» — A; C (пул по тикеру) и «?» — в раскрытой группе
        _sf("AAA", "binance_spot", 0.003, 0.02, rel=("A", "idx")),
        _sf("AAA", "gate_spot", 0.002, -0.01, ev="contract", rel=("C", "pool", "pancakeswap AAA-USDT")),
        _sf("AAA", "kucoin_spot", 0.001, 0.0, ident="unknown", ev="absent"),
        # BBB — правило 7 %: +7.01 % скрыта, +7.004 % (на экране «+7.00%») видна, −8 % скрыта, без курсового — видна
        _sf("BBB", "binance_spot", 0.004, 0.0701, rel=("A", "idx")),
        _sf("BBB", "gate_spot", 0.003, 0.07004, ev="venue_code", rel=("B", "code")),
        _sf("BBB", "kucoin_spot", 0.0035, -0.08, ev="hl_oracle_market", rel=("C", "oracle")),
        _sf("BBB", "bitget_spot", 0.001, None, ev="native_chain", rel=("B", "native")),
        # «не тот актив»: по цене ×1.5 скрыта правилом, вторая видна
        _sf("MIS", "binance_spot", 0.002, 0.5, ident="other", ev="contract_conflict"),
        _sf("MIS", "gate_spot", 0.001, 0.01, ident="other", ev="contract_conflict"),
        # токен другого перпа монеты (dex_link) и «не проверено»
        _sf("LNK", "binance_spot", 0.001, 0.0, ev="dex_link:name", rel=("C", "name", "Linkcoin")),
        _sf("UNK", "binance_spot", 0.0005, 0.0, ident="unknown", ev="absent"),
    ]
    ff = [
        # шорт B (ставка B выше): страница оставляет A|B, курсовой — B / A − 1: +7.4 % в table.json → «-6.89%» — видна
        _ff("FFA", "aster", "binance", 0.0001, 0.0008, 107.4, ("A", "mkt")),
        # шорт B: −7.2 % в table.json → на экране «+7.76%» — скрыта
        _ff("FFA", "aster", "hyperliquid", 0.0001, 0.0009, 92.8, ("B", "code")),
        # шорт A: разворот ног, курсовой как в table.json: +7.4 % — скрыта
        _ff("FFA", "binance", "hyperliquid", 0.00085, 0.0001, 107.4, ("C", "oracle")),
        _ff("FFB", "aster", "binance", 0.0002, 0.0001, 100.0, ("B", "code")),
    ]
    return dict(ts=NOW // 1000, tick_ts=NOW // 1000, ff_rows=ff, sf_rows=sf, health={}, notes={},
                windows=[str(w) for w in config.WINDOWS_H], venues=["aster", "binance", "hyperliquid"],
                spot_venues=["binance_spot", "gate_spot", "kucoin_spot", "bitget_spot"], n_mismatch_sf=2, n_mismatch_ff=0)


CHECKS = """
var out = {};
const ORIG = JSON.stringify(T);
out.gapHide = GAP_HIDE;
ui.min1d = ''; ui.mode = 'sf'; ui.sf = {sort:'spread', dir:-1}; ui.open.sf = []; render();
out.sf = __els['#rows'].innerHTML; out.shown = __els['#shown'].textContent; out.misN = __els['#misN'].textContent;
out.keys = filtered().map(r => r.key);
ui.open.sf = ['AAA', 'BBB']; render(); out.sfOpen = __els['#rows'].innerHTML;
ui.open.sf = []; ui.sf = {sort:'gap', dir:1}; render(); out.gapAsc = __els['#rows'].innerHTML;
ui.sf = {sort:'gap', dir:-1}; out.gapDesc = filtered().map(r => r.mismatch ? 'M' : r.gap);
ui.sf = {sort:'spread', dir:-1};
GAP_HIDE = Infinity; render(); out.allSf = __els['#rows'].innerHTML; out.allShown = __els['#shown'].textContent;
out.allMisN = __els['#misN'].textContent; GAP_HIDE = out.gapHide;
ui.mode = 'ff'; ui.ff = {sort:'spread', dir:-1}; ui.open.ff = ['FFA']; render();
out.ff = __els['#rows'].innerHTML; out.ffShown = __els['#shown'].textContent;
out.ffView = filtered().map(r => [r.key, spct(r.gap, 2)]);
GAP_HIDE = Infinity; out.ffAll = filtered().map(r => [r.key, spct(r.gap, 2)]); render(); out.ffAllHtml = __els['#rows'].innerHTML;
GAP_HIDE = out.gapHide;
ui.ff = {sort:'gap', dir:-1}; out.ffGapDesc = filtered().map(r => r.gap);
out.same = JSON.stringify(T) === ORIG;             // сама таблица не тронута: её же отдаёт следующий опрос
out.pageErr = pageErr;
print(JSON.stringify(out));
"""


def _run(tmp_path, table):
    src = tmp_path / "page.js"
    src.write_text(STUB + dashboard.page_script(table) + CHECKS)
    res = subprocess.run([JSC, str(src)], capture_output=True, text=True, timeout=30)
    assert res.returncode == 0, res.stderr + res.stdout
    return json.loads(res.stdout.strip().splitlines()[-1])


def _coin(html, base):
    """Ячейка монеты: значки рейтинга [(классы, подсказка, буква)] и текст ячейки без тегов."""
    m = re.search(r'<td class="l coin"[^>]*><b>' + base + r'</b>(.*?)</td>', html, re.S)
    assert m, base
    return re.findall(r'<span class="(rel[^"]*)" title="([^"]*)">([ABC])</span>', m.group(1)), re.sub(r"<[^>]+>", "", m.group(1))


def _venues(html, base):
    """Ячейки «Биржа» строк монеты (ведущая и раскрытые) — как есть, с тегами."""
    trs = html.split("<tr")[1:]
    i = next(k for k, tr in enumerate(trs) if f"<b>{base}</b>" in tr)
    group = [trs[i]]
    for tr in trs[i + 1:]:
        if 'class="l coin"' in tr:                                  # следующая монета
            break
        group.append(tr)
    return [re.search(r'<td class="l">(.*?)</td>', tr, re.S).group(1) for tr in group]


@pytest.mark.skipif(not os.path.exists(JSC), reason="JavaScriptCore есть только на Маке")
def test_rating_letter_with_hint_and_gap_rule_in_both_modes(tmp_path):
    tbl = _table()
    assert all("rel" not in r for r in tbl["sf_rows"] if r["ident"] != "same")       # у «?» и «≠» ключей нет
    out = _run(tmp_path, tbl)
    assert out["pageErr"] == "" and out["same"] and out["gapHide"] == config.DASH_GAP_HIDE_PCT == 7.0

    # буква у названия монеты = рейтинг ведущей строки; подсказка — буква, причина, шкала; C — цветом ⊘ (класс c)
    marks, text = _coin(out["sf"], "AAA")
    assert marks == [("rel", "A — рынок этого спота — в составе индекса перпа\n" + SCALE, "A")] and "3 спреда ⌄" in text
    marks, _t = _coin(out["sf"], "LNK")
    assert marks == [("rel c", "C — перпы цепочки совпали только по имени монеты: Linkcoin\nтокен — из индекса другого "
                      "перпа этой монеты; буква — по слабейшему звену цепочки\n" + SCALE, "C")]
    assert _coin(out["sf"], "UNK")[0] == [] and _coin(out["sf"], "MIS")[0] == []      # «?» и «≠» — без буквы
    # свёрнутая группа — «Биржа» без места под букву, как было
    assert _venues(out["sf"], "AAA")[0].startswith("<a ")
    # раскрытая группа: у каждой подстроки своя буква в начале «Биржи», у «?» — пустое место той же ширины; у ведущей —
    # тоже пустое место (её буква — у монеты), иначе её биржа на 16 px левее подстрок (ревью 13.09, F1)
    v = _venues(out["sfOpen"], "AAA")
    assert len(v) == 3 and v[0].startswith('<span class="rel sub"></span><a ')
    assert v[1].startswith('<span class="rel sub c" title="C — токен найден поиском пула DEX по тикеру: pancakeswap '
                           'AAA-USDT\n' + SCALE + '">C</span><a ')
    assert v[2].startswith('<span class="rel sub"></span><a ') and ">?</span>" in v[2]
    # сортировка сменила ведущую — сменилась и буква у монеты
    marks, _t = _coin(out["gapAsc"], "AAA")
    assert [(c, l) for c, _h, l in marks] == [("rel c", "C")]

    # правило 7 %: +7.01 % и −8 % скрыты, «+7.00%» и строка без курсового видны; скрытая ведущая уступила место
    assert "binance_spot:BBBUSDT|aster:BBBUSDT" not in out["keys"] and "kucoin_spot:BBBUSDT|aster:BBBUSDT" not in out["keys"]
    assert {"gate_spot:BBBUSDT|aster:BBBUSDT", "bitget_spot:BBBUSDT|aster:BBBUSDT"} <= set(out["keys"])
    assert "binance_spot:MISUSDT|aster:MISUSDT" not in out["keys"] and "gate_spot:MISUSDT|aster:MISUSDT" in out["keys"]
    marks, text = _coin(out["sf"], "BBB")
    assert [(c, l) for c, _h, l in marks] == [("rel", "B")] and "2 спреда ⌄" in text
    assert marks[0][1].startswith("B — состава индекса нет: монета с тем же кодом на споте той же биржи\n")
    assert out["shown"] == "5 монет · 8 из 11"                                       # M — всё, что в таблице
    assert out["misN"] == " (1)"                                                     # «≠» — только видимые
    # без правила — всё как раньше: ведущая BBB — A, 4 спреда, «≠» два
    marks, text = _coin(out["allSf"], "BBB")
    assert [(c, l) for c, _h, l in marks] == [("rel", "A")] and "4 спреда ⌄" in text
    assert out["allShown"] == "5 монет · 11 из 11" and out["allMisN"] == " (2)"
    # сортировка со знаком не сломана: плюс сверху, пустые, потом «≠»
    g = out["gapDesc"]
    assert g[-1] == "M" and g[-2] is None and g[:-2] == sorted(g[:-2], reverse=True)
    assert max(g[:-2]) < 0.0701 and min(g[:-2]) > -0.07

    # futures/futures — по развёрнутому числу: +7.4 % A/B, показанный «-6.89%», виден; «+7.76%» и «+7.40%» — скрыты
    assert out["ffAll"] == [["aster:FFA|hyperliquid:FFA", "+7.76%"], ["binance:FFA|hyperliquid:FFA", "+7.40%"],
                            ["aster:FFA|binance:FFA", "-6.89%"], ["aster:FFB|binance:FFB", "0.00%"]]
    assert out["ffView"] == [["aster:FFA|binance:FFA", "-6.89%"], ["aster:FFB|binance:FFB", "0.00%"]]
    assert out["ffShown"] == "2 монеты · 2 из 4"
    marks, text = _coin(out["ff"], "FFA")
    assert marks == [("rel", "A — оба индекса берут один и тот же рынок\n" + SCALE, "A")] and "спред" not in text
    marks, text = _coin(out["ffAllHtml"], "FFA")                                     # без правила ведущая — B, 3 спреда
    assert [(c, l) for c, _h, l in marks] == [("rel", "B")] and "3 спреда ⌃" in text
    v = _venues(out["ffAllHtml"], "FFA")
    assert [re.findall(r'class="rel sub( c)?"[^>]*>([ABC])<', x) for x in v[1:]] == [[(" c", "C")], [("", "A")]]
    assert v[0].startswith('<span class="rel sub"></span><a ')                      # ведущая раскрытой — место под букву
    assert _venues(out["ffAllHtml"], "FFB")[0].startswith("<a ")                     # одна комбинация — без места
    assert out["ffGapDesc"] == sorted(out["ffGapDesc"], reverse=True)


def test_rating_css_and_threshold_in_page():
    html = dashboard.render(_table())
    assert ".rel.c{color:var(--warn);border-color:var(--warn)}" in html
    # F2 (ревью 13.09): буква — значок в рамке, а не голая буква того же цвета, что подпись класса («RGTI в акция»);
    # ширина фиксирована (у всей страницы border-box), пустое место подстроки — той же ширины, без рамки
    rel = re.search(r"\.rel\{([^}]*)\}", html).group(1)
    assert "display:inline-block" in rel and "width:15px" in rel and "border:1px solid" in rel and "min-width" not in rel
    assert "*{box-sizing:border-box}" in html and ".rel:empty{border-color:transparent" in html
    assert "let GAP_HIDE = 7.0;" in dashboard.page_script(_table())
    assert 'id="relN"' not in html and "рейтинг:" not in html                       # без новых постоянных чипов


TITLES = """
const R = (rel, ev, extra) => Object.assign({ident:'same', mismatch:false, rel:rel, rel_ev:ev, rel_d:null}, extra || {});
const DX = {chain:'bsc', bridged:true};
print(JSON.stringify({
  cls: relTitle(R('B', 'class', {ident_ev:'class_ticker'})),
  cexCode: relTitle(R('B', 'code', {ident_ev:'venue_code'})),
  cexNative: relTitle(R('B', 'native', {ident_ev:'native_chain'})),
  dexCode: relTitle(R('B', 'code', {ident_ev:'contract', dex:DX})),
  dexAlpha: relTitle(R('B', 'alpha', {ident_ev:'contract', dex:DX})),
  lnkNative: relTitle(R('B', 'native', {ident_ev:'dex_link:name', dex:DX})),
  lnkMkt: relTitle(R('A', 'mkt', {ident_ev:'dex_link:shared_leg', dex:DX})),
  lnkCode: relTitle(R('B', 'code', {ident_ev:'dex_link:shared_leg', dex:DX})),
  lnkPool: relTitle(R('C', 'pool', {ident_ev:'dex_link:contract', dex:DX, rel_d:'pancakeswap X-USDT'})),
  lnkLeg: relTitle(R('A', 'leg', {ident_ev:'dex_link:contract', dex:DX})),
  lnkWhy: Object.fromEntries(['mkt','cex','vendor','contract','ref','pool2','native','code','alpha','leg','class','name',
                              'bridged','pool','oracle'].map(k => [k, relTitle(R('B', k, {ident_ev:'dex_link:x'})).split('\\n')[0]])),
}));
"""


@pytest.mark.skipif(not os.path.exists(JSC), reason="JavaScriptCore есть только на Маке")
def test_rating_hint_texts(tmp_path):
    """Ревью 13.09 — текст подсказки (буква, rel_ev, ident_ev и table.json не меняются):
    P3-class — у class равна база после приведения, а не символы бирж (BZ | BRENTOIL, COPPER | XCU): не «тот же тикер»;
    P3-dexlink / F4 — у DEX-токена другого перпа (dex_link:…) фраза про цепочку перпов, а не «родная монета сети» у
    мостового токена и не «оба индекса» / «спот той же биржи» у спота OKX DEX; своя DEX-строка с code — тоже не «той же»."""
    src = tmp_path / "titles.js"
    src.write_text(STUB + dashboard.page_script(_table()) + TITLES)
    res = subprocess.run([JSC, str(src)], capture_output=True, text=True, timeout=30)
    assert res.returncode == 0, res.stderr + res.stdout
    t = json.loads(res.stdout.strip().splitlines()[-1])
    CH = "\nтокен — из индекса другого перпа этой монеты; буква — по слабейшему звену цепочки\n"
    assert t["cls"] == "B — тот же актив в классе, который указали биржи (тикер или его известный синоним)\n" + SCALE
    assert "тот же тикер" not in t["cls"]
    # CEX-строки — прежние фразы
    assert t["cexCode"] == "B — состава индекса нет: монета с тем же кодом на споте той же биржи\n" + SCALE
    assert t["cexNative"] == "B — родная монета одной и той же сети\n" + SCALE
    # своя DEX-строка: код — у спота биржи ПЕРПА (спот строки — OKX DEX); alpha — прежняя фраза
    assert t["dexCode"] == "B — контракт токена — у монеты с тем же кодом на споте биржи перпа\n" + SCALE
    assert t["dexAlpha"] == "B — токен Binance Alpha из индекса перпа, найден в списке Alpha по тикеру\n" + SCALE
    # dex_link: FIL на BSC (мост) — родная сеть связывает перпы, а не токен
    assert t["lnkNative"] == "B — перпы цепочки — родная монета одной сети (про сам токен это не говорит)" + CH + SCALE
    assert t["lnkMkt"] == "A — оба перпа цепочки: индексы берут один и тот же рынок" + CH + SCALE
    assert t["lnkCode"] == "B — монета с тем же кодом на споте биржи одного из перпов цепочки" + CH + SCALE
    assert t["lnkPool"] == "C — токен найден поиском пула DEX по тикеру: pancakeswap X-USDT" + CH + SCALE
    assert t["lnkLeg"] == "A — контракт токена совпал с монетой рынка из индекса того перпа" + CH + SCALE
    # ни одна фраза цепочки не говорит «спот той же биржи», «родная монета одной и той же сети» или безадресное «оба индекса»
    for k, s in t["lnkWhy"].items():
        assert "причина не записана" not in s and "той же биржи" not in s and "одной и той же сети" not in s, k
        assert not s.startswith("B — оба индекса"), k
