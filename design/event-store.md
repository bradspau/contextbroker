# Events: storage, ingest, the events class and reconciliation (Phase 5)

How syslog lines become stored events, how a context request retrieves them, and how a recognised
event is set beside the configured state without either side overwriting the other. This note
describes Phase 5 as built. It closes gap 4 of `design/layer3-gap-analysis.md` (events need a real
store). Later phases read this store (Phase 6 retrieval; Phase 7 exchange and promotion, which reads the
stored lines again and so adds its own table beside these), so the rules below must not change without a
plan for the rows already stored.

The broker resolves, joins and delivers context. It never decides or acts. An event grants no
authority, feeds no verdict and changes no configured state.

Code: `data_contracts/syslog_events.py` (event id, kinds, no I/O), `ingestion/syslog_parser.py`
(strict parse and host mapping), `ingestion/syslog_ingest.py` (the command), `broker_state/store.py`
(the tables), `broker_engine/context_request.py` (the events branch),
`broker_engine/context_eligibility.py`, `broker_engine/context_graph.py` (the configured side) and
`state_reconciliation/event_reconciler.py` (the comparison).

## State vocabularies

The roadmap asked whether the reconciler's vocabulary (desired, configured, observed,
controller-reported) should be merged with the envelope's `StateCategory` (`intended`, `configured`,
`observed`, `inferred`). It is resolved as **two vocabularies, mapped, not merged** (D-01). The
mapping:

| Reconciliation input | Envelope category | Where it comes from |
|---|---|---|
| desired | `intended` | the goals and placements of an intent |
| configured | `configured` | the fluents in Fuseki (here: a link with an end on the device) |
| observed | `observed` | telemetry from VictoriaMetrics |
| controller-reported | `observed` | what a device or controller says about itself, such as a syslog event |

- `StateCategory` and `data_contracts/envelope.py` keep their four values. Adding a fifth would
  change every envelope consumer and every wire model, so it stays out.
- An event only ever yields an `observed` envelope (D-02). The events class derives no `inferred` state
  from events. Inferred state exists since Phase 7, but only as the promoted hypotheses of
  `design/semantic-promotion.md`: a separate, labelled `hypothesis` list written by an operator command,
  never an envelope and never evidence.
- The words desired and controller-reported are reconciliation vocabulary. They never appear as an
  envelope category.

## What is stored and where

Events live in two additive tables in the Phase 4 state file (`BROKER_STATE_PATH`, see
`design/durable-state.md`), so they share its lock, its refusal to start without a usable file, the
fixed 503 `state store unavailable` and its row in `/health` (D-03). No second file and no new path
variable.

- `events` holds one row per distinct line: the event id, a write sequence number, the ingest time,
  the event time (null when the line has none), the time it is placed in a window, severity,
  facility, format (`rfc5424` or `rfc3164`), the source host as written, the mapping
  (`mapped`, `ambiguous` or `unmapped`), the device (set exactly when the mapping is `mapped`), the
  recognised kind (or null) and the raw line. Indexes cover the write sequence, the ingest time and
  the placement time.
- `event_devices` holds one row per event and candidate device, so a request for a device finds its
  events (and the ambiguous ones that name it) without scanning. An index on the device and event id
  serves the lookup.
- Events are never updated. They leave only by the cap or the retention sweep, and their
  `event_devices` rows leave with them.
- All times are integer microseconds since the Unix epoch (UTC), as in the other tables.

Schema policy (D-34, recorded in plan 05-01): **additive**. The tables are created with
`IF NOT EXISTS` at schema version 1 and `PRAGMA user_version` stays 1. The Phase 4 statements are
unchanged, a Phase 4 file is upgraded in place the first time the new image opens it, and a file at a
higher version is still refused. Because the version did not move, the Phase 4 image can still open
the upgraded file, so rollback is an image rollback: stop the API, start the earlier image on the same
volume. The event tables are then ignored, not deleted. Take a backup copy of the state file with the
API stopped before the first start of the new image anyway. Phases 6 and 7 follow the same rule: a
version bump is only for a change to an existing table.

## Ingest

```
docker exec -i contextbroker-api python -m ingestion.syslog_ingest < syslog.txt
```

