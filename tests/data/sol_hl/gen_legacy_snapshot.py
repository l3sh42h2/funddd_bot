# НЕ ТЕСТ: генератор tests/data/sol_hl/legacy_snapshot.json (поведение parse/owner/tconfig/keys.redact ДО правок
# потока profiles). Запуск вручную на копии дерева с прежними файлами: PYTHONPATH=<копия>/src python gen_legacy_snapshot.py <копия>.
# 13.09 (шаг C1) снимок переснят на коде фазы 1 + рейтинг: разница с первым снимком — только ключи allow_contract_multiplier.
"""Снимок поведения НЕИЗМЕНЁННОЙ копии (до правок потока profiles): parse, owner, tconfig, keys.redact."""
import dataclasses, json, sys, tempfile, os
from decimal import Decimal
from pathlib import Path
W = Path(sys.argv[1])
from funding_bot.tg import parse
from funding_bot.trade import owner, tconfig, keys
assert str(W) in parse.__file__

def cmd_dict(c):
    d = {k: (str(v) if isinstance(v, Decimal) else v) for k, v in dataclasses.asdict(c).items()}
    return {"type": type(c).__name__, "fields": d}

PARSE = [
 "вход AIW3 okx·bsc aster 500", "Вход aiw3 OKX-BSC Aster $500", "вход AIW3 okx.bsc aster 500 usdt", "вход AIW3 okx bsc aster 500$",
 "вход  AIW3   okx/bsc   aster   500,5", "вход AIW3 okx·bsc aster $ 500", "вход AIW3 okx·bsc aster 500usdt", "/вход AIW3 okx_bsc aster 500",
 "вход AIW3 okx•bsc hl 250", "вход AIW3 okxdex:solana aster 10", "вход aiw3 okx·bnb aster 0.5", "вход AIW3 okx dex aster 450",
 "вход AIW3 OKX DEX aster 450", "вход AIW3 okxdex aster 450", "вход AIW3 okx-dex aster $450", "вход AIW3 okx dex bsc aster 450",
 "вход AIW3 okx dex sol aster 450", "вход AIW3 okx dex solana hl 450", "вход ANSEM okx·sol hl 200", "вход ansem okx sol hyperliquid 200",
 "вход ANSEM okx·sol hyperliquid 200", "вход ANSEM okx·solana aster 200 usdt", "вход ANSEM okx·rh aster 1", "вход $ansem okx·sol hl 3",
 "выход AIW3", "выход D7K2 всё", "выход d7k2 все", "Выход D7K2 200", "выход D7K2 200 usdt", "выход D7K2 перп", "выход D7K2 перп всё",
 "выход ANSEM", "выход ansem 200", "выход ANSEM весь", "выход ANSEM all",
 "позиции", "/positions", "/status@FundingBot", "статус", "стоп", "/stop", "СТОП!", "стоп всё немедленно", "/стоп",
 "продолжить", "продолжить E7K2", "Продолжить e7k2", "дохедж D7K2", "откат D7K2", "помощь", "/help", "/start", "/start@FundingBot", "ёжик",
 "", "   ", "привет", "/unknown", "вход", "вход AIW3 okx·bsc aster", "вход AIW3 okx·eth aster 500",
 "вход AIW3 uni·bsc aster 500", "вход AIW3 okx·bsc bybit 500", "вход AIW3 okx·bsc aster 1e3",
 "вход AIW3 okx·bsc aster -500", "вход AIW3 okx·bsc aster 0", "вход AIW3 okx·bsc aster 500 600",
 "вход AIW3 okx·bsc aster 500 000", "вход AIW3 okx·bsc aster 500 600 700", "вход AI-W3 okx·bsc aster 500",
 "вход AIW3 okx·bsc aster 1,000.50", "вход AIW3 okx·bsc·x aster 500", "выход", "выход D7K2 перп 100",
 "выход D7K2 abc", "выход D7K2 100 200", "дохедж", "откат D7K2 E7K2", "позиции все", "статус сейчас", "продолжить a b",
 "вход AIW3 okx·bsc aster 500 usdc", "вход AIW3 okx·bsc hyperliquid 500", "вход AIW3 okx·bsc hl·xyz 500",
 "позиции sol", "позиции hyperliquid·para", "выход ANSEM sol", "выход ANSEM 500 ansem", "выход ANSEM sol 200",
 "вход ANSEM sol-auto hyperliquid·para 200", "вход ANSEM jupiter·sol hyperliquid·para 200", "вход ANSEM okx·sol hyperliquid·para 200",
 "вход para:ANSEM sol-auto hyperliquid 200", "вход ANSEM jupiter·sol aster 200", "вход ANSEM okx·sol hl·para 200 usdc",
]
parse_snap = {t: cmd_dict(parse.parse(t)) for t in PARSE}
helpers = {
 "parse_spot": {s: parse.parse_spot(s) for s in ["okx·bsc", "OKX·BSC", "okx-bsc", "okx:bsc", "okx·rh", "okx", "okxdex", "okx-dex", "OKX·DEX", "dex", "uni·dex", "okx·sol", "okx·solana", "jupiter·sol", "sol-auto", "okx·bnb"]},
 "parse_coin": {s: parse.parse_coin(s) for s in ["aiw3", "$ansem", "para:ANSEM", "kPEPE", "AI-W3", ""]},
 "parse_amount": {s: (str(v) if (v := parse.parse_amount(s)) is not None else None) for s in ["$500", "500,25", "1e3", "0", "", "500usdc", "500usdt", "500$", "1.1234567"]},
}
OWN_OK = [
 None,  # deploy/owner.toml.example
 "[limits]\ndaily_loss_stop_usd = 25.5\n",
 '[exec]\nunhedged_usd_max = "auto"\n',
 '[exec]\nunhedged_usd_max = "auto"\nplan_cost_drift_pct = 0.5\n',
 '[dex]\nslippage_pct = 0.1\nimpact_cap_pct = ""\n[perp.aster]\nmaker_allowed = false\n',
 'mode = "readonly"\n[dex]\nslippage_pct = 2.5\n',
 '[perp.hyperliquid]\nleverage = 1\nmargin_type = "ISOLATED"\nmax_slip_bps = "auto"\n',
 'mode = "live"\n[telegram]\nowner_id = 42\n[wallets]\nbsc = "0xE4Ebf0815d0980E5a03f7D675F86dc5079fB8919"\n',
 "",
]
OWN_BAD = [
 "[dex]\nslipage_pct = 3\n", "[risk]\nx = 1\n", "[perp.bybit]\nleverage = 1\n", "[perp.aster]\nleverage = 1\nlevrage = 2\n",
 "[dex]\nslippage_pct = \"3\"\n", "[dex]\nslippage_pct = true\n", "[dex]\nslippage_pct = 0\n", "[dex]\nslippage_pct = 101\n",
 "[dex]\nslippage_pct = nan\n", "[perp.aster]\nleverage = 1.5\n", "[perp.aster]\ntouch_frac_max = 1.5\n",
 "[perp.aster]\nmargin_type = \"isolated\"\n", "[dex]\nallow_tax_tokens = \"true\"\n", "[exec]\nclip_max_usd = \"авто\"\n",
 "[wallets]\nbsc = \"0xE4Ebf0815d0980E5a03f7D675F86dc5079fB891\"\n", "[wallets]\nbsc = \"0xe4Ebf0815d0980E5a03f7D675F86dc5079fB8919\"\n",
 "[telegram]\nowner_id =\n", "[dex]\nslippage_pct = 1\nclip_slippage_pct = 2\n", "mode = \"prod\"\n",
 "[limits.sol_best_hyperliquid]\nmax_clip_usdc = \"30\"\n", "[profiles]\nx = 1\n", "[perp.hyperliquid]\napi_base = \"https://api.hyperliquid.xyz\"\n",
 "[wallets.sol_hl]\nsolana_address = \"\"\n", "schema_version = 2\n", "[wallets]\nbsc = 5\n", "limits = 5\n",
]
tmp = Path(tempfile.mkdtemp())
own = {"ok": [], "bad": []}
for text in OWN_OK:
    if text is None:
        cfg = owner.load(W / "deploy" / "owner.toml.example")
    else:
        p = tmp / "owner.toml"; p.write_text(text, encoding="utf-8"); cfg = owner.load(p)
    own["ok"].append({"text": text, "values": cfg.frozen()["values"], "mode": cfg.mode,
                      "live_missing": cfg.live_missing("aster"), "live_missing_hl": cfg.live_missing("hyperliquid", "bsc"),
                      "unresolved_auto": cfg.unresolved_auto(), "keys": sorted(cfg.values)})
