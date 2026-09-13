"""Верхняя строка дашборда (владелец 12.09): ⟳ — немедленный запрос /data.json без дублей, таймер свежести по часам
сервера с поправкой по заголовку Date, «Кабинет» справа сверху. Скрипт страницы — в настоящем JS-движке (JavaScriptCore)
с игрушечными DOM, fetch и таймерами: ответы, часы телефона и срабатывание таймеров ведёт сам тест."""
import copy, json, os, subprocess
import pytest
from funding_bot import dashboard
from fakes import make_world
from test_client_collector import JSC, _collector

NOW_MS = 1_789_000_000_400          # часы телефона в тесте: не на границе секунды — Date ответа округлён вниз до секунды

STUB = """
var __now = %(now)d; Date.now = () => __now;
var __els = {}, __ev = {}, __alerts = 0, __fetches = [], __timers = [], __ints = [];
function __cls(){ var s = new Set(); return {toggle(c, on){ if(on === undefined) on = !s.has(c); if(on) s.add(c); else s.delete(c); return on; },
                                               add(c){ s.add(c); }, remove(c){ s.delete(c); }, contains(c){ return s.has(c); }}; }
function __el(){ return {innerHTML:'', textContent:'', hidden:false, value:'', checked:false, disabled:false, title:'', className:'',
                         dataset:{}, scrollTop:0, scrollLeft:0, classList:__cls()}; }
// перерисовка строк в игрушечном DOM сбрасывает прокрутку таблицы (худший случай браузера) — вернуть её обязана страница
// __reset = false — обычный браузер: замена строк прокрутку не трогает; __scrollW — сколько раз страница писала прокрутку
__els['#rows'] = __el(); var __rowsHtml = '', __reset = true, __scrollW = 0;
Object.defineProperty(__els['#rows'], 'innerHTML', {get(){ return __rowsHtml; },
  set(v){ __rowsHtml = v; if(__reset && __els['.tbl']){ __sT = 0; __sL = 0; } }});
var __sT = 0, __sL = 0; __els['.tbl'] = __el();
Object.defineProperty(__els['.tbl'], 'scrollTop', {get(){ return __sT; }, set(v){ __sT = v; __scrollW++; }});
Object.defineProperty(__els['.tbl'], 'scrollLeft', {get(){ return __sL; }, set(v){ __sL = v; __scrollW++; }});
var document = {hidden:false, querySelector(s){ if(!__els[s]) __els[s] = __el(); return __els[s]; }, querySelectorAll(s){ return []; },
                addEventListener(t, f){ (__ev[t] = __ev[t] || []).push(f); }};
var __store = {fb_skew: '120000'};              // прошлый визит: часы телефона на 2 мин отстают от сервера
var localStorage = {getItem(k){ return k in __store ? __store[k] : null; }, setItem(k, v){ __store[k] = String(v); }};
function setTimeout(f, ms){ __timers.push({f:f, ms:ms, live:true}); return __timers.length; }
function clearTimeout(id){ if(id && __timers[id-1]) __timers[id-1].live = false; }
function setInterval(f, ms){ __ints.push({f:f, ms:ms}); }
function alert(){ __alerts++; }
var console = {error(){}, log(){}};
function fetch(url, opt){ return new Promise((res, rej) => __fetches.push({url:url, opt:opt, res:res, rej:rej})); }
function __resp(body, srvMs, status){ status = status || 200;
  return {ok: status < 400, status: status, json(){ return Promise.resolve(body); },
          headers: {get(k){ return k.toLowerCase() === 'date' && srvMs !== null ? new Date(srvMs).toUTCString() : null; }}}; }
// последний живой таймер с таким сроком — сработать и снять
function __fire(ms){ for(var i = __timers.length - 1; i >= 0; i--){ var t = __timers[i];
  if(t.live && t.ms === ms){ t.live = false; t.f(); return true; } } return false; }
const __live = ms => __timers.filter(t => t.live && t.ms === ms).length;
function __tick(){ __ints.filter(x => x.ms === 1000).forEach(x => x.f()); }
"""

