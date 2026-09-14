"""Pure exposure rules shared by operation coordinators; quantities are never prices.

The two historical rounding policies are explicit: rounding a change (legacy EVM)
and rounding a target (SOL). They agree on grid-aligned positions but must not be
silently substituted for each other when recovering off-grid historical fills.
"""
from dataclasses import dataclass
from decimal import Decimal as D, ROUND_FLOOR, ROUND_CEILING


@dataclass(frozen=True)
class HedgeDecision:
    side: str | None
    quantity: D
    reduce_only: bool = False
    surplus: bool = False
    deficit: bool = False


@dataclass(frozen=True)
class Exposure:
    spot_multiplier: D
    hedge_multiplier: D
    hedge_step: D

    def __post_init__(self):
        for value in (self.spot_multiplier, self.hedge_multiplier, self.hedge_step):
            if not isinstance(value, D) or not value.is_finite() or value <= 0:
                raise ValueError('exposure units must be finite positive Decimal')

    @property
    def token_step(self):
        return self.hedge_step * self.hedge_multiplier / self.spot_multiplier

    def delta(self, tokens, short):
        return tokens - short * self.hedge_multiplier / self.spot_multiplier

    def target(self, tokens):
        return self._round(tokens * self.spot_multiplier / self.hedge_multiplier)

    def _round(self, contracts, rounding=ROUND_FLOOR):
        return (contracts / self.hedge_step).to_integral_value(rounding) * self.hedge_step

    def decide(self, tokens, short, kind, *, rounding='change', last_full=False, capacity=None):
        for value in (tokens, short):
            if not isinstance(value, D) or not value.is_finite():
                raise ValueError('position must be a finite Decimal')
        if short < 0 or kind not in {'entry', 'exit', 'rehedge'}:
            raise ValueError('invalid hedge position or operation')
        if capacity is not None and (not isinstance(capacity, D) or not capacity.is_finite() or capacity < 0):
            raise ValueError('invalid approved hedge capacity')
        if rounding == 'target':
            target = self.target(tokens)
            desired = min(target, capacity) if capacity is not None and kind == 'entry' else target
            change = desired - short
            surplus = capacity is not None and kind == 'entry' and target > capacity
            deficit = kind == 'exit' and change > 0
            if change > 0 and kind != 'exit':
                return HedgeDecision('SELL', change, surplus=surplus)
            if change < 0 and kind != 'entry':
                return HedgeDecision('BUY', -change, True)
            return HedgeDecision(None, D(0), surplus=surplus, deficit=deficit)
        if rounding != 'change' or capacity is not None:
            raise ValueError('unsupported rounding policy')
        delta = self.delta(tokens, short)
        ratio = self.hedge_multiplier / self.spot_multiplier
        if last_full and tokens < self.token_step:
            return HedgeDecision('BUY', short, True)
        if kind in {'entry', 'rehedge'} and delta >= self.token_step:
            return HedgeDecision('SELL', self._round(delta / ratio))
        if kind in {'exit', 'rehedge'} and delta < 0:
            return HedgeDecision('BUY', min(self._round(-delta / ratio, ROUND_CEILING), short), True)
        return HedgeDecision(None, D(0))