`python -m ingestion.syslog_ingest [--file PATH] [--query-endpoint URL]` reads bytes from the file or
from standard input (D-04). There is no listener and no route, so nothing new to authenticate.

- The device table is read **once from Fuseki before any line is read** (D-07): the distinct
  `:onDevice` names of the topology's interfaces, each checked against the token grammar. The stored
  reference is therefore the canonical device name situation ids already use.
- When Fuseki cannot be reached, or answers with a device list that is not usable, the ingest stores
  nothing and changes no counter. It prints one fixed line to standard error and exits 1. An empty
  device list is not an error: it warns and every event is stored as unmapped.
- Lines are written in batches of 200, one transaction per batch. A batch is stored whole or not at
  all, and the counters for a batch are written in the same transaction.
- Every refusal is one fixed line naming at most a variable, never a path, host or server text:
  `Refused: the network graph could not be reached. Nothing was stored.`, `... answered with an
  unusable device list ...`, `... the input file could not be read ...`, `... state store
  unavailable ...`. If the state store or the input fails after some batches were written, the line
  says how many events were stored.
- On success it prints one summary line:
  `stored=N duplicates=N unmapped=N ambiguous=N quarantined=N (no priority=N, not text=N, too long=N,
  unparsable header=N)`.
- The ingest time of a batch is one clock read taken when it is written.

## Parsing and quarantine

A line is either an event or quarantined (D-20). The four reasons and the order they are checked in
are fixed, so a line has exactly one:

1. input that is not bytes or text: `not text`
2. more than 4096 bytes, checked before any decoding: `too long`
3. not valid UTF-8, a lone surrogate, or a control character (tab excepted): `not text`
4. no priority at the first byte, or a priority that is not a number from 0 to 191: `no priority`
5. a header that fits neither RFC 5424 nor RFC 3164: `unparsable header`

- Quarantined text is **never kept, printed or logged** (D-21). Only a count per reason is stored, in
  the state file, and it is shown in `/health`.
- RFC 5424 needs the version, a timestamp (or the nil value), host, application, process id, message
  id and structured data (or the nil value), with the field lengths the RFC gives. The timestamp is
  checked by pattern before it is read as a date, with the ranges in the pattern.
- RFC 3164 needs `Mmm dd hh:mm:ss HOST`. The month names are case sensitive.
- **Deliberate deviation from RFC 3164.** The RFC lets a receiver treat a line with no priority as
  priority 13. The broker does not: such a line is quarantined as `no priority`. A guessed priority
  would be a guessed fact.
- **Event time.** An RFC 3164 line carries no year or zone, and an RFC 5424 line may carry the nil
  timestamp. Neither has an event time. The event time stays null and the event carries the flag
  `time_unknown`. No year or zone is guessed (D-14).
- **Known coverage limits.** The default file form that rsyslog writes
  (`<34>2026-10-26T10:00:00.123456+01:00 host tag: msg`) is neither RFC and counts as `unparsable
  header`, as do vendor lines with sequence numbers or starred timestamps before the host. A pinned
  test keeps that from being widened by accident. The per-reason counters in `/health` show how many
  lines an operator's source loses this way.

## Identity (host to device)

The host is mapped to a device when the line is ingested, using the device table read at that
moment (D-07 to D-11):

- Exact match first, then a case-insensitive match. One device found: `mapped`, the device is
  stored. No device found: `unmapped`, the device is empty and the host stays as written. Two or
  more found, which only the case-insensitive step can produce: `ambiguous`, with every candidate
  stored and none chosen.
- No domain stripping and no address resolution. `edge-sw-1.example.net` does not match `edge-sw-1`.
  The nil host `-` never maps.
- **Unmapped events are kept, counted and never returned** for a device request.
- **The mapping is frozen at ingest** (D-11). A later change to the topology does not touch stored
  events. Re-resolving stored ambiguous events when the device table changes is deferred.
- Device only (D-08). A request scoped to an interface reads its device's events. The events class
  still infers no interface from message text. The Phase 7 promotion command reads interface names from
  the stored text of neighbour-discovery lines, but only as lookup keys against the network graph's own
  names, and stores graph names only (see `design/semantic-promotion.md`).
