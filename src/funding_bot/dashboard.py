"""Страница дашборда по эталону владельца (10.09): одна цифра в ячейке, без подписей и годовых.

Два списка, переключатель справа сверху (владелец 10.09: «чтоб не мешать всё в один список»):
  spot/futures    — Монета | Биржа (спот ▲ | перп ▼) | Текущий | Курсовой | Период | Комиссия | окна;
                    строка = сделка: спот любой спот-площадки (Binance, Gate, KuCoin, Bitget) + перп любой площадки
  futures/futures — Монета | Биржа (лонг ▲ | шорт ▼) | Фандинг (ставка лонга | ставка шорта) | Текущий | Курсовой |
                    Период | Комиссия | окна;  строка = пара рынков, спота здесь нет. Владелец 13.09: «первой биржей пиши
                    где лонг, второй где шорт» — страница разворачивает пару по знаку текущего спреда, все числа строки —
                    «шорт − лонг» (orientFF в скрипте; table.json остаётся A|B в порядке config.PERP_VENUES)
Знаки после запятой (владелец 13.09): ставки в час («Текущий 1ч», «Фандинг 1ч») — 3, всё остальное — 2.
Комбинации бирж одной монеты сгруппированы (владелец 12.09, целевой вид — скрин чужого скринера): видна лучшая
комбинация по текущей сортировке, под монетой — «N спредов ⌄», клик раскрывает все (раскрытые монеты помнятся).
«Отклонения» нет (владелец 12.09: «откл не нужно»).
«Не тот актив» (≠, строка серая) и «не проверено» (?) — по составу индекса перпа и контрактам монет, не по цене
(владелец 12.09); ⊘ — ввод/вывод монеты на споте закрыт. Токен акции с несколькими акциями — «gate·NFLXX ×10».
Рейтинг надёжности (владелец 13.09: «ставь с названием монеты рейтинг надежности, A, B, C (когда наводишь будет видно)») —
буква сразу после названия монеты, у раскрытых комбинаций — своя в начале «Биржи»; только у «тот» (у ? и ≠ свои значки).
Насколько надёжно доказано, что две ноги — один актив: A — биржа сама указала рынок или контракт, B — через первоисточник
(Binance Alpha, код той же биржи, родная сеть, реестр акций), C — догадка по тикеру или имени (AIW3 — пул DEX по тикеру).
Причина — в подсказке (код rel_ev из identity, текст — здесь). Торговлю рейтинг не ограничивает; колонки и сортировки нет.
Курсовой больше 7 % по модулю (владелец 13.09: «не показывай в таблице токены с курсовым спредом более 7%») — строка не
показывается в обоих режимах, по числу на экране; строки без курсового остаются; table.json не фильтруется.
Фильтры (владелец 12.09 вечер): «спот» и «фьючерсы» — площадки галочками (вкл/выкл), вместо «периода» и прежних
выпадающих «спот/перп/биржа»; сделка видна, если её спот и перп(ы) включены; «1d >» вместо «Текущий ≥» — сумма ставок за сутки, по умолчанию 0.5 %; строка состояния — только
при неполадке (старые данные, пауза площадки, дыры в истории кроме планового досбора Hyperliquid, диск, ошибки).
Курсовой: проценты крупно сверху, цены мелко под ними (владелец: «проценты важнее»).
Фандинг, Текущий — в час (владелец 10.09: «приводи к 1ч»); Период — родной интервал биржи справочно.
Сортировка во всех колонках со знаком: первый клик — плюс сверху, второй — минус сверху; пустые «—» всегда
в конце (владелец 10.09: «нужно позитив → негатив»). У каждого режима своя сортировка; монеты идут по своей
лучшей комбинации.
Качество данных (внешнее ревью 11.09): строка на устаревшей ставке — бледная и после годных; цена по марку, потому
что книга не обновилась, — «≈» перед ценами; чип площадки краснеет по возрасту самого старого из её источников.
Данные вшиты при рендере, дальше страница тянет /data.json раз в 10 с; режим, сортировки, фильтры и раскрытые
монеты — в localStorage.
Верхняя строка (владелец 12.09: «кнопку refresh и чтоб рядом был таймер свежести», «кнопку справа сверху личного
кабинета»): «обновлено N с назад» | ⟳ | переключатель режимов | «Кабинет» (/cabinet). Возраст — от ts таблицы (часы
сервера) с поправкой на часы телефона по заголовку Date ответов; ⟳ и плановый опрос — один запрос за раз.
"""
from __future__ import annotations
import json, html
from . import config

