"""Versioned value contracts; no exchange, wallet, database or UI imports."""
from dataclasses import dataclass, asdict
from decimal import Decimal
from enum import StrEnum
from typing import Protocol
import math
import hashlib
import json

D = Decimal


class ErrorKind(StrEnum):
    INVALID = 'invalid_request'
    UNSUPPORTED = 'unsupported'
    FUNDS = 'insufficient_funds'
    REJECTED = 'rejected'
    RATE_LIMIT = 'rate_limited'
    TRANSIENT = 'transient'
    UNKNOWN = 'unknown_result'
    IDENTITY = 'identity_mismatch'
    STALE = 'stale_data'
    CONFIG = 'configuration'


class AdapterError(RuntimeError):
    def __init__(self, kind: ErrorKind, message: str):
        self.kind = kind
        super().__init__(message)


class Status(StrEnum):
    ACCEPTED = 'ACCEPTED'
    PARTIAL = 'PARTIALLY_FILLED'
    SETTLED = 'SETTLED'
    CANCELLED = 'CANCELLED'
    REJECTED = 'REJECTED'
    UNKNOWN = 'UNKNOWN'


class RejectionKind(StrEnum):
    PRECISION = 'precision'
    REDUCE_ONLY = 'reduce_only'
    OTHER = 'other'


def decimal(value, name, *, positive=False):
    if not isinstance(value, D) or not value.is_finite() or (positive and value <= 0):
        raise AdapterError(ErrorKind.INVALID, f'{name}: finite Decimal required')
    return value


@dataclass(frozen=True)
class Capabilities:
    venue_kind: str
    market_kind: str
    network_family: str | None = None
    short: bool = False
    reduce_only: bool = False
    price_trigger: bool = False
    cancel: bool = False
    amount: bool = False
    quantity: bool = True

    def __post_init__(self):
        if self.venue_kind not in {'cex', 'dex'} or self.market_kind not in {'spot', 'perpetual'}:
            raise AdapterError(ErrorKind.CONFIG, 'unknown venue/market kind')
        if self.network_family not in {None, 'evm', 'solana'}:
            raise AdapterError(ErrorKind.CONFIG, 'unknown network family')
        if self.market_kind == 'spot' and self.short:
            raise AdapterError(ErrorKind.UNSUPPORTED, 'spot borrowing is not modeled')


@dataclass(frozen=True)
class LegSpec:
    leg_id: str
    role: str
    direction: str
    adapter_id: str
    venue: str
    account: str
    instrument: str
    asset_id: str
    identity_evidence: str
    multiplier: D
    step: D
    tick: D
    quote_currency: str
    settlement_currency: str
    capabilities: Capabilities
    network: str | None = None
    subaccount: str | None = None
    decimals: int | None = None
    margin_currency: str | None = None
    fee_currencies: tuple[str, ...] = ()
    metadata_revision: str = ''
    version: int = 1
    # Original frozen record is a reference, never reserialized or written back.
    legacy_hash: str | None = None
    # Appended in M4 so legacy positional LegSpec construction remains stable.
    quote_decimals: int | None = None

    def __post_init__(self):
        if self.version != 1 or self.direction not in {'long', 'short'}:
            raise AdapterError(ErrorKind.CONFIG, 'unsupported leg version/direction')
        for name in ('leg_id', 'role', 'adapter_id', 'venue', 'account', 'instrument', 'asset_id',
                     'identity_evidence', 'quote_currency', 'settlement_currency', 'metadata_revision'):
            if not isinstance(getattr(self, name), str) or not getattr(self, name):
                raise AdapterError(ErrorKind.IDENTITY, f'missing {name}')
        for name in ('multiplier', 'step', 'tick'):
            decimal(getattr(self, name), name, positive=True)
        for name in ('decimals', 'quote_decimals'):
            value = getattr(self, name)
            if value is not None and (type(value) is not int or not 0 <= value <= 36):
                raise AdapterError(ErrorKind.INVALID, f'invalid {name}')
        if self.direction == 'short' and not self.capabilities.short:
            raise AdapterError(ErrorKind.UNSUPPORTED, 'leg cannot hold short exposure')
        if self.capabilities.network_family and not self.network:
            raise AdapterError(ErrorKind.IDENTITY, 'network identity required')

    @property
    def fingerprint(self):
        return hashlib.sha256(json.dumps(asdict(self), sort_keys=True, default=str, ensure_ascii=False).encode()).hexdigest()

    @property
    def scope(self):
        return (self.venue, self.network, self.account, self.subaccount, self.instrument)

    def exposure(self, quantity: D):
        return decimal(quantity, 'quantity') * self.multiplier