- **The origin is unauthenticated.** Syslog carries no proof of who sent a line, and the broker does
  not add one. A mapped event is a claim by whoever sent it, matched by name. That is why an event
  only ever becomes an `observed` envelope and a conflict only shows both sides.

## Duplicates

The event id is `evt-` followed by the first 32 hex characters of the sha256 of the raw line (UTF-8)
(D-05). Two lines with the same bytes have the same id, whatever source or time they came from. The
second is not stored and is counted as a duplicate. Trade-off accepted: two real events with
identical text collapse into one, and a device that repeats a line verbatim shows it once. A repeat
ingest of the same file is a no-op for that reason.

## Cap, retention and counters

- **Cap** `BROKER_EVENT_CAP`, default 100000. When a batch would exceed it, the oldest events
  (lowest write sequence) are evicted with their `event_devices` rows, once per batch. A batch
  larger than the room evicts its own oldest rows.
- **Retention** `BROKER_EVENT_RETENTION`, default 7 days. Events age by **ingest time**, never by the
  device-supplied event time, so a line cannot pin itself in the store or age out early by naming a
  false time. The sweep runs inside a write, at most once a minute, with no background thread. A
  read-side cut-off also hides events older than the retention, so the policy holds when nothing is
  written.
- **Counters** are in the `counters` table so they survive a restart: duplicates dropped, unmapped
  events, quarantined lines per reason and events evicted.
- **Log lines** are fixed and name no host, device or id: `state eviction: table=events evicted=<n>`
  and `state retention: table=events removed=<n>` (the second only when something was removed).
- `GET /health` shows `evictions.events` and an `events` object with `duplicates`, `unmapped` and
  `quarantined` per fixed reason (whole numbers only). They come from the same cached state probe as
  the other rows. When the state store does not answer, `evictions` and `events` are `null` and the
  status is `degraded`.

## Configuration

One variable per row. A bad value stops the API at start with a message that names the variable and
never echoes the value.

| Variable | Default | Limits |
|---|---|---|
| `BROKER_EVENT_CAP` | `100000` | whole number from 1 to 1000000 |
| `BROKER_EVENT_RETENTION` | `7d` | digits then `h` or `d`, from `1h` to `90d` |
| `BROKER_EVENT_WINDOW_DEFAULT` | `15m` | digits then `s`, `m`, `h` or `d`, from `1m` to `7d`, not above the maximum |
| `BROKER_EVENT_WINDOW_MAX` | `24h` | same grammar, not above the event retention |

## The events class

`events` is the fourth value of the context classes (D-12). It is part of the signed authority claim:
a token without it is refused with 403 `outside authority scope: class` for an events request only,
before any read. `scripts/mint_token.py` takes `--class events`.

- **Window.** An optional `window_seconds` (a whole number from 1 to 604800) ending at the resolved
  instant (`as_of`). The default is `BROKER_EVENT_WINDOW_DEFAULT`. A value above
  `BROKER_EVENT_WINDOW_MAX` is a fixed 400, raised before any read. `window_seconds` without the
  events class is a 422.
- **Placement and knowledge time.** An event is placed in a window by its event time, or by its ingest
  time when it has none. Both ends of the window are inclusive. `known_at` cuts by ingest time: an
  event ingested after that instant is not yet known. Either `as_of` or `known_at` makes the answer
  historical.
- **Scope.** A device scope reads that device's events. An interface scope reads its device's events.
- **Result cap.** At most 100 events, newest placed first, then by event id. `truncated` is true when
  the cap cut the result.
- **Flags.** `time_unknown` (no event time), `future_dated` (event time more than 5 s after its ingest
  time) and `ambiguous`. Flags are information. They are never an eligibility reason.
- **Eligibility** (D-15, D-31, D-32): no event in the window is a `gap` (`no events in window`); a
  window that reaches back past the event retention is a `gap` (`window reaches beyond event
  retention`); at least one ambiguous event returned is `ineligible` (`ambiguous identity: event
  source`). `time_unknown`, truncation and conflicts are never reasons. Eligibility stays apart from
  the verdict and takes no conflict as input.
