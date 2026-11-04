from pathlib import Path

import re

import yaml

from broker_engine.validation import max_as_of_seconds

COMPOSE_PATH = Path(__file__).resolve().parent.parent / "docker-compose.yml"


def _fuseki_command_text(fuseki):
    """The wrapper command (sh -c '<script>') as one string."""
    command = fuseki["command"]
    assert isinstance(command, list)
    assert command[:2] == ["sh", "-c"]
    assert len(command) == 3
    return command[2]


def test_docker_compose_defines_victoriametrics_and_fuseki():
    content = yaml.safe_load(COMPOSE_PATH.read_text())
    services = content["services"]

    assert "victoriametrics" in services
    vm_ports = services["victoriametrics"]["ports"]
    assert vm_ports == ["127.0.0.1:8428:8428"]

    assert "fuseki" in services
    fuseki_ports = services["fuseki"]["ports"]
    assert "127.0.0.1:3030:3030" in fuseki_ports
    assert services["fuseki"]["image"] == "secoresearch/fuseki:6.1.0"
    command = _fuseki_command_text(services["fuseki"])
    assert "FusekiServerCmd" in command
    assert command.rstrip().endswith("/network-dataset")
    assert "--update" in command.split()


def test_fuseki_dataset_is_persistent():
    content = yaml.safe_load(COMPOSE_PATH.read_text())
    fuseki = content["services"]["fuseki"]

    words = _fuseki_command_text(fuseki).split()
    assert "--mem" not in words
    assert "--tdb2" in words
    loc = next(a for a in words if a.startswith("--loc="))[len("--loc="):]
    mounts = [v.split(":")[1] for v in fuseki["volumes"] if v.startswith("fuseki-data:")]
    assert mounts and loc.rstrip("/") == mounts[0].rstrip("/")
    assert "fuseki-data" in content["volumes"]


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


def test_fuseki_and_lodlive_proxy_are_bound_to_localhost():
    services = yaml.safe_load(COMPOSE_PATH.read_text())["services"]
    for name in ("fuseki", "lodlive-proxy", "victoriametrics"):
        for port in services[name]["ports"]:
            assert port.startswith("127.0.0.1:"), (name, port)


def _published_host_side(port):
    """Return (host_ip, host_port, container_port) for a short-form port string."""
    parts = str(port).split(":")
    if len(parts) == 3:
        return parts[0], parts[1], parts[2]
    if len(parts) == 2:
        return "", parts[0], parts[1]
    return "", "", parts[0]


def test_no_service_publishes_8428_on_a_host_interface():
    services = yaml.safe_load(COMPOSE_PATH.read_text())["services"]
    for name, svc in services.items():
        for port in svc.get("ports", []):
            host_ip, host_port, container_port = _published_host_side(port)
            if "8428" in (host_port, container_port.split("/")[0]):
                assert host_ip == "127.0.0.1", (name, port)


def test_api_port_unchanged():
    services = yaml.safe_load(COMPOSE_PATH.read_text())["services"]
    assert services["contextbroker-api"]["ports"] == ["8080:8080"]


def test_fuseki_launches_with_authentication_enforced():
    fuseki = yaml.safe_load(COMPOSE_PATH.read_text())["services"]["fuseki"]
    command = _fuseki_command_text(fuseki)
    words = command.split()
    assert any(w.startswith("--passwd=") for w in words)
    assert "--auth=basic" in words
    # The options come before the dataset name, which stays last.
    assert words.index("--auth=basic") < words.index("/network-dataset")
    # The shell sees ADMIN_PASSWORD; compose must not interpolate it (written $$).
    assert "$$ADMIN_PASSWORD" in command
    assert "${ADMIN_PASSWORD" not in command
    stripped = command.replace("$$", "")
    assert "$ADMIN_PASSWORD" not in stripped
    # The password file is private and holds the single admin line.
    assert "umask 077" in command


