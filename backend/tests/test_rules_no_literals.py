"""Rule modules read every threshold from the policy dict -- no hard-coded numbers.
AST-based (not textual grep) so string literals like "R5" don't trip it up."""

import ast
from pathlib import Path

RULES_DIR = Path(__file__).resolve().parents[1] / "finrecur" / "rules"
ALLOWED = {0, 1, 100}


def test_no_numeric_literals_other_than_0_1_100():
    offenders = []
    for path in sorted(RULES_DIR.glob("*.py")):
        tree = ast.parse(path.read_text(), filename=str(path))
        for node in ast.walk(tree):
            if isinstance(node, ast.Constant) and isinstance(node.value, (int, float)):
                if not isinstance(node.value, bool) and node.value not in ALLOWED:
                    offenders.append((str(path), node.lineno, node.value))
    assert not offenders, f"numeric literals outside {{0,1,100}} in finrecur/rules/: {offenders}"
