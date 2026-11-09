# Semantic promotion: promoted conflicts and hypotheses (Phase 7)

How the broker keeps a derived statement beyond the request that produced it, which statements it
keeps, how an operator asks for them to be kept, and why a kept statement can never change a judgement.
This note describes Phase 7 as built. It closes the part of gap 4 of `design/layer3-gap-analysis.md`
that Phase 5 left open (inferred state from events), and it is the "inferred" half of the state
vocabulary mapping in `design/event-store.md`.

The broker resolves, joins and delivers context. It never decides or acts. A promoted record is a
labelled observation about stored evidence; it feeds no verdict, no eligibility and no expiry, it
changes no configured state, and it grants no execution authority.

Code: `data_contracts/promotion.py` (the closed vocabulary and the identity, no I/O),
`data_contracts/adjacency.py` (the neighbour-discovery formats, pure),
`state_reconciliation/promotion.py` (the two selection rules, pure, no clock),
`ingestion/promote.py` (the operator command, the only writer), `broker_state/store.py` and
`broker_state/process.py` (the table and the four settings), `broker_engine/context_request.py` (the read
paths) and `data_contracts/context_contract.py` (the response models). Tests: `tests/test_promotion.py`,
`tests/test_promote_command.py`, `tests/test_promoted_store.py`, `tests/test_promotion_settings.py`,
`tests/test_adjacency.py`, `tests/test_hypothesis_flow.py`, `tests/test_context_promoted.py` and, for this
note, `tests/test_design_notes.py`.

## The two kinds

| Kind | What it is | Where a consumer finds it |
|---|---|---|
| `conflict` | A stored event kept because it disagreed with the configured link state more than once | `events.promoted_conflicts` of an events request, for the scope device |
| `hypothesis` | A link between two interfaces that stored neighbour-discovery lines report and the configured topology does not have | `graph.hypotheses` of a topology request that follows `connected_to` |

Both are separate from what the broker reads live. A live conflict (`events.conflicts`) is computed per
request from the events in the window and the configured side; a promoted conflict is a kept record of
an earlier finding. A hypothesis is never one of the graph's edges: it is typed `cb:Hypothesis`, labelled
`hypothesis` with state `inferred`, and sits in its own list. `RELATIONSHIP_TYPES`, `GraphEdge` and every
situation id input are unchanged by it.

## The closed rules

Promotion is chosen by two fixed rules in `PROMOTION_RULES`, each producing one kind (`RULE_KIND`). There
is no operator rule file, no per-request flag and no way to add a rule at run time (D-57). The thresholds
come from the environment (see Settings).

### `repeated_link_down_vs_configured` (kind `conflict`)

For the situation's scope device, a conflict is selected only when all of these hold:

1. The qualifying events are the stored events of that device with mapping `mapped` and recognised kind
   `link_down`, one per event id, ordered by `(placed time, event id)`. The placed time is the event
   time, else the ingest time.
2. At least `BROKER_PROMOTE_MIN_EVENTS` of them lie within `BROKER_PROMOTE_WINDOW` of each other. The edge
   is inclusive: a window of exactly the setting still counts.
3. A link fluent with an end on that device was in force at the situation's valid instant, as known at
   its knowledge instant (the same configured read the live reconciler uses).

Anything else selects nothing and nothing is stored. `link_up` events, other devices' events, events with
no recognised kind, a repeated event id and a device with no configured link are all ignored. `valid_from`
is the first event of the first qualifying window, never later than the promotion time (device times are
device-authored). The support holds the first 20 qualifying event ids.

### `adjacency_without_configured_link` (kind `hypothesis`)

A stored event cannot name the far end of a link: the events table keeps the sender's device, a kind and
the line. A link up or down line describes only the sender's own interface. So a hypothesis is inferred
only from **neighbour-discovery lines**, which name the local interface, the neighbour and the neighbour's
interface. For one unordered pair of interfaces, a hypothesis is selected only when all of these hold:

1. At least `BROKER_PROMOTE_MIN_EVENTS` distinct stored events of the situation, within
   `BROKER_PROMOTE_WINDOW`, are neighbour-discovery lines in one of the approved formats naming that pair.
2. The neighbour name maps to exactly one device (`mapped`, using the same host mapping as ingest), and
   each interface name equals an interface of its device in the network graph, exactly and case
   sensitively. The two nodes are different.
3. Neither interface has a configured link in force at the situation's valid and knowledge instants, and
   the link records read are not conflicting. A conflicting set stores no hypothesis; it does not
   refuse the run.

`valid_from` is the first event of the first qualifying window, never later than the promotion time. The
identity is taken over the two node ids, sorted, so the same pair always has the same record. Only graph
names reach a record: the names a line carries are lookup keys and nothing else.

### Neighbour-discovery formats

The formats are closed, in `ADJACENCY_FORMATS`. The owner approved all three at the plan 07-07 checkpoint
on 2026-11-09 (the answer `approve-all`, recorded in bead `contextbroker-6ev`). Each is case sensitive,
searched in the message part only (after the syslog parse), anchored so that it starts at its first literal
token with no letter or digit before it, and ends the message. Interface names are 1 to 64 characters of
letters, digits and `/ . : _ -`; a neighbour name is 1 to 128 characters of letters, digits and `_ . -`.
Any other text matches nothing.

| Name | Shape (the `<...>` parts are the names) | Source caveat |
|---|---|---|
| `cdp_duplex_mismatch` | `%CDP-4-DUPLEX_MISMATCH: duplex mismatch discovered on <local> (duplex), with <peer> <remote> (duplex)` | Cisco IOS wording from community posts, not from a vendor guide |
| `cdp_native_vlan_mismatch` | `%CDP-4-NATIVE_VLAN_MISMATCH: Native VLAN mismatch discovered on <local> (<VLAN>), with <peer> <remote> (<VLAN>)` | the same sourcing |
| `lldp_neighbor_up` | `LLDP_NEIGHBOR_UP: LLDP neighbor up on local port <local>, remote system <peer>, remote port <remote>` | **proposed wording, not verified against any device** |

The third format may never match a real line, or may need to be replaced by the line a real device emits.
That is a code change with a near-miss test, not a migration. Ingest, the event kind table and the stored
shape are untouched: the text is read only by the operator command, at promotion time, from the raw line
of a stored event.

## When promotion runs

Only from the operator command. No request, no ingest and no timer writes a promoted row, and a route
inventory test pins that no route can. A context request still writes only its situation row. It never
pushes, never writes a high-water mark and never records an outcome (an events or topology request that
returns a promoted record reads the state file and nothing else for it).

## The command

```
docker exec contextbroker-api python -m ingestion.promote --situation sit-... [--dry-run]
```

`python -m ingestion.promote --situation ID [--dry-run] [--query-endpoint URL]`. The id is that of a stored
situation that holds the events class (the answer to an events request returns it). `--dry-run` does
everything except the write. `--query-endpoint` defaults to `FUSEKI_QUERY_ENDPOINT`. The command reads the
situation and its events from the state file and the configured links from Fuseki with SELECT queries only,
and only when a rule could select (Fuseki is not asked unless enough events exist). It never updates
Fuseki. It needs `FUSEKI_ADMIN_PASSWORD` in its environment, which the API container has; the value is
never printed.

Records are written in one transaction. On success it prints one summary line of counts only, and exits 0:

```
conflicts=N hypotheses=N events=N counted=N evicted=N
```

A dry run prefixes `dry run, nothing written: `. A re-run on the same situation refreshes the same record
instead of adding another.

Every refusal is one line on standard error, `Refused: <text>. Nothing was promoted.`, exit status 1,
nothing on standard output and nothing written. The texts are fixed and name no value:

| Cause | Text |
|---|---|
| the id is not `sit-` and 32 lower-case hex digits | the situation id is malformed |
| unknown id, past retention, or a body that does not read back | situation not found |
| the situation was not asked with the events class | the situation has no events class |
| Fuseki unreachable during the configured read | the network graph could not be reached |
| conflicting or malformed link records at the conflict read | the network graph answered with an unusable topology |
| the state file cannot be opened, read or written | state store unavailable |