CHECKS = """
var out = {}, NEXT = %(next)s, NEXT2 = %(next2)s;
const F = () => __els['#fresh'], B = () => __els['#reload'];
const snap = () => ({text: F().innerHTML.replace(/<[^>]+>/g, ''), cls: F().className, title: F().title, dis: B().disabled,
                     spin: B().classList.contains('spin'), err: B().classList.contains('err'), btitle: B().title});
out.first = snap();                                    // вшитые данные + поправка прошлого визита
out.ticks = __ints.map(x => x.ms);
__now += 25000; __tick(); out.t32 = snap();
__now += 90000; __tick(); out.t122 = snap();
out.fmt = [0, 29, 30, 59, 60, 119, 120, 3599, 3600, 7300].map(s => freshText(s) + ':' + freshCls(s));
out.polls0 = __live(10000);
// фильтры, сортировка, раскрытая монета и прокрутка — до нажатия
ui.mode = 'sf'; ui.sf = {sort:'fee', dir:1}; ui.min1d = ''; ui.q = 'ABC'; ui.open.sf = ['ABC']; render();
$('.tbl').scrollTop = 480; $('.tbl').scrollLeft = 30;
out.rowsBefore = __rowsHtml;
// ⟳: один запрос; второе нажатие, плановый опрос и прямые вызовы его не удваивают
B().onclick(); B().onclick(); __fire(10000); refresh(false); refresh(true);
out.nFetch1 = __fetches.length; out.busy = snap(); out.url = __fetches[0].url; out.cache = __fetches[0].opt.cache;
out.pollsBusy = __live(10000);
// ответ: сервер на 5 мин впереди телефона (Date), таблица записана за 3 с до ответа
__fetches[0].res(__resp(NEXT, __now + 300000, 200)); drainMicrotasks();
out.after = snap(); out.skew = skew; out.stored = __store.fb_skew; out.ts = T.ts;
out.ui = [ui.mode, ui.sf.sort, ui.sf.dir, ui.q, ui.open.sf.join()]; out.rowsAfter = __rowsHtml; out.head = __els['#head'].innerHTML;
out.scroll = [__els['.tbl'].scrollTop, __els['.tbl'].scrollLeft];
// второй ответ дошёл на 0.7 с позже — нижняя граница хуже, поправка остаётся наибольшей
__now += 10000; __fire(10000); out.nFetch2 = __fetches.length;
__fetches[1].res(__resp(NEXT2, __now + 300000 - 700, 200)); drainMicrotasks(); out.skew2 = skew; out.after2 = snap();
// неудачи видны: HTTP-ошибка и сеть — красным с причиной в подсказке, таблица прежняя, alert нет
__now += 1000; B().onclick(); __fetches[2].res(__resp(null, __now + 300000, 502)); drainMicrotasks();
out.e502 = snap(); out.ts502 = T.ts;
B().onclick(); __fetches[3].rej(new TypeError('Load failed')); drainMicrotasks(); out.eNet = snap();
// зависший запрос: через 30 с брошен, кнопка снова жива, опоздавший ответ не подменяет таблицу
B().onclick(); out.nHung = __fetches.length; out.hungBusy = snap(); __fire(30000); out.hung = snap();
B().onclick(); out.nAfterHung = __fetches.length;
__fetches[4].res(__resp(Object.assign({}, NEXT, {ts: 1}), __now + 300000, 200));
__fetches[5].res(__resp(NEXT2, __now + 300000, 200)); drainMicrotasks(); out.lateTs = T.ts; out.recovered = snap();
// таблица с ошибкой: остаётся прежняя, неудача на кнопке, ошибка страницы в строке состояния
B().onclick(); __fetches[6].res(__resp({ts: NEXT2.ts + 10, sf_rows: [{key: 'x', base: 'ABC'}], ff_rows: []}, __now + 300000, 200));
drainMicrotasks(); out.broken = snap(); out.brokenTs = T.ts; out.status = __els['#status'].innerHTML; out.pageErr = pageErr;
// скрытая вкладка плановым опросом не тянет; вернулись на вкладку — сразу свежие данные
document.hidden = true; var nf = __fetches.length; __now += 20000; __fire(10000);
out.hidden = [__fetches.length - nf, __live(10000)];
document.hidden = false; (__ev.visibilitychange || []).forEach(f => f()); out.visible = __fetches.length - nf;
out.alerts = __alerts;
print(JSON.stringify(out));
"""


