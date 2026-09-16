"""Presentation of the private generic leg DTO; no financial calculations."""
from html import escape


def render(report):
    if report.get('version') != 2:
        raise ValueError('unsupported generic leg report')
    lines = []
    for leg in report['legs']:
        qty = 'не подтверждено' if leg['qty'] is None else str(leg['qty'])
        lines.append(f"<b>{escape(leg['leg_id'])}</b>: экспозиция {escape(qty)}")
        for key, label in (('fees', 'Комиссии'), ('funding', 'Фандинг'), ('rent_locked_delta', 'Изменение депозита')):
            if leg.get(key):
                values = ', '.join(f'{escape(str(amount))} {escape(currency)}' for currency, amount in leg[key].items())
                lines.append(f'{label}: {values}')
        if not leg['fees_complete']:
            lines.append('Комиссии подтверждены не полностью.')
        if not leg['cash_complete']:
            lines.append('Денежные потоки подтверждены не полностью.')
        basis = leg.get('cost_basis')
        if basis is not None:
            if basis['complete']:
                unit = escape(str(basis['quote_currency']))
                if basis['average_cost_quote'] is not None:
                    lines.append(f"Средняя стоимость остатка: {escape(str(basis['average_cost_quote']))} {unit}.")
                if basis['realized_pnl_quote'] is not None:
                    lines.append(f"Реализованный спот-результат: {escape(str(basis['realized_pnl_quote']))} {unit}.")
            else:
                lines.append('Cost basis подтверждён не полностью: ' + escape(', '.join(basis['reasons'])) + '.')
        if leg.get('market_kind') == 'perpetual' and not leg.get('funding_complete'):
            lines.append('Полнота истории фандинга не подтверждена.')
    if report.get('pnl') is None:
        lines.append('PnL: нет подтверждённой оценки.')
    return '\n'.join(lines)