Two more refusals come before the id is looked at: `FUSEKI_ADMIN_PASSWORD` unset (the text names the
variable) and a bad promotion setting (the text names the variable, never the value). The counts are
all the command prints: no raw line, message text, host, neighbour name or requester is printed, logged or
stored in a record.

## The promoted table

One additive table, `promoted`, in the state file of `design/durable-state.md` (created with `IF NOT
EXISTS` at schema version 1, so `PRAGMA user_version` stays 1).

| Column | Holds |
|---|---|
| `promoted_id` | `prm-` and 32 hex digits, over the kind, rule and subject (the device, or the two sorted node ids) |
| `kind`, `rule` | one of the two kinds and the rule that selected it |
| `device_a`, `device_b` | the device of a conflict (`device_b` empty), or the two devices of a hypothesis |
| `body` | a small JSON object of graph names and closed vocabulary words, never line text |
| `valid_from_us` | when the finding began; kept at the first value on every re-run |
| `promoted_at_us`, `last_promoted_at_us`, `repeat_count` | first promotion, latest promotion, number of runs that selected it |
| `written_seq` | the write sequence; the cap evicts the lowest |
| `situation_id`, `situation_requester` | the support situation and the requester who asked it |
| `event_ids` | the support events, at most 20 |

- **Upsert.** A re-run keeps the earliest `valid_from`, takes the newest support, adds one to the count
  and moves the row to the newest write. A row past retention that is promoted again starts over (count 1,
  new promotion time), so a record hidden from reads is never revived with an old history.
- **Cap.** `BROKER_PROMOTED_CAP` rows. When a write would exceed it, the least recently written rows go,
  inside the write. The count is kept in `counters` as `evicted_promoted` and announced by one fixed line,
  `state eviction: table=promoted evicted=<n>`, which names no id or device. It is **not** on `/health`,
  which keeps its eight keys.
- **Retention.** Counted from the last promotion: `BROKER_PROMOTED_RETENTION`. A sweep inside a promotion
  write, at most once a minute, removes older rows with the line `state retention: table=promoted
  removed=<n>`, written only when something was removed. Reads hide a row past retention without waiting.

## Support

Every returned record points at what supported it: first the situation, then each event, in stored order.
Each reference has a kind (`situation` or `event`), an id and a state, computed at read time:

- `present`: the source still exists.
- `evicted`: the source was removed by its own cap or retention. It stays listed and marked, never left
  dangling and never dropped, so a consumer can see that the evidence is gone.