def _run(tmp_path, checks):
    """Страница на таблице игрушечного коллектора в JSC: вшитая таблица, два следующих ответа сервера (NEXT, NEXT2), проверки."""
    w = make_world(); col, t = _collector(tmp_path, w, now=NOW_MS / 1000)
    tbl = col.once()
    assert any(r["base"] == "ABC" for r in tbl["sf_rows"]) and any(r["base"] != "ABC" for r in tbl["sf_rows"])
    tbl["ts"] = (NOW_MS + 120_000) // 1000 - 7                    # по часам сервера таблице 7 с при открытии страницы
    now1 = NOW_MS + 115_000                                       # часы телефона к моменту ответа на ⟳
    nxt = copy.deepcopy(tbl); nxt["ts"] = (now1 + 300_000) // 1000 - 3
    for r in nxt["sf_rows"]:
        r["spot_label"] = "NEWSPOT"                               # по ней видно, что строки перерисованы новыми данными
    nxt2 = copy.deepcopy(nxt); nxt2["ts"] = nxt["ts"] + 10
    src = tmp_path / "page.js"
    src.write_text(STUB % dict(now=NOW_MS) + dashboard.page_script(tbl)
                   + checks % dict(next=json.dumps(nxt), next2=json.dumps(nxt2)))
    res = subprocess.run([JSC, str(src)], capture_output=True, text=True, timeout=30)
    assert res.returncode == 0, res.stderr + res.stdout
    return dashboard.render(tbl), json.loads(res.stdout.strip().splitlines()[-1]), nxt, nxt2


