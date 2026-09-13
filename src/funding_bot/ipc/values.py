"""Lossless Decimal DTO codec. No Python object deserialization."""
from decimal import Decimal


def encode(value):
    if isinstance(value, Decimal):
        if not value.is_finite():
            raise ValueError('nonfinite decimal')
        return {'$decimal': str(value)}
    if isinstance(value, dict):
        return {k: encode(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [encode(v) for v in value]
    return value


def decode(value):
    if isinstance(value, dict):
        if set(value) == {'$decimal'}:
            d = Decimal(value['$decimal'])
            if not d.is_finite():
                raise ValueError('nonfinite decimal')
            return d
        return {k: decode(v) for k, v in value.items()}
    if isinstance(value, list):
        return [decode(v) for v in value]
    return value
