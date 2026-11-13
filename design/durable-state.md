# Durable state (Phase 4, with the Phase 5 event tables and the Phase 7 promoted table)

What the broker remembers between requests, where it keeps it, for how long, how it makes room and
how it fails. Phase 5 added the event tables to the same file (see `design/event-store.md`) and Phase 7
added the promoted table (see `design/semantic-promotion.md`); Phase 6 retrieval and the Phase 7
exchange profile read situations and events by id, so the rules here must not change without a plan for
the rows already stored. The broker still resolves, joins and
delivers context and never decides or acts: nothing stored here feeds a verdict or grants execution
authority.

## What is stored and where

One SQLite file, `/data/broker-state.sqlite3` by default (`BROKER_STATE_PATH`), on the
`broker-state` named volume mounted at `/data` in the API container. It is opened in WAL mode with
`synchronous=FULL` and a 5 s busy timeout, with file mode 0600 (the `-wal` and `-shm` files too).
`PRAGMA user_version` is 1 (it stays 1 with the Phase 5 event tables, see the schema policy below); a
file with a higher version is refused rather than read. All times are
integer microseconds since the Unix epoch (UTC). Every statement is literal SQL with parameters.

| Table | Key | Holds |
|---|---|---|
| `outcomes` | intent id | The last outcome per intent (`success`, `failure` or `inconclusive`) and when it was written |
| `marks` | intent id and metric | The high-water mark: the observation time and value last pushed to the TMF921 server |
| `situations` | situation id | The situation record (below), its expiry, first and last recorded time, repeat count |
| `counters` | name | Eviction counters, one per table, and the event counters (duplicates, unmapped, quarantined per reason), so they survive a restart |
| `events` | event id | A stored syslog event (Phase 5): times, severity, facility, format, source host, mapping, device, recognised kind and the raw line |
| `event_devices` | event id and device | One row per event and candidate device, so a device request finds its events |
| `promoted` | promoted id | A promoted conflict or hypothesis (Phase 7): its rule, devices, valid-from, support (the situation and up to 20 event ids) and promotion times. Written only by the operator command |

Each of the data tables (`outcomes`, `marks`, `situations`, `events`, `promoted`) carries a write
sequence number. Eviction orders by it. What the event tables hold and how they are filled, mapped and
read is in `design/event-store.md`; what the promoted table holds and how it is written and read is in
`design/semantic-promotion.md`.

Schema policy (Phase 5, D-34; kept in Phase 7): **additive**. The event tables and the promoted table
are created with `IF NOT EXISTS` at schema version 1, so `user_version` stays 1, an earlier file is
upgraded in place by the first start of the new image, and the earlier image can still open the upgraded
file (it ignores the new tables). The promoted table has its own existence check, so a Phase 5 or Phase 6
file gains it too.
Rollback is therefore an image rollback. `user_version` moves only when an existing table changes
shape or meaning.

A situation row written by a newer image can carry a field the older image's model does not know (the
stored models refuse unknown fields on purpose, so a record is never read with part of its meaning dropped).
`GET /context/situation/{id}` then answers the 404 `situation not found` of an unknown id and logs one fixed
line, `stored situation unreadable: ValidationError`, with no body and no id. Asking the same question again
writes the row in the older image's shape. Nothing else reads the body.

Decision records are not in this file. The `decision_records` library keeps them in their own append-only
file (`BROKER_DECISION_RECORDS_PATH`, `/data/decision-records.sqlite3` as the documented path). The package
is left out of the image (`.dockerignore`) and no running service opens one, so compose does not set the
variable and the API writes no decision record.
`RecordStore(":memory:")` exists only when a caller passes it explicitly; setting the variable to
`:memory:` is refused.

## One worker

One API worker is the supported setup. The per-intent lock (in-process) orders the pre-push check,
the push to the TMF921 server and the mark advance for one intent. The mark advance itself is one
`BEGIN IMMEDIATE` transaction that moves a mark only forward, so a second process opening the same
file can never move a mark backwards or corrupt it. What the transaction does not give is atomicity
of the pre-push check with the push across processes: two workers could both pass the check and both
push. Making marks and locks safe across several workers needs a shared store and is deferred.

## When the store fails

- The API refuses to start if the state file cannot be opened or a state variable is malformed. The
  lifespan logs the exception class only (`state store unavailable: <Class>`), never the path or the
  exception text. `BROKER_STATE_PATH=:memory:` is refused. There is no memory fallback, because
  forgetting marks would let a stale observation be pushed again.
