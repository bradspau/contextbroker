from pathlib import Path

import re

import rdflib
import yaml

from broker_engine.validation import max_as_of_seconds
from broker_state.process import load_settings

COMPOSE_PATH = Path(__file__).resolve().parent.parent / "docker-compose.yml"
FUSEKI_CONFIG_PATH = Path(__file__).resolve().parent.parent / "docker" / "fuseki" / "config.ttl"
FUSEKI = rdflib.Namespace("http://jena.apache.org/fuseki#")
TDB2 = rdflib.Namespace("http://jena.apache.org/2016/tdb#")


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
    assert services["fuseki"]["image"].split("@")[0] == "secoresearch/fuseki:6.1.0"
    command = _fuseki_command_text(services["fuseki"])
    assert "FusekiServerCmd" in command
    # The dataset, its endpoints and who may use each are in the mounted config (docker/fuseki/config.ttl).
    assert "--conf=/etc/fuseki/config.ttl" in command.split()
    assert "./docker/fuseki/config.ttl:/etc/fuseki/config.ttl:ro" in services["fuseki"]["volumes"]


def test_fuseki_dataset_is_persistent():
    content = yaml.safe_load(COMPOSE_PATH.read_text())
    fuseki = content["services"]["fuseki"]

    words = _fuseki_command_text(fuseki).split()
    assert "--mem" not in words
    config = rdflib.Graph().parse(FUSEKI_CONFIG_PATH, format="turtle")
    locations = [str(o) for o in config.objects(None, TDB2.location)]
    assert len(locations) == 1
    mounts = [v.split(":")[1] for v in fuseki["volumes"] if v.startswith("fuseki-data:")]
    assert mounts and locations[0].rstrip("/") == mounts[0].rstrip("/")
    assert "fuseki-data" in content["volumes"]


def _allowed_users_by_operation():
    config = rdflib.Graph().parse(FUSEKI_CONFIG_PATH, format="turtle")
    found = {}
    for endpoint in config.objects(None, FUSEKI.endpoint):
        operation = config.value(endpoint, FUSEKI.operation)
        name = str(config.value(endpoint, FUSEKI.name))
        found[(name, str(operation).rsplit("#", 1)[-1])] = {
            str(u) for u in config.objects(endpoint, FUSEKI.allowedUsers)}
    return found


def test_fuseki_lets_only_admin_change_the_graph_and_reader_query():
    # Bead contextbroker-oiq: the API and the LodLive proxy hold the reader credential.
    assert _allowed_users_by_operation() == {
        ("query", "query"): {"admin", "reader"},
        ("update", "update"): {"admin"},
        ("data", "gsp-rw"): {"admin"},
    }


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
    for name in ("fuseki", "lodlive-proxy", "victoriametrics", "qdrant"):
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
    # The shell sees both passwords; compose must not interpolate them (written $$).
    for name in ("ADMIN_PASSWORD", "READER_PASSWORD"):
        assert f"$${name}" in command
        assert f"${{{name}" not in command
    stripped = command.replace("$$", "")
    assert "$ADMIN_PASSWORD" not in stripped and "$READER_PASSWORD" not in stripped
    # The password file is private and holds exactly the two users.
    assert "umask 077" in command
    assert "printf 'admin: %s\\nreader: %s\\n'" in command
    # The container refuses to start when both users would share one password.
    assert '[ "$$ADMIN_PASSWORD" != "$$READER_PASSWORD" ]' in command


def test_no_default_fuseki_credential():
    services = yaml.safe_load(COMPOSE_PATH.read_text())["services"]
    for name, svc in services.items():
        env = _env_as_dict(svc.get("environment", []))
        assert env.get("ADMIN_PASSWORD") != "admin", name
        assert "admin" not in [v for v in env.values()], name
        for value in env.values():
            for variable in ("FUSEKI_ADMIN_PASSWORD", "FUSEKI_READER_PASSWORD"):
                if variable in value:
                    assert f"{variable}:?" in value, (name, "needs the :? form, no default")
    fuseki_env = _env_as_dict(services["fuseki"]["environment"])
    assert fuseki_env["ADMIN_PASSWORD"].startswith("${FUSEKI_ADMIN_PASSWORD:?")
    assert fuseki_env["READER_PASSWORD"].startswith("${FUSEKI_READER_PASSWORD:?")


def test_api_and_proxy_receive_only_the_query_only_credential():
    # design/fuseki-auth.md records "API reads need the credential: yes". The API and the proxy get the
    # reader variable (query only), in the same :? form as Fuseki, and never the admin one, so neither
    # can change the network graph (bead contextbroker-oiq). Nothing else gets either.
    services = yaml.safe_load(COMPOSE_PATH.read_text())["services"]
    for name in ("contextbroker-api", "lodlive-proxy"):
        env = _env_as_dict(services[name]["environment"])
        assert env["FUSEKI_READER_PASSWORD"].startswith("${FUSEKI_READER_PASSWORD:?"), name
        assert "FUSEKI_ADMIN_PASSWORD" not in env and "ADMIN_PASSWORD" not in env, name
    for name in ("victoriametrics", "lodlive", "tmf921-server"):
        env = _env_as_dict(services[name].get("environment", []))
        for variable in ("FUSEKI_ADMIN_PASSWORD", "ADMIN_PASSWORD", "FUSEKI_READER_PASSWORD", "READER_PASSWORD"):
            assert variable not in env, (name, variable)


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