CSS = """
:root{--bg:#f2f4f1;--card:#fbfcfa;--card2:#e9ece7;--line:#d3d8cf;--fg:#1b201c;--mut:#616b62;--acc:#8a6d2f;
 --good:#1e7a4f;--bad:#a83b3b;--goodbg:#dfeee5;--badbg:#f6e2e0;--warnbg:#f4ecd8;--warn:#8a6d2f;--infobg:#e4e9ee;--info:#3a5b78;--link:#2f6ea8;}
@media (prefers-color-scheme: dark){:root{--bg:#12140f;--card:#191c16;--card2:#22261e;--line:#31362a;--fg:#e6e9df;--mut:#8f9787;
 --acc:#d9b45f;--good:#5cc48d;--bad:#e07a72;--goodbg:#18301f;--badbg:#33201d;--warnbg:#33290f;--warn:#d9b45f;--infobg:#1b2530;--info:#8fb2cc;--link:#8fb2cc;}}
*{box-sizing:border-box}
[hidden]{display:none!important}
body{margin:0;background:var(--bg);color:var(--fg);font:13px/1.4 "IBM Plex Sans",-apple-system,Segoe UI,Roboto,sans-serif}
.wrap{max-width:1600px;margin:0 auto;padding:16px 16px 24px}
.top{display:flex;align-items:center;justify-content:space-between;gap:12px;flex-wrap:wrap}
h1{font-size:15px;font-weight:600;margin:0}
.seg{display:inline-flex;border:1px solid var(--line);border-radius:6px;overflow:hidden;background:var(--card)}
.seg button{font:inherit;font-size:12px;font-weight:600;border:0;background:transparent;color:var(--mut);padding:6px 14px;cursor:pointer}
.seg button+button{border-left:1px solid var(--line)}
.seg button.on{background:var(--fg);color:var(--bg)}
.mono{font-family:"IBM Plex Mono",ui-monospace,monospace;font-variant-numeric:tabular-nums}
.g{color:var(--good)}.r{color:var(--bad)}.m{color:var(--mut)}
a{color:var(--link);text-decoration:none}a:hover{text-decoration:underline}
.chip{display:inline-block;padding:1px 6px;border-radius:3px;font-size:10.5px;font-weight:600;letter-spacing:.03em;margin-right:4px;white-space:nowrap}
.c-ok{background:var(--goodbg);color:var(--good)}.c-warn{background:var(--warnbg);color:var(--warn)}
.c-bad{background:var(--badbg);color:var(--bad)}.c-info{background:var(--infobg);color:var(--info)}.c-mut{background:var(--card2);color:var(--mut)}
.bar{display:flex;flex-wrap:wrap;gap:6px;align-items:center;margin:10px 0}
.ctl{display:flex;flex-wrap:wrap;gap:10px;align-items:center;margin:8px 0 12px;font-size:12px;color:var(--mut)}
.ctl label{display:inline-flex;gap:4px;align-items:center}
.ctl input,.ctl select{font:inherit;color:var(--fg);background:var(--card);border:1px solid var(--line);border-radius:4px;padding:2px 6px}
.ctl input[type=number]{width:70px}.ctl input[type=text]{width:100px}
/* таблица сама прокручивается по вертикали — иначе прилипающая шапка не прилипает (ревью 10.09) */
.tbl{overflow:auto;max-height:calc(100vh - 150px);border:1px solid var(--line);border-radius:6px;background:var(--card)}
table{border-collapse:collapse;width:100%;min-width:1000px}
th,td{padding:7px 9px;border-bottom:1px solid var(--line);vertical-align:middle;text-align:center;white-space:nowrap}
th{position:sticky;top:0;z-index:1;background:var(--card2);font-size:11px;color:var(--mut);font-weight:600;cursor:pointer;user-select:none}
th.l,td.l{text-align:left}
th.on{color:var(--fg)}
td b{font-weight:600}
td .s{display:block;font-size:11px;color:var(--mut)}
td.coin{vertical-align:top;border-right:1px solid var(--line);background:var(--card)}
.sp{display:inline-block;margin-top:3px;color:var(--link);font-size:12px;cursor:pointer;user-select:none;white-space:nowrap}
tr.mis td:not(.coin){color:var(--mut);opacity:.6}
tr.stale td:not(.coin){opacity:.5}
tr:hover td:not(.coin){background:var(--card2)}
.up{color:var(--good)}.dn{color:var(--bad)}
.warn{color:var(--warn);font-weight:700;margin-left:3px;cursor:help}
/* рейтинг надёжности A/B/C (владелец 13.09) — значок в рамке, как .ico: голой буквой «RGTI B акция» читалось «RGTI в
   акция» (ревью 13.09); C — цветом ⊘; ширина фиксирована — у подстрок группы пустое место (без рамки) той же ширины */
.rel{display:inline-block;width:15px;margin-left:5px;border:1px solid var(--line);border-radius:3px;text-align:center;font-size:10px;font-weight:700;line-height:13px;color:var(--mut);cursor:help;vertical-align:1px}
.rel.c{color:var(--warn);border-color:var(--warn)}
.rel.sub{margin:0 4px 0 0}
.rel:empty{border-color:transparent;cursor:default}
/* выбор бирж (владелец 12.09, как у чужого скринера): кнопка и выпадающий список с галочками */
.dd{position:relative;display:inline-flex;gap:4px;align-items:center}
.dd>button{font:inherit;color:var(--fg);background:var(--card);border:1px solid var(--line);border-radius:4px;padding:2px 8px;cursor:pointer;white-space:nowrap}
.dd .menu{position:absolute;z-index:5;top:calc(100% + 4px);left:0;min-width:200px;background:var(--card);border:1px solid var(--line);border-radius:6px;box-shadow:0 6px 18px rgba(0,0,0,.25);padding:4px 0}
.dd .menu label{display:flex;gap:9px;align-items:center;padding:6px 12px;cursor:pointer;color:var(--fg);font-size:13px}
.dd .menu label:hover{background:var(--card2)}
.dd .menu label.all{border-bottom:1px solid var(--line);margin-bottom:2px}
.ico{display:inline-flex;align-items:center;justify-content:center;width:18px;height:18px;border-radius:50%;font-size:10px;font-weight:700}
/* верхняя строка справа (владелец 12.09): свежесть, ⟳, режимы, «Кабинет»; на телефоне «Кабинет» остаётся справа сверху
   рядом с именем, а свежесть, ⟳ и режимы уходят второй строкой */
.tools{display:flex;align-items:center;gap:10px;margin-left:auto;flex-wrap:wrap}
.fr{display:inline-flex;align-items:center;gap:6px}
.fresh{font-size:12px;white-space:nowrap;font-variant-numeric:tabular-nums}
.fresh.y{color:var(--warn)}
.rl,.cab{font:inherit;font-size:12px;font-weight:600;line-height:1.2;color:var(--fg);background:var(--card);border:1px solid var(--line);border-radius:6px;padding:6px 12px;cursor:pointer;white-space:nowrap}
.rl{font-size:16px;padding:4px 10px}
.rl:disabled{cursor:default;opacity:.55}
.rl.err{color:var(--bad);border-color:var(--bad)}
.rl.spin span{display:inline-block;animation:spin .9s linear infinite}
@keyframes spin{to{transform:rotate(360deg)}}
@media (prefers-reduced-motion: reduce){.rl.spin span{animation:none}}
.cab:hover{text-decoration:none;background:var(--card2)}
@media (max-width:640px){
 .top{row-gap:8px}
 .cab{order:1;margin-left:auto}
 .tools{order:2;flex:1 1 100%;margin-left:0;justify-content:space-between}
 .fw{display:none}
 /* место под самый длинный текст («59 мин назад!»): иначе при 332–356 px режимы перепрыгивали на третью строку и
    обратно, как только менялось число цифр в таймере, а ⟳ ездила вбок (ревью 12.09) */
 .fresh{min-width:7em;text-align:right}
 .seg button{padding:6px 9px}
}
"""

