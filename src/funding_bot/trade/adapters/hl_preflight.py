"""Hyperliquid-only preflight; the operation passes frozen identity and expected position."""
from decimal import Decimal as D
from ..keys import redact
from ..spot_router import margin_for


class PreflightRefused(RuntimeError):
    def __init__(self, code, detail):
        self.code = code
        super().__init__(detail)


def agent_gate(perp, *, sim, what):
    if not sim:
        why = perp.agent_refusals()
        if why:
            raise PreflightRefused('hl_agent', '; '.join(why) + f' — {what}')


def entry(perp, inst, *, sim, leverage, capacity, fee_rate, reserve, expected_short, book_levels):
    try:
        ref = perp.identity()
    except Exception as e:
        raise PreflightRefused('hl_meta', f'мета Hyperliquid не прочитана: {redact(e)}') from None
    if ref.fullcoin != inst.perp_symbol or ref.is_delisted or (
            inst.perp_asset_id is not None and ref.asset != inst.perp_asset_id):
        raise PreflightRefused('hl_meta', f'рынок {inst.perp_symbol} изменился (asset {ref.asset}) — нужен новый план')
    if leverage is None:
        if sim:
            return
        raise PreflightRefused('owner_missing', 'плечо perp.hyperliquid.leverage не задано')
    if not sim:
        book = perp.book(inst.perp_symbol, book_levels)
        need = margin_for(capacity, book.asks[0][0], D(leverage), fee_rate()) if book.asks else None
        if need is None or need <= 0:
            raise PreflightRefused('margin', 'маржа под шорт не посчитана (пустые аски или нулевой объём)')
        refusals = perp.entry_margin_refusals(need, reserve)
        if refusals:
            raise PreflightRefused('margin', '; '.join(refusals) + ' — своп не начинаю')
        pos = perp.position(inst.perp_symbol)
        if pos is None:
            raise PreflightRefused('position_unknown', f'позиция {inst.perp_symbol} не прочитана — своп не начинаю')
        if expected_short is None or pos != -expected_short:
            raise PreflightRefused('position_mismatch', f'позиция HL {pos} ≠ журнал сделки {-(expected_short or 0)} — своп не начинаю')
        agent_gate(perp, sim=sim, what='своп не начинаю')
    try:
        perp.setup(inst.perp_symbol, int(leverage), 'ISOLATED')
    except Exception as e:
        raise PreflightRefused('setup', f'настройка {inst.perp_symbol}: {redact(e)}') from None