@pytest.mark.skipif(not os.path.exists(JSC), reason="JavaScriptCore есть только на Маке")
def test_refresh_button_freshness_timer_clock_skew_and_no_double_fetch(tmp_path):
    html, out, nxt, nxt2 = _run(tmp_path, CHECKS)

    # вёрстка: свежесть и ⟳ рядом с переключателем режимов, «Кабинет» — последним, справа сверху
    top = html[html.index('<div class="top">'):html.index('<div class="bar"')]
    assert [top.index(x) for x in ('<h1>', 'id="fresh"', 'id="reload"', 'class="seg"', 'href="/cabinet"')] == \
        sorted(top.index(x) for x in ('<h1>', 'id="fresh"', 'id="reload"', 'class="seg"', 'href="/cabinet"'))
    assert '>Кабинет</a>' in top and 'data-m="sf">spot/futures' in top and 'data-m="ff">futures/futures' in top
    assert 'id="spBtn"' in html and 'id="ppMenu" hidden' in html and 'id="status"' in html      # пикеры и строка состояния на месте
    phone = dashboard.CSS[dashboard.CSS.index("@media (max-width:640px){"):]            # на телефоне «Кабинет» — справа сверху
    assert ".cab{order:1;margin-left:auto}" in phone and ".tools{order:2;flex:1 1 100%" in phone

    # первая загрузка — по вшитым данным и поправке прошлого визита (без неё было бы «0 с»: часы телефона позади)
    assert out["first"]["text"] == "обновлено 7 с назад" and "g" in out["first"]["cls"].split()
    assert "расходятся с сервером на 120 с" in out["first"]["title"] and not out["first"]["dis"] and not out["first"]["err"]
    assert 1000 in out["ticks"]                                                      # тикает раз в секунду
    assert out["t32"]["text"] == "обновлено 32 с назад" and "y" in out["t32"]["cls"].split()
    assert out["t122"]["text"] == "обновлено 2 мин назад" and "r" in out["t122"]["cls"].split()
    assert out["fmt"] == ["0 с:g", "29 с:g", "30 с:y", "59 с:y", "1 мин:y", "1 мин:y", "2 мин:r", "59 мин:r", "1 ч:r", "2 ч:r"]
    assert out["polls0"] == 1

    # без дублей: один запрос, кнопка погашена и крутится, плановый опрос не наложился
    assert out["nFetch1"] == 1 and out["url"] == "/data.json" and out["cache"] == "no-store"
    assert out["busy"]["dis"] and out["busy"]["spin"] and out["pollsBusy"] == 1
    # удачный ответ: новая таблица, фильтры/сортировка/раскрытие/прокрутка те же, поправка часов из Date
    assert out["ts"] == nxt["ts"] and out["ui"] == ["sf", "fee", 1, "ABC", "ABC"]
    assert "NEWSPOT" in out["rowsAfter"] and "NEWSPOT" not in out["rowsBefore"] and "<b>XYZ</b>" not in out["rowsAfter"]
    assert "<b>ABC</b>" in out["rowsAfter"] and "Комиссия ↑" in out["head"] and out["scroll"] == [480, 30]
    assert out["skew"] == 300_000 - 400 and out["stored"] == str(300_000 - 400)      # Date округлён вниз — нижняя граница
    assert out["after"]["text"] == "обновлено 3 с назад" and "g" in out["after"]["cls"].split()
    assert not out["after"]["dis"] and not out["after"]["spin"] and not out["after"]["err"]
    assert out["nFetch2"] == 2 and out["skew2"] == 300_000 - 400                     # поздний ответ поправку не портит
    assert out["after2"]["text"] == "обновлено 3 с назад"                           # +10 с на часах, таблица на 10 с новее
    # неудачи: красным, причина в подсказке, таблица прежняя, alert нет
    assert out["e502"]["err"] and "r" in out["e502"]["cls"].split() and "HTTP 502" in out["e502"]["btitle"]
    assert "HTTP 502" in out["e502"]["title"] and out["e502"]["text"].endswith("назад!") and out["ts502"] == nxt2["ts"]
    assert out["eNet"]["err"] and "Load failed" in out["eNet"]["btitle"] and not out["eNet"]["dis"]
    assert out["nHung"] == 5 and out["hungBusy"]["dis"]
    assert not out["hung"]["dis"] and out["hung"]["err"] and "нет ответа 30 с" in out["hung"]["btitle"]
    assert out["nAfterHung"] == 6 and out["lateTs"] == nxt2["ts"]                  # опоздавший ответ (ts=1) — мимо
    assert not out["recovered"]["err"] and "r" not in out["recovered"]["cls"].split()
    assert out["broken"]["err"] and "таблица не отрисовалась" in out["broken"]["btitle"] and out["brokenTs"] == nxt2["ts"]
    assert "страница: ошибка" in out["status"] and out["pageErr"]
    # скрытая вкладка: опрос переназначен, запроса нет; вернулись — запрос сразу
    assert out["hidden"] == [0, 1] and out["visible"] == 1
    assert out["alerts"] == 0