- `withheld`: the situation of another requester. The situation id is shown only to the requester who asked
  that question; anyone else sees state `withheld` and no id, whether or not the situation still exists.
  The event ids are shown (they are the evidence about a device the caller's entities cover).

Each returned record also carries one provenance record: source system `broker-promotion` (the
`PROMOTION_SOURCE`), the promoted id as the reference, the promotion time as the observation time and the
retrieval time. A stored situation lists one such record per returned promoted record, so a stored answer
still shows that promoted state was part of it.

## How a request reads it

All read paths read the state file only, after the inputs of the eligibility, the expiry and the verdict
are fixed, and they add fields beside the existing ones.

- **Conflicts**, for an events request: the promoted conflicts of the scope device (the device was already
  checked against the caller's entities), at most 10, newest valid-from first, valid at the request's
  valid instant and, when `known_at` is given, promoted at or before it. They are returned even when the
  window holds no event. The sentence is rebuilt from two closed vocabularies; a stored text is never
  echoed.
- **Hypotheses**, for a topology request that follows `connected_to`: the promoted hypotheses with at least
  one end on a node the graph returned. A hypothesis is returned only when **both** ends are on devices
  the caller's entities cover; one that is not is counted in `hypotheses_withheld` and never named. A
  hypothesis whose pair is already a configured `connected_to` edge of the graph is hidden (computed at
  read time; nothing is stored for it). The list is sorted by `(from, to, promoted_id)` and capped at 50;
  `hypotheses_truncated` is true when the cap or a per-device read cut anything.
- A row that does not build its model (a body that is not the closed shape, or a hypothesis body whose
  ends do not start with its two devices) is skipped with the one fixed line `promoted record
  unreadable`.

### Lifecycle, at read time

There is no retire command and no stored status. What a consumer sees is computed on each read:

- a record is current while it is inside retention and valid at the request's instant;
- a hypothesis stops being returned the moment a configured edge covers its pair, and is returned again
  if that edge is removed, because nothing was ever stored about it;
- an evicted support is shown as `evicted`.

Retiring a hypothesis early is deferred (see below).

## Why nothing promoted is evidence

- Promoted records enter the response only through their own fields (`events.promoted_conflicts`,
  `graph.hypotheses` and its two counters). The eligibility, the expiry and the verdict are computed from
  the same reads as before and never see them. Spy tests compare the whole answer minus those fields,
  the Fuseki and VictoriaMetrics query lists, the assess call and the triple count, with and without
  promoted rows, and they are identical.
- A hypothesis is labelled `hypothesis` and `inferred` in the model, so no consumer can take it for
  configured topology, and it adds no node, edge, limit or fluent value.
- Nothing promoted changes configured state. The command never updates Fuseki and the broker never writes
  a configured fact because of an event.

## Security

- **Syslog origin is unauthenticated.** A line is a claim by whoever sent it, matched by name. Counting
  the same claim three times makes it a repeated claim, not a verified one. `BROKER_PROMOTE_MIN_EVENTS`
  lines is no defence against a determined forger, who can send as many lines as it likes.
- The real defence is structural: nothing promoted changes configured state, eligibility, expiry or the
  verdict. The worst a forged line can do is put a labelled `hypothesis` or a kept `conflict`, with its
  support and provenance, in front of a consumer that is told it is inferred.
- The device-authored names in a line are never trusted as identities. A neighbour name goes through the
  host mapping and must map to one device; an interface name must equal a graph interface exactly; only
  graph names are stored.
- The matching is anchored, case sensitive, bounded in length and linear. A hostile 4000-character input
  matches nothing.
- Authority applies on the way out: an uncovered end hides a hypothesis (counted, never named), and
  another requester's situation id is withheld.

## Settings

All four are read and checked when the API starts (a bad value stops it, naming the variable and never
the value), are in `docker-compose.yml` as `${VAR:-default}`, and are listed in `.env.example`. The same
table is in `design/durable-state.md`.

| Variable | Default | Limits |
|---|---|---|
| `BROKER_PROMOTED_CAP` | 10000 | whole number from 1 to 1000000 |
| `BROKER_PROMOTED_RETENTION` | 30d | digits then `h` or `d`, from `1h` to `90d`, counted from the last promotion |
| `BROKER_PROMOTE_MIN_EVENTS` | 3 | whole number from 2 to 1000: the distinct events a rule needs |
| `BROKER_PROMOTE_WINDOW` | 15m | digits then `s`, `m`, `h` or `d`, from `1m` to `7d`, not above `BROKER_EVENT_RETENTION` |

## Operating it

- Back up the state file with the API stopped before the first start of the Phase 7 image: that start adds
  the `promoted` table. The change is additive, so the rollback is an image rollback. The earlier image
  opens the same file and ignores the new table.
- Run the command with `docker exec` in the API container, as above. Try `--dry-run` first: it prints what
  would be written, in counts.
- The file holds only graph names, closed words and ids in `promoted`. It never holds raw text, a message,
  a host or a neighbour name written by a device.

## Deferred

- NGSI-LD views (see `design/exchange-profile.md`).
- A served MCP server. It is built and unit-tested, and an optional extra; nothing serves it.
- Promotion of recurring eligibility gaps.
- Telemetry-based inference of links.
- An operator rule file or any rule added at run time.
- Retiring a hypothesis by hand or on a schedule.
- Verifying the third neighbour-discovery wording against a real device.