for text in OWN_BAD:
    p = tmp / "owner.toml"; p.write_text(text, encoding="utf-8")
    try:
        owner.load(p); own["bad"].append({"text": text, "err": None})
    except owner.OwnerConfigError as e:
        own["bad"].append({"text": text, "err": str(e).replace(str(p), "<path>")})
tc = {}
for c in ["bsc", "56", "BSC", " bsc ", "solana", "501", "robinhood", "4663", "sol", "SOL", "eth", "rh", "bnb", "", "solana-mainnet"]:
    try: tc[c] = tconfig.chain_index(c)
    except KeyError as e: tc[c] = "KeyError"
RED = ["https://api.telegram.org/bot123456789:AAHdqTcvCH1vGWJxfSeofSAs0K5PALDsaw-x/getUpdates", "key=" + "ab"*32,
       "0x" + ("ab"*32).upper() + " end", "0x02f8" + "cd"*60, "tx 0x" + "ef"*32, "plain text 123", "addr 0xE4Ebf0815d0980E5a03f7D675F86dc5079fB8919",
       "okx error code=50011 https://web3.okx.com/api/v6/dex/aggregator/quote?chainIndex=56&amount=100", "mint 9cRCn9rGT8V2imeM2BaKs13yhMEais3ruM3rPvTGpump"]
red = {s: {"redact": keys.redact(s), "redact_secrets": keys.redact_secrets(s)} for s in RED}
out = {"parse": parse_snap, "helpers": helpers, "owner": own, "tconfig_chain_index": tc, "redact": red,
       "schema_keys": sorted(owner.SCHEMA)}
(W / "tests" / "data" / "sol_hl" / "legacy_snapshot.json").write_text(json.dumps(out, ensure_ascii=False, indent=1, sort_keys=True), encoding="utf-8")
print("ok", len(parse_snap), len(own["ok"]), len(own["bad"]))
