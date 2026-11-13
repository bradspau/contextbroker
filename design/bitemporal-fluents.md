# Goal and topology history: the fluent model

How the network graph records what a goal, a placement or a topology link was, and when the
broker knew it. This note describes Phase 2 as built. It closes gap 1 of
`design/layer3-gap-analysis.md` (facts were current-state only).

## The model

A `:ServiceBinding` (one per intent and metric) and a `:TopologicalLink` stay as stable nodes.
What changes over time lives on version nodes, called fluents:

| Fluent class | Held by | Carries |
|---|---|---|
| `:GoalFluent` | `:hasGoal` from the binding | `:pinnedBound` (one finite number, `xsd:double`) |
| `:PlacementFluent` | `:hasPlacement` from the binding | `:onInterface` (one `:Interface` node) |
| `:LinkFluent` | `:ofLink` to the link | `:hasInterface` (exactly two interfaces) |

Every fluent has four times, all `xsd:dateTime` with a zone (`Z` or an offset, enforced by SHACL):

- the valid interval `[:validStart, :validEnd)`: when the fact held in the world;
- the knowledge interval `[:recordedAt, :supersededAt)`: when the broker held that record.

Both intervals are half-open and an open end is an absent triple, so the only writes are
inserts. A fluent is in force at valid time V as known at knowledge time K when
`validStart <= V`, V is before `validEnd` (or there is none), `recordedAt <= K`, and K is before
`supersededAt` (or there is none). With no K the test is only "not superseded".

The predicates are `:validStart` and `:validEnd`, not `:validFrom` and `:validTo`, because
migration 001 deletes every triple with the latter two predicates; reusing them would let a
re-run of 001 erase history.

The stored `:pinnedBound` name is unchanged; the vocabulary elsewhere says "goal".

## Writes: `python -m ingestion.bind_intent`

One run is one SPARQL update, one HTTP request, one transaction. The change is built as an rdflib
graph, validated against the SHACL shapes, rendered as a single guarded `INSERT { ... } WHERE
{ guard }`, and sent once. The guard requires each old fluent to exist and to carry no `:validEnd`
and no `:supersededAt`, so a stale read applies nothing instead of a half change. All values in
the update text are code-formatted timestamps, validated IRIs or rdflib N-Triples.

- **First bind:** creates the binding, one goal fluent and one placement fluent per metric, valid
  and recorded from the run's instant.
- **`--replace`:** the goal changed. The old fluent gets a `:validEnd` equal to the new fluent's
  `:validStart`; the new fluent opens. Nothing is deleted.
- **`--correct`:** the earlier record was wrong. The old fluent gets a `:supersededAt` and is kept;
  a new fluent carries the same valid interval and a new `:recordedAt`. "What did the broker know
  at K" therefore still returns the old value for any K before the correction.
- **`--correct --at T --goal METRIC=VALUE`:** the goal that was in force at T is wrong, even if it has
  closed since. The operator names the valid time and the corrected value (the intent holds only its current
  goal, so the TMF921 server is not asked). The goal in force at T is kept and gets a `:supersededAt`; a new
  fluent has the same `:validStart` and `:validEnd` (an open goal stays open) and a new `:recordedAt`. Several
  `--goal` options correct several metrics in one update. No `--place`, `--replace` or `--valid-from` with it;
  placements are unchanged. A value equal to the stored one changes nothing.
- **`--replace --valid-from T`:** the operator says when the goal really changed. The old fluent
  is superseded, a reissued copy of it ends at T, and the new fluent starts at T.
- **No flag on an existing binding:** refused. **Nothing changed:** exit 0 with the message
  `Nothing changed: the binding already matches the intent.`
- **A metric cannot be dropped.** If the intent no longer has a stored metric the run is refused;
  bind the intent's new shape under a new intent id.