JS = r"""
const W = %(windows)s, WL = %(labels)s, STALE = %(stale)d;
// площадки галочками (владелец 12.09: «биржи спот и futures отмечать нужно галочками какие вкл какие выкл»):
// [площадка, подпись, фон и цвет значка, буква]; спот и фьючерс одной биржи включаются отдельно
const SPOTS = %(spots)s, PERPS = %(perps)s;
const PICK = {sp: {list: SPOTS, key: 'spSel'}, pp: {list: PERPS, key: 'ppSel'}};
const D1 = '24';                     // окно «1 день» — фильтр «1d >» (владелец 12.09: вместо «Текущий ≥», по умолчанию 0.5 %%)
// владелец 13.09: «не показывай в таблице токены с курсовым спредом более 7%%» — порог, %%, по модулю (config.DASH_GAP_HIDE_PCT).
// let, а не const: проверки порядка в тестах, где «не тот актив» стоит по цене ×120, отключают правило (GAP_HIDE = Infinity)
let GAP_HIDE = %(gap_hide)s;
let T = %(table)s;
let pageErr = '';
// число «не тот актив» у «скрыть» — по строкам после правила 7 %% (почти все «≠» им скрыты); null — страница ещё не считала
const visMis = {ff: null, sf: null};
const $ = s => document.querySelector(s);
// spSel / ppSel — включённые споты / фьючерсы (null = все); min1d — «1d >» в процентах (ключ новый: прежний «Текущий ≥»
// из сохранённых настроек не должен перебить значение по умолчанию 0.5)
const DEF = {mode:'sf', ff:{sort:'spread', dir:-1}, sf:{sort:'spread', dir:-1}, open:{ff:[], sf:[]},
             hideMis:false, spSel:null, ppSel:null, min1d:'0.5', q:''};
let ui = Object.assign({}, DEF);
// fb_ui4 — настройки прежней версии страницы (режим, сортировки, фильтры); убранная сортировка dev сбрасывается ниже
try { ui = Object.assign({}, DEF, JSON.parse(localStorage.getItem('fb_ui5') || localStorage.getItem('fb_ui4') || '{}')); } catch(e) {}
ui.ff = Object.assign({}, DEF.ff, ui.ff); ui.sf = Object.assign({}, DEF.sf, ui.sf);
ui.open = Object.assign({ff:[], sf:[]}, ui.open);
if(ui.mode !== 'ff' && ui.mode !== 'sf') ui.mode = 'sf';
// сохранённый выбор — только из площадок, что есть сейчас; включены все — это «все» (null)
for(const p of Object.values(PICK)){
  const s = Array.isArray(ui[p.key]) ? ui[p.key].filter(k => p.list.some(e => e[0] === k)) : null;
  ui[p.key] = s && s.length === p.list.length ? null : s;
}
function save(){ try { localStorage.setItem('fb_ui5', JSON.stringify(ui)); } catch(e) {} }
const nz = x => x === null || x === undefined || (typeof x === 'number' && !isFinite(x));
const esc = s => String(s).replace(/[&<>"]/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c]));
// знаков после запятой (владелец 13.09: «0.0338 фандосы округляем до 0.xxx знаков тут (текущий 1ч)… во всех остальных
// местах X.XX»): ставки в час — «Текущий 1ч» и «Фандинг 1ч» (из них и сложен «Текущий») — 3 знака, всё остальное
// (окна истории, «Курсовой», «Комиссия», подсказки) — 2. Ноль на этой точности — без знака: «0.00%%», а не «−0.00%%» / «+0.00%%»
const spct = (x, d) => { if(nz(x)) return '—'; const s = (x*100).toFixed(d);
  return (/^-?0\.?0*$/.test(s) ? s.replace('-', '') : (x > 0 ? '+' : '') + s) + '%%'; };
const pct = (x, d) => nz(x) ? '—' : ((x*100).toFixed(d) + '%%');
// цвет знака; d — точность показа: число, что на этой точности ноль («0.00%%» без знака), — без цвета, как и без знака
// (ревью 13.09: иначе «0.00%%» красный или зелёный — на живой таблице сотни окон)
const cls = (x, d) => nz(x) ? 'm' : (d !== undefined && !+(x*100).toFixed(d) ? '' : (x > 0 ? 'g' : (x < 0 ? 'r' : '')));
const px = x => nz(x) ? '—' : (x >= 1000 ? x.toFixed(1) : x >= 100 ? x.toFixed(2) : x >= 1 ? x.toFixed(4) : x.toPrecision(4));
function age(ts, now){ if(!ts) return '—'; const s = Math.max(0, Math.round(now/1000 - ts)); return s < 90 ? s + ' с' : Math.round(s/60) + ' мин'; }
function ageS(s){ return nz(s) ? '—' : (s < 90 ? Math.round(s) + ' с' : Math.round(s/60) + ' мин'); }
// у площадки без страницы рынка (Lighter на Robinhood Chain) ссылки нет — имя без ссылки, а не href="null"
const link = (href, text, c) => href ? `<a${c ? ` class="${c}"` : ''} href="${esc(href)}" target="_blank" rel="noopener">${text}</a>`
                                     : `<span${c ? ` class="${c}"` : ''}>${text}</span>`;
const num = x => nz(x) ? null : x;
// 1 спред, 2 спреда, 5 спредов, 11 спредов, 21 спред
function plural(n, one, few, many){
  const a = n %% 10, b = n %% 100;
  return n + ' ' + (a === 1 && b !== 11 ? one : (a >= 2 && a <= 4 && (b < 12 || b > 14) ? few : many));
}

// --- колонки и ключи сортировки -------------------------------------------------------------------
const WCOLS = W.map(w => ['w'+w, WL[w]]);
const COLS = {
  ff: [['base','Монета','l'], [null,'Биржа','l'], [null,'Фандинг 1ч'], ['spread','Текущий 1ч'], ['gap','Курсовой'],
       ['period','Период'], ['fee','Комиссия']].concat(WCOLS),
  sf: [['base','Монета','l'], [null,'Биржа','l'], ['spread','Текущий 1ч'], ['gap','Курсовой'],
       ['period','Период'], ['fee','Комиссия']].concat(WCOLS),
};
const KEYS = {base: r => r.base, spread: r => num(r.spread), gap: r => num(r.gap), period: r => num(r.period),
              fee: r => num(r.fee)};
W.forEach(w => { KEYS['w'+w] = r => num((r.windows[w] || {}).spread); });
for(const m of ['ff', 'sf']) if(!KEYS[ui[m].sort]) ui[m] = Object.assign({}, DEF[m]);   // колонка убрана (Отклонение)
// годные → устаревшие → «не тот актив»; внутри группы пустые — в конце при ЛЮБОМ направлении; остальное со знаком
const rank = r => r.mismatch ? 2 : (r.stale ? 1 : 0);
function cmp(x, y, key, dir){
  if(rank(x) !== rank(y)) return rank(x) - rank(y);
  const k = KEYS[key] || KEYS.spread, a = k(x), b = k(y);
  if(nz(a) !== nz(b)) return nz(a) ? 1 : -1;
  let c = 0;
  if(!nz(a)) c = key === 'base' ? String(a).localeCompare(String(b)) * -dir    // первый клик — А→Я
                                : (a < b ? -1 : a > b ? 1 : 0) * dir;          // первый клик — плюс сверху
  if(c) return c;
  // равенство по ключу (Монета — всегда, Период и Комиссия — часто): первой в группе монеты идёт лучшая комбинация по
  // текущему со знаком, а не первая по порядку бирж (проверка исправлений 12.09); дальше — ключ строки, порядок стабилен
  if(key !== 'spread'){
    const s1 = num(x.spread), s2 = num(y.spread);
    if(nz(s1) !== nz(s2)) return nz(s1) ? 1 : -1;
    if(!nz(s1) && s1 !== s2) return s2 - s1;
  }
  return String(x.key) < String(y.key) ? -1 : String(x.key) > String(y.key) ? 1 : 0;
}

function winCells(r){
  return W.map(w => { const x = r.windows[w] || {};
    return `<td><b class="mono ${cls(x.spread, 2)}">${spct(x.spread, 2)}</b>${x.incomplete ? '<span class="warn" title="история неполная">!</span>' : ''}</td>`; }).join('');
}
// «не тот актив» и «не проверено» — по составу индекса и контрактам, не по цене (владелец 12.09); ⊘ — ввод/вывод
// монеты на споте закрыт: цена может уйти от перпа, а переложить монету нельзя
function misMark(r){
  if(r.mismatch) return ` <span class="m" title="не тот актив: ${esc(r.ident_why || '')}">≠</span>`;
  let s = '';
  if(r.ident === 'unknown') s += ` <span class="m" style="cursor:help" title="не проверено: ${esc(r.ident_why || '')}">?</span>`;
  if(r.xfer) s += ` <span class="warn" title="${esc(r.xfer)} на споте во всех сетях: цена может уйти от перпа, монету не переложить">⊘</span>`;
  // OKX DEX: мостовая версия токена (владелец 12.09: «да, с пометкой «мост»») — и её обычный сдвиг к перпу
  if(r.dex && r.dex.bridged) s += ` <span class="m" style="cursor:help" title="мостовая версия токена (не родная сеть монеты): обычный сдвиг цены к перпу ${nz(r.dex.usual) ? '—' : spct(r.dex.usual, 2)}">мост</span>`;
  return s;
}
// рейтинг надёжности «тот» (владелец 13.09: «ставь с названием монеты рейтинг надежности, A, B, C (когда наводишь будет
// видно)»): в table.json — буква rel, код причины rel_ev и у C подробность rel_d (пул, имя); текст подсказки — здесь, по коду
const REL_WHY = {
  idx: 'рынок этого спота — в составе индекса перпа', leg: 'контракт совпал с монетой рынка из индекса перпа',
  contract: 'общий контракт монет из индексов обоих перпов', ref: 'индекс одного перпа ссылается на другой перп',
  mkt: 'оба индекса берут один и тот же рынок', cex: 'оба индекса берут один рынок другой биржи',
  vendor: 'оба индекса берут цену у одного поставщика',
  alpha: 'токен Binance Alpha из индекса перпа, найден в списке Alpha по тикеру',
  code: 'состава индекса нет: монета с тем же кодом на споте той же биржи', native: 'родная монета одной и той же сети',
  stock: 'токен акции по реестру площадки, перп — та же акция', gold: 'проверенный токен золота',
  // class: равна база после приведения, а не символы бирж — BZ | BRENTOIL, COPPER | XCU — синонимы config.PERP_CANON (ревью 13.09)
  fx: 'та же валюта', class: 'тот же актив в классе, который указали биржи (тикер или его известный синоним)',
  pool2: 'оба индекса берут пул DEX с тем же названием пары',
  name: 'совпало только имя монеты', bridged: 'контракты разные, совпало только имя (мост или миграция?)',
  oracle: 'оракул Hyperliquid сопоставлен по тикеру', pool: 'токен найден поиском пула DEX по тикеру'};
// своя DEX-строка (OKX DEX), токен доказан индексом этого же перпа: спот строки — DEX, а не «спот той же биржи»
const REL_DEX = {code: 'контракт токена — у монеты с тем же кодом на споте биржи перпа'};
// DEX-токен другого перпа монеты (ident_ev «dex_link:…», identity.dex_links): буква — слабейшее звено цепочки «токен →
// тот перп → этот перп», а код — либо связи перпов, либо класса токена у того перпа (при равенстве букв — связи), на
// странице их не различить. Фразы REL_WHY — для пары перп/перп или спот/перп и тут врут («оба индекса» — чьи? «родная
// монета сети» у мостового BSC-токена FIL — ревью 13.09), поэтому здесь фразы, верные при любом из двух раскладов
const REL_LINK = {
  mkt: 'оба перпа цепочки: индексы берут один и тот же рынок', cex: 'оба перпа цепочки: индексы берут один рынок другой биржи',
  vendor: 'оба перпа цепочки: индексы берут цену у одного поставщика',
  contract: 'оба перпа цепочки: общий контракт монет из их индексов', ref: 'индекс одного перпа цепочки ссылается на другой',
  pool2: 'оба перпа цепочки: индексы берут пул DEX с тем же названием пары',
  native: 'перпы цепочки — родная монета одной сети (про сам токен это не говорит)',
  code: 'монета с тем же кодом на споте биржи одного из перпов цепочки',
  alpha: 'токен Binance Alpha из индекса одного из перпов цепочки, найден в списке Alpha по тикеру',
  leg: 'контракт токена совпал с монетой рынка из индекса того перпа',
  class: 'перпы цепочки — тот же актив в классе, который указали биржи',
  name: 'перпы цепочки совпали только по имени монеты',
  bridged: 'у перпов цепочки контракты разные, совпало только имя (мост или миграция?)'};
const REL_SCALE = 'A — биржа сама указала рынок или контракт · B — через первоисточник · C — догадка по тикеру или имени';
function relTitle(r){
  const lnk = String(r.ident_ev || '').startsWith('dex_link:');
  const why = (lnk ? REL_LINK[r.rel_ev] : r.dex ? REL_DEX[r.rel_ev] : null) || REL_WHY[r.rel_ev] || 'причина не записана';
  return `${r.rel} — ${why}${r.rel_d ? ': ' + r.rel_d : ''}` +
    (lnk ? '\nтокен — из индекса другого перпа этой монеты; буква — по слабейшему звену цепочки' : '') + '\n' + REL_SCALE;
}
// значок-буква с подсказкой; sub — у раскрытой подстроки группы: без буквы (? и ≠) — пустое место той же ширины,
// чтобы биржи не ездили
function relMark(r, sub){
  const ok = r.ident === 'same' && !r.mismatch && /^[ABC]$/.test(r.rel || '');
  if(!ok) return sub ? '<span class="rel sub"></span>' : '';
  return `<span class="rel${sub ? ' sub' : ''}${r.rel === 'C' ? ' c' : ''}" title="${esc(relTitle(r))}">${r.rel}</span>`;
}
// начало «Биржи»: в раскрытой группе у подстрок — своя буква, у ведущей — пустое место той же ширины (её буква — у
// монеты), иначе её биржа стояла на 16 px левее подстрок (ревью 13.09); свёрнутая группа — без места, как было
const relSlot = (r, lead, open) => !open ? '' : (lead ? '<span class="rel sub"></span>' : relMark(r, true));
// подсказка «Комиссии» DEX-строки: из чего сложено «всё включено» на клип
function dexTip(r){
  const d = r.dex || {}, p = x => nz(x) ? '—' : (x*100).toFixed(2) + '%%';
  const out = [`OKX DEX, сеть ${d.chain}` + (d.name ? ` · ${d.name}` : '')];
  if(!nz(d.liq)) out.push(`ликвидность токена $${Math.round(d.liq).toLocaleString('ru-RU')}`);
  if(!nz(d.rt)) out.push(`всё включено на клип $${d.clip}: круг по цене (пул + удар в обе стороны) ${p(d.rt)}, газ входа и выхода ${p(d.gas)}, налог токена ${p(d.tax)}, тейкер фьючерса ×2`);
  else out.push(d.err ? `котировки нет: ${d.err}` : 'котировка на клип ещё не получена');
  if(!nz(d.q_age)) out.push(`котировка ${Math.max(0, Math.round(d.q_age/60))} мин назад`);
  return out.join('\n');
}
// «Комиссия» строки с ногой Variational (ревью 13.09): комиссии там нет, издержка — спред котировки RFQ (fee_q — только у таких строк)
function feeTip(r){
  if(!('fee_q' in r)) return '';
  return nz(r.fee_q) ? 'Variational: котировки RFQ нет — издержку круга не оценить'
                     : `Variational: комиссии нет, издержка — спред котировки RFQ на $1k: ${pct(r.fee_q, 2)} за круг (купить по ask, продать по bid); остальное — тейкер ×2`;
}
function feeAttr(r){
  const t = [r.dex ? dexTip(r) : '', feeTip(r)].filter(Boolean).join('\n');
  return t ? ` style="cursor:help" title="${esc(t)}"` : '';
}
function trAttr(r, sub){
  const c = [r.mismatch ? 'mis' : (r.stale ? 'stale' : ''), sub ? 'sub' : ''].filter(Boolean).join(' ');
  const t = !r.mismatch && r.stale ? ' title="данные устарели: ставка или книга спота не обновлялись дольше минуты"' : '';
  return (c ? ` class="${c}"` : '') + t;
}
function gapCell(r, p1, p2, marked){
  const approx = marked ? '<span title="цена по марку: книга не обновилась в этом снимке">≈ </span>' : '';
  return `<td><b class="mono ${Math.abs(r.gap ?? 0) > 0.005 ? 'r' : ''}">${spct(r.gap, 2)}</b><span class="s mono">${approx}${px(p1)} | ${px(p2)}</span></td>`;
}
// класс актива: монеты без подписи; акция / сырьё / индекс — подписаны (у QNT монета Quant и акция Quantinuum — разные группы)
const CLS = {equity:'акция', commodity:'сырьё', index:'индекс', preipo:'до IPO', fx:'валюта'};
const gkey = r => r.base + '|' + (r.cls || 'crypto');
// ячейка монеты — одна на группу: имя, рейтинг ведущей (видимой в свёрнутом виде) строки и «N спредов ⌄/⌃», если
// комбинаций больше одной
function coinCell(g, shown, open){
  const r = g[0], tag = CLS[r.cls] ? ` <span class="m">${CLS[r.cls]}</span>` : '';
  const tog = g.length > 1 ? `<br><span class="sp" data-b="${esc(gkey(r))}">${plural(g.length, 'спред', 'спреда', 'спредов')} ${open ? '⌃' : '⌄'}</span>` : '';
  return `<td class="l coin"${shown > 1 ? ` rowspan="${shown}"` : ''}><b>${esc(r.base)}</b>${relMark(r)}${tag}${tog}</td>`;
}
// futures/futures в сторону сделки (владелец 13.09: «первой биржей пиши где лонг, второй где шорт»). В table.json пара
// лежит A|B в порядке config.PERP_VENUES, и все числа там — A − B (calc.build_ff_row; так их читают audit, tg, trade —
// их не трогаем). На странице строка — копия, развёрнутая по знаку ТЕКУЩЕГО спреда: первой — нога-лонг ▲, второй —
// нога-шорт ▼, и ВСЕ числа строки — «второй − первый», то есть «шорт − лонг»:
//   «Текущий» = час(шорт) − час(лонг) ≥ 0; окно = сумма(шорт) − сумма(лонг) в той же ориентации (в прошлом бывает минус:
//   в этом окне выгоднее было наоборот); «Фандинг» и цены — «лонг | шорт»; ссылки, «Период» (a|b) — вслед за ногами;
//   «Курсовой» = цена(второй) / цена(первой) − 1 = цена(шорт) / цена(лонг) − 1 — как у spot/futures (перп / спот − 1):
//   плюс — продаём дороже, чем покупаем, на входе это в нашу пользу. В table.json gap = A / B − 1, поэтому при
//   развороте ног он тот же, без разворота — 1 / (1 + gap) − 1 (множители лотов fa/fb в отношении сокращаются).
// Направления нет (спред 0 или ставки нет, «не тот актив» — сделки нет): ноги как лежат, A|B без стрелок, числа по тому
// же правилу «второй − первый» (B − A) — у колонки одно определение для всех строк.
// Сортировка, «1d >», галочки бирж, поиск и «N спредов» работают по этой копии — со знаком по тому, что на экране.
const FF_PAIRS = [['va','vb'], ['sa','sb'], ['la','lb'], ['iv_a','iv_b'], ['rate_a','rate_b'], ['rate_h_a','rate_h_b'],
                  ['next_a','next_b'], ['mark_a','mark_b'], ['px_a','px_b'], ['px_src_a','px_src_b']];
const neg = x => nz(x) ? x : -x + 0;                 // + 0: без «−0»
function orientFF(r){
  const lng = !r.mismatch && (r.side === 'short_a' || r.side === 'short_b');     // первая нога — лонг, стрелки есть
  const sw = lng && r.side === 'short_a';           // шорт на A — A встаёт второй
  const o = Object.assign({}, r, {lng: lng});
  if(sw){
    for(const [a, b] of FF_PAIRS){ o[a] = r[b]; o[b] = r[a]; }
    o.urls = [(r.urls || [])[1], (r.urls || [])[0]];
  }
  const d = x => sw ? x : neg(x);                   // второй − первый: при развороте это A − B, как в table.json, иначе B − A
  o.spread = d(r.spread); o.spread_h = d(r.spread_h);
  o.gap = sw || nz(r.gap) ? r.gap : 1 / (1 + r.gap) - 1;
  o.windows = {};
  for(const [w, x] of Object.entries(r.windows || {}))
    o.windows[w] = Object.assign({}, x, sw ? {a: x.b, na: x.nb, b: x.a, nb: x.na} : {}, {spread: d(x.spread)});
  return o;
}
function venueFF(r){
  const a = link(r.urls[0], esc(r.la || r.va)), b = link(r.urls[1], esc(r.lb || r.vb));
  return r.lng ? `${a} <span class="up">▲</span> | ${b} <span class="dn">▼</span>` : `${a} | ${b}`;
}
function rowFF(r, lead, open){
  const per = r.iv_a === r.iv_b ? `${r.period}` : `${r.period} <span class="m">(${r.iv_a}|${r.iv_b})</span>`;
  return `<tr${trAttr(r, !lead)}>${lead}
<td class="l">${relSlot(r, lead, open)}${venueFF(r)}${misMark(r)}</td>
<td class="mono"><span class="${cls(r.rate_h_a, 3)}">${spct(r.rate_h_a, 3)}</span> <span class="m">|</span> <span class="${cls(r.rate_h_b, 3)}">${spct(r.rate_h_b, 3)}</span></td>
<td><b class="mono ${r.mismatch ? 'm' : cls(r.spread, 3)}">${r.mismatch ? '—' : spct(r.spread, 3)}</b></td>
${gapCell(r, r.px_a, r.px_b, r.px_src_a === 'mark' || r.px_src_b === 'mark')}
<td class="mono">${per}</td>
<td class="mono"${feeAttr(r)}>${pct(r.fee, 2)}</td>${winCells(r)}</tr>`;
}
function rowSF(r, lead, open){
  // структура сделки фиксирована: спот только в лонг, перп только в шорт; знак ставки — красный, если платим мы
  const venue = `${link(r.urls.spot, esc(r.spot_label || r.spot_ex))} <span class="up">▲</span> | ${link(r.urls.perp, esc(r.perp_label || r.perp_ex))} <span class="dn">▼</span>`;
  return `<tr${trAttr(r, !lead)}>${lead}
<td class="l">${relSlot(r, lead, open)}${venue}${misMark(r)}</td>
<td><b class="mono ${r.mismatch ? 'm' : cls(r.spread, 3)}">${r.mismatch ? '—' : spct(r.spread, 3)}</b></td>
${gapCell(r, r.px_spot, r.px_perp, r.px_src_perp === 'mark')}
<td class="mono">${r.period}</td>
<td class="mono"${feeAttr(r)}>${pct(r.fee, 2)}</td>${winCells(r)}</tr>`;
}

// futures/futures — копии строк в сторону сделки (orientFF); сама таблица T не меняется: её же берёт следующий опрос
const rawRows = () => (ui.mode === 'ff' ? T.ff_rows : T.sf_rows) || [];
function allRows(){ return ui.mode === 'ff' ? rawRows().map(orientFF) : rawRows(); }
const selOf = id => ui[PICK[id].key] ? new Set(ui[PICK[id].key]) : null;      // null — включены все
function filtered(){
  let rows = allRows().slice();
  // владелец 13.09: «не показывай в таблице токены с курсовым спредом более 7%%» — по модулю и по тому, что на экране
  // (futures/futures — после разворота orientFF, округление как в ячейке: «+7.00%%» видна, «+7.01%%» — нет); строки без
  // курсового остаются; table.json не фильтруется (его читают трейдер и тестировщик). Правило — первым: группы, ведущая
  // строка монеты (а с ней и буква рейтинга), «N спредов» и число «не тот актив» — по оставшимся
  rows = rows.filter(r => nz(r.gap) || !(Math.abs(+(r.gap*100).toFixed(2)) > GAP_HIDE));
  visMis[ui.mode] = rows.filter(r => r.mismatch).length;
  if(ui.hideMis) rows = rows.filter(r => !r.mismatch);
  // площадки галочками: сделка видна, если её спот среди включённых спотов, а перп(ы) — среди включённых фьючерсов
  const sp = selOf('sp'), pp = selOf('pp');
  if(ui.mode === 'ff'){ if(pp) rows = rows.filter(r => pp.has(r.va) && pp.has(r.vb)); }
  else { if(sp) rows = rows.filter(r => sp.has(r.spot_ex)); if(pp) rows = rows.filter(r => pp.has(r.perp_ex)); }
  // «1d >»: сумма расчитанных ставок за сутки (колонка «1 день»); в futures/futures — по модулю (строка развёрнута по
  // текущему спреду, а за сутки выгодной могла быть обратная сторона).
  // Только по числу: «не тот актив» прячет свой переключатель, устаревшие остаются бледными внизу (ревью 12.09 — фильтр
  // по умолчанию включён, и он молча выкидывал обе группы: «скрыть «не тот актив»» ничего не менял)
  const md = parseFloat(ui.min1d);
  if(!isNaN(md)) rows = rows.filter(r => { const d = (r.windows[D1] || {}).spread;
    return !nz(d) && (ui.mode === 'ff' ? Math.abs(d*100) > md : d*100 > md); });
  const q = (ui.q || '').trim().toUpperCase(); if(q) rows = rows.filter(r => String(r.base).toUpperCase().includes(q));
  const st = ui[ui.mode];
  rows.sort((x, y) => cmp(x, y, st.sort, st.dir));
  return rows;
}
// комбинации одной монеты вместе; строки уже отсортированы, поэтому монета стоит на месте своей лучшей комбинации,
// а внутри группы порядок тот же, что у сортировки
function grouped(rows){
  const g = new Map();
  for(const r of rows){ const k = gkey(r); if(!g.has(k)) g.set(k, []); g.get(k).push(r); }
  return Array.from(g.values());
}
function renderHead(){
  const st = ui[ui.mode];
  $('#head').innerHTML = '<tr>' + COLS[ui.mode].map(([k, t, l]) => {
    const on = k && k === st.sort, arrow = on ? (k === 'base' ? (st.dir < 0 ? ' ↑' : ' ↓') : (st.dir < 0 ? ' ↓' : ' ↑')) : '';
    return `<th class="${l ? 'l' : ''} ${on ? 'on' : ''}"${k ? ` data-k="${k}"` : ' style="cursor:default"'}>${t}${arrow}</th>`;
  }).join('') + '</tr>';
  document.querySelectorAll('#head th[data-k]').forEach(th => th.onclick = () => {
    const s = ui[ui.mode];
    if(s.sort === th.dataset.k) s.dir = -s.dir; else { s.sort = th.dataset.k; s.dir = -1; }
    save(); render();
  });
}
function renderControls(){
  document.querySelectorAll('.seg button').forEach(b => b.classList.toggle('on', b.dataset.m === ui.mode));
  document.querySelectorAll('.ff-only').forEach(el => el.hidden = ui.mode !== 'ff');
  document.querySelectorAll('.sf-only').forEach(el => el.hidden = ui.mode !== 'sf');
  $('#minLbl').textContent = ui.mode === 'ff' ? '|1d| >' : '1d >';
}
// выбор площадок: кнопка с включёнными и список с галочками; «Все» включает и выключает все сразу
function pickLabel(id){
  const p = PICK[id], on = selOf(id); if(!on) return 'все'; if(!on.size) return 'ни одной';
  const names = p.list.filter(e => on.has(e[0])).map(e => e[1]);
  return names.length <= 2 ? names.join(', ') : names.length + ' из ' + p.list.length;
}
function renderPick(id){
  const p = PICK[id], on = selOf(id);
  $('#' + id + 'Btn').textContent = pickLabel(id) + ' ⌄';
  $('#' + id + 'Menu').innerHTML = `<label class="all"><input type="checkbox" data-x="*"${on ? '' : ' checked'}> Все</label>` +
    p.list.map(([k, t, bg, fg, l]) => `<label><input type="checkbox" data-x="${esc(k)}"${!on || on.has(k) ? ' checked' : ''}>` +
      `<span class="ico" style="background:${bg};color:${fg}">${esc(l)}</span>${esc(t)}</label>`).join('');
}
function setPick(id, x, checked){
  const p = PICK[id], all = p.list.map(e => e[0]);
  let cur = new Set(ui[p.key] || all);
  if(x === '*') cur = checked ? new Set(all) : new Set();
  else if(checked) cur.add(x); else cur.delete(x);
  ui[p.key] = cur.size === all.length ? null : all.filter(k => cur.has(k));
  save(); renderPick(id); safeRender();
}
// строка состояния — только когда что-то не так (владелец 12.09: «эти предупреждения тут зачем?»): данные или площадка
// старше минуты, пауза после 429, строки на старых ставках, дыры в истории (плановый досбор после часового расчёта
// дырой не считает сам коллектор — funding.completeness, catchup), диск, ошибки шагов, ошибка страницы. Всё в порядке —
// строки нет.
function renderStatus(now){
  const tickAge = Math.round(now/1000 - (T.tick_ts || 0));
  let chips = tickAge > STALE ? `<span class="chip c-bad">данные ${age(T.tick_ts, now)}</span>` : '';
  const drift = Math.max(0, now/1000 - (T.ts || now/1000));      // сколько секунд назад снят table.json
  for(const [ex, h] of Object.entries(T.health || {})){
    const src = (T.src_age || {})[ex];
    // возраст площадки — по самому старому из её источников (ставки, книга), а не по последнему удачному запросу
    const a = src && Object.keys(src).length ? Math.max(...Object.values(src)) + drift : now/1000 - (h.last_ok_ts || 0);
    const banned = (h.banned_until || 0) > now/1000;
    if(a <= (h.stale_s || STALE) && !banned) continue;             // у OKX DEX цены пачкой раз в 3 мин — свой предел
    const detail = src ? Object.entries(src).map(([k, v]) => `${k === 'prem' ? 'ставки' : 'книга'} ${ageS(v + drift)}`).join(', ') : '';
    chips += `<span class="chip c-bad" title="${esc(detail)}; вес ${h.used_weight}, 429: ${h.n_429}, ошибок: ${h.n_err}">${esc(ex)}: ${banned ? 'пауза после 429' : 'нет данных ' + ageS(a)}</span>`;
  }
  const bf = T.backfill || {};
  if(bf.running) chips += `<span class="chip c-info">история ${bf.done}/${bf.total}</span>`;
  // «не тот актив» — посчитанное страницей после правила 7 %% (до первой отрисовки — из таблицы)
  const vm = visMis[ui.mode], mis = vm === null ? T['n_mismatch_' + ui.mode] : vm;
  const inc = T['n_incomplete_' + ui.mode], stl = T['n_stale_' + ui.mode];
  if(stl) chips += `<span class="chip c-bad">устарело: ${stl}</span>`;
  if(inc) chips += `<span class="chip c-warn" title="в истории не хватает расчётов: суммы окон с «!» могут быть занижены">неполных: ${inc}</span>`;
  if(!nz(T.disk_free_gb) && T.disk_free_gb < 3) chips += `<span class="chip c-bad">диск ${T.disk_free_gb} ГБ</span>`;
  for(const [k, v] of Object.entries(T.notes || {})) chips += `<span class="chip c-bad" title="${esc(v)}">${esc(k)}: ошибка</span>`;
  if(pageErr) chips += `<span class="chip c-bad" title="${esc(pageErr)}">страница: ошибка</span>`;
  $('#status').innerHTML = chips; $('#status').hidden = !chips;
  $('#misN').textContent = mis ? ` (${mis})` : '';                // число «не тот актив» — у переключателя «скрыть»
}
function render(){
  renderControls(); renderHead();
  const rows = filtered(), groups = grouped(rows), open = new Set(ui.open[ui.mode] || []);
  const rowFn = ui.mode === 'ff' ? rowFF : rowSF;
  let h = '';
  for(const g of groups){
    const isOpen = g.length > 1 && (open.has(gkey(g[0])) || open.has(g[0].base)), show = isOpen ? g : [g[0]];
    show.forEach((r, i) => { h += rowFn(r, i === 0 ? coinCell(g, show.length, isOpen) : '', isOpen); });
  }
  $('#rows').innerHTML = h;
  $('#shown').textContent = plural(groups.length, 'монета', 'монеты', 'монет') + ' · ' + rows.length + ' из ' + rawRows().length;   // счёт — без второго разворота всех строк
  renderStatus(srvNow());
}
function toggle(base){
  const lst = ui.open[ui.mode] || [], i = lst.indexOf(base);
  if(i >= 0) lst.splice(i, 1); else { lst.push(base); if(lst.length > 300) lst.shift(); }
  ui.open[ui.mode] = lst; save(); safeRender();
}
function safeRender(){
  try { render(); pageErr = ''; }
  catch(e){ pageErr = (e && e.message) || String(e); try { console.error(e); } catch(_) {} try { renderStatus(srvNow()); } catch(_) {} }
}

// --- свежесть и обновление (владелец 12.09: «кнопку refresh в дашборд, и чтоб рядом был таймер свежести») ---------
// Возраст — от ts таблицы: когда коллектор записал table.json, по часам СЕРВЕРА. Часы телефона могут уходить на минуты,
// поэтому «сейчас» = часы страницы + skew, где skew (мс) — поправка по заголовку Date ответов /data.json. Date точен до
// секунды и снят раньше, чем ответ дошёл, так что каждый ответ даёт только нижнюю границу: Date − момент получения;
// берём наибольшую из последних шести — она ближе всех к истине и не дёргает таймер на секунду туда-сюда. Поправка
// помнится (fb_skew): при открытии страницы ответа ещё не было, таймер идёт по вшитым данным и прежней поправке.
// Те же часы — у строки состояния («данные N», пауза площадки): иначе таймер зелёный, а чип рядом красный.
const POLL_MS = 10000, FETCH_MS = 30000;
let skew = 0, skews = [];
try { const v = parseFloat(localStorage.getItem('fb_skew')); if(isFinite(v)) skew = v; } catch(e) {}
function srvNow(){ return Date.now() + skew; }
function noteDate(hdr, got){
  const d = Date.parse(hdr || ''); if(!isFinite(d)) return;
  skews.push(d - got); if(skews.length > 6) skews.shift();
  skew = Math.max(...skews);
  try { localStorage.setItem('fb_skew', String(skew)); } catch(e) {}
}
// «обновлено N с назад»: до минуты — секунды, дальше минуты, после часа — часы; зелёный < 30 с, жёлтый < 2 мин
const freshText = s => s < 60 ? s + ' с' : s < 3600 ? Math.floor(s/60) + ' мин' : Math.floor(s/3600) + ' ч';
const freshCls = s => s < 30 ? 'g' : s < 120 ? 'y' : 'r';
const hms = d => [d.getHours(), d.getMinutes(), d.getSeconds()].map(x => String(x).padStart(2, '0')).join(':');
// busy — идёт запрос (кнопкой или плановый): второй не начинается, нажатие ⟳ во время планового опроса присоединяется к
// нему; manual — ⟳ нажата: кнопка крутится и погашена до ответа (плановый опрос кнопку не трогает — ревью 12.09: раньше
// она бледнела на время каждого опроса раз в 10 с и глотала нажатия); fetchErr — неудача последнего запроса: кнопка и
// таймер красные, причина в подсказке (не alert); abort — бросить текущий запрос (AbortController, если он есть)
let busy = false, manual = false, fetchErr = '', reqN = 0, lastStart = 0, pollT = null, freshHtml = null, abort = null;
function renderFresh(){
  const el = $('#fresh'), btn = $('#reload');
  const s = T.ts ? Math.max(0, Math.floor(srvNow()/1000 - T.ts)) : null;
  const h = (s === null ? 'нет данных' : `<span class="fw">обновлено </span>${freshText(s)} назад`) + (fetchErr ? '<span class="warn">!</span>' : '');
  if(h !== freshHtml){ el.innerHTML = h; freshHtml = h; }                     // раз в секунду — без лишних перерисовок
  el.className = 'fresh ' + (fetchErr || s === null ? 'r' : freshCls(s));
  el.title = (fetchErr ? 'не удалось обновить: ' + fetchErr + '\n' : '') +
    (T.ts ? `таблица записана коллектором в ${hms(new Date(T.ts*1000 - skew))} по часам этого устройства` +
            (Math.abs(skew) >= 1000 ? ` (часы устройства расходятся с сервером на ${Math.round(skew/1000)} с)` : '') : '');
  btn.disabled = busy && manual;
  btn.classList.toggle('spin', busy && manual);
  btn.classList.toggle('err', !!fetchErr);
  btn.title = fetchErr ? 'не удалось обновить: ' + fetchErr + ' — нажмите, чтобы повторить' : 'обновить сейчас';
}
// плановый опрос — через 10 с после начала последнего запроса: нажатие ⟳ отодвигает его, два 5-МБ ответа подряд не идут
function schedule(){ clearTimeout(pollT); pollT = setTimeout(() => refresh(false), POLL_MS); }
// прокрутка таблицы — как была: перерисовка не должна прыгать к началу. Пишется, только если сдвинулась: лишняя запись
// scrollTop раз в 10 с обрывает инерцию прокрутки пальцем (iOS)
function keepScroll(fn){ const b = $('.tbl'), y = b.scrollTop, x = b.scrollLeft; fn();
  if(b.scrollTop !== y) b.scrollTop = y; if(b.scrollLeft !== x) b.scrollLeft = x; }
// бросить текущий запрос: его ответ, если всё же придёт, — мимо (reqN), кнопка снова жива
function drop(){ reqN++; busy = false; manual = false; const a = abort; abort = null; if(a) try { a(); } catch(e) {} }
async function refresh(byHand){
  // один запрос за раз: плановый опрос ждёт следующего срока, ⟳ присоединяется к идущему запросу (крутится, второго нет)
  if(busy){ if(!byHand) schedule(); else if(!manual){ manual = true; renderFresh(); } return; }
  if(!byHand && document.hidden){ schedule(); return; }                          // скрытая вкладка не тянет
  const my = ++reqN; busy = true; manual = !!byHand; lastStart = Date.now(); schedule(); renderFresh();
  // зависший запрос (туннель встал) не должен навсегда запереть кнопку: через 30 с бросаем его, ответ опоздавшего — мимо
  const ctl = typeof AbortController === 'function' ? new AbortController() : null;
  abort = ctl ? () => ctl.abort() : null;
  const kill = setTimeout(() => { if(my !== reqN) return; drop(); fetchErr = `нет ответа ${FETCH_MS/1000} с · ${hms(new Date())}`; renderFresh(); }, FETCH_MS);
  let next = null, err = '', date = null, got = 0;
  try {
    const r = await fetch('/data.json', ctl ? {cache:'no-store', signal:ctl.signal} : {cache:'no-store'});
    date = r.headers && r.headers.get ? r.headers.get('Date') : null; got = Date.now();
    if(!r.ok) err = 'HTTP ' + r.status;
    else { next = await r.json(); if(!next || typeof next !== 'object' || !Array.isArray(next.sf_rows)){ next = null; err = 'ответ не похож на таблицу'; } }
  } catch(e) { err = (e && e.message) || String(e); }
  clearTimeout(kill);
  if(my !== reqN) return;
  busy = false; manual = false; abort = null;
  // часы сервера — только по ответу, который и есть наша таблица (ревью 12.09): Date страницы входа в Wi-Fi или прокси —
  // чужие часы, а наибольшая из шести поправок держала бы чужую минуту и помнила её между визитами
  if(next) noteDate(date, got);
  if(next){
    const prev = T; T = next;
    keepScroll(safeRender);
    // таблица не отрисовалась — остаётся прежняя, ошибка страницы в строке состояния, неудача — на кнопке
    if(pageErr){ T = prev; const pe = pageErr; keepScroll(safeRender); pageErr = pe; renderStatus(srvNow()); err = 'таблица не отрисовалась: ' + pe; }
  }
  fetchErr = err ? err + ' · ' + hms(new Date()) : '';
  renderFresh();
}
$('#reload').onclick = () => refresh(true);
// вернулись на вкладку (телефон разблокирован) — таймер сразу, свежие данные сразу, если плановый опрос просрочен. Запрос,
// начатый до ухода, после сна телефона обычно мёртв: ревью 12.09 — раньше новый не шёл, пока старый не сбросится по 30 с,
// и до следующего опроса висело красное «нет ответа»; теперь старый бросается и спрашиваем заново
function onShow(){
  if(document.hidden) return;
  if(busy && Date.now() - lastStart >= POLL_MS) drop();
  if(!busy && Date.now() - lastStart >= POLL_MS) refresh(false); else renderFresh();
}
if(document.addEventListener) document.addEventListener('visibilitychange', onShow);
document.querySelectorAll('.seg button').forEach(b => b.onclick = () => { if(ui.mode !== b.dataset.m){ ui.mode = b.dataset.m; save(); safeRender(); } });
$('#rows').onclick = e => { const t = e.target && e.target.closest ? e.target.closest('.sp') : null; if(t) toggle(t.dataset.b); };
{ const el = $('#hideMis'); el.checked = !!ui.hideMis; el.onchange = () => { ui.hideMis = el.checked; save(); safeRender(); }; }
for(const id of ['min1d','q']){ const el = $('#'+id); el.value = ui[id] || ''; el.oninput = () => { ui[id] = el.value; save(); safeRender(); }; }
for(const id of Object.keys(PICK)){
  $('#' + id + 'Btn').onclick = e => { e.stopPropagation(); const m = $('#' + id + 'Menu'), open = m.hidden;
    for(const o of Object.keys(PICK)) $('#' + o + 'Menu').hidden = true; m.hidden = !open; };
  $('#' + id + 'Menu').onclick = e => e.stopPropagation();           // клик по галочке не закрывает список
  $('#' + id + 'Menu').onchange = e => { const x = e.target && e.target.dataset ? e.target.dataset.x : null; if(x) setPick(id, x, e.target.checked); };
  renderPick(id);
}
if(document.addEventListener) document.addEventListener('click', () => { for(const o of Object.keys(PICK)) $('#' + o + 'Menu').hidden = true; });
lastStart = Date.now();                // вшитые данные — как только что полученные: показ вкладки сразу после открытия не тянет их второй раз
safeRender(); renderFresh(); schedule();
setInterval(() => { if(!document.hidden) renderFresh(); }, 1000);                  // таймер свежести тикает раз в секунду
setInterval(() => { if(!document.hidden) renderStatus(srvNow()); }, 15000);
"""