- **Expiry.** A current answer with at least one event expires at the retrieval time plus the
  freshness limit, with the basis `event_window`. Precedence among the bases: `historical_request`,
  `oldest_observation`, `event_window`, `knowledge_set` (added in Phase 6, see `design/knowledge.md`),
  `configured_state`, `no_evidence`.
- **Situation id.** The window enters the id only for an events request, as a conditional key
  (D-33). `SITUATION_VERSION` stays 1 and every earlier id is unchanged. The default is applied
  before it is hashed, so a request without `window_seconds` and one that names the default are the
  same situation.
- **Ambiguous events and authority.** An ambiguous event is returned to a request for any of its
  candidate devices, flagged, with `device` null. It names the candidates the caller's entities
  cover and only counts the rest (`candidates_withheld`), so the response never discloses a device
  the token does not cover.
- **What is never returned.** The raw line and the message text. An event shows its id, mapping,
  device, the source host (only when it is a plain token), format, facility, severity, event and
  ingest times, recognised kind, flags and one provenance record (source `syslog`, the event id as
  reference, the event time, the retrieval time).
- **Stored situation.** The situation row keeps the syslog provenance records of the events returned
  (and the Fuseki record of a link fluent a reconciliation read). It points at its events and holds
  none of their content.

## Reconciliation

A recognised event is compared with the configured state and any conflict is published. Nothing is
decided.

- **Recognised kinds** (D-16). A small closed table of message patterns maps a message to `link_down`
  or `link_up` for a device: the Cisco-style `%LINK-n-UPDOWN` and `%LINEPROTO-n-UPDOWN` lines and the
  Linux `NIC Link is Down|Up` line. The patterns are case sensitive and are marked in the code as
  community-sourced wordings, not taken from a licensed text. Any other event is stored and returned
  and never produces a conflict. The older whole-message text comparison is gone.
- **The configured side** (D-17, D-30). It is **device-level**: a link fluent with an end on the
  device that is in force at the request's valid and knowledge instants. The interface the message
  names is not matched to the interface of the link, so the comparison does not claim the same
  interface. Statuses: `held` (value `link_in_force`, with the link fluents), `not_held` (none in
  force, or every one cut by authority; it cannot conflict) and `invalid` (conflicting fluents on one
  link or a malformed record; nothing is claimed either way). The side is read only when at least
  one recognised, mapped, non-ambiguous event was returned, with `SELECT` queries only.
- **Authority.** A link whose other end is on a device the caller's entities do not cover is dropped,
  named nowhere, and sets `authority_cut` true. A link with an end on the scope device itself is
  always covered.
- **The conflict.** The pair `link_down` against `link_in_force` is the whole contradiction table.
  The conflict names both sides (the event id and kind, the configured value and its fluents) with the
  fixed text `event link_down contradicts configured link_in_force`. A `link_up` never conflicts.
  The text and every value are built from closed vocabularies, never from message text.
- **Where it shows.** The events result carries `envelopes` (one `observed` envelope per recognised
  mapped event, source `syslog`), `configured` (device, status, value, fluents, `authority_cut`) and
  `conflicts`. The conflict text is also in the event envelope's `quality.conflicts`. The link
  fluents read are added to `fluent_values` with the class `events`.
- **Nothing is written, nothing is ranked** (D-18, D-19). The broker never changes Fuseki, never
  marks the configured value as stale because a newer event contradicts it, and never says which
  side is right. Source precedence is the consumer's decision.
- **Verdict and eligibility never read conflicts.** The conflict does not reach the TMF921 assess,
  the verdict or the events eligibility (spy tests). A context request still pushes nothing, writes
  no high-water mark and records no outcome.
- A Fuseki failure during the configured read is the existing 502, and no situation row is written.

## Deferred

- A live syslog listener (UDP or TCP) or an authenticated ingest route.
- Interface-level mapping from message text.
- Inferred state from events and semantic promotion: built in Phase 7 (`design/semantic-promotion.md`).
  The events class itself still infers nothing; what that phase deferred is listed in its own note.
- Re-resolving stored ambiguous events when the device table changes.
- Marking configured state possibly stale when a newer event contradicts it.
