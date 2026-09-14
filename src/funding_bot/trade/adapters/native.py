"""Common boundary around the existing native executors, not a second executor.

Bindings are installed by the core composition root, one scope at a time. Network
requests, signing, allowance and recovery stay native. The barrier must use core DB.
"""
from dataclasses import dataclass
from decimal import Decimal as D
import time
from typing import Callable
from .contracts import (Action, AdapterError, ErrorKind, Observation, Prepared, Quote,
                        Result, Status, ExecutionPage)
from . import outcomes


@dataclass
class Bindings:
    native: object
    journal: object
    authorize: Callable
    quote: Callable
    submit: Callable
    resolve: Callable
    observe: Callable
    executions: Callable
    cancel: Callable | None = None
    clock: Callable = time.time
    authorize_cancel: Callable | None = None


class NativeAdapter:
    market_kind = None
    network_family = None

    def __init__(self, spec, bindings: Bindings):
        self.spec, self.bindings = spec, bindings
        if spec.capabilities.market_kind != self.market_kind:
            raise AdapterError(ErrorKind.CONFIG, 'wrong adapter market kind')
        if spec.capabilities.network_family != self.network_family:
            raise AdapterError(ErrorKind.CONFIG, 'wrong adapter network family')

    def capabilities(self):
        return self.spec.capabilities

    def describe(self):
        return self.spec

    @staticmethod
    def _read(fn, *args):
        try:
            return fn(*args)
        except AdapterError:
            raise
        except Exception:
            raise AdapterError(ErrorKind.TRANSIENT, 'native read unavailable or malformed') from None

    def observe(self):
        result = self._read(self.bindings.observe, self.spec)
        if not isinstance(result, Observation):
            raise AdapterError(ErrorKind.CONFIG, 'invalid native observation mapping')
        return result

    def quote(self, action: Action, bounds):
        action.validate(self.spec)
        result = self._read(self.bindings.quote, self.spec, action, bounds)
        if not isinstance(result, Quote) or result.action != action:
            raise AdapterError(ErrorKind.IDENTITY, 'quote action mismatch')
        self._fresh(result)
        return result

    def _fresh(self, quote):
        quote.action.validate(self.spec)
        if quote.expires_at <= self.bindings.clock():
            raise AdapterError(ErrorKind.STALE, 'quote expired')

    def prepare(self, attempt_id: str, quote: Quote):
        self._fresh(quote)
        if not attempt_id:
            raise AdapterError(ErrorKind.INVALID, 'attempt id required')
        prepared = Prepared(attempt_id, quote, self.spec.fingerprint)
        self.bindings.journal.prepare(prepared)
        return prepared

    def submit(self, prepared: Prepared):
        if prepared.spec_hash != self.spec.fingerprint:
            raise AdapterError(ErrorKind.IDENTITY, "prepared leg specification changed")
        self._fresh(prepared.quote)
        self.bindings.authorize(self.spec, prepared.quote.action)
        self.bindings.journal.claim(prepared)
        try:
            native = self.bindings.submit(self.spec, prepared)
            return self.normalize(native, prepared.quote.action.side)
        except Exception:
            # After claim an exception cannot prove zero execution, even a parser error.
            return Result(Status.UNKNOWN, None, 'unresolved', True,
                          (f'attempt:{prepared.attempt_id}',), error=ErrorKind.UNKNOWN)

    def resolve(self, attempt_ref):
        # Native resolver must recover side/amounts from its persisted attempt, not a new plan.
        native, side = self._read(self.bindings.resolve, self.spec, attempt_ref)
        if side not in {'BUY', 'SELL'}:
            raise AdapterError(ErrorKind.IDENTITY, 'stored attempt side is unknown')
        return self.normalize(native, side)

    def cancel(self, attempt_ref):
        if not self.spec.capabilities.cancel or self.bindings.cancel is None or self.bindings.authorize_cancel is None:
            raise AdapterError(ErrorKind.UNSUPPORTED, 'native cancellation unavailable')
        self.bindings.authorize_cancel(self.spec, attempt_ref)
        try:
            native, side = self.bindings.cancel(self.spec, attempt_ref)
            return self.normalize(native, side)
        except Exception:
            return Result(Status.UNKNOWN, None, 'unresolved', True,
                          (f'cancel:{attempt_ref}',), error=ErrorKind.UNKNOWN)

    def read_executions(self, cursor):
        page = self._read(self.bindings.executions, self.spec, cursor)
        if not isinstance(page, ExecutionPage):
            raise AdapterError(ErrorKind.CONFIG, 'native execution pagination mapping required')
        return page


class FuturesAdapter(NativeAdapter):
    market_kind = 'perpetual'

    def normalize(self, native, side):
        return outcomes.perpetual(native, self.spec.scope)


class EvmSpotAdapter(NativeAdapter):
    market_kind = 'spot'
    network_family = 'evm'

    def normalize(self, native, side):
        if self.spec.decimals is None:
            raise AdapterError(ErrorKind.IDENTITY, 'token decimals unknown')
        return outcomes.evm_swap(native, self.spec, side)


class SolanaSpotAdapter(NativeAdapter):
    market_kind = 'spot'
    network_family = 'solana'

    def normalize(self, native, side):
        if self.spec.decimals is None:
            raise AdapterError(ErrorKind.IDENTITY, 'token decimals unknown')
        return outcomes.sol_swap(native, self.spec, side)
