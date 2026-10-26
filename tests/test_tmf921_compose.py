import os
import subprocess
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parent.parent
COMPOSE_PATH = ROOT / "docker-compose.yml"
DOCKERFILE = ROOT / "docker" / "tmf921.Dockerfile"
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
    return DOCKERFILE.read_text()


def test_dockerfile_does_not_copy_ontology():
    text = _effective_dockerfile_text()
    copies = [l for l in text.splitlines() if l.strip().upper().startswith(("COPY", "ADD"))]
    assert copies
    assert not any("ontology" in l.lower() for l in copies)
    assert "maven:3.9-eclipse-temurin-21-jammy" in text
    assert "eclipse-temurin:21-jre-jammy" in text


def test_broker_has_tmf921_url():
    env = _env(_services()["contextbroker-api"]["environment"])
    assert env["TMF921_BASE_URL"] == "http://tmf921-server:8000/tmf-api/intentManagement/v5"
    assert "tmf921-server" not in _services()["contextbroker-api"]["depends_on"]


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
