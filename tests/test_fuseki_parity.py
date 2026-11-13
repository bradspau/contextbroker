"""Real-engine parity check for the fluent history code and migration 002.

The in-memory tests (tests/test_fluent_history.py, tests/test_migration_002.py,
tests/test_context_graph.py) run the real bind, resolve, neighbours, migration and context graph
reader code against an rdflib stand-in. This module
runs the same code against a real Apache Jena Fuseki 6.1.0, which is the one thing a mock
cannot show: the engine's transaction behaviour (one update request is one transaction),
its comparison of zoned xsd:dateTime values, and how it writes timestamps back.

It is skipped unless FUSEKI_PARITY_URL is set. It must never be pointed at the persistent
store: parity_target() refuses any host other than 127.0.0.1 or localhost, any port 3030 (the
persistent store's published port), any URL without an explicit port, and any dataset named
network-dataset, before a single request is sent. Every real-engine test uses the dummy
credential "parity0" for FUSEKI_ADMIN_PASSWORD, so the real credential is never sent to the
throwaway container. Every identifier carries a random per-run suffix so repeated runs on one
container do not collide, and nothing here sends CLEAR, DROP or a store-wide delete.

The exact command (plan 02-09): a throwaway container with an in-memory dataset named
parity-dataset and no authentication, no volume, published on loopback port 3939 only.

    docker run -d --rm --name parity-fuseki -p 127.0.0.1:3939:3030 secoresearch/fuseki:6.1.0 \\
        java -cp '*:/javalibs/*' org.apache.jena.fuseki.main.cmds.FusekiServerCmd \\
        --update --mem /parity-dataset
    FUSEKI_PARITY_URL=http://127.0.0.1:3939/parity-dataset \\
        venv/bin/python -m pytest -q tests/test_fuseki_parity.py
    docker rm -f parity-fuseki

Migration 002 is a store-wide update. Its test therefore stops (fails) if the store holds an
old-shape binding or link that is not this run's, so a store holding real data is never
migrated by this test. The old-shape fixture is removed again when the run ends or stops (only its
own subjects, found by the run suffix); a run killed outright, with no chance to clean up, still leaves
it behind, and then the throwaway container is removed and recreated.
"""
import os
import re
import uuid
from datetime import timedelta
from pathlib import Path
from unittest.mock import Mock, patch
from urllib.parse import urlsplit

import pytest
import rdflib
import requests

from broker_engine.binding_resolver import resolve_binding
from broker_engine.context_graph import build_graph
from broker_engine.operations import interface_neighbours
from data_contracts import fluents as f
from data_contracts.fluents import parse_dt
from ingestion.bind_intent import bind
from ingestion.load_ontologies import load_ttl_to_fuseki
from tests.fluent_helpers import JIT, LAT, NS, QUERY_URL, T0, T1, T2, GraphFuseki

ROOT = Path(__file__).resolve().parent.parent
MIGRATION = ROOT / "ontologies" / "migrations" / "002-bitemporal-fluents.ru"
URL_ENV = "FUSEKI_PARITY_URL"
DUMMY_PASSWORD = "parity0"
HOUR = timedelta(hours=1)
DAY = timedelta(days=1)
REAL_POST = requests.post  # captured before any test patches requests.post
TIMEOUT_S = 30
QUOTE = ('@prefix quan: <http://tio.models.tmforum.org/tio/v3.6.0/QuantityOntology/> .\n'
         '@prefix rdf: <http://www.w3.org/1999/02/22-rdf-syntax-ns#> .\n'
         '@prefix xsd: <http://www.w3.org/2001/XMLSchema#> .\n'
         '_:c quan:smaller ( <%s> [ rdf:value "%s"^^xsd:decimal ] ) .\n')


