"""Истины бирж для тестировщика (audit.py). Реестр TRUTHS = {площадка: класс} собирается сам: каждый модуль пакета
(кроме base и модулей на «_») импортируется, и каждый класс, у которого venue — площадка из config.PERP_VENUES и есть
markets/rates/history, встаёт в реестр. Новая истина = новый файл в пакете, реестр править не нужно (в одном модуле
может жить несколько площадок: lighter и lighter_rh). Модуль, который не импортируется, в реестр не попадает — причина
в TRUTH_ERRORS, и сводка --all показывает её как «не проверено».
"""
from __future__ import annotations
import importlib, inspect, pkgutil
from .. import config
from .base import Truth

TRUTHS: dict[str, type] = {}
TRUTH_ERRORS: dict[str, str] = {}


def _is_truth(obj) -> bool:
    return inspect.isclass(obj) and isinstance(getattr(obj, "venue", None), str) and obj.venue in config.PERP_VENUES \
        and all(callable(getattr(obj, m, None)) for m in ("markets", "rates", "history"))


def _load():
    for info in sorted(pkgutil.iter_modules(__path__), key=lambda i: i.name):
        if info.name == "base" or info.name.startswith("_"):
            continue
        try:
            mod = importlib.import_module(f"{__name__}.{info.name}")
        except Exception as e:  # noqa — сломанная истина одной площадки не роняет остальные
            TRUTH_ERRORS[info.name] = f"{type(e).__name__}: {e}"
            continue
        for _n, obj in inspect.getmembers(mod, _is_truth):
            if obj.__module__ != mod.__name__:
                continue                       # импортированный из другого модуля класс считается там, где определён
            if obj.venue in TRUTHS and TRUTHS[obj.venue] is not obj:
                TRUTH_ERRORS[obj.venue] = f"две истины: {TRUTHS[obj.venue].__module__} и {obj.__module__}"
                continue
            TRUTHS[obj.venue] = obj


_load()

__all__ = ["TRUTHS", "TRUTH_ERRORS", "Truth"]
