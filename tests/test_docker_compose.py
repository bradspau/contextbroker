from pathlib import Path

import yaml

COMPOSE_PATH = Path(__file__).resolve().parent.parent / "docker-compose.yml"


def test_docker_compose_defines_victoriametrics_and_fuseki():
    content = yaml.safe_load(COMPOSE_PATH.read_text())
    services = content["services"]

    assert "victoriametrics" in services
    vm_ports = services["victoriametrics"]["ports"]
    assert "8428:8428" in vm_ports

    assert "fuseki" in services
    fuseki_ports = services["fuseki"]["ports"]
    assert "3030:3030" in fuseki_ports
    assert services["fuseki"]["image"] == "secoresearch/fuseki:6.1.0"
    fuseki_command = services["fuseki"]["command"]
    assert isinstance(fuseki_command, list)
    assert fuseki_command[0] == "java"
    assert fuseki_command[-3:] == ["--update", "--mem", "/network-dataset"]


def _env_as_dict(env):
    if isinstance(env, dict):
        return {k: str(v) for k, v in env.items()}
    return dict(item.split("=", 1) for item in env)


def test_docker_compose_defines_contextbroker_api():
    services = yaml.safe_load(COMPOSE_PATH.read_text())["services"]
    api = services["contextbroker-api"]

    assert "8080:8080" in api["ports"]
    env = _env_as_dict(api["environment"])
    assert env["FUSEKI_QUERY_ENDPOINT"] == "http://fuseki:3030/network-dataset/query"
    assert env["VM_QUERY_ENDPOINT"] == "http://victoriametrics:8428/api/v1/query"
    assert {"fuseki", "victoriametrics"} <= set(api["depends_on"])
    assert api["restart"] == "unless-stopped"