- A failure in the middle of a request is the fixed answer `503 {"detail": "state store
  unavailable"}`, the same on every route that touches the store (`POST /context/assess`,
  `POST /context/request`, `GET /context/situation/{id}`). It is logged by exception class only. The
  TMF921 assess keeps the failure as itself; it is never relabelled "intent server unavailable".
- `GET /health` carries a `state` row (`"answering"` or `"not answering"`), an `evictions` object
  of whole numbers per table, outcomes, marks, situations and events, and an `events` object of event
  counters (duplicates, unmapped and quarantined per fixed reason). Both are `null` when the store does
  not answer. A state store that does not
  answer makes the status `degraded` and the HTTP status 503. The ping and the counters are one
  probe inside the existing 5 s cache.
- A mark write that fails after a successful push cannot undo the push. The pushed pinned metric
  names (the current one included) are logged through the fixed fault line, the request is the fixed
  503, no outcome is recorded and nothing is retried. The next assess for that intent reads whatever
  the store holds: it refuses to push again if the mark did advance, and may push the same
  observation again if it did not. The server sees an observation twice at worst; no value is lost.
- A mark that did not advance because another process moved it past this one logs
  `high-water mark not advanced: <Metric>` and the assess continues.

## Situation records

`POST /context/request` writes one situation row just before every 200 and never for a refusal (4xx)
or the 502. The stored body holds the resolved request parameters (requester, purpose, scope device
and interface, intent id, the resolved instants, classes, depth, relationship types), the evidence
references (the de-duplicated provenance records and the sorted graph node ids), the per-class
eligibility with its fixed reasons, the expiry and its basis, and the verdict label when there was
one. It holds no metric value and no full envelope; those are fetched again.

There is one row per situation id. Because the id floors time to the second and ignores freshness, a
repeat of the same question replaces the body and the expiry and keeps `first_recorded_at`; the
`repeat_count` goes up by one and `last_recorded_at` is the new write.

`GET /context/situation/{id}` needs `context:read` and the authority claims. Only the original
requester gets the record. Anyone else, an unknown id, a malformed id and a record past retention all
get the same `404 {"detail": "situation not found"}`, and the requester check comes before the
authority check so a refusal never confirms that an id exists. Then the request's authority rules
apply to the stored scope, in the same order and with the same strings as the request: entity, then
purpose, then class. A situation that names an intent also needs `context:assess`. The response is
the stored record plus `expired`, `first_recorded_at`, `last_recorded_at` and `repeat_count`.
`expired` is computed at read time from `expires_at` and is never stored.

## Retention

A situation is reported `expired` from its `expires_at` on and is removed 7 days after that by
default. `BROKER_SITUATION_RETENTION` is digits then `h` or `d`, from `1h` to `90d`. The removal
sweep runs inside a situation write, at most once a minute, with no background thread. A read-side
cut-off also hides a row past expiry plus retention, so the policy holds when nothing is written. The
situation sweep touches situations only: never outcomes, marks or decision records.

Events age differently: by **ingest time**, never by the time a device put in the line, so a line
cannot pin itself in the store or age out early. `BROKER_EVENT_RETENTION` (default `7d`, from `1h` to
`90d`) sets how long an event is kept, with its own sweep (inside an event write, at most once a minute)
and its own read-side cut-off. The log line is `state retention: table=events removed=<n>`, written
only when something was removed.

## Caps and eviction

| Table | Cap variable | Default | Limits |
|---|---|---|---|
| `outcomes` | `BROKER_OUTCOME_CAP` | 1024 | whole number from 1 to 1000000 |
| `marks` | `BROKER_MARK_CAP` | 1024 | whole number from 1 to 1000000 |
| `situations` | `BROKER_SITUATION_CAP` | 100000 | whole number from 1 to 1000000 |
| `events` | `BROKER_EVENT_CAP` | 100000 | whole number from 1 to 1000000 |

When a write would exceed a cap, the least recently written entry is evicted (by the write sequence,
so an intent still being assessed keeps its place). The old behaviour, a silent stop that refused new
intents, is gone. Each eviction is announced by one fixed log line, `state eviction: table=<table>
evicted=<n>`, which names no intent, and counted in `counters` and in the `/health` `evictions`
object. The response bodies of the routes do not change.

Why losing a mark is mostly safe: the pre-push check also refuses any sample older than the
300 s freshness limit, so a mark that was evicted cannot let an old sample through. The edge: a
sample under 300 s old but older than the evicted mark could be pushed again. Evicting the least
recently written entry avoids this unless more intents are active inside that window than the cap
allows; raise `BROKER_MARK_CAP` if the number of active intents nears it.