def parity_target(url: str) -> dict:
    """The query, update and data endpoints of a throwaway dataset URL, or ValueError.

    Accepts only http on 127.0.0.1 or localhost with an explicit port other than 3030 and a
    dataset path that is not /network-dataset. Sends nothing."""
    try:
        parts = urlsplit(url)
        port = parts.port
    except ValueError:
        raise ValueError("not a usable parity URL") from None
    path = parts.path.rstrip("/")
    if parts.scheme != "http" or parts.hostname not in ("127.0.0.1", "localhost"):
        raise ValueError("the parity URL must be http on 127.0.0.1 or localhost")
    if parts.username or parts.password or parts.query or parts.fragment:
        raise ValueError("the parity URL must not carry credentials, a query or a fragment")
    if port is None or port == 3030:
        raise ValueError("the parity URL needs an explicit port other than 3030")
    segments = [s for s in path.split("/") if s]
    if len(segments) != 1 or segments[0].lower() == "network-dataset":
        raise ValueError("the parity URL must name one throwaway dataset, not network-dataset")
    base = f"http://{parts.hostname}:{port}{path}"
    return {"query": base + "/query", "update": base + "/update", "data": base + "/data"}


# ---- the safety check itself: always runs, sends nothing ------------------------------------------

@pytest.mark.parametrize("url", [
    "http://127.0.0.1:3030/network-dataset",
    "http://10.0.0.5:3939/parity-dataset",
    "http://localhost:3939/network-dataset",
    "http://127.0.0.1:3030/parity-dataset",
    "http://127.0.0.1/parity-dataset",
    "https://127.0.0.1:3939/parity-dataset",
    "http://user:pw@127.0.0.1:3939/parity-dataset",
    "http://127.0.0.1:3939/",
    "http://127.0.0.1:3939/a/b",
    "http://127.0.0.1:3939/Network-Dataset/",
    "http://fuseki:3939/parity-dataset",
])
def test_parity_target_refuses_anything_but_a_loopback_throwaway(url):
    with pytest.raises(ValueError):
        parity_target(url)


def test_parity_target_accepts_a_loopback_throwaway_dataset():
    got = parity_target("http://127.0.0.1:3939/parity-dataset")
    assert got == {"query": "http://127.0.0.1:3939/parity-dataset/query",
                   "update": "http://127.0.0.1:3939/parity-dataset/update",
                   "data": "http://127.0.0.1:3939/parity-dataset/data"}
    assert parity_target("http://localhost:3939/parity-dataset/")["data"].endswith("/parity-dataset/data")


# ---- the real-engine tests ------------------------------------------------------------------------

@pytest.fixture(scope="module")
def target():
    url = os.environ.get(URL_ENV)
    if not url:
        pytest.skip(f"real-engine parity test; set {URL_ENV} to a throwaway Fuseki dataset URL")
    return parity_target(url)  # raises, failing the run, for the persistent store's address


@pytest.fixture(scope="module", autouse=True)
def _dummy_credential():
    # module scope so the module fixtures below see it too; the real credential is never read
    with pytest.MonkeyPatch.context() as mp:
        mp.setenv("FUSEKI_ADMIN_PASSWORD", DUMMY_PASSWORD)
        yield


@pytest.fixture(scope="module")
def sfx():
    return uuid.uuid4().hex[:8]


def _post(url, **kw):
    return REAL_POST(url, auth=("admin", DUMMY_PASSWORD), timeout=TIMEOUT_S, **kw)


def select(target, sparql):
    r = _post(target["query"], data={"query": sparql},
              headers={"Accept": "application/sparql-results+json"})
    r.raise_for_status()
    return [{k: v["value"] for k, v in row.items()} for row in r.json()["results"]["bindings"]]


def ask(target, sparql):
    r = _post(target["query"], data={"query": sparql},
              headers={"Accept": "application/sparql-results+json"})
    r.raise_for_status()
    return r.json()["boolean"]


def update(target, text):
    return _post(target["update"], data=text.encode("utf-8"),
                 headers={"Content-Type": "application/sparql-update"})


