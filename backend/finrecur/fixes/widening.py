"""Classifies an amend_policy edit as widening, narrowing, or unsupported (spec
3.5/3.9). Widening = R2 window up, R3 threshold up, R4 percent up, a cap removed
(numeric -> null), or R4.must_fit_shipping true -> false. An edit this detector
doesn't recognise (unknown rule/key, or a structural change) is `unsupported`,
which the caller treats as widening for approval purposes -- never as a free pass."""

from typing import Literal

Classification = Literal["widening", "narrowing", "unsupported"]

# (rule_id, key) -> True means "a larger value is wider" for that setting.
_UP_IS_WIDENING = {
    ("R2", "window_days"): True,
    ("R3", "threshold_centavos"): True,
    ("R4", "max_percent"): True,
    ("R7", "tolerance_centavos"): True,
}


def classify(rule_id: str, key: str, before: object, after: object) -> Classification:
    if rule_id == "R4" and key == "must_fit_shipping":
        if before is True and after is False:
            return "widening"
        if before is False and after is True:
            return "narrowing"
        return "unsupported"

    if (rule_id, key) not in _UP_IS_WIDENING:
        return "unsupported"

    if before is not None and after is None:
        return "widening"  # cap removed
    if before is None and after is not None:
        return "narrowing"  # cap added

    if not isinstance(before, int | float) or not isinstance(after, int | float):
        return "unsupported"
    if after > before:
        return "widening"
    if after < before:
        return "narrowing"
    return "unsupported"  # no actual change; caller should have rejected this earlier
