"""Library-only packages stay out of the image and out of every runtime path (bead contextbroker-c8g).

governance_gate, action_lifecycle, decision_records, investigation_workers and loop_coordination are kept
in the repository with their tests, but nothing the API, ingestion, the agent or the scripts import uses
them, and an executor that applies actions has no place in the running container ("the broker never decides
or acts"). platform_services and source_federation are empty placeholders."""
import ast
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
UNWIRED = ("governance_gate", "action_lifecycle", "decision_records", "investigation_workers",
           "loop_coordination", "platform_services", "source_federation")
SKIPPED_TOP = {"venv", ".git", "tests", "docs", ".planning", ".beads", "node_modules", "lodlive"}


def _imports(path: Path) -> set[str]:
    tree = ast.parse(path.read_text(), filename=str(path))
    found = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            found.update(a.name.split(".")[0] for a in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module and node.level == 0:
            found.add(node.module.split(".")[0])
    return found


def _runtime_files():
    for path in sorted(ROOT.rglob("*.py")):
        top = path.relative_to(ROOT).parts[0]
        if top in SKIPPED_TOP or top in UNWIRED or "venv" in path.parts or "__pycache__" in path.parts:
            continue
        yield path


def test_the_image_leaves_the_unwired_packages_out():
    ignored = {line.strip().rstrip("/") for line in (ROOT / ".dockerignore").read_text().splitlines()
               if line.strip() and not line.startswith("#")}
    assert set(UNWIRED) <= ignored


def test_the_unwired_packages_still_exist_in_the_repository():
    # Kept on purpose: the decision is "out of the image", not deletion.
    for name in ("governance_gate", "action_lifecycle", "decision_records", "investigation_workers",
                 "loop_coordination"):
        assert any((ROOT / name).glob("*.py")), name


def test_nothing_the_image_ships_imports_an_unwired_package():
    offenders = {str(p.relative_to(ROOT)): sorted(_imports(p) & set(UNWIRED)) for p in _runtime_files()
                 if _imports(p) & set(UNWIRED)}
    assert offenders == {}

