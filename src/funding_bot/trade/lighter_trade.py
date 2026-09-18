"""Перп-нога Lighter (zkLighter, mainnet-инстанс) — FuturesAdapter. ВЫКЛЮЧЕНО: подпись L2-транзакций НЕ реализована.

ГЛАВНЫЙ ИТОГ ЭТОГО МОДУЛЯ: реализовано всё, что можно честно подтвердить публичной документацией/спекой Lighter
без реальной криптографии (чтение позиции/баланса/маржи, инструменты и фильтры, ошибки и rate limit, построение
и валидация заявки, чтение статуса транзакции). Подпись ордера — единственная НЕ реализованная часть, и это
осознанный выбор, а не недосмотр: см. «Почему подпись не реализована» ниже. `ioc()`/`cancel_order()`/
`setup()`/`history_fills()` доходят до места, где нужна подпись, и там осознанно бросают SigningNotAvailable —
это ЗАГЛУШКА С ОПИСАНИЕМ, а не тихая неверная реализация. deploy/owner.toml.example содержит [perp.lighter] со
всеми полями пустыми (ПУСТО = ЗАПРЕЩЕНО) — ни один профиль пока не ссылается на perp_venue="lighter". Подробности
и источники — в PATCHNOTES/lighter-futures-adapter-20260918.md.

Что такое Lighter. zkLighter — perp/spot DEX на собственном zk-rollup (не обычная CEX). Идентификатор аккаунта —
целый `account_index` (НЕ адрес), сопоставляется с L1-адресом через accountsByL1Address. Инстанс Robinhood Chain
(api.rh.lighter.xyz, квота USDG) — ВНЕ SCOPE этой задачи (только mainnet, квота USDC); модуль читает и торгует
только основной инстанс. Существующий src/funding_bot/audit_truth/lighter.py и src/funding_bot/lighter.py —
read-only скринер дашборда (публичные рыночные данные), сознательно не используется как база: у него другая
зона ответственности (см. его же докстроку «свои запросы и свой разбор, без клиента коллектора lighter.py») —
этот же принцип соблюдён и здесь: торговый путь не импортирует и не переиспользует код скринера.

Источники (все прочитаны 18.09.2026, см. патчноут — там же точные URL и цитаты):
- apidocs.lighter.xyz: get-started, api-keys, trading (Signing Transactions), rate-limits,
  data-structures-constants-and-errors, и «сырые» OpenAPI-фрагменты reference/{account-1,sendtx,nextnonce,
  tokens_create,orderbookdetails,apikeys,tx,accountorders,get_accounts-param-positions}.md — используются как
  ОСНОВНОЙ источник схем полей (машиночитаемый OpenAPI, не пересказ).
- github.com/elliottech/lighter-python (lighter/signer_client.py) — официальный Python SDK: `SignerClient`
  оборачивает НЕ pure-Python код, а платформенную скомпилированную библиотеку через ctypes
  (`lighter-signer-{darwin,linux,windows}-{amd64,arm64}.{dylib,so,dll}` из lighter-go/sharedlib). Каждая
  подписывающая операция (SignCreateOrder, SignCancelOrder, SignChangePubKey, SignWithdraw, CreateAuthToken,
  ...) — вызов в эту библиотеку, НЕ арифметика на Python.
- github.com/elliottech/lighter-go — официальный Go SDK; каталог signer/ собирается в sharedlib/ для ctypes-
  обёртки выше. Сам Go-исходник подписи не читался построчно (вне разумного бюджета этой задачи — портирование
  криптографии из чужого языка без единого способа проверить результат живым вызовом было бы именно тем
  «неверным тихим угадыванием», от которого явно предостерегает задача).
- crates.io/lighter-rs (docs.rs/lighters) — НЕЗАВИСИМЫЙ (не от Lighter) порт на Rust; его же описание: «from-
  scratch Rust port… producing byte-identical transaction hashes and Schnorr signatures… ECgFp5 curve…
  Poseidon2… Goldilocks field… ports from the official lighter-go implementation, though they have not been
  independently audited». Это подтверждает НАЗВАНИЕ схемы (Schnorr/ECgFp5/Poseidon2 над полем Goldilocks —
  zk-friendly конструкция сродни StarkEx, как и предполагала задача), но сам порт: (а) сторонний, не Lighter;
  (б) прямо помечен как неаудированный; (в) на Rust. Использовать его как основу для новой Python-реализации
  внутри этой задачи означало бы полагаться на неаудированный сторонний порт стороннего порта — риск тихой
  ошибки подписи денежного кода, а не её отсутствие.

Почему подпись не реализована (осознанно, не «не успел»). Каждая ЗАПИСЬ в Lighter (создание/отмена/изменение
ордера, вывод, смена ключа) — это `sendTx`: POST {tx_type, tx_info} без заголовка Authorization (аутентификация
— это и есть подпись внутри tx_info, как в блокчейн-транзакции). Официальный путь получения tx_info — только
через скомпилированный сигнер (см. источники выше). Публичной, независимо проверяемой спецификации самой схемы
подписи (порядок полей хеша Poseidon2, кодирование точки ECgFp5, формат nonce/expiry внутри подписываемого
сообщения) в задокументированном виде не найдено. Гадать её по аналогии с EIP-712 (Aster/Hyperliquid) или HMAC
(Gate) — прямо запрещено постановкой задачи, и было бы неверно даже как гипотеза (схема другая, см. выше).
Даже ПРОЧТЕНИЕ собственных приватных данных (allowed active/inactive orders, история сделок `trades`) требует
auth-токена (`tokens/create`), а создание ЛЮБОГО auth-токена — тоже вызов в скомпилированный сигнер
(`CreateAuthToken` в ctypes-таблице signer_client.py). Поэтому даже «только читать свои ордера» заблокировано
без подписи, а не только «торговать».

Что ПУБЛИЧНО и не требует подписи (подтверждено полями/отсутствием security-схемы в самих OpenAPI-фрагментах,
не только пересказом доков) и поэтому реализовано и покрыто тестами на фейках:
- GET /api/v1/orderBookDetails?filter=perp — инструменты и фильтры (эту же форму независимо использует и
  audit_truth/lighter.py по живому снятию 12-13.09.2026 — здесь не импортируется, но схема совпадает).
- GET /api/v1/account?by=index&value=... — DetailedAccounts{accounts:[DetailedAccount{available_balance,
  collateral, positions:[AccountPosition{symbol, sign, position, avg_entry_price, unrealized_pnl,
  liquidation_price, allocated_margin, ...}], assets:[...], cross_asset_value, ...}]} — позиция и маржа.
- GET /api/v1/nextNonce?account_index=&api_key_index= — публичный (нонс сам по себе не секрет).
- GET /api/v1/apikeys?account_index=&api_key_index= — публичный реестр публичных ключей аккаунта (для проверки
  конфигурации api_key_index, БЕЗ приватного материала).
- GET /api/v1/tx?by=hash&value=... — статус транзакции по хэшу (без авторизации). ЧТО НЕ ДОКАЗАНО: поле
  `event_info` (JSON-строка) вероятно несёт экономику исполнения (аналогично TradeWithFunding у отдельного
  explorer.elliot.ai/api /accounts/{acc}/logs — другой сервис, другая OpenAPI-схема, не проверялась живым
  вызовом) — но типизированной схемы `event_info` в OpenAPI НЕТ, поэтому этот модуль читает только верхнеуровневый
  numeric `status` (0 Failed/1 Pending/2 Executed/3 Pending-FinalState) и НЕ пытается вытащить qty/price из
  event_info. `query()` поэтому возвращает 'UNKNOWN' даже для status=2 — количество экономики не подтверждено,
  а не потому что transaction не нашлась.

Коды ошибок (ERROR_KIND, ниже) взяты из apidocs.lighter.xyz/docs/data-structures-constants-and-errors (18.09.2026)
как документация, НЕ проверены против живого ответа биржи (в задаче запрещены любые реальные вызовы) — если
реальный код отличается по смыслу от документации, это узнается только на первой реальной ошибке.
"""
from __future__ import annotations

