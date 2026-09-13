"""OKX DEX — спот-нога на DEX (владелец 12.09: «в спот добавить okx dex»; «почему для этих монет нет okx dex? надо сделать»).

Что и откуда:
- токены — только доказанные составом индекса перпа и контрактами (identity.dex_tokens): контракт монеты на BSC, Solana
  или Robinhood Chain, который перечисляют наши споты для той же монеты; токен Binance Alpha из индекса; пул DEX из
  индекса (Aster AI, MEME — токены сети Robinhood). Поиска по тикеру нет;
- строка — токен × КАЖДЫЙ перп той же монеты на наших площадках (12.09, ANSEM): кроме перпов «не тот» против токена;
  связь без доказательств — «?» (identity.dex_links). Токены те же — котировок OKX не больше, строк больше;
- ident_ev=contract своей строки — не доказательство, а метка «свой токен перпа» (её читает трейдер); сила — rel (рейтинг
  A/B/C, владелец 13.09): класс прямой записи индекса с этим контрактом (identity.dex_token_rel) — AIW3 (пул по тикеру) C;
- только перпы наших перп-бирж (config.PERP_VENUES — список расширяет владелец), только сети владельца
  (config.OKX_DEX_CHAINS); строка появляется при ликвидности токена от config.OKX_DEX_MIN_LIQUIDITY_USD ($50k, порог
  владельца);
- цена — пачкой POST /market/price раз в OKX_DEX_JOB_S (Market API: 100K бесплатных вызовов в месяц); ликвидность и имя —
  раз в час (/market/price-info, /market/token/basic-info);
- котировки на клип $OKX_DEX_QUOTE_USD в обе стороны (/aggregator/quote) по очереди, самые старые первыми, не дольше
  OKX_DEX_QUOTE_BUDGET_S за задание (триальный ключ — 1 запрос/с). Из них «Комиссия» DEX-строки — всё включено (владелец):
  круг по цене (пул + удар в обе стороны) + газ входа и выхода + налог токена + 2 × тейкер перпа;
- мост (владелец: «да, с пометкой «мост»»): родная монета другой сети (BTC → BTCB на BSC) или имя токена с признаком моста
  (Binance-Peg, Wormhole, Portal…); в подсказке — обычный сдвиг цены к перпу (скользящее среднее курсового).
Замерено 12.09 с ireland ключом владельца: у агрегатора 35 сетей (56, 501, 4663 есть); ESPORTS на BSC: ликвидность
$116k, круг на $15 — 0.39 %, газ $0.031 + $0.014; 龙虾: $3.2M, 0.30 %.
Сеть — только в job() (работник пула); состояние меняет apply() на главном потоке.
"""
from __future__ import annotations
import re, time, logging
from . import config, identity
from .client import BannedError, PermanentHTTPError
from .okxdex import NoLiquidity, Unsupported

log = logging.getLogger(__name__)

VENUE = "okxdex"
SLUG = {"56": "bsc", "501": "solana", "4663": "robinhood"}      # страница токена web3.okx.com/token/<сеть>/<адрес>
TAG = {"56": "bsc", "501": "sol", "4663": "rh"}                  # подпись строки: «okx·bsc»
# только явные признаки мостовой копии; голое имя проекта — не признак (ревью 12.09: W — родной токен Wormhole на Solana,
# ZRO — LayerZero на BSC); мост по родной сети монеты (BTC → BTCB) решает identity.dex_tokens
_BRIDGE_NAME = re.compile(r"binance-peg|\bbridged\b|\((pos|wormhole|portal|axelar|multichain|layerzero|allbridge|celer)\)"
                          r"|\bpeg(ged)?\b", re.I)
_FINAL_ERR = {"нет ликвидности на клип", "unsupported", "honeypot"}    # ответ по существу, а не сбой — котировку заменяет


def _tok(spot_sym: str) -> tuple[str, str]:
    c, a = spot_sym.split(":", 1)
    return c, a