@dataclass(frozen=True)
class Action:
    action_id: str
    leg_id: str
    side: str
    quantity: D
    reduce_only: bool = False

    def validate(self, spec: LegSpec):
        decimal(self.quantity, 'quantity', positive=True)
        if not self.action_id or self.leg_id != spec.leg_id or self.side not in {'BUY', 'SELL'}:
            raise AdapterError(ErrorKind.INVALID, 'invalid action identity/side')
        if self.quantity % spec.step:
            raise AdapterError(ErrorKind.INVALID, 'quantity is off step')
        if self.reduce_only and not spec.capabilities.reduce_only:
            raise AdapterError(ErrorKind.UNSUPPORTED, 'native reduce-only unavailable')


@dataclass(frozen=True)
class Observation:
    quantity: D | None
    as_of: float
    source: str
    quality: str
    available: D | None = None
    currency: str | None = None


@dataclass(frozen=True)
class Quote:
    action: Action
    expires_at: float
    max_spend: D
    min_receive: D
    spend_currency: str
    receive_currency: str
    # Native request is private, immutable JSON. No signing material here.
    native: str = '{}'
    costs: tuple = ()

    def __post_init__(self):
        if isinstance(self.expires_at, bool) or not isinstance(self.expires_at, (int, float)) or not math.isfinite(self.expires_at):
            raise AdapterError(ErrorKind.INVALID, "quote expiry must be finite")
        for name in ('max_spend', 'min_receive'):
            if decimal(getattr(self, name), name) < 0:
                raise AdapterError(ErrorKind.INVALID, 'negative quote bounds')
        if not self.spend_currency or not self.receive_currency:
            raise AdapterError(ErrorKind.INVALID, 'quote currencies required')
        if not isinstance(json.loads(self.native), dict):
            raise AdapterError(ErrorKind.INVALID, 'native request must be object')

    @property
    def fingerprint(self):
        a = self.action
        data = (a.action_id, a.leg_id, a.side, str(a.quantity), a.reduce_only, self.expires_at,
                str(self.max_spend), str(self.min_receive), self.spend_currency, self.receive_currency, self.native)
        return hashlib.sha256(json.dumps(data, ensure_ascii=False).encode()).hexdigest()


@dataclass(frozen=True)
class Prepared:
    attempt_id: str
    quote: Quote
    spec_hash: str = ""


@dataclass(frozen=True)
class NativeRef:
    kind: str
    id: str

    def __post_init__(self):
        if not isinstance(self.kind, str) or not self.kind or not isinstance(self.id, str) or not self.id:
            raise AdapterError(ErrorKind.INVALID, 'native reference kind/id required')


@dataclass(frozen=True)
class RawAmount:
    asset_id: str
    raw: int
    decimals: int

    def __post_init__(self):
        if not isinstance(self.asset_id, str) or not self.asset_id:
            raise AdapterError(ErrorKind.IDENTITY, 'raw amount asset required')
        if type(self.raw) is not int or self.raw < 0:
            raise AdapterError(ErrorKind.INVALID, 'raw amount must be a non-negative integer')
        if type(self.decimals) is not int or not 0 <= self.decimals <= 36:
            raise AdapterError(ErrorKind.INVALID, 'raw amount decimals invalid')

    @property
    def amount(self):
        return D(self.raw) / D(10) ** self.decimals


@dataclass(frozen=True)
class QuoteAmount:
    amount: D
    currency: str

    def __post_init__(self):
        if decimal(self.amount, 'quote amount') < 0 or not isinstance(self.currency, str) or not self.currency:
            raise AdapterError(ErrorKind.INVALID, 'quote amount/currency invalid')