import hashlib
import time
from dataclasses import dataclass
from decimal import Decimal as D, InvalidOperation
from typing import Callable, Protocol, runtime_checkable

import requests

from .keys import effective_mode, gate as mode_gate
from .types import Filters, PerpFill, PerpInstrument

VENUE = "lighter"

MAIN_REST = "https://mainnet.zklighter.elliot.ai/api/v1"
MAIN_WS = "wss://mainnet.zklighter.elliot.ai/stream"
QUOTE_ASSET = "USDC"          # mainnet-инстанс; Robinhood-инстанс (USDG) вне scope этой задачи

# apidocs.lighter.xyz/docs/rate-limits (18.09.2026): Standard-аккаунт — 60 запросов/скользящую минуту на IP,
# ОДИН И ТОТ ЖЕ лимит для публичных чтений и sendTx/sendTxBatch. 429 или 405 — сигнал превышения (документация
# не называет заголовок Retry-After для этого хоста) — берём консервативную паузу, как уже проверено вживую
# в audit_truth/lighter.py (BAN_S=61.0 там же, тот же хост).
STANDARD_REQ_PER_MIN = 60
BAN_S = 61.0
IOC_EXPIRY_S = 30.0            # срок действия tx (не срок жизни в стакане — сам ордер IOC); см. патчноут: число
                               # не подтверждено вживую, выбрано консервативно в документированных рамках.