def _options(values, labels: dict | None = None) -> str:
    labels = labels or {}
    return "".join(f'<option value="{html.escape(v)}">{html.escape(labels.get(v, v))}</option>' for v in values)


# биржи в выборе (владелец 12.09, как у чужого скринера): подпись, фон и цвет значка, буква. Картинок-логотипов нет —
# страница не ходит на чужие адреса; незнакомая биржа получает серый значок со своей буквой.
BRANDS = {"aster": ("Aster", "#e8a54b", "#fff", "A"), "binance": ("Binance", "#f0b90b", "#1b1b1b", "B"),
          "bitget": ("Bitget", "#1da2b4", "#fff", "B"), "gate": ("Gate", "#2354e6", "#fff", "G"),
          "hyperliquid": ("Hyperliquid", "#50d2c1", "#0b2b26", "H"), "kucoin": ("KuCoin", "#23af91", "#fff", "K"),
          "okx": ("OKX DEX", "#3a3f45", "#fff", "O"),
          # 12.09: Lighter — два инстанса, основной и Robinhood Chain; спот и перп каждого включаются отдельно
          "lighter": ("Lighter", "#16181d", "#fff", "L"), "lighter·rh": ("Lighter Robinhood", "#00c805", "#0b2b00", "R"),
          # 13.09: новые перп-биржи; цвета — догадка по фирменным (владелец поправит), подписи — как пишут сами биржи
          "backpack": ("Backpack", "#e33e3f", "#fff", "B"), "variational": ("Variational", "#5b4bff", "#fff", "V"),
          "edgex": ("edgeX", "#c8f53c", "#101010", "E"), "extended": ("Extended", "#0f1a2b", "#fff", "E"),
          "pacifica": ("Pacifica", "#0e7c86", "#fff", "P"), "apex": ("ApeX", "#2f5bff", "#fff", "X")}


