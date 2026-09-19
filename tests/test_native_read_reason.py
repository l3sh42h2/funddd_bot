"""19.09: отказ гарда OKX при котировке выхода AIW3 дошёл до Телеграма общей фразой «native read unavailable or
malformed» — причина терялась в NativeAdapter._read, и «продолжить» повторял вслепую. Теперь причина в тексте."""
import pytest
from funding_bot.trade.adapters.contracts import AdapterError, ErrorKind
from funding_bot.trade.adapters.native import NativeAdapter
from funding_bot.trade.evm_swap import GuardError


def test_read_keeps_guard_reason_in_error_text():
    def quote():
        raise GuardError("calldata_tail", "trim с 1 < котировки 2")
    with pytest.raises(AdapterError) as ei:
        NativeAdapter._read(quote)
    assert ei.value.kind == ErrorKind.TRANSIENT
    assert "calldata_tail" in str(ei.value) and "GuardError" in str(ei.value)


def test_read_passes_adapter_errors_through_unchanged():
    err = AdapterError(ErrorKind.IDENTITY, "EVM native scope mismatch")
    def quote():
        raise err
    with pytest.raises(AdapterError) as ei:
        NativeAdapter._read(quote)
    assert ei.value is err