# apidocs.lighter.xyz/docs/data-structures-constants-and-errors (18.09.2026) — коды ошибок биржи. Только
# классификация по смыслу (для сообщений и логов), НЕ мэппинг в adapters.contracts.ErrorKind: native-модули
# в этом репозитории (Aster/Gate/HL) тоже не делают такой мэппинг сами — обобщённый adapters-слой сводит любое
# отличное от AdapterError исключение к TRANSIENT/UNKNOWN на своём уровне (native.py:_read, submit()).
FUNDS_CODES = frozenset({21301, 21304, 21305, 21507, 21508, 21739})
NONCE_CODES = frozenset({21101, 21104, 21105})
SIGNATURE_CODES = frozenset({21120})
ORDER_INVALID_CODES = frozenset({21701, 21702, 21708, 21732, 21733, 21734, 21738})
ORDER_STATE_CODES = frozenset({21600, 21601, 21709, 21730})
RATE_LIMIT_CODES = frozenset({23000, 23001, 23003, 23004})

_CODE_LABELS = (
    (FUNDS_CODES, "insufficient_funds_or_margin"),
    (NONCE_CODES, "nonce"),
    (SIGNATURE_CODES, "invalid_signature"),
    (ORDER_INVALID_CODES, "invalid_order_params"),
    (ORDER_STATE_CODES, "order_state"),
    (RATE_LIMIT_CODES, "rate_limit"),
)

# apidocs.lighter.xyz/docs/data-structures-constants-and-errors (18.09.2026): статус транзакции.
TX_STATUS_FAILED = 0
TX_STATUS_PENDING = 1
TX_STATUS_EXECUTED = 2
TX_STATUS_PENDING_FINAL = 3

# Схема ключей в .env (владелец подключает позже, значения никогда не коммитятся — docs/COORDINATION.md §2).
# Формат приватного значения — то, что вернёт официальный create_api_key()/GenerateAPIKey Lighter; конкретную
# байтовую кодировку эта задача не подтверждала (см. модульную докстроку), поэтому загрузчик ключа из .env
# сознательно НЕ реализован (ни здесь, ни в trade/keys.py) — это было бы кодом, который нечем проверить.
LIGHTER_API_KEY_ENV = "LIGHTER_API_PRIVATE_KEY"


def _dec(x) -> D | None:
    if x is None:
        return None
    try:
        d = D(str(x))
    except (InvalidOperation, ValueError, TypeError):
        return None
    return d if d.is_finite() else None


def _int(x) -> int | None:
    try:
        return int(x)
    except (TypeError, ValueError):
        try:
            return int(float(x))
        except (TypeError, ValueError):
            return None


def _pow10(decimals) -> D | None:
    n = _int(decimals)
    if n is None or n < 0:
        return None
    return D(1).scaleb(-n)


def classify_code(code) -> str | None:
    """Метка смысла документированного числового кода ошибки Lighter (см. _CODE_LABELS) — только для сообщений
    и логов, не для программных решений «повторять/не повторять» (это делает LighterApiError по обычному
    HTTP-статусу и transport-уровню, как у остальных venue в этом репозитории)."""
    n = _int(code)
    if n is None:
        return None
    for codes, label in _CODE_LABELS:
        if n in codes:
            return label
    return None


class LighterError(RuntimeError):
    def __init__(self, code, message):
        self.code = code
        super().__init__(message)


class LighterApiError(LighterError):
    """Биржа ответила телом {code != 200} или HTTP 4xx — код документирован (см. _CODE_LABELS) и НЕ повторяем
    автоматически (заявка могла быть отклонена намеренно; ретрай отклонённой заявки — не наша политика)."""


class LighterNetError(LighterError):
    """Нет ответа / не JSON / 5xx после повторов — вопрос не про отказ биржи, а про то, что исход НЕ известен."""