@dataclass(frozen=True)
class Result:
    status: Status
    executed_quantity: D | None
    finality: str
    provisional: bool
    evidence: tuple[str, ...]
    fees: tuple = ()
    terminal: bool = False
    error: ErrorKind | None = None
    fees_complete: bool = False
    version: int = 1
    leg_id: str | None = None
    spec_hash: str | None = None
    scope: tuple | None = None
    native_ref: NativeRef | None = None
    spot_input_raw: RawAmount | None = None
    spot_output_raw: RawAmount | None = None
    perp_quote: QuoteAmount | None = None
    trade_notional: QuoteAmount | None = None
    avg_price: QuoteAmount | None = None
    rejection_kind: RejectionKind | None = None

    def __post_init__(self):
        if self.executed_quantity is not None and decimal(self.executed_quantity, 'executed quantity') < 0:
            raise AdapterError(ErrorKind.INVALID, 'negative executed quantity')
        if not self.evidence or not self.finality:
            raise AdapterError(ErrorKind.INVALID, 'outcome evidence/finality required')
        if self.status == Status.UNKNOWN and self.terminal:
            raise AdapterError(ErrorKind.INVALID, 'unknown outcome is not terminal')
        if self.rejection_kind is not None:
            if not isinstance(self.rejection_kind, RejectionKind) or self.status != Status.REJECTED:
                raise AdapterError(ErrorKind.INVALID, 'rejection kind requires a rejected result')
        if self.status == Status.SETTLED and (self.provisional or not self.terminal or self.executed_quantity is None):
            raise AdapterError(ErrorKind.INVALID, 'settled requires proven execution')
        if self.version not in (1, 2):
            raise AdapterError(ErrorKind.INVALID, 'unsupported result version')
        if self.version == 2:
            if (not isinstance(self.leg_id, str) or not self.leg_id or
                    not isinstance(self.spec_hash, str) or not self.spec_hash or not isinstance(self.scope, tuple)
                    or not self.scope or not isinstance(self.native_ref, NativeRef)):
                raise AdapterError(ErrorKind.IDENTITY, 'v2 result identity incomplete')
            one_spot = (self.spot_input_raw is None) != (self.spot_output_raw is None)
            if one_spot:
                raise AdapterError(ErrorKind.INVALID, 'spot input/output must be paired')
            one_perp = (self.perp_quote is None) != (self.trade_notional is None)
            if one_perp:
                raise AdapterError(ErrorKind.INVALID, 'perpetual quote/notional must be paired')
            if self.spot_input_raw is not None and self.perp_quote is not None:
                raise AdapterError(ErrorKind.INVALID, 'spot and perpetual accounting cannot be mixed')
            if self.executed_quantity is not None and self.executed_quantity > 0:
                spot_complete = self.spot_input_raw is not None and self.spot_output_raw is not None
                perp_complete = self.perp_quote is not None and self.trade_notional is not None
                if not (spot_complete or perp_complete):
                    raise AdapterError(ErrorKind.INVALID, 'v2 execution accounting incomplete')
            if (self.status in {Status.SETTLED, Status.PARTIAL} and
                    (self.executed_quantity is None or self.executed_quantity <= 0)):
                raise AdapterError(ErrorKind.INVALID, 'v2 settled/partial execution must be positive')


@dataclass(frozen=True)
class ExecutionPage:
    executions: tuple
    cursor: str | None
    complete: bool


class Adapter(Protocol):
    def capabilities(self) -> Capabilities: ...
    def describe(self) -> LegSpec: ...
    def observe(self) -> Observation: ...
    def quote(self, action: Action, bounds: dict) -> Quote: ...
    def prepare(self, attempt_id: str, quote: Quote) -> Prepared: ...
    def submit(self, prepared: Prepared) -> Result: ...
    def resolve(self, attempt_ref: str) -> Result: ...
    def cancel(self, attempt_ref: str) -> Result: ...
    def read_executions(self, cursor: str | None) -> ExecutionPage: ...
