"""Ручной вход владельца — позиция, открытая ИМ ВРУЧНУЮ вне бота (владелец 18.09: «я вошел руками … пусть в
дашборде запишет этот вход как ручной и считает фандос»). Только чтение публичных API, без ключей и без подписи;
НИЧЕГО не пишет в trade.db — торговое ядро об этой позиции не знает и не управляет ей (сознательно: деньги входа
уже потрачены владельцем вручную, а не через сигнатуру бота, так что семантика trade.db "чья позиция" здесь не
подошла бы — restart_check/reconciliation ядра рассчитаны на то, что каждая открытая сделка была открыта ботом).

Факты (адрес, монета, дата входа) — runtime/manual_positions.toml, отдельно от owner.toml и НЕ в git (репозиторий
публичный — владелец узнал об этом 16.09; адреса кошельков лучше туда не класть). Заполняется вручную на VPS.
Живые числа (сторона/размер/марк/фандинг с начала) — runtime/manual_positions_live.json, обновляет разовый прогон
`funding_bot manual-poll` (крон/systemd-таймер раз в минуту) тем же atomic_json, что коллектор пишет table.json.
deal_views() только читает эти два файла — безопасно звать на каждый запрос страницы кабинета, сеть — только в
poll_once().

Формат manual_positions.toml:
  [[position]]
  id = "ansem-lighter-sol-1"
  coin = "ANSEM"
  opened = 1789700000        # unix ts, владелец называет момент входа приблизительно
  note = "вход руками 18.09: шорт Lighter + спот Solana, закрывающие лимитники Jupiter на выход"
  [position.perp]
  venue = "lighter"          # пока только lighter — единственная площадка с публичным account-API без ключей
  l1_address = "0x..."
  symbol = "ANSEM"           # как называется рынок в ответе Lighter (positions[].symbol)
  [position.spot]
  venue = "solana"
  wallet = "..."
  mint = "..."
"""
from __future__ import annotations
import json, logging, time, tomllib
from decimal import Decimal
from pathlib import Path
import requests
from . import config
from .market_snapshot import atomic_json
from .trade.report import dval

log = logging.getLogger(__name__)

CONFIG_PATH = config.RUNTIME / "manual_positions.toml"
LIVE_PATH = config.RUNTIME / "manual_positions_live.json"
SCHEMA = 1
POLL_TIMEOUT_S = 10.0
LIVE_STALE_S = 5 * 60             # старше (при опросе раз в минуту — 5 пропущенных подряд) — "устарело"
LIGHTER_ACCOUNT_URL = "https://mainnet.zklighter.elliot.ai/api/v1/account"


def _dv(x) -> Decimal | None:
    try:
        v = dval(x)
    except (ArithmeticError, ValueError, TypeError):
        return None
    return v if v is None or v.is_finite() else None


def _num(x) -> float | None:
    try:
        f = float(x)
    except (TypeError, ValueError):
        return None
    return f if f == f and abs(f) != float("inf") else None


def load_config(path=None) -> list[dict]:
    """runtime/manual_positions.toml → список позиций; файла нет, он битый, или запись без id/coin — молча
    пропускается (страница кабинета не имеет права упасть из-за опечатки в ручном файле)."""
    p = Path(path or CONFIG_PATH)
    try:
        raw = p.read_bytes()
    except OSError:
        return []
    try:
        doc = tomllib.loads(raw.decode("utf-8"), parse_float=Decimal)
    except (UnicodeDecodeError, tomllib.TOMLDecodeError) as e:
        log.warning("manual_positions: %s не прочитан: %s", p, e)
        return []
    out = []
    for pos in doc.get("position") or []:
        if isinstance(pos, dict) and pos.get("id") and pos.get("coin"):
            out.append(pos)
    return out