For events the oldest event (lowest write sequence) goes first, with its `event_devices` rows, and the
eviction runs once per ingest batch. `/health` shows the count under `evictions.events`.

## Decision records, reservations and the action-readiness check

- Decision records are a file by default, append-only (the update, replace and delete triggers
  stay) and never swept by retention. A retention period would loosen those triggers and is deferred.
- The reservation book (`loop_coordination/reservations.py`) stays in memory: reservations are
  short-lived leases used by library code. One lock covers check-then-take, so a scope has exactly
  one holder under many threads. Persisting reservations is deferred until a caller runs the action
  path.
- The action-readiness check (`governance_gate/gate_proxy.py`) returns the template version and
  reservation holder it saw from its decision as a local value, not through shared instance state, so
  each decision record carries its own template version under concurrent use.
- None of the three has a route.

## Configuration

One variable per row. A bad value stops the API at start with a message that names the variable and
never echoes the value.

| Variable | Default | Limits |
|---|---|---|
| `BROKER_STATE_PATH` | `/data/broker-state.sqlite3` | a file path; `:memory:` refused |
| `BROKER_DECISION_RECORDS_PATH` | none in compose (library only; `/data/decision-records.sqlite3` as the documented path) | a file path; `:memory:` refused |
| `BROKER_OUTCOME_CAP` | `1024` | whole number from 1 to 1000000 |
| `BROKER_MARK_CAP` | `1024` | whole number from 1 to 1000000 |
| `BROKER_SITUATION_CAP` | `100000` | whole number from 1 to 1000000 |
| `BROKER_SITUATION_RETENTION` | `7d` | digits then `h` or `d`, from `1h` to `90d` |
| `BROKER_EVENT_CAP` | `100000` | whole number from 1 to 1000000 |
| `BROKER_EVENT_RETENTION` | `7d` | digits then `h` or `d`, from `1h` to `90d` |
| `BROKER_EVENT_WINDOW_DEFAULT` | `15m` | digits then `s`, `m`, `h` or `d`, from `1m` to `7d`, not above the maximum |
| `BROKER_EVENT_WINDOW_MAX` | `24h` | same grammar, not above the event retention |
| `BROKER_PROMOTED_CAP` | `10000` | whole number from 1 to 1000000 |
| `BROKER_PROMOTED_RETENTION` | `30d` | digits then `h` or `d`, from `1h` to `90d`, counted from the last promotion |
| `BROKER_PROMOTE_MIN_EVENTS` | `3` | whole number from 2 to 1000 |
| `BROKER_PROMOTE_WINDOW` | `15m` | digits then `s`, `m`, `h` or `d`, from `1m` to `7d`, not above the event retention |

## Operating it

- Never run `docker compose down -v`: it deletes the `broker-state` volume together with the Fuseki
  and VictoriaMetrics data.
- A backup is a copy of the two files with the API stopped. Take one before an upgrade that changes
  the file (the Phase 5 image adds the event tables on its first start, and the Phase 7 image adds the
  promoted table on its first start). Under the additive policy the rollback is an image rollback: start
  the earlier image on the same volume; it ignores the new tables. A restore of the copy is only needed if the file itself was damaged.
- Losing the state file loses outcomes, marks, situation records, events and promoted records. After that the 300 s freshness
  check is the only limit on re-pushing an observation.
- The file holds requester subjects, device names and, for events, the raw syslog lines as sent (a
  line can carry anything a device wrote). Keep the volume private. No route returns a raw line.
- Tests get a state file of their own from `tests/conftest.py`, so no test shares marks with another.

## Deferred

- Marks and locks that are safe across several API workers (needs a shared store).
- Persisting reservations across restarts.
- A retention period for decision records.
- Storing the full response body for each situation.
- Events are built (Phase 5): see `design/event-store.md`, which also lists what that phase deferred
  (a live listener, interface-level mapping, re-resolving stored ambiguous events).
- Knowledge retrieval (Phase 6) and the JSON-LD exchange profile (Phase 7) build on the situation row and
  its id (`design/knowledge.md`, `design/exchange-profile.md`).
- Promoted records are built (Phase 7): see `design/semantic-promotion.md`, which also lists what that
  phase deferred (retiring a hypothesis, an operator rule file, telemetry-based inference).
- The promoted eviction count is a counter (`evicted_promoted`), not a `/health` key: `/health` keeps its
  eight keys.
