"""Registration template, NOT an implemented/live venue.

Implement make_bindings with scoped API credentials, account/instrument verification,
quote bounds, authoritative resolution and paginated execution evidence. Until then
construction fails explicitly. Reuse core journal/authorize; do not introduce wallet/gas.
"""
from funding_bot.trade.adapters.native import NativeAdapter
from funding_bot.trade.adapters.contracts import AdapterError, ErrorKind, Result


class NewCexSpot(NativeAdapter):
    market_kind = 'spot'
    network_family = None

    def normalize(self, native, side):
        # Replace with venue status/partial/finality/fee mapping and its contract tests.
        if not isinstance(native, Result):
            raise AdapterError(ErrorKind.UNSUPPORTED, 'implement native result normalization')
        return native


def make_bindings(spec, context):
    raise AdapterError(ErrorKind.UNSUPPORTED, 'implement and test venue bindings before registration')


def register(registry):
    registry.register('new_cex_spot', lambda spec, context: NewCexSpot(spec, make_bindings(spec, context)))
