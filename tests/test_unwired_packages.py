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


def _allow_list():
    """The names `.dockerignore` re-includes after its leading `*`, at the top level."""
    lines = [line.strip() for line in (ROOT / ".dockerignore").read_text().splitlines()
             if line.strip() and not line.startswith("#")]
    assert lines[0] == "*", "the image context is an allow-list: ignore everything first"
    return {line[1:].rstrip("/") for line in lines if line.startswith("!") and "/" not in line[1:].rstrip("/")}


def _local_top_levels():
    return {p.name.removesuffix(".py") for p in ROOT.iterdir()
            if (p.is_dir() and any(p.glob("*.py"))) or (p.suffix == ".py")}


def test_the_image_context_is_an_allow_list_without_the_unwired_packages():
    allowed = _allow_list()
    assert allowed.isdisjoint(UNWIRED)
    assert {"broker_engine", "broker_state", "data_contracts", "okf_retrieval", "artefact_governance",
            "ingestion", "intent_management", "outcome_assurance", "state_reconciliation",
            "requirements.txt"} <= allowed


def test_every_local_package_the_runtime_imports_is_in_the_image():
    # From the API, the commands run inside it and the two scripts, follow the imports to a fixed point.
    entry = ["broker_engine", "ingestion", "agent_harness", "okf_retrieval", "artefact_governance",
             "broker_state", "data_contracts", "intent_management", "outcome_assurance",
             "state_reconciliation"]
    local = _local_top_levels()
    reached = set(entry) | {"broker", "seed_graph"}
    queue = list(reached)
    while queue:
        name = queue.pop()
        target = ROOT / name
        files = list(target.rglob("*.py")) if target.is_dir() else [ROOT / f"{name}.py"]
        for path in files:
            if "__pycache__" in path.parts:
                continue
            for imported in _imports(path) & local:
                if imported not in reached:
                    reached.add(imported)
                    queue.append(imported)
    allowed = _allow_list()
    assert sorted(n for n in reached if n not in allowed and f"{n}.py" not in allowed) == []
    assert reached.isdisjoint(UNWIRED)


def test_the_unwired_packages_still_exist_in_the_repository():
    # Kept on purpose: the decision is "out of the image", not deletion.
    for name in ("governance_gate", "action_lifecycle", "decision_records", "investigation_workers",
                 "loop_coordination"):
        assert any((ROOT / name).glob("*.py")), name


def test_nothing_the_image_ships_imports_an_unwired_package():
    offenders = {str(p.relative_to(ROOT)): sorted(_imports(p) & set(UNWIRED)) for p in _runtime_files()
                 if _imports(p) & set(UNWIRED)}
    assert offenders == {}

