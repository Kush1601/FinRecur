"""Enforces the two hard boundaries from CLAUDE.md: finrecur/ never imports api/, and
data/faults.json is read only by scripts/seed.py and scripts/eval.py."""

import ast
from pathlib import Path

BACKEND_ROOT = Path(__file__).resolve().parents[1]
REPO_ROOT = BACKEND_ROOT.parent


def _python_files(root: Path) -> list[Path]:
    return [p for p in root.rglob("*.py") if "__pycache__" not in p.parts]


def test_finrecur_never_imports_api():
    finrecur_dir = BACKEND_ROOT / "finrecur"
    offenders = []
    for path in _python_files(finrecur_dir):
        tree = ast.parse(path.read_text(), filename=str(path))
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                names = [alias.name for alias in node.names]
            elif isinstance(node, ast.ImportFrom):
                names = [node.module] if node.module else []
            else:
                continue
            if any(name and (name == "api" or name.startswith("api.")) for name in names):
                offenders.append(str(path))
    assert not offenders, f"finrecur/ modules importing api/: {offenders}"


def test_faults_json_read_only_by_allowed_scripts():
    """Only scripts/seed.py and scripts/eval.py may reference faults.json outside of
    backend/tests/ -- tests are allowed to load the manifest to check fault behaviour."""
    tests_dir = BACKEND_ROOT / "tests"
    offenders = [
        str(path)
        for path in _python_files(BACKEND_ROOT)
        if tests_dir not in path.parents and "faults.json" in path.read_text()
    ]
    assert not offenders, (
        f"faults.json referenced outside scripts/seed.py and scripts/eval.py: {offenders}"
    )

    seed_script = REPO_ROOT / "scripts" / "seed.py"
    assert seed_script.exists() and "faults.json" in seed_script.read_text()