def load_turtle(target, tmp_path, text, name="part.ttl"):
    path = tmp_path / name
    path.write_text(text, encoding="utf-8")
    load_ttl_to_fuseki(path, target["data"], auth=("admin", DUMMY_PASSWORD))


def remove_run_subjects(target, needle):
    """Delete every triple whose subject IRI carries this run's suffix, and nothing else."""
    assert re.fullmatch(r"m?[0-9a-f]{8}", needle)
    return update(target, 'DELETE { ?s ?p ?o } WHERE { ?s ?p ?o . '
                          f'FILTER(CONTAINS(STR(?s), "{needle}")) }}')


def scoped_count(target, needle):
    assert re.fullmatch(r"m?[0-9a-f]{8}", needle)
    rows = select(target, 'SELECT (COUNT(*) AS ?n) WHERE { ?s ?p ?o . '
                          f'FILTER(CONTAINS(STR(?s), "{needle}")) }}')
    return int(rows[0]["n"])


class Scenario:
    def __init__(self, target, sfx, tmp_path):
        self.target, self.sfx, self.tmp_path = target, sfx, tmp_path
        self.intent = f"parity-{sfx}"
        self.device = f"olt{sfx}"
        self.last_update = None

    def run_bind(self, goal, place, at, **kw):
        client = Mock()
        client.get_intent.return_value = {"expression": {"expressionValue": QUOTE % (LAT, goal)}}
        t = self.target

        def spy(url, *a, **kwargs):
            if url.endswith("/update"):
                data = kwargs.get("data")
                self.last_update = data.decode("utf-8") if isinstance(data, bytes) else data
            return REAL_POST(url, *a, **kwargs)

        with pytest.MonkeyPatch.context() as mp:
            mp.setattr(requests, "post", spy)
            return bind(self.intent, [f"LatencyMetric={self.device}/{place}"],
                        abox_dir=self.tmp_path / "abox", query_endpoint=t["query"],
                        update_endpoint=t["update"], data_endpoint=t["data"],
                        client=client, now=at, **kw)

    def read(self, **kw):
        return resolve_binding(self.intent, self.target["query"], **kw)


@pytest.fixture(scope="module")
def scenario(target, sfx, tmp_path_factory):
    tmp_path = tmp_path_factory.mktemp("parity")
    s = Scenario(target, sfx, tmp_path)
    load_turtle(target, tmp_path, f"""@prefix : <{NS}> .
    :if-{s.device}-pon0 a :Interface ; :onDevice "{s.device}" ; :interfaceName "pon0" .
    :if-{s.device}-pon1 a :Interface ; :onDevice "{s.device}" ; :interfaceName "pon1" .
    """)
    s.run_bind(100, "pon0", T0)                      # create at T0
    s.run_bind(150, "pon0", T1, replace=True)        # the goal changed at T1
    s.run_bind(150, "pon1", T2, correct=True)        # the earlier placement was wrong, found at T2
    return s


def test_bind_then_read_answers_the_in_memory_table(scenario):
    v = T1 + HOUR
    assert scenario.read(valid_at=T0 - HOUR).gaps == ((LAT, "goal"), (LAT, "placement"))
    first = scenario.read(valid_at=T0 + DAY)
    assert first.bounds == {LAT: 100.0} and first.sources == {LAT: (scenario.device, "pon1")}
    latest = scenario.read(valid_at=v)
    assert latest.bounds == {LAT: 150.0} and latest.sources == {LAT: (scenario.device, "pon1")}
    before = scenario.read(valid_at=v, known_at=T2 - HOUR)
    assert before.bounds == {LAT: 150.0} and before.sources == {LAT: (scenario.device, "pon0")}
    after = scenario.read(valid_at=v, known_at=T2)
    assert after.bounds == {LAT: 150.0} and after.sources == {LAT: (scenario.device, "pon1")}
    # known before the goal change was recorded, a later valid time has no goal in force: a gap
    assert scenario.read(valid_at=v, known_at=T1 - HOUR).gaps == ((LAT, "goal"),)
    # known before anything was recorded
    assert scenario.read(valid_at=T0 + DAY, known_at=T0 - HOUR).gaps == (
        (LAT, "goal"), (LAT, "placement"))