def pickers(venues, spots) -> tuple[list, list]:
    """Списки для галочек: споты и фьючерсы по алфавиту (как у чужого скринера), у каждой площадки — значок её биржи."""
    def item(v):
        b = config.LABELS.get(v, v)
        return [v, *BRANDS.get(b, (b.capitalize(), "#888", "#fff", b[:1].upper()))]
    by_label = lambda lst: sorted((item(v) for v in lst), key=lambda x: x[1].lower())
    return by_label(spots), by_label(venues)


def page_script(table: dict) -> str:
    """Скрипт страницы с вшитыми данными — отдельно, чтобы тест мог прогнать его в JS-движке."""
    windows = [str(w) for w in (table.get("windows") or config.WINDOWS_H)]
    labels = table.get("window_labels") or {w: config.WINDOW_LABELS.get(int(w), f"{w} ч") for w in windows}
    spots, perps = pickers(table.get("venues") or config.PERP_VENUES, table.get("spot_venues") or config.SPOT_VENUES)
    return JS % dict(windows=json.dumps(windows), labels=json.dumps(labels, ensure_ascii=False), stale=config.STALE_S,
                     spots=json.dumps(spots), perps=json.dumps(perps), gap_hide=json.dumps(float(config.DASH_GAP_HIDE_PCT)),
                     table=json.dumps(table, separators=(",", ":")).replace("</", "<\\/"))