- **The ABox file** `ontologies/abox/<intent>.ttl` is written only after the update is confirmed,
  holds only the new fluents of the run, and starts with the marker line
  `# written by ingestion.bind_intent`. `--write-only` writes it and posts nothing; applying a file
  written for `--replace` or `--correct` means running `bind_intent` again, because loading it with
  `load_ontologies` would add an open fluent without closing the old one.

The run's instant is one UTC timestamp (to the millisecond) used as every `:recordedAt` of the
run. A run whose instant is not later than the current fluent's start or record time is refused.

## The ABox rule

`ontologies/tbox/topology.ttl` is schema only. Instances live in `ontologies/abox/`. The loader
(`python -m ingestion.load_ontologies`) loads an ABox file only if its first line is the
`bind_intent` marker, or its name is on the static allow-list (`topology-demo.ttl`). The demo
topology file is tracked through a `.gitignore` negation; every other ABox file is untracked.
The demo file holds the demo link as a link fluent valid from 2026-11-05 and is for fresh stores
only: loading it into a store that migration 002 already converted would give the link a second
open fluent.

## Reads

- **Bindings** (`broker_engine/binding_resolver.py`): one SELECT returns every fluent of the
  intent; `data_contracts.fluents.select_in_force` picks the one in force at (V, K) in Python
  (the engines disagree on comparing zoned and unzoned `xsd:dateTime`, so times are parsed
  with `fromisoformat` and never compared as strings). The resolver starts from the binding nodes.
  A metric with no goal or no placement in force is a gap; any gap makes the assess
  `inconclusive`, never `success` for a partial set. Two fluents covering the same time are an
  error, and the assess is `inconclusive` with the reason `invalid binding`.
- **Assess** with `as_of` and/or `known_at`: V is now minus `as_of` (now when absent), K is now
  minus `known_at` (latest when absent). The goal and placement come from the fluents only; the
  TMF921 server is never asked about the past, so a TERMINATED intent stays answerable and
  `intent_status` is `unknown`. The result adds `goals` (metric, value, `valid_from`, `valid_to`)
  and `known_at`, and `requirement` is a fixed string. Reasons are fixed strings built from
  pinned metric names: `no goal in force: <Metric>`, `no placement in force: <Metric>`, and, for
  `mode: "within"`, `goal not in force for the whole window: <Metric>` or
  `placement not in force for the whole window: <Metric>` when a fluent started inside the window
  (samples before the change would be judged against a later goal). A past assess pushes nothing,
  writes no high-water mark and does not update the stored fulfilment.
- **Neighbours** (`broker_engine/operations.py`): filtered in SPARQL on link fluents. `as_of` and
  `known_at` work as for assess. An interface in the graph with no link valid then is an empty
  list with `quality.missing` false; an interface not in the graph is missing.

`as_of` and `known_at` are offsets (`90s`, `10m`, `2h`, `1d`), at most the retention period.
Absolute timestamps are not accepted.

## Retention

One variable, `BROKER_RETENTION_PERIOD` (digits then `h` or `d`, at least `1d`, default `30d`),
feeds both VictoriaMetrics (`-retentionPeriod`) and the API's `as_of` and `known_at` limit, so the
API never accepts a time the store has already dropped. A bare number is refused because
VictoriaMetrics would read it as months. The previous implicit default was 31 days; the first start
with `30d` drops samples in the 30 to 31 day band. A bad value stops the API at start-up. Goal and
topology history lives in Fuseki (the `fuseki-data` volume), not in VictoriaMetrics, and has no
retention limit.

## Migration 002

`ontologies/migrations/002-bitemporal-fluents.ru` converts a store loaded before Phase 2.

What it does, in one request (one transaction on Fuseki):

1. stores one `NOW()` instant in a scratch triple (`NOW()` differs between operations of one
   request on both engines, so one instant must be read back);
2. gives each `:ServiceBinding` with a `:pinnedBound` a goal fluent and removes the pin;
3. gives each binding with an `:onInterface` a placement fluent and removes the triple;
4. gives each `:TopologicalLink` with `:hasInterface` triples one link fluent and removes them;
5. removes the scratch triple.