class SigningNotAvailable(LighterError):
    """Операция требует подписи L2-транзакции Lighter (Schnorr/ECgFp5/Poseidon2, см. докстроку модуля) —
    в этой поставке не реализована умышленно. Это НЕ баг и НЕ забытая часть: см.
    PATCHNOTES/lighter-futures-adapter-20260918.md, раздел «Почему подпись не реализована»."""

    def __init__(self, action: str):
        self.action = action
        super().__init__(None, (
            f"lighter: «{action}» требует подписи Lighter L2 (см. докстроку lighter_trade.py) — не реализовано "
            "в этой поставке. Нужно решение владельца: (а) принять lighter-python как зависимость (несёт "
            "скомпилированный бинарный артефакт lighter-go/sharedlib, который эта задача не аудировала), либо "
            "(б) ждать независимо аудируемой реализации схемы Schnorr/ECgFp5/Poseidon2/Goldilocks. До решения "
            "— эта операция запрещена и конфигурацией (owner.toml: [perp.lighter] пуст, ни один профиль не "
            "ссылается на perp_venue=\"lighter\"), и кодом (это исключение)."
        ))


@runtime_checkable
class SignerProtocol(Protocol):
    """Ожидаемый интерфейс реального подписанта (например, обёртка над lighter-python SignerClient) — НЕ
    реализован в этой поставке; см. докстроку модуля. sign_create_order/sign_cancel_order должны выполнить
    настоящую криптографию Lighter и вернуть (tx_type, tx_info_json) готовыми для POST /sendTx. Тестовые
    дублёры в tests/test_trade_lighter.py реализуют этот же интерфейс БЕЗ настоящей криптографии — это фейки
    для проверки оркестрации, а не образец для реального подписанта."""

    def sign_create_order(self, *, market_index: int, client_order_index: int, base_amount: int, price: int,
                          is_ask: bool, order_type: str, time_in_force: str, reduce_only: bool,
                          trigger_price: int, order_expiry_ms: int, nonce: int,
                          api_key_index: int) -> tuple[int, str]: ...

    def sign_cancel_order(self, *, market_index: int, order_index: int, nonce: int,
                          api_key_index: int) -> tuple[int, str]: ...


def _client_order_index(client_id: str) -> int:
    """Детерминированный uint48-номер для Lighter client_order_index. Lighter требует лишь «число на ваш выбор
    для сверки» (get-started, 18.09.2026) — конкретный способ получить число из нашего строкового client_id
    (`fb-{deal}-{e|x|h}{clip:02d}-c{n}-a{n}`, см. adapters/execution.py) — НАШЕ решение, не предписание Lighter.
    SHA-256, младшие 48 бит: коллизия потребовала бы ~2**24 заявок (граница дня рождения) — для реального
    объёма этого бота риск признан пренебрежимым; после включения подписи стоит перепроверить у владельца."""
    if not isinstance(client_id, str) or not client_id:
        raise LighterError(None, "lighter: client_id обязателен для client_order_index")
    digest = hashlib.sha256(client_id.encode()).digest()
    return int.from_bytes(digest[-6:], "big")


def _scale(value: D, decimals, what: str) -> int:
    n = _int(decimals)
    if n is None or n < 0:
        raise LighterError(None, f"lighter: {what}: у рынка нет decimals-метаданных")
    scaled = value * (D(10) ** n)
    if scaled != scaled.to_integral_value():
        raise LighterError(None, f"lighter: {what} не выровнен по шагу рынка ({n} знаков)")
    return int(scaled)


@dataclass(frozen=True)
class LighterIdentity:
    account_index: int
    l1_address: str | None = None
    api_key_index: int | None = None