def test_the_valid_intervals_are_half_open(scenario):
    assert scenario.read(valid_at=T0).bounds == {LAT: 100.0}
    assert scenario.read(valid_at=T1 - timedelta(milliseconds=1)).bounds == {LAT: 100.0}
    assert scenario.read(valid_at=T1).bounds == {LAT: 150.0}
    assert scenario.read(valid_at=T0 - timedelta(milliseconds=1)).bounds == {}


def test_replaying_the_last_update_changes_nothing(scenario):
    assert scenario.last_update is not None
    before = scoped_count(scenario.target, scenario.sfx)
    assert before > 0
    response = update(scenario.target, scenario.last_update)
    assert response.status_code < 300
    assert scoped_count(scenario.target, scenario.sfx) == before


def test_a_failing_second_operation_leaves_the_first_one_out(target, sfx):
    subject = f"urn:parity:{sfx}:atomic"
    bad = (f'INSERT DATA {{ <{subject}> <urn:parity:p> "one" }} ; '
           f'LOAD <http://nonexistent.invalid/{sfx}.ttl>')
    response = update(target, bad)
    assert response.status_code >= 400
    assert ask(target, f'ASK {{ <{subject}> <urn:parity:p> ?o }}') is False
    # the control: a good request is applied, so the ASK above could have said true
    good = update(target, f'INSERT DATA {{ <{subject}> <urn:parity:q> "two" }}')
    assert good.status_code < 300
    assert ask(target, f'ASK {{ <{subject}> <urn:parity:q> ?o }}') is True


def test_link_fluents_answer_neighbours_by_valid_time(target, sfx, tmp_path):
    a, b, c = (f"{n}{sfx}" for n in ("ra", "rb", "rc"))
    load_turtle(target, tmp_path, f"""@prefix : <{NS}> .
    @prefix rdfs: <http://www.w3.org/2000/01/rdf-schema#> .
    :if-{a} a :Interface ; :onDevice "{a}" ; :interfaceName "ge0" .
    :if-{b} a :Interface ; :onDevice "{b}" ; :interfaceName "ge1" .
    :if-{c} a :Interface ; :onDevice "{c}" ; :interfaceName "ge2" .
    :link-ab-{sfx} rdfs:label "A to B" . :link-ac-{sfx} rdfs:label "A to C" .
    """)
    g = rdflib.Graph()
    f.add_link_fluent(g, f"{NS}link-ab-{sfx}", f"{NS}if-{a}", f"{NS}if-{b}", T0, T0, T1)
    f.add_link_fluent(g, f"{NS}link-ac-{sfx}", f"{NS}if-{a}", f"{NS}if-{c}", T1, T1)
    assert update(target, f.guarded_insert(g, [])).status_code < 300
    now = T1 + 2 * DAY

    def devices(**kw):
        out = interface_neighbours(a, "ge0", query_endpoint=target["query"], now=now, **kw)
        return [n["device"] for n in out["neighbours"]]

    assert devices(as_of="60h") == [b]   # between T0 and T1
    assert devices(as_of="24h") == [c]   # after T1
    assert devices() == [c]
    assert devices(as_of="5d") == []     # before any link: empty, not missing
    out = interface_neighbours(a, "ge0", query_endpoint=target["query"], now=now, as_of="5d")
    assert out["quality"]["missing"] is False


def test_a_millisecond_timestamp_reads_back_in_a_form_parse_dt_accepts(target, sfx):
    subject = f"urn:parity:{sfx}:time"
    assert update(target, 'PREFIX xsd: <http://www.w3.org/2001/XMLSchema#> '
                          f'INSERT DATA {{ <{subject}> <urn:parity:t> '
                          '"2026-11-01T10:00:00.000Z"^^xsd:dateTime }').status_code < 300
    rows = select(target, f'SELECT ?t WHERE {{ <{subject}> <urn:parity:t> ?t }}')
    assert len(rows) == 1
    assert parse_dt(rows[0]["t"]) == T0


