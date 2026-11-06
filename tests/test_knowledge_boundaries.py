"""The knowledge path cannot reach code that executes or schedules an action (plan 06-05, OKF-03).

Import-direction pins read with ast, in the shape of tests/test_context_eligibility.py::imported.
"""
import ast
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
EXECUTION = ("action_lifecycle", "governance_gate", "loop_coordination")
ALLOWED = ("yaml", "pydantic", "qdrant_client", "fastembed", "data_contracts",
           "artefact_governance.action_catalogue", "broker_engine.faults", "okf_retrieval")


def python_files(directory):
    return sorted((ROOT / directory).rglob("*.py"))


def imported(path):
    tree = ast.parse(Path(path).read_text())
    found = []
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            found.append((node.module or "", [a.name for a in node.names]))
        elif isinstance(node, ast.Import):
            found += [(a.name, []) for a in node.names]
    return found


def under(module, names):
    return any(module == name or module.startswith(name + ".") for name in names)


def test_there_are_files_to_check():
    assert python_files("okf_retrieval") and python_files("broker_engine")


def test_no_broker_engine_or_retrieval_module_imports_execution_code():
    offenders = [f"{p.relative_to(ROOT)}: {m}" for d in ("broker_engine", "okf_retrieval")
                 for p in python_files(d) for m, _ in imported(p) if under(m, EXECUTION)]
    assert offenders == []


def test_no_import_names_execution_code_from_a_package_import():
    offenders = [f"{p.relative_to(ROOT)}: {n}" for d in ("broker_engine", "okf_retrieval")
                 for p in python_files(d) for m, names in imported(p) if m == "" or "." not in m
                 for n in names if n in EXECUTION]
    assert offenders == []


def test_retrieval_imports_only_the_standard_library_and_the_allowed_set():
    offenders = []
    for path in python_files("okf_retrieval"):
        for module, _ in imported(path):
            if module.split(".")[0] in sys.stdlib_module_names or under(module, ALLOWED):
                continue
            offenders.append(f"{path.name}: {module}")
    assert offenders == []


def test_retrieval_uses_no_relative_imports():
    for path in python_files("okf_retrieval"):
        for node in ast.walk(ast.parse(path.read_text())):
            assert not (isinstance(node, ast.ImportFrom) and node.level), path.name


def test_the_model_library_is_imported_only_inside_a_function_of_the_embedder():
    for directory in ("okf_retrieval", "broker_engine"):
        for path in python_files(directory):
            tree = ast.parse(path.read_text())
            inside = {id(n) for fn in ast.walk(tree) if isinstance(fn, (ast.FunctionDef, ast.AsyncFunctionDef))
                      for n in ast.walk(fn)}
            for node in ast.walk(tree):
                names = ([a.name for a in node.names] if isinstance(node, ast.Import)
                         else [node.module or ""] if isinstance(node, ast.ImportFrom) else [])
                if any(under(n, ("fastembed",)) for n in names):
                    assert path.name == "embedder.py" and id(node) in inside, str(path)


def test_the_data_contracts_do_not_import_retrieval():
    offenders = [p.name for p in python_files("data_contracts")
                 for m, _ in imported(p) if under(m, ("okf_retrieval",))]
    assert offenders == []


def test_context_eligibility_imports_nothing_from_retrieval():
    assert [m for m, _ in imported(ROOT / "broker_engine" / "context_eligibility.py")
            if under(m, ("okf_retrieval",))] == []


def test_broker_engine_reaches_retrieval_only_through_the_service_or_the_config():
    allowed = ("okf_retrieval.service", "okf_retrieval.config")
    offenders = []
    for path in python_files("broker_engine"):
        for module, names in imported(path):
            # `from okf_retrieval import config` reaches okf_retrieval.config
            reached = [f"okf_retrieval.{n}" for n in names] if module == "okf_retrieval" else [module]
            offenders += [f"{path.name}: {m}" for m in reached if under(m, ("okf_retrieval",)) and m not in allowed]
    assert offenders == []


def test_loading_the_api_and_the_service_loads_no_execution_module():
    code = ("import sys; sys.path.insert(0, sys.argv[1]); import broker_engine.main, okf_retrieval.service; "
            "print(sorted(m for m in sys.modules if m.split('.')[0] in %r))" % (EXECUTION,))
    out = subprocess.run([sys.executable, "-c", code, str(ROOT)], capture_output=True, text=True, check=True)
    assert out.stdout.strip() == "[]"


def test_the_knowledge_models_carry_identity_only():
    from data_contracts.context_contract import ActionRef, KnowledgeRef, KnowledgeResult, MatchedScope
    assert set(KnowledgeRef.model_fields) == {
        "identifier", "version", "status", "title", "content_hash", "matched_scope", "source_references",
        "retrieval_rank", "linked_actions", "unresolved_actions", "provenance"}
    assert set(ActionRef.model_fields) == {"template_id", "version", "approved"}
    assert set(MatchedScope.model_fields) == {"entities", "purpose", "ontology_class"}
    assert set(KnowledgeResult.model_fields) == {"retrieval", "cap", "truncated", "references", "withheld"}
    banned = {"score", "similarity", "body", "excerpt", "content"}
    for model in (ActionRef, KnowledgeRef, KnowledgeResult, MatchedScope):
        assert banned & set(model.model_fields) == set()
