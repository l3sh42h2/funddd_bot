"""Общее для тестов потока hl: записанные публичные ответы HL (tests/data/hl), подделка requests.Session, часы,
тестовый ключ. Ключ — ПУБЛИЧНЫЙ тестовый ключ из тестов официального SDK: средств за ним нет."""
import json as _json
import pathlib

DATA = pathlib.Path(__file__).resolve().parent / "data" / "hl"
PUB = _json.loads((DATA / "public_20260913.json").read_text())
GOLD = _json.loads((DATA / "golden_sdk_20260913.json").read_text())
E2E = _json.loads((DATA / "golden_e2e_20260913.json").read_text())
TEST_KEY = GOLD["key"]
AGENT = GOLD["agent"]
MASTER = "0x9a6f1bd2f1b7c1d2e3f4a5b6c7d8e9f0a1b2c3d4"      # синтетический мастер (как в golden_e2e.py)
SUB = "0x1719884eb866cb12b2287399b15f7db5e7d775ea"         # синтетический субаккаунт (vaultAddress)
ZERO = "0x" + "0" * 40
U0 = PUB["user_0"]["address"]      # чужой публичный участник para:ANSEM, userAbstraction = "default"
U1 = PUB["user_1"]["address"]      # чужой публичный участник para:ANSEM, userAbstraction = "unifiedAccount"
T0 = 1789305063.5                  # 13.09 ≈13:37:43Z; ×1000 точно = 1789305063500 (как nonce эталона)


class Clock:
    """Поддельные часы: sleep двигает время."""

    def __init__(self, t: float = T0):
        self.t = t

    def __call__(self) -> float:
        return self.t

    def sleep(self, s: float) -> None:
        self.t += s


class Resp:
    def __init__(self, status=200, body=None, raw: bytes | None = None, headers=None):
        self.status_code = status
        self.content = raw if raw is not None else _json.dumps(body).encode()
        self.text = self.content.decode(errors="replace")
        self.headers = headers or {}


def _page(rows, body, limit):
    s, e = body.get("startTime", 0), body.get("endTime", 10 ** 16)
    return [r for r in rows if s <= r["time"] <= e][:limit]


def _ch(positions=(), withdrawable="100.0"):
    """clearinghouseState в форме живого ответа (позиции — (coin, szi))."""
    z = {"accountValue": "0.0", "totalNtlPos": "0.0", "totalRawUsd": "0.0", "totalMarginUsed": "0.0"}
    return {"marginSummary": dict(z), "crossMarginSummary": dict(z), "crossMaintenanceMarginUsed": "0.0",
            "withdrawable": withdrawable, "time": 1789305063000,
            "assetPositions": [{"type": "oneWay", "position": {"coin": c, "szi": s, "leverage": {"type": "isolated",
                                                                                                   "value": 1}}}
                               for c, s in positions]}


ch = _ch


def _user_defaults():
    out = {}
    for key, addr in (("user_0", U0), ("user_1", U1)):
        u = PUB[key]
        out[addr.lower()] = {
            "userAbstraction": u["userAbstraction"]["data"],
            "clearinghouseState": u["clearinghouseState_para"]["data"],
            "activeAssetData": u["activeAssetData"]["data"],
            "userRole": u["userRole"]["data"], "userFees": u["userFees"]["data"],
            "extraAgents": u["extraAgents"]["data"], "spotClearinghouseState": u["spotClearinghouseState"]["data"],
            "frontendOpenOrders": u["frontendOpenOrders_para"]["data"],
            "userFillsByTime": (lambda rows: lambda b: _page(rows, b, 2000))(u["userFillsByTime"]["data"]),
            "userFunding": (lambda rows: lambda b: _page(rows, b, 500))(u["userFunding"]["data"]),
        }
    z = PUB["zero"]
    out[ZERO] = {"userAbstraction": z["userAbstraction"]["data"],
                 "clearinghouseState": z["clearinghouseState_para"]["data"],
                 "activeAssetData": z["activeAssetData"]["data"], "userFees": z["userFees"]["data"],
                 "orderStatus": z["orderStatus_cloid"]["data"],
                 "spotClearinghouseState": z["spotClearinghouseState"]["data"],
                 "userFillsByTime": [], "userFunding": []}
    return out