Every new fluent has `validStart = recordedAt =` that one instant. History therefore starts at the
migration: a question about a time before it finds no goal in force and is `inconclusive`, never
answered with today's goal. The broker does not claim history it did not record.

It deletes nothing except the three old predicates and its scratch triple, is idempotent (a second
run matches nothing), and leaves a graph that conforms to the shapes. Migration 001 run over the
result changes nothing. After it, the triple count is the count before plus 8 for each binding
plus 4 for each link, when every binding has one pin and one placement and every link has two
interfaces. It is one-way: undoing it needs a reverse migration.

### Runbook (the live run is a separate step, only after the operator's approval)

1. Pre-flight, read only. These must return no row: a binding with other than exactly one pin
   and one placement would give one fluent two values.

   ```sparql
   PREFIX : <http://example.org/network-ontology#>
   SELECT ?b (COUNT(?p) AS ?pins) (COUNT(?i) AS ?places) WHERE {
     ?b a :ServiceBinding . OPTIONAL { ?b :pinnedBound ?p } OPTIONAL { ?b :onInterface ?i } }
   GROUP BY ?b HAVING (COUNT(?p) != 1 || COUNT(?i) != 1)
   ```

2. Record the counts before: all triples, `:ServiceBinding` nodes, `:TopologicalLink` nodes, old pins
   (`?b a :ServiceBinding ; :pinnedBound ?p`), and link pairs (`?l a :TopologicalLink ;
   :hasInterface ?i`).
3. Run the file as one credentialed update. The password stays inside the container: the shapes in
   `design/fuseki-auth.md` ("Credentialed requests from inside the container") read the
   container's own `$ADMIN_PASSWORD`; pipe the migration file to the update shape on stdin.
4. Record the counts after. Expected: old pins 0; goal fluents and placement fluents equal to the
   bindings; link fluents equal to the links; total equal to the count before plus 8 per binding
   plus 4 per link.
5. A second run changes nothing. If the counts differ from the formula, stop and do not run it
   again before finding out why.

## Engine facts the design relies on, and how they are checked

The in-memory tests (`tests/test_fluent_history.py`, `tests/test_migration_002.py`) prove what
the code does on rdflib. These engine behaviours are not provable there, so
`tests/test_fuseki_parity.py` checks them on a real Fuseki 6.1.0:

- Fuseki applies one update request in one transaction: an `INSERT DATA` followed by a failing
  `LOAD` in one request leaves the inserted triple absent (D-04 relies on this for close and open).
- `NOW()` differs between operations of one request (the reason for the scratch triple), so the
  migration is checked to give one shared instant.
- A replayed update changes nothing, and the half-open boundaries hold on the real engine.
- Fuseki rewrites lexical forms (`.000Z` comes back without a fraction); every read goes through
  `parse_dt`, so the form does not matter.

The parity test is skipped unless `FUSEKI_PARITY_URL` is set and refuses the persistent store's
address (non-loopback hosts, port 3030, `network-dataset`) before any request. It uses a dummy
credential, random per-run identifiers, and no store-wide delete. See its module docstring for the
throwaway container command.

## Known limits

- A `--replace` without `--valid-from`, asked with a knowledge time before the run about a valid
  time after it, is `inconclusive`: the closed goal does not cover it and the new one is not yet
  known. Use `--valid-from` when the goal really changed earlier than the run.
- Removing a metric from a binding needs a new intent id.
- Adding a metric later makes questions about times before that addition `inconclusive` for the
  whole intent, because the broker did not record that goal then.
- A `--correct` without `--at` can target only the goal or placement that is open now (the intent carries
  only the current goal). A goal that has already closed is corrected with `--correct --at TIME --goal
  METRIC=VALUE`; a closed placement still cannot be corrected from the command line (bead
  `contextbroker-46q` covered goals).
- The demo topology file is for fresh stores only.
- Telemetry (metric history, non-zero interfaces) takes no knowledge time; only `assess` and
  `neighbours` do.