# ---- migration 002 on the real engine -------------------------------------------------------------

class Migrated:
    pass


@pytest.fixture(scope="module")
def migrated(target, sfx, tmp_path_factory):
    tmp_path = tmp_path_factory.mktemp("parity-migration")
    m = f"m{sfx}"
    old_shape = f"""@prefix : <{NS}> .
    @prefix rdfs: <http://www.w3.org/2000/01/rdf-schema#> .
    @prefix xsd: <http://www.w3.org/2001/XMLSchema#> .
    :if-{m}-a a :Interface ; :onDevice "{m}a" ; :interfaceName "ge0" .
    :if-{m}-b a :Interface ; :onDevice "{m}b" ; :interfaceName "ge1" .
    :link-{m} a :TopologicalLink ; rdfs:label "fixture link" ;
        :hasInterface :if-{m}-a , :if-{m}-b .
    :binding-{m}-1-Latency a :ServiceBinding ; :forIntent "{m}-1" ; :forMetric "{LAT}" ;
        :onInterface :if-{m}-a ; :pinnedBound "12.5"^^xsd:double .
    :binding-{m}-1-Jitter a :ServiceBinding ; :forIntent "{m}-1" ; :forMetric "{JIT}" ;
        :onInterface :if-{m}-b ; :pinnedBound "3.25"^^xsd:double .
    :binding-{m}-2-Latency a :ServiceBinding ; :forIntent "{m}-2" ; :forMetric "{LAT}" ;
        :onInterface :if-{m}-b ; :pinnedBound "40"^^xsd:double .
    """
    foreign = ask(target, f"""PREFIX : <{NS}>
        ASK {{ {{ ?b a :ServiceBinding ; :pinnedBound ?p }} UNION
              {{ ?b a :ServiceBinding ; :onInterface ?p }} UNION
              {{ ?b a :TopologicalLink ; :hasInterface ?p }}
              FILTER(!CONTAINS(STR(?b), "{m}")) }}""")
    if foreign:
        pytest.fail("the store holds an old-shape binding or link that is not this run's; "
                    "migration 002 is store-wide, so it is not run here (after an "
                    "interrupted run, remove and recreate the throwaway container)")
    try:
        load_turtle(target, tmp_path, old_shape, "old-shape.ttl")
        result = Migrated()
        result.m = m
        result.before = scoped_count(target, m)
        result.response = update(target, MIGRATION.read_text())
        result.after = scoped_count(target, m)
        yield result
    finally:
        # A run that stops after the fixture is loaded (an error, a failed migration, Ctrl-C) removes the
        # fixture's own subjects, so the next run on the same container passes the foreign-data guard.
        # Scoped to this run's suffix: no other subject is touched (bead contextbroker-z94).
        remove_run_subjects(target, m)


