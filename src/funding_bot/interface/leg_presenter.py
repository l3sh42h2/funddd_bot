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
        if leg.get('market_kind') == 'perpetual' and not leg.get('funding_complete'):
            lines.append('Полнота истории фандинга не подтверждена.')
    if report.get('pnl') is None:
        lines.append('PnL: нет подтверждённой оценки.')
    return '\n'.join(lines)