class DexLeg:
    def __init__(self, client=None):
        ok = client is not None and (not hasattr(client, "enabled") or client.enabled())
        self.cl = client if ok else None
        self.cand: dict[tuple[str, str], list[tuple[str, str, bool]]] = {}   # перп → [(сеть, адрес, мост)]
        self.own_rel: dict[tuple[str, str], dict[tuple[str, str], tuple]] = {}   # перп → {свой токен → рейтинг}
        self.px: dict[tuple[str, str], tuple[float, float]] = {}             # токен → (цена USD, время наблюдения, с)
        self.info: dict[tuple[str, str], dict] = {}                          # токен → {liq, name, symbol, t, seen}
        self.q: dict[tuple[str, str], dict] = {}                             # токен → котировка на клип
        self.info_ts = 0.0
        self.ema: dict[str, tuple[float, float]] = {}                        # сделка → (обычный курсовой, obs)

    @property
    def enabled(self) -> bool:
        return self.cl is not None

    def tokens(self) -> list[tuple[str, str]]:
        return sorted({(c, a) for lst in self.cand.values() for c, a, *_ in lst})

    # --- вселенная (главный поток, после вердиктов identity) ------------------------------------------------
    def set_candidates(self, R, instruments: dict[str, dict[str, dict]]):
        """Перп → [(сеть, адрес, мост, вердикт | None)]. Токен — только доказанный индексом хоть одного перпа монеты
        (identity.dex_tokens); предлагается он ВСЕМ перпам той же монеты (класс crypto, та же база во вселенной) на наших
        площадках (владелец 12.09: «не вижу пары с монетой ANSEM» — токен доказан на Aster и Gate, а HIP-3 para:ANSEM и
        Lighter строк не получали). Свой токен перпа — вердикт None (same, как раньше); чужой — identity.dex_links:
        «не тот» — строки нет, «не проверено» — строка с «?». Набор токенов (и запросов OKX) от этого не меняется."""
        own, group, own_rel = {}, {}, {}
        for v in config.PERP_VENUES:
            for sym, i in (instruments.get(v) or {}).items():
                if (i.get("cls") or "crypto") != "crypto":
                    continue
                group.setdefault(i.get("base") or sym, []).append((v, sym))
                # стейбл, за который котируется клип, — не кандидат (перп USDC не покупает USDC за USDC)
                toks = [t for t in identity.dex_tokens(R, v, sym)
                        if t[0] in SLUG and t[1] != config.OKX_DEX_STABLES[t[0]][0]]
                if toks:
                    own[(v, sym)] = toks
                    # рейтинг своих токенов — здесь, до dex_links: её падение не лишает свои строки буквы
                    rel = identity.dex_token_rel(R, v, sym)
                    own_rel[(v, sym)] = {t[:2]: rel.get(t[:2]) for t in toks}
        cand = {}
        for base, perps in group.items():
            memo = {}                           # пары перп/перп группы — общий кэш (вердикт пары симметричен)
            src, br = {}, {}                    # токен → перпы-источники; мост — признак токена, от любого источника
            for p in perps:
                for c, a, b in own.get(p, ()):
                    src.setdefault((c, a), []).append(p)
                    br[(c, a)] = br.get((c, a), False) or b
            for p in perps:
                mine = {(c, a) for c, a, _b in own.get(p, ())}
                links = {t: None for t in mine}
                if len(mine) < len(src):
                    try:
                        links = identity.dex_links(R, p[0], p[1], src, mine, group=perps, base=base, memo=memo)
                    except Exception as e:  # noqa — кривая запись одной монеты: только свои токены, вердикты прочих живы
                        log.warning("okxdex связь токенов %s:%s: %s: %s", p[0], p[1], type(e).__name__, e)
                if links:
                    cand[p] = [(c, a, br[(c, a)], d) for (c, a), d in sorted(links.items())]
        self.cand, self.own_rel = cand, own_rel

    def liquid(self, tok) -> bool:
        q = self.q.get(tok) or {}
        return ((self.info.get(tok) or {}).get("liq") or 0) >= config.OKX_DEX_MIN_LIQUIDITY_USD \
            and not q.get("honeypot") and q.get("err") != "unsupported"

    def plan(self, now: float) -> dict:
        toks = self.tokens()
        full = now - self.info_ts >= config.OKX_DEX_INFO_S
        info = toks if full else [t for t in toks if t not in self.info]
        prices = [t for t in toks if t not in self.info or self.liquid(t)]     # тонкие токены цены не требуют
        quotes = sorted((t for t in toks if self.liquid(t)), key=lambda t: (self.q.get(t) or {}).get("t", 0))
        return dict(prices=prices, info=info, full_info=full, quotes=quotes)

    # --- сеть (работник пула) ----------------------------------------------------------------------------------
    def job(self, plan: dict) -> dict:
        cl = self.cl
        out = dict(t=time.time(), px={}, info={}, q={}, full_info=plan.get("full_info"), err=None)
        try:
            if plan["info"]:
                inf = cl.price_info(plan["info"])
                try:
                    names = cl.basic_info(plan["info"])
                except Exception as e:  # noqa — без имён строка подписана адресом, мост — только по родной сети
                    log.warning("okxdex basic-info: %s: %s", type(e).__name__, e)
                    names = {}
                for tok in plan["info"]:
                    r, n = inf.get(tok), names.get(tok) or {}
                    out["info"][tok] = dict(liq=(r or {}).get("liq", 0.0), name=n.get("name"), symbol=n.get("symbol"),
                                            t=time.time(), seen=r is not None)
            if plan["prices"]:
                for tok, (p, ms) in cl.market_prices(plan["prices"]).items():
                    out["px"][tok] = (p, min(ms / 1000.0, time.time()) if ms else time.time())
        except (BannedError, PermanentHTTPError) as e:       # 429, квота (402), ключ (401): до следующего задания
            out["err"] = e
            return out
        # токены, ликвидность которых узнали в этом же задании, котируются сразу — иначе первые котировки ждали бы
        # следующего задания (OKX_DEX_JOB_S)
        fresh = [t for t in plan["info"] if t not in plan["quotes"]
                 and (out["info"].get(t) or {}).get("liq", 0) >= config.OKX_DEX_MIN_LIQUIDITY_USD]
        t_end = time.time() + config.OKX_DEX_QUOTE_BUDGET_S
        for tok in fresh + list(plan["quotes"]):
            if time.time() > t_end:
                break
            try:
                out["q"][tok] = self._quote(tok)
            except (BannedError, PermanentHTTPError) as e:
                out["err"] = e
                break
            except NoLiquidity:
                out["q"][tok] = dict(t=time.time(), err="нет ликвидности на клип")
            except Unsupported:
                out["q"][tok] = dict(t=time.time(), err="unsupported")
            except Exception as e:  # noqa — сбой одной котировки: повтор в следующем задании
                log.warning("okxdex котировка %s: %s: %s", tok, type(e).__name__, e)
                out["q"][tok] = dict(t=time.time(), err=type(e).__name__)
        return out

    def _quote(self, tok) -> dict:
        """Клип в обе стороны: купить токен на стейбл, продать купленное обратно. Круг по цене = 1 − вернулось/клип:
        пул и удар в обе стороны (агрегатор своей комиссии не берёт; на триальном ключе оставляет себе положительное
        проскальзывание — исполнение не лучше котировки)."""
        c, a = tok
        stable, sdec = config.OKX_DEX_STABLES[c]
        clip = float(config.OKX_DEX_QUOTE_USD)
        b = self.cl.quote(c, stable, a, int(clip * 10 ** sdec))
        if b["honeypot"]:
            return dict(t=time.time(), err="honeypot", honeypot=True)
        qty = b["to_amount"]
        if not qty:
            return dict(t=time.time(), err="нет ликвидности на клип")
        s = self.cl.quote(c, a, stable, qty)
        units = qty / 10 ** (b["to_decimals"] or 0)
        back = s["to_amount"] / 10 ** (s["to_decimals"] or sdec)
        return dict(t=time.time(), buy_px=clip / units, sell_px=back / units, rt=1.0 - back / clip,
                    gas=(b["gas_usd"] or 0.0) + (s["gas_usd"] or 0.0), tax=(b["buy_tax"] or 0.0) + (s["sell_tax"] or 0.0),
                    impact=b["price_impact"], honeypot=bool(s["honeypot"]), err=None)

    # --- главный поток ------------------------------------------------------------------------------------------
    def apply(self, res: dict):
        self.info.update(res["info"])
        if res.get("full_info") and res["info"]:
            self.info_ts = res["t"]
        self.px.update(res["px"])
        for tok, q in res["q"].items():
            old = self.q.get(tok)
            if q.get("err") and q["err"] not in _FINAL_ERR:
                # ревью 12.09: сбой (таймаут, 5xx) не стирает хорошую котировку и не отправляет токен в конец очереди
                self.q[tok] = dict(old, last_err=q["err"]) if old and not old.get("err") else dict(q, t=0.0)
            else:
                self.q[tok] = q
        keep = set(self.tokens())
        for d in (self.px, self.info, self.q):
            for k in [k for k in d if k not in keep]:
                del d[k]
        if res.get("err"):
            raise res["err"]

    def rows(self, instruments: dict[str, dict[str, dict]]) -> list[dict]:
        """Сделки «спот DEX (лонг) + перп (шорт)»: токен × перп, для которого он доказан, при ликвидности от порога."""
        out = []
        for (v, sym), toks in sorted(self.cand.items()):
            i = (instruments.get(v) or {}).get(sym)
            if not i:
                continue
            for c, a, bridged, d in toks:
                if not self.liquid((c, a)):
                    continue
                # свой токен перпа — «тот» по контракту (как раньше); токен другого перпа монеты — вердикт связи (dex_links).
                # Рейтинг своего — класс прямой записи индекса (own_rel), связанного — из вердикта связи
                g = ((d.get("rel"), d.get("rel_ev"), d.get("rel_d")) if d else
                     (self.own_rel.get((v, sym)) or {}).get((c, a)) or (None, None, None))
                d = d or dict(ident="same", ident_ev="contract", ident_why=None)
                inf = self.info.get((c, a)) or {}
                label = inf.get("symbol") or inf.get("name")
                br = bridged or bool(_BRIDGE_NAME.search(inf.get("name") or ""))
                # символ токена другой, чем у монеты (BTCB у BTC) — в подписи; «$WIF» у WIF — тот же
                norm = lambda s: re.sub(r"[^A-Z0-9]", "", str(s).upper())
                tag = TAG[c] + (f"·{label}" if label and norm(label) != norm(i["base"]) else "")
                out.append(dict(key=f"{VENUE}:{c}:{a}|{v}:{sym}", base=i["base"], cls="crypto", spot_ex=VENUE,
                                spot=f"{c}:{a}", spot_asset=label or a[:10], spot_factor=1.0, spot_fee=None, spot_tag=tag,
                                perp_ex=v, perp=sym, perp_factor=i["factor"], dex=dict(chain=c, addr=a, bridged=br),
                                ident=d["ident"], ident_ev=d["ident_ev"], ident_why=d["ident_why"], mismatch=False,
                                xfer=None, rel=g[0], rel_ev=g[1], rel_d=g[2]))
        return out

    def book(self, spot_sym: str) -> dict | None:
        """Цена DEX как книга без спреда: курсовой — к ней; исполнимость клипа — в «Комиссии» (котировки)."""
        p = self.px.get(_tok(spot_sym))
        if not p or not p[0] or p[0] <= 0:
            return None
        return dict(bid=p[0], ask=p[0], bid_qty=0.0, ask_qty=0.0, obs=p[1])

    def extra(self, item: dict, now_s: float) -> dict:
        """Для calc: издержки DEX-ноги (всё включено) и подсказка строки."""
        tok = (item["dex"]["chain"], item["dex"]["addr"])
        q, inf = self.q.get(tok) or {}, self.info.get(tok) or {}
        age = now_s - q["t"] if q.get("t") else None
        ok = bool(q) and not q.get("err") and age is not None and age <= config.OKX_DEX_QUOTE_MAX_AGE_S
        clip = float(config.OKX_DEX_QUOTE_USD)
        e = self.ema.get(item["key"])
        return dict(cost=(q["rt"] + q["gas"] / clip + q["tax"]) if ok else None,
                    url=f"https://web3.okx.com/token/{SLUG[tok[0]]}/{tok[1]}",
                    tip=dict(chain=SLUG[tok[0]], name=inf.get("name"), liq=inf.get("liq"), clip=clip,
                             rt=q.get("rt") if ok else None, gas=q["gas"] / clip if ok else None,
                             tax=q.get("tax") if ok else None, impact=q.get("impact") if ok else None,
                             q_age=age, err=q.get("err"), bridged=item["dex"]["bridged"], usual=e[0] if e else None))

    # --- состояние между рестартами: котировки клипа собираются ~40 мин полным кругом — рестарт (выкат) не должен
    # обнулять «Комиссию» всех DEX-строк (владелец 12.09: «сделай чтоб не падал») ------------------------------------
    def snapshot(self) -> dict:
        k = lambda tok: f"{tok[0]}:{tok[1]}"
        return dict(info={k(t): v for t, v in self.info.items()}, q={k(t): v for t, v in self.q.items()},
                    px={k(t): list(v) for t, v in self.px.items()}, info_ts=self.info_ts,
                    ema={key: list(v) for key, v in self.ema.items()})

    def restore(self, snap: dict):
        """Прежние котировки живут до своего срока (OKX_DEX_QUOTE_MAX_AGE_S), цены — до OKX_DEX_STALE_S; дальше их сменит
        очередное задание. Кривой снимок — не повод падать: начинаем с пустого."""
        try:
            self.info = {_tok(s): v for s, v in (snap.get("info") or {}).items()}
            self.q = {_tok(s): v for s, v in (snap.get("q") or {}).items()}
            self.px = {_tok(s): (float(v[0]), float(v[1])) for s, v in (snap.get("px") or {}).items()}
            self.ema = {key: (float(v[0]), float(v[1])) for key, v in (snap.get("ema") or {}).items()}
            self.info_ts = float(snap.get("info_ts") or 0.0)
        except Exception as e:  # noqa
            log.warning("okxdex: сохранённое состояние не прочитано (%s: %s) — собираю заново", type(e).__name__, e)
            self.info, self.q, self.px, self.ema, self.info_ts = {}, {}, {}, {}, 0.0

    def track(self, key: str, gap: float | None, obs: float | None):
        """Обычный курсовой сделки: скользящее среднее по новым ценам DEX (сдвиг мостового токена не выдать за базис)."""
        if gap is None or obs is None:
            return
        e = self.ema.get(key)
        if e and e[1] == obs:
            return
        self.ema[key] = (gap if not e else e[0] + 0.05 * (gap - e[0]), obs)