def poll_lighter_account(l1_address: str, symbol: str, *, session=None, timeout: float = POLL_TIMEOUT_S) -> dict:
    """GET /api/v1/account?by=l1_address — публично, без ключей. total_funding_paid_out — счётчик самого Lighter
    (не история и не наш расчёт), это и есть «фандос» из просьбы владельца."""
    s = session or requests.Session()
    try:
        r = s.get(LIGHTER_ACCOUNT_URL, params={"by": "l1_address", "value": l1_address}, timeout=timeout,
                  headers={"user-agent": config.USER_AGENT})
        r.raise_for_status()
        body = r.json()
    except (requests.RequestException, ValueError) as e:
        return {"error": f"{type(e).__name__}: {e}"[:200]}
    accounts = body.get("accounts") if isinstance(body, dict) else None
    if not accounts:
        return {"error": "аккаунт не найден"}
    positions = accounts[0].get("positions") or []
    pos = next((p for p in positions if str(p.get("symbol") or p.get("market_symbol") or "") == symbol), None)
    if pos is None:
        return {"error": f"рынок {symbol} не найден в позициях аккаунта"}
    size = _dv(pos.get("position") if pos.get("position") is not None else pos.get("size"))
    side = None if size is None or size == 0 else ("short" if size < 0 else "long")
    mark = _dv(pos.get("mark_price") or pos.get("avg_entry_price"))
    funding = _dv(pos.get("total_funding_paid_out"))
    # JSON/atomic_json не сериализует Decimal — числа наружу только строками (как TEXT из БД у остального кабинета;
    # deal_views() читает их обратно через _dv()).
    return {"side": side, "size": str(abs(size)) if size is not None else None,
            "mark": str(mark) if mark is not None else None,
            "funding_total": str(funding) if funding is not None else None}


def poll_once(positions=None, *, session=None, now=None, path=None) -> dict:
    """Один опрос всех ручных позиций → runtime/manual_positions_live.json (atomic). Вызывается отдельным разовым
    прогоном (`funding_bot manual-poll`), не веб-процессом: сеть не должна попадать в обработку HTTP-запроса
    кабинета."""
    now = time.time() if now is None else now
    positions = load_config() if positions is None else positions
    live = {}
    for pos in positions:
        perp = pos.get("perp") or {}
        if str(perp.get("venue")) != "lighter" or not perp.get("l1_address") or not perp.get("symbol"):
            live[pos["id"]] = {"error": "неизвестная или неполная перп-площадка ручного входа"}
            continue
        live[pos["id"]] = {**poll_lighter_account(str(perp["l1_address"]), str(perp["symbol"]), session=session),
                            "ts": now}
    atomic_json(path or LIVE_PATH, {"schema_version": SCHEMA, "ts": now, "positions": live}, 0o644)
    return live


def load_live(path=None) -> dict:
    try:
        with open(path or LIVE_PATH, "rb") as f:
            body = json.loads(f.read())
    except (OSError, ValueError):
        return {}
    return body if isinstance(body, dict) and body.get("schema_version") == SCHEMA else {}


def deal_views(*, now=None, config_path=None, live_path=None) -> list[dict]:
    """Карточки ручных позиций для /cabinet — файлы только читаются, сети здесь нет (см. docstring модуля)."""
    now = time.time() if now is None else now
    positions = load_config(config_path)
    if not positions:
        return []
    live_doc = load_live(live_path)
    live_ts = live_doc.get("ts")
    live_by_id = live_doc.get("positions") or {}
    doc_stale = not isinstance(live_ts, (int, float)) or now - live_ts > LIVE_STALE_S
    out = []
    for pos in positions:
        lv = live_by_id.get(pos["id"]) or {}
        perp, spot = pos.get("perp") or {}, pos.get("spot") or {}
        err = lv.get("error")
        out.append({
            "manual": True, "state": "MANUAL", "id": pos["id"], "coin": pos.get("coin"),
            "opened": _num(pos.get("opened")),
            "note": str(pos.get("note") or ""),
            "perp": {"venue": perp.get("venue"), "address": perp.get("l1_address"), "symbol": perp.get("symbol"),
                     "side": lv.get("side"), "size": _dv(lv.get("size")), "mark": _dv(lv.get("mark"))},
            "spot": {"venue": spot.get("venue"), "wallet": spot.get("wallet"), "mint": spot.get("mint")},
            "funding": {"total": _dv(lv.get("funding_total")), "ccy": "USDC"},
            "stale": doc_stale, "error": str(err) if err else None, "live_ts": live_ts if isinstance(live_ts, (int, float)) else None,
        })
    return out