class LighterHttp:
    """GET/POST на mainnet.zklighter.elliot.ai — независимая от audit_truth/lighter.py реализация (та — read-
    only скринер дашборда с собственным бюджетом; здесь — торговый путь со своим, см. докстроку модуля).
    429/405 — пауза BAN_S и явная ошибка (документация не называет заголовок Retry-After для этого хоста)."""

    def __init__(self, *, session=None, base: str = MAIN_REST, timeout: float = 10.0, retries: int = 3,
                gap_s: float = 60.0 / STANDARD_REQ_PER_MIN, now: Callable[[], float] = time.time,
                sleep: Callable[[float], None] = time.sleep):
        self.s = session or requests.Session()
        self.base, self.timeout, self.retries, self.gap_s = base, timeout, max(1, retries), gap_s
        self.now, self.sleep = now, sleep
        self._last_call = 0.0
        self.banned_until = 0.0
        self.n_429 = 0
        self.n_err = 0

    def _pace(self):
        wait = self.banned_until - self.now()
        if wait > 0:
            raise LighterApiError(429, f"lighter: пауза после 429/405 ещё {wait:.0f} с")
        gap = self.gap_s - (self.now() - self._last_call)
        if gap > 0:
            self.sleep(gap)
        self._last_call = self.now()

    def _handle_response(self, r, method: str, path: str):
        if r.status_code in (429, 405):
            self.banned_until = self.now() + BAN_S
            self.n_429 += 1
            raise LighterApiError(r.status_code,
                                  f"lighter: {method} {path} → HTTP {r.status_code} (лимит "
                                  f"{STANDARD_REQ_PER_MIN}/мин на IP), пауза {BAN_S:.0f} с")
        if r.status_code >= 400:
            raise LighterApiError(r.status_code, f"lighter: {method} {path} → HTTP {r.status_code} {r.text[:200]}")
        try:
            body = r.json()
        except ValueError as e:
            raise LighterNetError(None, f"lighter: {method} {path}: ответ не JSON: {e}") from None
        code = body.get("code") if isinstance(body, dict) else None
        if code is not None and _int(code) != 200:
            raise LighterApiError(code, f"lighter: {method} {path} → code {code} "
                                        f"({classify_code(code) or 'unclassified'}) {str(body)[:200]}")
        return body

    def get(self, path: str, params: dict | None = None) -> dict:
        last = None
        for i in range(self.retries):
            self._pace()
            try:
                r = self.s.get(self.base + path, params=params, timeout=self.timeout)
            except requests.RequestException as e:
                last = f"{type(e).__name__}: {e}"
                self.n_err += 1
                self.sleep(1.0 * (i + 1))
                continue
            if r.status_code >= 500:
                last = f"HTTP {r.status_code}"
                self.n_err += 1
                self.sleep(1.0 * (i + 1))
                continue
            return self._handle_response(r, "GET", path)
        raise LighterNetError(None, f"lighter: GET {path}: {last}")

    def post_form(self, path: str, data: dict) -> dict:
        """sendTx — requestBody content-type application/x-www-form-urlencoded (OpenAPI ReqSendTx, 18.09.2026),
        поэтому именно `data=`, не `json=`. Один повтор только на сетевую ошибку/5xx — как и у submit() других
        venue в этом репозитории, после отправки исход трактуется как неизвестный, а не «не отправлено»."""
        self._pace()
        try:
            r = self.s.post(self.base + path, data=data, timeout=self.timeout)
        except requests.RequestException as e:
            raise LighterNetError(None, f"lighter: POST {path}: нет ответа: {e}") from None
        return self._handle_response(r, "POST", path)


