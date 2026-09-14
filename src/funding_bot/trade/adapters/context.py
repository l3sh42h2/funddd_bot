"""Core-owned binding table: each factory receives only the requested leg's resources."""
from .contracts import AdapterError, ErrorKind


class AdapterContext:
    def __init__(self):
        self._legs = {}

    def add(self, spec, bindings):
        if spec.leg_id in self._legs:
            raise AdapterError(ErrorKind.CONFIG, 'leg resources already registered')
        self._legs[spec.leg_id] = (spec, bindings)

    def for_leg(self, spec):
        stored = self._legs.get(spec.leg_id)
        if stored is None or stored[0] != spec:
            raise AdapterError(ErrorKind.IDENTITY, 'no resources for this frozen leg specification')
        return stored[1]