def test_migration_002_gives_each_fixture_binding_one_goal_and_one_placement(target, migrated):
    m = migrated.m
    assert migrated.response.status_code < 300
    q = f"""PREFIX : <{NS}>
        SELECT ?b ?g ?p ?pl ?i ?gs ?gr ?ps ?pr WHERE {{
          ?b :hasGoal ?g ; :hasPlacement ?pl .
          ?g :pinnedBound ?p ; :validStart ?gs ; :recordedAt ?gr .
          ?pl :onInterface ?i ; :validStart ?ps ; :recordedAt ?pr .
          FILTER(CONTAINS(STR(?b), "{m}")) }}"""
    rows = select(target, q)
    assert len(rows) == 3
    assert sorted(float(r["p"]) for r in rows) == [3.25, 12.5, 40.0]
    instants = set()
    for r in rows:
        assert parse_dt(r["gs"]) == parse_dt(r["gr"]) == parse_dt(r["ps"]) == parse_dt(r["pr"])
        instants.add(parse_dt(r["gs"]))
    link = select(target, f"""PREFIX : <{NS}>
        SELECT ?fl ?vs ?ra (COUNT(?i) AS ?n) WHERE {{
          ?fl a :LinkFluent ; :ofLink :link-{m} ; :hasInterface ?i ; :validStart ?vs ; :recordedAt ?ra .
        }} GROUP BY ?fl ?vs ?ra""")
    assert len(link) == 1 and int(link[0]["n"]) == 2
    assert parse_dt(link[0]["vs"]) == parse_dt(link[0]["ra"])
    instants.add(parse_dt(link[0]["vs"]))
    assert len(instants) == 1  # one shared, zoned migration instant
    # no old triple and no scratch triple is left
    assert not ask(target, f"""PREFIX : <{NS}>
        ASK {{ {{ ?b :pinnedBound ?p }} UNION {{ ?b :onInterface ?p }} UNION {{ ?b :hasInterface ?p }}
              FILTER(CONTAINS(STR(?b), "{m}") && !CONTAINS(STR(?b), "-goal-") && !CONTAINS(STR(?b), "-place-")
                     && !CONTAINS(STR(?b), "-fluent-") && !CONTAINS(STR(?b), "if-")) }}""")
    assert not ask(target, f"PREFIX : <{NS}> ASK {{ :migration-002 ?p ?o }}")
    # the formula: 3 bindings add 8 each, the link adds 4 (the interface nodes and the pair stay)
    assert migrated.after == migrated.before + 8 * 3 + 4


def test_migration_002_reads_back_through_the_resolver_and_a_second_run_changes_nothing(
        target, migrated):
    m = migrated.m
    start = parse_dt(select(target, f"""PREFIX : <{NS}>
        SELECT ?vs WHERE {{ ?g a :GoalFluent ; :validStart ?vs . FILTER(CONTAINS(STR(?g), "{m}")) }}
        LIMIT 1""")[0]["vs"])
    after = resolve_binding(f"{m}-1", target["query"], valid_at=start + timedelta(seconds=1))
    assert after.bounds == {LAT: 12.5, JIT: 3.25}
    assert after.sources == {LAT: (f"{m}a", "ge0"), JIT: (f"{m}b", "ge1")}
    before = resolve_binding(f"{m}-1", target["query"], valid_at=start - timedelta(seconds=1))
    assert before.bounds == {} and len(before.gaps) == 4

    count = scoped_count(target, m)
    assert update(target, MIGRATION.read_text()).status_code < 300
    assert scoped_count(target, m) == count
    again = resolve_binding(f"{m}-2", target["query"], valid_at=start + timedelta(seconds=1))
    assert again.bounds == {LAT: 40.0}


# ---- the context graph readers (plan 03-05) -------------------------------------------------------


def chain_fixture(sfx):
    """Three devices a-b-c: a/ge0 - b/ge1 (closed at T1) and b/ge2 - c/ge3 (open from T0).

    Returns the Turtle of the interfaces and the graph of the two link fluents; the same pair
    loads into the real engine and into the in-memory stand-in. The link names carry "chain" so
    they never collide with the links of test_link_fluents_answer_neighbours_by_valid_time: a
    fluent IRI is derived from the link IRI and the recorded time, and two tests naming the same
    fluent in one store would give it four interfaces (found live in plan 03-09)."""
    a, b, c = (f"{n}{sfx}" for n in ("ga", "gb", "gc"))
    turtle = f"""@prefix : <{NS}> .
    :if-{a} a :Interface ; :onDevice "{a}" ; :interfaceName "ge0" .
    :if-{b}-1 a :Interface ; :onDevice "{b}" ; :interfaceName "ge1" .
    :if-{b}-2 a :Interface ; :onDevice "{b}" ; :interfaceName "ge2" .
    :if-{c} a :Interface ; :onDevice "{c}" ; :interfaceName "ge3" .
    """
    g = rdflib.Graph()
    f.add_link_fluent(g, f"{NS}link-chain-ab-{sfx}", f"{NS}if-{a}", f"{NS}if-{b}-1", T0, T0, T1)
    f.add_link_fluent(g, f"{NS}link-chain-bc-{sfx}", f"{NS}if-{b}-2", f"{NS}if-{c}", T0, T0)
    return (a, b, c), turtle, g