def test_no_default_fuseki_credential():
    services = yaml.safe_load(COMPOSE_PATH.read_text())["services"]
    for name, svc in services.items():
        env = _env_as_dict(svc.get("environment", []))
        assert env.get("ADMIN_PASSWORD") != "admin", name
        assert "admin" not in [v for v in env.values()], name
        for value in env.values():
            if "FUSEKI_ADMIN_PASSWORD" in value:
                assert "FUSEKI_ADMIN_PASSWORD:?" in value, (name, "needs the :? form, no default")
    fuseki_env = _env_as_dict(services["fuseki"]["environment"])
    assert fuseki_env["ADMIN_PASSWORD"].startswith("${FUSEKI_ADMIN_PASSWORD:?")


def test_api_and_proxy_receive_the_credential_in_the_required_form():
    # design/fuseki-auth.md records "API reads need the credential: yes", so both
    # readers get the variable, in the same :? form as Fuseki. Nothing else does.
    services = yaml.safe_load(COMPOSE_PATH.read_text())["services"]
    for name in ("contextbroker-api", "lodlive-proxy"):
        env = _env_as_dict(services[name]["environment"])
        assert env["FUSEKI_ADMIN_PASSWORD"].startswith("${FUSEKI_ADMIN_PASSWORD:?"), name
    for name in ("victoriametrics", "lodlive", "tmf921-server"):
        env = _env_as_dict(services[name].get("environment", []))
        assert "FUSEKI_ADMIN_PASSWORD" not in env, name
        assert "ADMIN_PASSWORD" not in env, name


def test_compose_holds_no_literal_password_value():
    text = COMPOSE_PATH.read_text()
    assert "ADMIN_PASSWORD=admin" not in text
    assert "--passwd=/tmp/fuseki-passwd" in text


def test_api_jwt_configuration():
    services = yaml.safe_load(COMPOSE_PATH.read_text())["services"]
    api = services["contextbroker-api"]
    env = _env_as_dict(api["environment"])

    assert env["BROKER_JWT_PUBLIC_KEY_FILE"] == "/run/secrets/broker_jwt_public.pem"
    assert "BROKER_JWT_ISSUER" in env and "BROKER_JWT_AUDIENCE" in env

    mounts = [v for v in api.get("volumes", []) if "broker_jwt_public.pem" in v]
    assert mounts == ["${HOST_BROKER_JWT_PUBLIC_KEY:-/dev/null}:/run/secrets/broker_jwt_public.pem:ro"]
    assert len([v for v in api.get("volumes", []) if v.endswith(":/run/secrets/broker_jwt_public.pem:ro")]) == 1

    # the API container gets the public key only: no private key anywhere in compose
    assert "private" not in repr(api).lower()
    assert api["ports"] == ["8080:8080"]


def test_api_healthcheck_uses_health_route():
    api = yaml.safe_load(COMPOSE_PATH.read_text())["services"]["contextbroker-api"]
    hc = api["healthcheck"]
    test = hc["test"]
    assert isinstance(test, list)
    assert test[:3] == ["CMD", "python", "-c"]
    assert "http://127.0.0.1:8080/health" in test[3]
    assert "timeout=" in test[3]
    assert "curl" not in repr(test)
    assert (hc["interval"], hc["timeout"], hc["retries"], hc["start_period"]) == ("10s", "5s", 5, "10s")


def test_retention_is_one_value_for_victoriametrics_and_the_api():
    services = yaml.safe_load(COMPOSE_PATH.read_text())["services"]
    vm = services["victoriametrics"]
    assert vm["command"] == ["-retentionPeriod=${BROKER_RETENTION_PERIOD:-30d}"]
    assert vm["ports"] == ["127.0.0.1:8428:8428"]
    assert vm["volumes"] == ["vm-data:/victoria-metrics-data"]

    api_env = services["contextbroker-api"]["environment"]
    api_item = "BROKER_RETENTION_PERIOD=${BROKER_RETENTION_PERIOD:-30d}"
    assert api_item in api_env

    pattern = re.compile(r"\$\{BROKER_RETENTION_PERIOD:-([^}]*)\}")
    vm_default = pattern.search(vm["command"][0]).group(1)
    api_default = pattern.search(api_item).group(1)
    assert vm_default == api_default
    assert max_as_of_seconds({"BROKER_RETENTION_PERIOD": api_default}) == 2592000

    for name, svc in services.items():
        if name in ("victoriametrics", "contextbroker-api"):
            continue
        assert "BROKER_RETENTION_PERIOD" not in repr(svc), name