CHECKS2 = """
var out = {}, NEXT = %(next)s, NEXT2 = %(next2)s;
const B = () => __els['#reload'], st = () => [B().disabled, B().classList.contains('spin'), B().classList.contains('err')];
// плановый опрос идёт: кнопка жива (раньше бледнела раз в 10 с и глотала нажатия); нажатие присоединяется к нему
__fire(10000); out.poll = st(); out.n0 = __fetches.length;
B().onclick(); out.join = st(); out.n1 = __fetches.length;
// ответ, прокрутка как в обычном браузере (замена строк её не трогает) — страница прокрутку не пишет
__reset = false; $('.tbl').scrollTop = 480; __scrollW = 0;
__fetches[0].res(__resp(NEXT, __now + 300000, 200)); drainMicrotasks();
out.done = st(); out.scrollW = __scrollW; out.scroll = $('.tbl').scrollTop; out.skew1 = skew; out.ts1 = T.ts;
// чужой ответ: 200, но не JSON (страница входа в Wi-Fi), часы на час вперёд — поправка прежняя, неудача видна
__now += 10000; __fire(10000);
__fetches[1].res({ok:true, status:200, json(){ return Promise.reject(new SyntaxError('JSON Parse error')); },
                  headers:{get(k){ return k.toLowerCase() === 'date' ? new Date(__now + 3600000).toUTCString() : null; }}});
drainMicrotasks(); out.foreign = [skew, __store.fb_skew, st()[2]];
// JSON, но не таблица — тоже неудача, а не молчаливый «успех» со старой таблицей
__now += 10000; __fire(10000); __fetches[2].res(__resp(null, __now + 3600000, 200)); drainMicrotasks();
out.nullBody = [skew, st()[2], B().title];
__now += 10000; __fire(10000); __fetches[3].res(__resp(NEXT, __now + 300000, 200)); drainMicrotasks(); out.ok = st();
// телефон заснул посреди планового опроса; при разблокировке visibilitychange пришёл раньше просроченных таймеров
__now += 10000; __fire(10000); var n0 = __fetches.length;
document.hidden = true; __now += 300000; document.hidden = false; (__ev.visibilitychange || []).forEach(f => f());
out.resume = [__fetches.length - n0, st()];
// просроченный сброс старого запроса и его опоздавший ответ — мимо: ни красного, ни старой таблицы
var old = __timers.find(t => t.live && t.ms === 30000); old.live = false; old.f();
__fetches[n0 - 1].res(__resp(Object.assign({}, NEXT, {ts: 1}), __now + 300000, 200)); drainMicrotasks();
out.oldKill = [st(), T.ts];
__fetches[n0].res(__resp(NEXT2, __now + 300000, 200)); drainMicrotasks(); out.fresh = [st(), T.ts];
print(JSON.stringify(out));
"""


@pytest.mark.skipif(not os.path.exists(JSC), reason="JavaScriptCore есть только на Маке")
def test_refresh_review_join_foreign_date_scroll_and_resume(tmp_path):
    """Ревью 12.09: ⟳ во время планового опроса, Date чужого ответа, запись прокрутки, разблокировка посреди запроса."""
    html, out, nxt, nxt2 = _run(tmp_path, CHECKS2)
    # [disabled, spin, err]: плановый опрос кнопку не гасит; нажатие — крутится, второго запроса нет; ответ — всё сброшено
    assert out["poll"] == [False, False, False] and out["n0"] == 1
    assert out["join"] == [True, True, False] and out["n1"] == 1
    assert out["done"] == [False, False, False] and out["ts1"] == nxt["ts"]
    assert out["scrollW"] == 0 and out["scroll"] == 480                     # прокрутка не сдвинулась — не пишется
    assert out["skew1"] == 300_000 - 400
    # Date не нашего ответа поправку не двигает и в браузере не запоминается
    assert out["foreign"] == [300_000 - 400, str(300_000 - 400), True]
    assert out["nullBody"][:2] == [300_000 - 400, True] and "не похож на таблицу" in out["nullBody"][2]
    assert out["ok"] == [False, False, False]
    # разблокировка: новый запрос сразу, без красного «нет ответа»; старый сброс и старый ответ — мимо
    assert out["resume"] == [1, [False, False, False]]
    assert out["oldKill"] == [[False, False, False], nxt["ts"]]
    assert out["fresh"] == [[False, False, False], nxt2["ts"]]