class FakeHL:
    """HL за requests.Session.post: /info — по type (рынок) или по (user, type) (счёт); /exchange — скрипт
    (Resp | исключение | callable(payload) → Resp). Неожиданный запрос — AssertionError (тест падает громко)."""

    def __init__(self, clock: Clock | None = None):
        self.clock = clock or Clock()
        f = PUB["fresh"]
        self.market = {
            "perpDexs": f["perpDexs"]["data"],
            "meta": lambda b: f["meta_para"]["data"] if b.get("dex") == "para" else PUB["meta_main"]["data"],
            "metaAndAssetCtxs": lambda b: f["metaAndAssetCtxs_para"]["data"] if b.get("dex") == "para" else _boom(b),
            "l2Book": lambda b: f["l2Book"]["data"] if b.get("coin") == "para:ANSEM" else _boom(b),
            "perpDexLimits": lambda b: PUB["perpDexLimits"]["data"] if b.get("dex") == "para" else _boom(b),
            "perpsAtOpenInterestCap": lambda b: PUB["perpsAtOpenInterestCap"]["data"],
            "perpAnnotation": PUB["perpAnnotation"]["data"],
            "fundingHistory": lambda b: _page(PUB["fundingHistory"]["data"], b, 500),
            "exchangeStatus": lambda b: {"specialStatuses": None, "time": int(self.clock() * 1000)},
        }
        self.users = _user_defaults()
        self.exchange_script: list = []
        self.calls: list[tuple[str, dict]] = []

    def user(self, addr: str, typ: str, value) -> None:
        self.users.setdefault(addr.lower(), {})[typ] = value

    def post(self, url, data=None, headers=None, timeout=None, **kw):
        payload = _json.loads(data)
        path = "/" + url.rsplit("/", 1)[-1]
        self.calls.append((path, payload))
        if path == "/info":
            if "user" in payload:
                h = self.users.get(str(payload["user"]).lower(), {}).get(payload["type"], _MISSING)
            else:
                h = self.market.get(payload["type"], _MISSING)
            if h is _MISSING:
                raise AssertionError(f"неожиданный /info {payload}")
            r = h(payload) if callable(h) else h
            if isinstance(r, BaseException):
                raise r
            return r if isinstance(r, Resp) else Resp(200, r)
        if path == "/exchange":
            if not self.exchange_script:
                raise AssertionError(f"неожиданный /exchange {payload}")
            s = self.exchange_script.pop(0)
            if isinstance(s, BaseException):
                raise s
            return s(payload) if callable(s) else s
        raise AssertionError(url)

    def info_calls(self, typ: str | None = None) -> list[dict]:
        return [p for path, p in self.calls if path == "/info" and (typ is None or p["type"] == typ)]

    def exchange_calls(self) -> list[dict]:
        return [p for path, p in self.calls if path == "/exchange"]


_MISSING = object()


def _boom(b):
    raise AssertionError(f"неожиданный запрос {b}")


# --- ответы /exchange (форма — по документации Exchange endpoint; своих записанных ордеров нет — синтетика) ---
def ok_filled(total, avg, oid=777001, cloid=None):
    f = {"totalSz": total, "avgPx": avg, "oid": oid}
    if cloid:
        f["cloid"] = cloid
    return {"status": "ok", "response": {"type": "order", "data": {"statuses": [{"filled": f}]}}}


def ok_error(text):
    return {"status": "ok", "response": {"type": "order", "data": {"statuses": [{"error": text}]}}}


def ok_resting(oid=777002):
    return {"status": "ok", "response": {"type": "order", "data": {"statuses": [{"resting": {"oid": oid}}]}}}


def err(text):
    return {"status": "err", "response": text}


OK_DEFAULT = {"status": "ok", "response": {"type": "default"}}
IOC_NO_MATCH = "Order could not immediately match against any resting orders. asset=180025"


def order_status(cloid, oid, orig, remaining, status, coin="para:ANSEM", side="A", px="0.16608"):
    """Форма живого ответа orderStatus (tests/data/hl/public: user_1 orderStatus_oid)."""
    return {"status": "order", "order": {"order": {
        "coin": coin, "side": side, "limitPx": px, "sz": remaining, "oid": oid, "timestamp": 1789305063600,
        "triggerCondition": "N/A", "isTrigger": False, "triggerPx": "0.0", "children": [], "isPositionTpsl": False,
        "reduceOnly": False, "orderType": "Limit", "origSz": orig, "tif": "Ioc", "cloid": cloid},
        "status": status, "statusTimestamp": 1789305063600}}


UNKNOWN_OID = {"status": "unknownOid"}


def fill(oid, sz, px, time, tid, fee="0.01", coin="para:ANSEM", cloid=None, side="A", builder_fee=None,
         hash_="0x" + "ab" * 32):
    """Строка userFillsByTime в форме живого ответа."""
    r = {"coin": coin, "px": px, "sz": sz, "side": side, "time": time, "startPosition": "0.0", "dir": "Open Short",
         "closedPnl": "0.0", "hash": hash_, "oid": oid, "crossed": True, "fee": fee, "tid": tid, "cloid": cloid,
         "feeToken": "USDC", "twapId": None}
    if builder_fee is not None:
        r["builderFee"] = builder_fee
    return r