class LighterTrade:
    """Native perp-клиент zkLighter (только mainnet-инстанс). venue/ioc_partial_terminal — атрибуты, которых
    ждёт adapters/futures_bindings.bind(). Публичные чтения полностью реализованы и покрыты тестами; подписанные
    операции доходят до `_require_signer` и там останавливаются (см. докстроку модуля)."""

    venue = VENUE
    # НЕ подтверждено живым ответом (нет подписи → нет реальной IOC-заявки для наблюдения): Order Status биржи
    # (data-structures-constants-and-errors) не показывает отдельного «partially filled», отличного от
    # FilledOrder/CanceledOrder — по аналогии с Gate ("только status=finished даёт PARTIALLY_FILLED") здесь
    # выбран консервативный дефолт False, а не наугад True. Перепроверить, когда появится живой ответ.
    ioc_partial_terminal = False

    def __init__(self, account_index: int, *, l1_address: str | None = None, api_key_index: int | None = None,
                signer: SignerProtocol | None = None, mode_state: Callable[[], tuple] | None = None,
                session=None, base: str = MAIN_REST, now: Callable[[], float] = time.time,
                sleep: Callable[[float], None] = time.sleep):
        if type(account_index) is not int or account_index < 0:
            raise LighterError(None, "lighter: account_index должен быть неотрицательным целым числом")
        if api_key_index is not None and (type(api_key_index) is not int or not 0 <= api_key_index <= 254):
            raise LighterError(None, "lighter: api_key_index должен быть в диапазоне 0..254")
        self.identity = LighterIdentity(account_index, l1_address, api_key_index)
        self.signer = signer          # None по умолчанию — единственный безопасный дефолт (см. модульную докстроку)
        self.mode_state = mode_state
        self.now, self.sleep = now, sleep
        self.http = LighterHttp(session=session, base=base, now=now, sleep=sleep)
        self._markets: dict[str, dict] = {}
        self._markets_ts = 0.0

    def __repr__(self):
        return f"LighterTrade(account_index={self.identity.account_index}, signer={'set' if self.signer else None!r})"

    # ------------------------------------------------------------------ публичные чтения: рынки/фильтры -------
    def _details(self) -> list[dict]:
        body = self.http.get("/orderBookDetails", {"filter": "perp"})
        det = [m for m in (body.get("order_book_details") or []) if isinstance(m, dict) and m.get("symbol")]
        if not det:
            raise LighterError(None, "lighter: orderBookDetails без перпов")
        self._markets = {str(m["symbol"]): m for m in det}
        self._markets_ts = self.now()
        return det

    def _market(self, symbol: str) -> dict:
        m = self._markets.get(symbol)
        if m is None:
            self._details()
            m = self._markets.get(symbol)
        if m is None:
            raise LighterError(None, f"lighter: рынка {symbol} нет в orderBookDetails")
        return m

    def instrument(self, symbol: str) -> PerpInstrument:
        self._market(symbol)      # проверка существования рынка до построения инструмента
        # orderBookDetails не отдаёт отдельного base_asset/множителя контракта (в отличие от Gate
        # quanto_multiplier) — каждый рынок торгуется в «сырых» единицах базового актива, множитель = 1.
        return PerpInstrument(symbol, symbol, symbol, D(1), QUOTE_ASSET, "PERPETUAL")

    def filters(self, symbol: str) -> Filters:
        m = self._market(symbol)
        tick, step = _pow10(m.get("price_decimals")), _pow10(m.get("size_decimals"))
        if tick is None or step is None:
            raise LighterError(None, f"lighter: {symbol}: нет price_decimals/size_decimals")
        min_qty = _dec(m.get("min_base_amount")) or step
        min_notional = _dec(m.get("min_quote_amount")) or D(0)
        # orderBookDetails НЕ публикует максимальный размер заявки (в отличие от Aster/Gate order_size_max) —
        # проверено по «сырой» OpenAPI-схеме (reference/orderbookdetails.md, 18.09.2026), не только по пересказу.
        # Значение не выдумываем: локального потолка нет, отказ — на стороне биржи (FUNDS_CODES/ORDER_INVALID_CODES).
        return Filters(tick, step, min_qty, D("Infinity"), D("Infinity"), min_notional, frozenset({"IOC"}))

    # ------------------------------------------------------------------ публичные чтения: аккаунт --------------
    def _account_row(self) -> dict:
        body = self.http.get("/account", {"by": "index", "value": str(self.identity.account_index)})
        accounts = body.get("accounts") or []
        if not accounts:
            raise LighterError(None, f"lighter: аккаунт {self.identity.account_index} не найден")
        return accounts[0]

    def position(self, symbol: str) -> D | None:
        """DetailedAccount.positions перечисляет каждый рынок, где у аккаунта была активность (account-1.md,
        18.09.2026) — отсутствие symbol в списке означает «флэт», а не «неизвестно», поэтому возвращаем
        Decimal(0), а не None. None здесь используется только если сама биржа прислала нечисловое поле."""
        row = self._account_row()
        for p in row.get("positions") or []:
            if p.get("symbol") == symbol:
                qty = _dec(p.get("position"))
                if qty is None:
                    return None
                sign = _int(p.get("sign"))
                return qty if (sign is None or sign >= 0) else -qty
        return D(0)

    def available_margin(self) -> D | None:
        return _dec(self._account_row().get("available_balance"))

    def next_nonce(self, api_key_index: int | None = None) -> int:
        idx = self.identity.api_key_index if api_key_index is None else api_key_index
        if idx is None:
            raise LighterError(None, "lighter: api_key_index не сконфигурирован")
        body = self.http.get("/nextNonce", {"account_index": self.identity.account_index, "api_key_index": idx})
        nonce = _int(body.get("nonce"))
        if nonce is None:
            raise LighterError(None, "lighter: nextNonce: поле nonce отсутствует или не число")
        return nonce

    def registered_public_key(self, api_key_index: int | None = None) -> str | None:
        """Публичный реестр /apikeys — можно свериться, что account_index/api_key_index действительно
        зарегистрированы на бирже, БЕЗ приватного материала (см. модульную докстроку)."""
        idx = self.identity.api_key_index if api_key_index is None else api_key_index
        if idx is None:
            raise LighterError(None, "lighter: api_key_index не сконфигурирован")
        body = self.http.get("/apikeys", {"account_index": self.identity.account_index, "api_key_index": idx})
        for row in body.get("api_keys") or []:
            if _int(row.get("api_key_index")) == idx:
                return row.get("public_key")
        return None

    def health(self) -> dict:
        return {"exchange": self.venue, "account_index": self.identity.account_index,
               "banned_until": int(self.http.banned_until), "n_429": self.http.n_429, "n_err": self.http.n_err,
               "signer": "configured" if self.signer is not None else None}

    # ------------------------------------------------------------------ учётный журнал (execution history) ----
    def history_account(self):
        return self.identity.account_index

    def history_fills(self, symbol: str, from_id):
        """Достоверная история сделок требует auth-токена (trades/accountOrders — 18.09.2026: «auth is required
        for master accounts and sub accounts») — токен, в свою очередь, требует подписи (CreateAuthToken тоже
        идёт через скомпилированный сигнер, см. модульную докстроку). Публичный explorer.elliot.ai/api/accounts/
        {account}/logs — ДРУГОЙ сервис с НЕ проверенной здесь схемой (см. докстроку) — не используется как
        тихая замена; для будущего исполнителя оставлена явная зацепка в PATCHNOTES, не код с угаданной схемой."""
        raise SigningNotAvailable("history_fills (auth-gated trades/accountOrders)")

    # ------------------------------------------------------------------ чтение транзакции (публично) ----------
    def _parse_tx(self, body: dict) -> dict:
        status = _int(body.get("status"))
        return {
            "hash": body.get("hash"), "type": _int(body.get("type")), "status": status,
            "account_index": _int(body.get("account_index")), "nonce": _int(body.get("nonce")),
            # event_info: JSON-строка, вероятно с экономикой исполнения (по аналогии с explorer /logs
            # TradeWithFunding) — типизированной схемы в OpenAPI НЕТ (reference/tx.md, 18.09.2026), поэтому
            # оставляем непарсенной строкой, а не угадываем поля внутри.
            "event_info_raw": body.get("event_info"),
        }

    def query(self, symbol: str, ref: str) -> PerpFill:
        """ref — tx_hash Lighter (см. модульную докстроку: без подписи это поле никогда не заполняется реальным
        значением в этой поставке, но сама функция читает и разбирает публичный /tx корректно и тестируется
        отдельно). status=Executed(2) всё равно даёт 'UNKNOWN': экономика исполнения не подтверждена (см. выше),
        только Failed(0) классифицируется как окончательный REJECTED."""
        body = self.http.get("/tx", {"by": "hash", "value": ref})
        parsed = self._parse_tx(body)
        if parsed["status"] == TX_STATUS_FAILED:
            return PerpFill(ref, None, "REJECTED", D(0), D(0), D(0), 0)
        return PerpFill(ref, None, "UNKNOWN", D(0), D(0), D(0), parsed["nonce"] or 0)

    # ------------------------------------------------------------------ подписанные операции: ЗАБЛОКИРОВАНО ---
    def _require_signer(self, action: str) -> SignerProtocol:
        if self.signer is None:
            raise SigningNotAvailable(action)
        return self.signer

    def _check_mode(self, *, hedge: bool = False):
        """Ворота режима dry/readonly/live (keys.gate) — то же ACTIONS['send'], что и у Aster/Gate/Hyperliquid
        для любой отправки. Проверяются ДО подписи: запрещённый вызов не тратит nonce и не уходит в сеть."""
        if self.mode_state is None:
            return
        mode, paused = self.mode_state()
        mode_gate(effective_mode(mode), "send", paused=paused, hedge=hedge)

    def setup(self, symbol: str, leverage, margin_type: str):
        """SignUpdateLeverage/SignUpdateMargin — подписанные транзакции (data-structures-constants-and-errors,
        TxType, 18.09.2026); заблокировано так же, как ioc/cancel_order."""
        self._check_mode()
        self._require_signer("setup (leverage/margin_type)")

    def ioc(self, symbol: str, side: str, quantity: D, price: D, client_id: str, reduce_only: bool, *,
           on_signed: Callable[[int], None] | None = None, hedge: bool = False, links: dict | None = None) -> PerpFill:
        """Строит и валидирует заявку ПОЛНОСТЬЮ (рынок, шаг/тик, force_reduce_only, масштабирование в целые
        base_amount/price, детерминированный client_order_index) и только ПОСЛЕ этого требует подписанта —
        так неверный вызов получает конкретную, а не общую ошибку. reduce_only и обычный вход — один и тот же
        путь (Lighter IOC = ORDER_TIME_IN_FORCE_IMMEDIATE_OR_CANCEL с ценой-границей, не безграничный market —
        как и у Aster/Gate/Hyperliquid в этом репозитории)."""
        if side not in ("BUY", "SELL"):
            raise LighterError(None, f"lighter: side должен быть BUY или SELL, получено {side!r}")
        if not isinstance(quantity, D) or not quantity.is_finite() or quantity <= 0:
            raise LighterError(None, "lighter: quantity должен быть положительным Decimal")
        if not isinstance(price, D) or not price.is_finite() or price <= 0:
            raise LighterError(None, "lighter: price должен быть положительным Decimal")
        m = self._market(symbol)
        mc = m.get("market_config") or {}
        if not reduce_only:
            if m.get("status") != "active":
                raise LighterError(None, f"lighter: {symbol} недоступен для входа (status={m.get('status')})")
            if mc.get("force_reduce_only"):
                raise LighterError(None, f"lighter: {symbol} только на закрытие (force_reduce_only)")
        base_amount = _scale(quantity, m.get("size_decimals"), "quantity")
        px_int = _scale(price, m.get("price_decimals"), "price")
        market_id = _int(m.get("market_id"))
        if market_id is None:
            raise LighterError(None, f"lighter: {symbol}: market_id отсутствует или не число")
        client_order_index = _client_order_index(client_id)
        self._check_mode(hedge=hedge)
        signer = self._require_signer("ioc (create_order)")
        if self.identity.api_key_index is None:
            raise LighterError(None, "lighter: api_key_index не сконфигурирован")
        nonce = self.next_nonce()
        order_expiry_ms = int((self.now() + IOC_EXPIRY_S) * 1000)
        tx_type, tx_info = signer.sign_create_order(
            market_index=market_id, client_order_index=client_order_index, base_amount=base_amount, price=px_int,
            is_ask=(side == "SELL"), order_type="LIMIT", time_in_force="IOC", reduce_only=bool(reduce_only),
            trigger_price=0, order_expiry_ms=order_expiry_ms, nonce=nonce, api_key_index=self.identity.api_key_index)
        if callable(on_signed):
            on_signed(nonce)
        self.http.post_form("/sendTx", {"tx_type": tx_type, "tx_info": tx_info})
        # sendTx подтверждает только приём сиквенсором (RespSendTx: tx_hash + predicted_execution_time_ms) —
        # ни qty, ни price исполнения в ответе НЕТ (sendtx.md, 18.09.2026). Экономику даёт только query() по
        # этому tx_hash, и даже там — с оговоркой выше. Если post_form не бросил исключение — заявка принята
        # сиквенсором; исход всегда UNKNOWN здесь, никогда не «угаданный» FILLED.
        return PerpFill(client_id, None, "UNKNOWN", D(0), D(0), D(0), nonce)

    def cancel_order(self, symbol: str, order_index: int, *, api_key_index: int | None = None) -> PerpFill:
        """SignCancelOrder (TxTypeL2CancelOrder=15, data-structures-constants-and-errors) — заблокировано, как
        и ioc(). Не подключено к adapters.futures_bindings.Bindings.cancel: у Aster/Gate/Hyperliquid этот слот
        тоже не используется (bind() не передаёт cancel= вовсе) — IOC у всех четырёх venue либо исполняется,
        либо снимается биржей мгновенно, отдельного шага «снять отдыхающий ордер» в общем контракте нет."""
        m = self._market(symbol)
        market_id = _int(m.get("market_id"))
        if market_id is None:
            raise LighterError(None, f"lighter: {symbol}: market_id отсутствует или не число")
        self._check_mode(hedge=True)   # снятие заявки уменьшает риск — как и reduce_only, разрешено и на паузе
        signer = self._require_signer("cancel_order")
        idx = self.identity.api_key_index if api_key_index is None else api_key_index
        if idx is None:
            raise LighterError(None, "lighter: api_key_index не сконфигурирован")
        nonce = self.next_nonce(idx)
        tx_type, tx_info = signer.sign_cancel_order(market_index=market_id, order_index=order_index, nonce=nonce,
                                                    api_key_index=idx)
        self.http.post_form("/sendTx", {"tx_type": tx_type, "tx_info": tx_info})
        return PerpFill(str(order_index), order_index, "UNKNOWN", D(0), D(0), D(0), nonce)
