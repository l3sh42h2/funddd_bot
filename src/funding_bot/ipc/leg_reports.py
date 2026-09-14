"""Private, serializable DTO for two independent leg accounting reports."""
from dataclasses import dataclass
from decimal import Decimal
from typing import Any


@dataclass(frozen=True)
class LegReport:
    leg_id: str
    spec_hash: str
    scope: str
    qty: Decimal
    executions: int
    fees: dict[str, Decimal]
    unknown_fees: int
    fees_complete: bool
    funding: dict[str, Decimal]


@dataclass(frozen=True)
class LegAccountingReport:
    version: int
    operation_id: str | None
    legs: tuple[LegReport, ...]


def from_projection(projection: dict[str, Any]) -> LegAccountingReport:
    return LegAccountingReport(
        version=int(projection["version"]), operation_id=projection.get("operation_id"),
        legs=tuple(LegReport(leg_id=x["leg_id"], spec_hash=x["spec_hash"], scope=x["scope"],
            qty=Decimal(x["qty"]), executions=int(x["executions"]),
            fees={k: Decimal(v) for k, v in x.get("fees", {}).items()},
            unknown_fees=int(x.get("unknown_fees", 0)),
            fees_complete=bool(x.get("fees_complete", False)),
            funding={k: Decimal(v) for k, v in x.get("funding", {}).items()})
            for x in projection.get("legs", ())))


def to_dict(report: LegAccountingReport) -> dict[str, Any]:
    return {"version": report.version, "operation_id": report.operation_id,
            "legs": [{"leg_id": x.leg_id, "spec_hash": x.spec_hash, "scope": x.scope,
                       "qty": str(x.qty), "executions": x.executions,
                       "fees": {k: str(v) for k, v in x.fees.items()},
                       "unknown_fees": x.unknown_fees, "fees_complete": x.fees_complete,
                       "funding": {k: str(v) for k, v in x.funding.items()}}
                     for x in report.legs]}
