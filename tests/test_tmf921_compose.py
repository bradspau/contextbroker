import os
import subprocess
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parent.parent
COMPOSE_PATH = ROOT / "docker-compose.yml"
PREFLIGHT = ROOT / "scripts" / "tmf921_preflight.sh"

DEFAULT_SERVICES = {
    "victoriametrics", "fuseki", "lodlive-proxy", "lodlive", "contextbroker-api",
}


def _services():
    return yaml.safe_load(COMPOSE_PATH.read_text())["services"]


def _env(env):
    if isinstance(env, dict):
        return {k: str(v) for k, v in env.items()}
    return dict(item.split("=", 1) for item in env)


def test_tmf921_server_only_under_profile():
    services = _services()
    assert services["tmf921-server"]["profiles"] == ["tmf921"]
    others = {n for n, s in services.items() if n != "tmf921-server"}
    assert others == DEFAULT_SERVICES
    assert all("profiles" not in services[n] for n in DEFAULT_SERVICES)


def test_no_published_fuseki_ports():
    ports = [str(p) for p in _services()["tmf921-server"]["ports"]]
    assert ports == ["127.0.0.1:8000:8000"]
    assert not any("3030" in p or "8001" in p for p in ports)


def test_ontology_mounted_read_only():
    vols = _services()["tmf921-server"]["volumes"]
    onto = [v for v in vols if "/app/ontology" in v]
    assert len(onto) == 1
    assert onto[0].endswith("/app/ontology:ro")
    assert "TIO_ONTOLOGY_DIR" in onto[0]
    assert "tdb2-tmf921:/app/tdb2-data" in vols


def test_runtime_env_and_healthcheck():
    svc = _services()["tmf921-server"]
    env = _env(svc["environment"])
    assert env["SPRING_PROFILES_ACTIVE"] == "standalone"
    assert env["INTENT_EVALUATOR"] == "${INTENT_EVALUATOR:-java}"
    assert env["INTENT_FUSEKI_LOOPBACK"] == "false"
    assert svc["healthcheck"]["test"][-1] == "http://localhost:8000/health"


def _effective_dockerfile_text():
    build = _services()["tmf921-server"]["build"]
    if "dockerfile_inline" in build:
        return build["dockerfile_inline"]
    ctx = (ROOT / build["context"]).resolve()
    return (ctx / build.get("dockerfile", "Dockerfile")).read_text()


def test_build_is_independent_of_cwd():
    build = _services()["tmf921-server"]["build"]
    assert "PWD" not in yaml.safe_dump(build)
    assert build["context"] == "./docker/tmf921"
    assert "dockerfile" not in build or not str(build["dockerfile"]).startswith("/")
    assert "bbf" in build["additional_contexts"]


def test_dockerfile_copies_exact_sources_and_no_ontology():
    text = _effective_dockerfile_text()
    sources = []
    for line in text.splitlines():
        parts = line.split()
        if parts and parts[0].upper() in ("COPY", "ADD"):
            args = [p for p in parts[1:]]
            frm = next((a.split("=", 1)[1] for a in args if a.startswith("--from=")), None)
            pos = [a for a in args if not a.startswith("--")]
            sources.append((frm, tuple(pos[:-1])))
    assert sources == [
        ("bbf", ("pom.xml",)),
        ("bbf", ("src/",)),
        ("build", ("/app/target/*.jar",)),
        ("bbf", ("BBF_access/",)),
        ("bbf", ("seed_data/",)),
    ]
    assert not any("ontology" in str(x).lower() for x in sources)
    assert "maven:3.9-eclipse-temurin-21-jammy" in text
    assert "eclipse-temurin:21-jre-jammy" in text


def test_root_dockerignore_blocks_licensed():
    lines = (ROOT / ".dockerignore").read_text().splitlines()
    assert "ontologies/licensed" in lines
    assert "**/ontology/**/*.ttl" in lines
    git = [l for l in (ROOT / ".gitignore").read_text().splitlines()
           if l.startswith(("ontologies/tbox", "!ontologies/tbox", "ontologies/**", "docs/spec/TR29",
                            "docs/spec/TMF921"))]
    assert git and all(l in lines for l in git)


def test_broker_has_tmf921_url():
    env = _env(_services()["contextbroker-api"]["environment"])
    assert env["TMF921_BASE_URL"] == "http://tmf921-server:8000/tmf-api/intentManagement/v5"
    assert "tmf921-server" not in _services()["contextbroker-api"]["depends_on"]


def test_broker_bindings_mount_read_only_and_no_ontology():
    svc = _services()["contextbroker-api"]
    env = _env(svc["environment"])
    assert env["INTENT_BINDINGS_PATH"] == "/app/config/intent_bindings.json"
    vols = svc["volumes"]
    assert len(vols) == 1
    assert vols[0].endswith(":/app/config/intent_bindings.json:ro")
    assert "./data_contracts/intent_bindings.json" in vols[0]
    assert not any("ontolog" in v.lower() or "TIO" in v for v in vols)
    assert (ROOT / "data_contracts" / "intent_bindings.json").read_text().strip() == "{}"
    assert svc["ports"] == ["8080:8080"]


def _run(dir_path):
    env = dict(os.environ, TIO_ONTOLOGY_DIR=str(dir_path))
    return subprocess.run(["bash", str(PREFLIGHT)], env=env, capture_output=True, text=True)


def test_preflight_fails_on_empty_and_missing(tmp_path):
    r = _run(tmp_path)
    assert r.returncode != 0 and "no *.ttl" in r.stderr
    r = _run(tmp_path / "nope")
    assert r.returncode != 0 and "does not exist" in r.stderr


def test_preflight_passes_with_ttl(tmp_path):
    (tmp_path / "x.ttl").write_text("")
    assert _run(tmp_path).returncode == 0


def test_preflight_reads_dotenv_and_repo_relative(tmp_path):
    # copy script into a fake repo root so relative paths and .env resolve against it
    root = tmp_path / "repo"
    (root / "scripts").mkdir(parents=True)
    script = root / "scripts" / "tmf921_preflight.sh"
    script.write_text(PREFLIGHT.read_text())
    onto = root / "onto"
    onto.mkdir()
    (onto / "a.ttl").write_text("")
    env = {k: v for k, v in os.environ.items() if k != "TIO_ONTOLOGY_DIR"}
    (root / ".env").write_text("TIO_ONTOLOGY_DIR=./onto\n")
    # run from an unrelated cwd
    r = subprocess.run(["bash", str(script)], env=env, cwd="/", capture_output=True, text=True)
    assert r.returncode == 0, r.stderr
    (root / ".env").write_text("TIO_ONTOLOGY_DIR=./missing\n")
    r = subprocess.run(["bash", str(script)], env=env, cwd="/", capture_output=True, text=True)
    assert r.returncode != 0
    # shell env wins over .env
    env2 = dict(env, TIO_ONTOLOGY_DIR="onto")
    r = subprocess.run(["bash", str(script)], env=env2, cwd="/", capture_output=True, text=True)
    assert r.returncode == 0, r.stderr