STATE_ENV_ITEMS = [
    "BROKER_STATE_PATH=/data/broker-state.sqlite3",
    "BROKER_OUTCOME_CAP=${BROKER_OUTCOME_CAP:-1024}",
    "BROKER_MARK_CAP=${BROKER_MARK_CAP:-1024}",
    "BROKER_SITUATION_CAP=${BROKER_SITUATION_CAP:-100000}",
    "BROKER_SITUATION_RETENTION=${BROKER_SITUATION_RETENTION:-7d}",
    "BROKER_EVENT_CAP=${BROKER_EVENT_CAP:-100000}",
    "BROKER_EVENT_RETENTION=${BROKER_EVENT_RETENTION:-7d}",
    "BROKER_EVENT_WINDOW_DEFAULT=${BROKER_EVENT_WINDOW_DEFAULT:-15m}",
    "BROKER_EVENT_WINDOW_MAX=${BROKER_EVENT_WINDOW_MAX:-24h}",
]
EVENT_KEYS = ("BROKER_EVENT_CAP", "BROKER_EVENT_RETENTION", "BROKER_EVENT_WINDOW_DEFAULT",
              "BROKER_EVENT_WINDOW_MAX")
ENV_EXAMPLE_PATH = COMPOSE_PATH.parent / ".env.example"


def test_state_volume_is_mounted_on_the_api_only():
    content = yaml.safe_load(COMPOSE_PATH.read_text())
    api = content["services"]["contextbroker-api"]
    assert "broker-state" in content["volumes"]
    assert api["volumes"].count("broker-state:/data") == 1
    assert len([v for v in api["volumes"] if v.endswith(":/run/secrets/broker_jwt_public.pem:ro")]) == 1
    for name, svc in content["services"].items():
        if name == "contextbroker-api":
            continue
        assert "broker-state" not in repr(svc), name


def test_state_environment_items_and_defaults():
    services = yaml.safe_load(COMPOSE_PATH.read_text())["services"]
    api_env = services["contextbroker-api"]["environment"]
    for item in STATE_ENV_ITEMS:
        assert api_env.count(item) == 1, item
    env = _env_as_dict(api_env)
    # Decision records are a library-only package left out of the image (bead contextbroker-c8g).
    assert "BROKER_DECISION_RECORDS_PATH" not in env
    assert env["BROKER_STATE_PATH"].startswith("/data/")
    assert "$" not in env["BROKER_STATE_PATH"]
    for name, svc in services.items():
        if name == "contextbroker-api":
            continue
        text = repr(svc)
        for key in ("BROKER_STATE_PATH", "BROKER_DECISION_RECORDS_PATH", "BROKER_OUTCOME_CAP",
                    "BROKER_MARK_CAP", "BROKER_SITUATION_CAP", "BROKER_SITUATION_RETENTION",
                    *EVENT_KEYS):
            assert key not in text, (name, key)


def test_event_compose_defaults_equal_the_code_defaults():
    api_env = yaml.safe_load(COMPOSE_PATH.read_text())["services"]["contextbroker-api"]["environment"]
    pattern = re.compile(r"^(BROKER_EVENT_[A-Z_]+)=\$\{\1:-([^}]*)\}$")
    defaults = {}
    for item in api_env:
        match = pattern.match(item)
        if match:
            defaults[match.group(1)] = match.group(2)
    assert set(defaults) == set(EVENT_KEYS)
    from_compose = load_settings(defaults)
    from_code = load_settings({})
    for field in ("event_cap", "event_retention_s", "event_window_default_s", "event_window_max_s"):
        assert getattr(from_compose, field) == getattr(from_code, field), field


def test_env_example_documents_each_event_setting_with_a_limit_line_and_no_live_assignment():
    lines = ENV_EXAMPLE_PATH.read_text().splitlines()
    for key in EVENT_KEYS:
        assert not any(line.startswith(key + "=") for line in lines), key
        indexes = [i for i, line in enumerate(lines) if line.startswith(f"# {key}=")]
        assert len(indexes) == 1, key
        i = indexes[0]
        assert lines[i - 1].startswith("# ") and not re.match(r"# BROKER_[A-Z_]+=", lines[i - 1]), key
        assert " to " in lines[i - 1], key  # the limit line states a range
    # the four sit inside the broker state block, before the TMF921 block
    block_end = lines.index("# TMF921 server (compose profile tmf921)")
    block_start = next(i for i, line in enumerate(lines) if line.startswith("# Broker state."))
    for key in EVENT_KEYS:
        i = next(i for i, line in enumerate(lines) if line.startswith(f"# {key}="))
        assert block_start < i < block_end, key
    assert "events" in "\n".join(lines[block_start:block_start + 6])


