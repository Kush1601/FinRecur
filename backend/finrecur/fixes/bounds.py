"""F6 guardrails (spec 3.5: "2.9->4% is a disaster"). A proposal outside bounds is
never silently dropped -- the caller stores it with status=rejected and a ledger
reason "out_of_bounds", and a reviewer can propose an in-bounds edit instead."""

from dataclasses import dataclass

MIN_WIDENING_CLUSTER = 3
MAX_MOVE_FRACTION = 0.25
PERCENT_CEILING = 5.0


@dataclass(frozen=True)
class BoundsResult:
    within_bounds: bool
    reason: str | None = None


def check_bounds(
    key: str,
    before: object,
    after: object,
    is_widening: bool,
    cluster_size: int,
) -> BoundsResult:
    if isinstance(before, int | float) and isinstance(after, int | float) and before != 0:
        move = abs(after - before) / abs(before)
        if move > MAX_MOVE_FRACTION:
            return BoundsResult(
                False,
                f"move of {move:.1%} exceeds {MAX_MOVE_FRACTION:.0%} of the current value",
            )

    if "percent" in key and isinstance(after, int | float) and after > PERCENT_CEILING:
        return BoundsResult(False, f"after {after} exceeds the {PERCENT_CEILING}% percent ceiling")

    if is_widening and cluster_size < MIN_WIDENING_CLUSTER:
        return BoundsResult(
            False, f"widening needs a cluster of at least {MIN_WIDENING_CLUSTER} members"
        )

    return BoundsResult(True)
