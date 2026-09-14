"""Lossless native identifiers for strict history readers."""
import re


def exact_id(value):
    return ((type(value) is int and value >= 0) or
            (type(value) is str and re.fullmatch(r'0|[1-9][0-9]*', value) is not None))