# ---- Qdrant, the knowledge store (D-35, D-38) ----

def test_qdrant_is_its_own_unprivileged_container_on_loopback_without_grpc():
    content = yaml.safe_load(COMPOSE_PATH.read_text())
    qdrant = content["services"]["qdrant"]
    assert qdrant["image"].split("@")[0] == "qdrant/qdrant:v1.19.2-unprivileged"
    assert qdrant["container_name"] == "qdrant"
    assert qdrant["ports"] == ["127.0.0.1:6333:6333"]
    assert qdrant["volumes"] == ["qdrant-data:/qdrant/storage"]
    assert qdrant["restart"] == "unless-stopped"
    assert "depends_on" not in qdrant
    assert "6334" not in COMPOSE_PATH.read_text()  # the gRPC port is never published or named
    assert "qdrant-data" in content["volumes"]


def test_qdrant_requires_a_key_with_no_default_and_runs_without_telemetry():
    qdrant = yaml.safe_load(COMPOSE_PATH.read_text())["services"]["qdrant"]
    env = _env_as_dict(qdrant["environment"])
    assert env["QDRANT__SERVICE__API_KEY"].startswith("${QDRANT_API_KEY:?")
    assert ":-" not in env["QDRANT__SERVICE__API_KEY"]
    assert env["QDRANT__TELEMETRY_DISABLED"] == "true"


def test_only_qdrant_and_the_api_receive_the_key_and_the_index_volume_is_on_qdrant_only():
    content = yaml.safe_load(COMPOSE_PATH.read_text())
    for name, svc in content["services"].items():
        if name in ("qdrant", "contextbroker-api"):
            continue
        assert "QDRANT" not in repr(svc).upper().replace("QDRANT-DATA", ""), name
        assert "qdrant-data" not in repr(svc), name
    api = content["services"]["contextbroker-api"]
    assert "qdrant-data" not in repr(api)
    env = _env_as_dict(api["environment"])
    assert env["QDRANT_API_KEY"].startswith("${QDRANT_API_KEY:?")
    assert ":-" not in env["QDRANT_API_KEY"]
    text = COMPOSE_PATH.read_text()
    assert len(re.findall(r"\$\{QDRANT_API_KEY:\?", text)) == 2
    # the compose file holds no literal key value
    for line in text.splitlines():
        if "QDRANT" in line and "API_KEY" in line and not line.lstrip().startswith("#"):
            assert "${QDRANT_API_KEY:?" in line, line


def test_the_api_gets_the_qdrant_url_and_the_knowledge_directory_read_only():
    api = yaml.safe_load(COMPOSE_PATH.read_text())["services"]["contextbroker-api"]
    env = _env_as_dict(api["environment"])
    assert env["BROKER_QDRANT_URL"] == "http://qdrant:6333"
    assert env["BROKER_KNOWLEDGE_PATH"] == "/knowledge"
    assert api["volumes"].count("./knowledge:/knowledge:ro") == 1
    assert len([v for v in api["volumes"] if "/knowledge" in v]) == 1


def test_the_api_does_not_wait_for_qdrant_to_be_healthy():
    # A Qdrant outage must never stop the API: /health reports it and the other classes keep answering.
    api = yaml.safe_load(COMPOSE_PATH.read_text())["services"]["contextbroker-api"]
    assert isinstance(api["depends_on"], list)
    assert {"fuseki", "victoriametrics", "qdrant"} <= set(api["depends_on"])
    assert "service_healthy" not in COMPOSE_PATH.read_text()
    assert "condition" not in repr(api["depends_on"])


def test_qdrant_url_and_key_settings_equal_what_the_code_reads():
    from okf_retrieval import config

    env = _env_as_dict(yaml.safe_load(COMPOSE_PATH.read_text())["services"]["contextbroker-api"]["environment"])
    assert config.ENV_QDRANT_URL in env and config.ENV_QDRANT_API_KEY in env
    assert config.ENV_KNOWLEDGE_PATH in env
    assert config.qdrant_url({config.ENV_QDRANT_URL: env[config.ENV_QDRANT_URL]}) == "http://qdrant:6333"


def test_env_example_requires_the_qdrant_key_with_no_default():
    lines = ENV_EXAMPLE_PATH.read_text().splitlines()
    assert lines.count("QDRANT_API_KEY=") == 1
    i = lines.index("QDRANT_API_KEY=")
    comment = "\n".join(lines[max(0, i - 6):i])
    assert "required" in comment.lower() and "letters and digits only" in comment
    assert "no default" in comment and "openssl rand -hex 24" in comment
    assert "# BROKER_QDRANT_URL=http://localhost:6333" in lines
    assert not any(line.startswith("BROKER_QDRANT_URL=") for line in lines)


def test_dockerignore_keeps_the_knowledge_directory_out_of_the_image():
    # The read-only mount is the only copy of the knowledge files the API reads.
    lines = [l.strip() for l in (COMPOSE_PATH.parent / ".dockerignore").read_text().splitlines()]
    assert lines.count("knowledge/") == 1