def render(table: dict) -> str:
    return f"""<!doctype html><html lang="ru"><head><meta charset="utf-8"><title>funding_bot</title>
<meta name="viewport" content="width=device-width,initial-scale=1"><style>{CSS}</style></head><body><div class="wrap">
<div class="top"><h1>funding_bot</h1>
 <div class="tools">
  <span class="fr"><span id="fresh" class="fresh"></span><button id="reload" class="rl" type="button" title="обновить сейчас" aria-label="обновить"><span>⟳</span></button></span>
  <div class="seg" role="tablist"><button data-m="sf">spot/futures</button><button data-m="ff">futures/futures</button></div>
 </div>
 <a class="cab" href="/cabinet">Кабинет</a></div>
<div class="bar" id="status"></div>
<div class="ctl">
 <span class="dd sf-only">спот <button id="spBtn" type="button">все ⌄</button><div class="menu" id="spMenu" hidden></div></span>
 <span class="dd">фьючерсы <button id="ppBtn" type="button">все ⌄</button><div class="menu" id="ppMenu" hidden></div></span>
 <label><input type="checkbox" id="hideMis"> скрыть «не тот актив»<span id="misN"></span></label>
 <label><span id="minLbl">1d &gt;</span> <input type="number" id="min1d" step="0.1" placeholder="%"></label>
 <label><input type="text" id="q" placeholder="монета"></label>
 <span id="shown" class="m"></span>
</div>
<div class="tbl"><table><thead id="head"></thead><tbody id="rows"></tbody></table></div>
</div><script>{page_script(table)}</script></body></html>"""
