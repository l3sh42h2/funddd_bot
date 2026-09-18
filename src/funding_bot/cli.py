from __future__ import annotations
import argparse, logging, signal, sys, json
from . import config


def main(argv=None):
    ap = argparse.ArgumentParser(prog="funding_bot")
    ap.add_argument("-v", "--verbose", action="store_true")
    sub = ap.add_subparsers(dest="cmd", required=True)
    c = sub.add_parser("collector", help="демон: тики, история, снимки, table.json")
    c.add_argument("--once", action="store_true", help="один проход без бэкфилла (смоук)")
    s = sub.add_parser("serve", help="дашборд по HTTP (localhost; с Мака через ssh-туннель)")
    s.add_argument("--port", type=int, default=config.WEB_PORT); s.add_argument("--host", default=config.WEB_HOST)
    sub.add_parser("universe", help="собрать вселенную и напечатать сводку (без демона)")
    b = sub.add_parser("backfill", help="разовый бэкфилл истории в текущем процессе")
    b.add_argument("--days", type=int, default=config.HISTORY_DAYS)
    au = sub.add_parser("audit", help="тестировщик: сверка дашборда с истиной биржи (запуск по команде владельца)")
    aug = au.add_mutually_exclusive_group(required=True)
    aug.add_argument("--venue", choices=list(config.PERP_VENUES))
    aug.add_argument("--all", action="store_true", help="все площадки разом + сводка runtime/audit/ALL_<время>.md")
    au.add_argument("--top", type=int, default=20, help="сколько рынков сверху и снизу по фандингу проверять по истории")
    au.add_argument("--sample", type=int, default=10, help="плюс случайных рынков для проверки истории")
    au.add_argument("--days", type=int, default=7, help="глубина сверки истории, сутки")
    t = sub.add_parser("top", help="напечатать верх таблицы из table.json")
    t.add_argument("-n", type=int, default=25)
    t.add_argument("--mode", default="sf", choices=["sf", "ff"], help="sf = spot/futures, ff = futures/futures")
    t.add_argument("--by", default="spread", choices=["spread", "24", "72", "720", "gap"])
    sub.add_parser("trader", help="фаза 2: Telegram-бот владельца + исполнитель (сделки только по его команде)")
    sub.add_parser("core", help="headless торговое ядро")
    ui = sub.add_parser("interface", help="Telegram и dashboard через IPC ядра")
    ui.add_argument("--port", type=int, default=config.WEB_PORT)
    ui.add_argument("--host", default=config.WEB_HOST)
    tc = sub.add_parser("trade-check", help="фаза 2: проверки без отправок — owner.toml, ключи, подписанные чтения")
    tc.add_argument("--mode", default="readonly", choices=["dry", "readonly"],
                    help="не выше режима owner.toml; live не нужен — проверки ничего не отправляют")
    tc.add_argument("--symbol", default="AIW3USDT", help="символ для комиссии и leverageBracket")
    pl = sub.add_parser("plan", help="фаза 2: план входа в симуляции на живых публичных данных (без ключей и записи)")
    pl.add_argument("coin")
    pl.add_argument("spot", help="okx·bsc")
    pl.add_argument("perp", help="aster")
    pl.add_argument("usd", help="сумма на ногу, USDT")
    sub.add_parser("cabinet-hash", help="хэш пароля личного кабинета: пароль из stdin (с терминала — без эха) → "
                                        "строка CABINET_PASS_HASH=… для runtime/cabinet.env")
    # связка SOL×HL, только чтение; свои ключи команды разбирает sol_doctor (импорт — только при вызове)
    sh = sub.add_parser("sol-hl", help="связка SOL×HL, только чтение: doctor | quote-compare | hl-preflight | record")
    sh.add_argument("sol_cmd", choices=["doctor", "quote-compare", "hl-preflight", "record"])
    sh.add_argument("sol_args", nargs=argparse.REMAINDER, help="ключи команды: funding_bot sol-hl doctor --help")
    sub.add_parser("manual-poll", help="разовый опрос ручных позиций владельца (runtime/manual_positions.toml) → "
                                       "runtime/manual_positions_live.json; крон/таймер раз в минуту, только чтение "
                                       "публичных API, trade.db не трогает")
    a = ap.parse_args(argv)
    logging.basicConfig(level=logging.DEBUG if a.verbose else logging.INFO,
                        format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    if a.cmd == "collector":
        from .collector import Collector
        col = Collector()
        if a.once:
            tbl = col.once()
            print(json.dumps({k: v for k, v in tbl.items() if k not in ("ff_rows", "sf_rows")}, ensure_ascii=False, indent=1))
            print("ff_rows:", len(tbl["ff_rows"]), "sf_rows:", len(tbl["sf_rows"]))
            return 0
        signal.signal(signal.SIGTERM, lambda *_: col.stop())
        col.run()
    elif a.cmd == "serve":
        from .serve import serve
        serve(a.port, a.host)
    elif a.cmd == "universe":
        from .collector import Collector
        from collections import Counter
        col = Collector(); col.step_universe(timeout=120); col.step_tick()
        other = lambda items: [(it["key"], it.get("ident_why")) for it in items if it.get("mismatch")]
        by_pair = dict(Counter(i["va"] + "|" + i["vb"] for i in col.ff))      # без f-строк в f-строке: на VPS Python 3.11
        print(f"перп/перп: {len(col.ff)} {by_pair}, не тот актив: {other(col.ff)}")
        print(f"спот/перп: {len(col.sf)} {dict(Counter(i['perp_ex'] for i in col.sf))}, не тот актив: {other(col.sf)}")
        print("вердикты (по составу индекса и контрактам, не по цене):", col.ident_summary)
        print("ног с историей:", len(col.legs()), "заметки:", col.notes)
        for ex in col.venues:
            print(ex, "интервалы:", dict(Counter(i.get("interval_h") for i in col.instruments[ex].values())))
    elif a.cmd == "backfill":
        from .collector import Collector
        from . import funding
        col = Collector(); col.step_universe(timeout=120)
        st = funding.backfill(col.con, col.clients, col.legs(), col.intervals(), a.days,
                              progress=lambda i, n, s: (i % 50 == 0) and print(f"{i}/{n} {s}", file=sys.stderr))
        print(st)
    elif a.cmd == "audit":
        from .audit import run, run_all
        path, errors = run_all(a.top, a.sample, a.days) if a.all else run(a.venue, a.top, a.sample, a.days)
        print(path.read_text())
        print(f"\nотчёт: {path}\nданные: {path.with_suffix('.json')}")
        return 1 if errors else 0                  # 0 — ошибок нет, 1 — есть
    elif a.cmd == "top":
        from .serve import load_table
        tbl = load_table()
        # со знаком, как в дашборде (владелец 10.09: «позитив → негатив»); пустые — в конце
        val = lambda x: x if x is not None else float("-inf")
        wk = lambda w: (lambda r: val(r["windows"][w]["spread"]))
        key = {"spread": lambda r: val(r["spread"]), "24": wk("24"), "72": wk("72"), "720": wk("720"),
               "gap": lambda r: val(r["gap"])}[a.by]
        src = tbl.get(f"{a.mode}_rows") or []
        rows = sorted([r for r in src if not r["mismatch"]], key=key, reverse=True)[:a.n]
        print(f"ts={tbl.get('ts')} {a.mode}: строк {len(src)}, не тот актив {tbl.get('n_mismatch_' + a.mode)}, "
              f"неполных {tbl.get('n_incomplete_' + a.mode)}")
        f = lambda x, d=4: "—" if x is None else f"{x*100:+.{d}f}%"
        for r in rows:
            wins = "  ".join(f"{tbl['window_labels'][w]} {f(r['windows'][w]['spread'], 3)}{'!' if r['windows'][w]['incomplete'] else ''}"
                             for w in ("24", "72", "168", "720") if w in r["windows"])
            if a.mode == "ff":
                per = f"{r['period']}" if r["iv_a"] == r["iv_b"] else f"{r['period']}({r['iv_a']}|{r['iv_b']})"
                head = (f"{r['base']:<10} {r['va'] + '|' + r['vb']:<20} {f(r['rate_a'])}|{f(r['rate_b'])}  "
                        f"текущий {f(r['spread'])}/{per}ч")
            else:
                head = f"{r['base']:<12} {r.get('spot_label') or 'спот'}|{r['perp_ex']:<11} текущий {f(r['spread'])}/{r['period']}ч"
            print(f"{head}  курс {f(r['gap'], 2)}  {wins}")          # «Отклонения» нет (владелец 12.09)
    elif a.cmd == "core":
        from .core.bootstrap import run_core
        return run_core()
    elif a.cmd == "interface":
        from .interface.runtime import run_interface
        return run_interface(a.port, a.host)
    elif a.cmd == "trader":
        from .tg.bot import run_trader
        return run_trader()
    elif a.cmd == "trade-check":
        # подписанные GET агента Aster: не запускать одновременно с идущим исполнением (nonce агента — один клиент)
        from .trade.reconcile import check_report
        code, text = check_report(a.mode, a.symbol)
        print(text)
        return code
    elif a.cmd == "cabinet-hash":
        # пароль не уходит ни в аргументы (история shell, ps), ни в журнал — только stdin
        from .cabinet import cli_hash
        return cli_hash()
    elif a.cmd == "plan":
        from .operator_commands import PERP_ALIASES, parse_amount, parse_coin, parse_spot
        from .tg.sender import to_plain
        from .trade.engine import Refused, plan_cli
        coin, spot, perp, usd = parse_coin(a.coin), parse_spot(a.spot), PERP_ALIASES.get(a.perp.lower()), parse_amount(a.usd)
        if None in (coin, spot, perp, usd):
            print("формат: funding_bot plan AIW3 okx·bsc aster 500", file=sys.stderr)
            return 2
        try:
            print(plan_cli(coin, spot, perp, usd))
        except Refused as e:
            from .interface.presenter import render_execution_notice
            print(to_plain(render_execution_notice(e.topic, e.facts)), file=sys.stderr)
            return 1
    elif a.cmd == "sol-hl":
        from . import sol_doctor
        return sol_doctor.main([a.sol_cmd, *a.sol_args])
    elif a.cmd == "manual-poll":
        from . import manual_positions
        live = manual_positions.poll_once()
        errs = {pid: v["error"] for pid, v in live.items() if v.get("error")}
        if errs:
            print(f"manual-poll: {len(errs)} из {len(live)} с ошибкой: {errs}", file=sys.stderr)
        return 0
    return 0


if __name__ == "__main__":
    sys.exit(main())