def load_chain(target, tmp_path, sfx):
    devices, turtle, fluents = chain_fixture(sfx)
    load_turtle(target, tmp_path, turtle)
    assert update(target, f.guarded_insert(fluents, [])).status_code < 300
    return devices


def in_memory(turtle, extra=None):
    g = rdflib.Graph()
    g.parse(data=turtle, format="turtle")
    if extra is not None:
        g += extra
    return GraphFuseki(g)


def graph_args(**kw):
    args = dict(max_depth=3, relationship_types=["has_interface", "connected_to"],
                known_at=None, retrieved_at=T2, allowed=lambda d: True)
    args.update(kw)
    return args


def test_the_context_graph_is_the_same_on_the_real_engine_as_in_memory(target, sfx, tmp_path):
    (a, b, c), turtle, fluents = chain_fixture(sfx)
    load_chain(target, tmp_path, sfx)
    memory = in_memory(turtle, fluents)
    seen = []
    for valid_at in (T0 + HOUR, T1 + HOUR):
        real = build_graph(a, None, valid_at=valid_at, query_endpoint=target["query"], **graph_args())
        with patch("broker_engine.context_graph.requests.post", memory.post):
            local = build_graph(a, None, valid_at=valid_at, query_endpoint=QUERY_URL, **graph_args())
        assert real.graph.model_dump(by_alias=True) == local.graph.model_dump(by_alias=True)
        assert [v.model_dump(by_alias=True) for v in real.fluent_values] == [
            v.model_dump(by_alias=True) for v in local.fluent_values]
        assert (real.scope_identity, real.conflicting_links, real.colliding_ids, real.malformed) == (
            local.scope_identity, local.conflicting_links, local.colliding_ids, local.malformed)
        seen.append(real)
    before, after = seen
    # the answers differ across T1, so the comparison above is not vacuous
    assert [n.id for n in before.graph.nodes] == [a, f"{a}/ge0", f"{b}/ge1", b]
    assert before.graph.limits.cut_by == ["depth"]
    assert [n.id for n in after.graph.nodes] == [a, f"{a}/ge0"]
    assert after.graph.limits.cut_by == [] and after.fluent_values == []


def test_a_twin_interface_node_is_ambiguous_on_the_real_engine(target, sfx, tmp_path):
    t = f"tw{sfx}"
    load_turtle(target, tmp_path, f"""@prefix : <{NS}> .
    :if-{t} a :Interface ; :onDevice "{t}" ; :interfaceName "ge0" .
    :if-{t}-twin a :Interface ; :onDevice "{t}" ; :interfaceName "ge0" .
    """)
    got = build_graph(t, "ge0", valid_at=T2, query_endpoint=target["query"], **graph_args())
    assert got.scope_identity == "ambiguous"
    assert got.graph.nodes == []
    device = build_graph(t, None, valid_at=T2, query_endpoint=target["query"], **graph_args())
    assert device.colliding_ids is True
    assert [n.id for n in device.graph.nodes] == [t, f"{t}/ge0"]


def test_authority_cuts_the_context_graph_on_the_real_engine(target, sfx, tmp_path):
    a, b, c = load_chain(target, tmp_path, sfx)
    got = build_graph(a, None, valid_at=T0 + HOUR, query_endpoint=target["query"],
                      **graph_args(allowed=lambda d: d == a))
    assert [n.id for n in got.graph.nodes] == [a, f"{a}/ge0"]
    assert got.graph.limits.cut_by == ["authority"]
    assert got.fluent_values == []
