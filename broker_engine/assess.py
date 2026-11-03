"""Deterministic assessment of a TMF921 intent against the broker's telemetry."""
from broker_engine.validation import ClientInputError, window_seconds
from intent_management.tmf921_source import assess_tmf921_as_of, assess_via_tmf921


def assess_intent(
    intent_id: str,
    time_window: str,
    *,
    join=None,
    source: str = "tmf921",
    as_of: str | None = None,
    mode: str | None = None,
) -> dict:
    """Assess a TMF921 intent: read its goals from the TMF921 server, join the
    broker's telemetry and evaluate it. `join` defaults to the real joiner
    (looked up at call time so patches apply).

    TMF921 is the one intent model. The network graph in Fuseki holds only
    service bindings and topology, so there is no graph intent to assess.
    `source` exists only so that a caller naming another source, "graph"
    included, gets a ClientInputError instead of a silent fallback.

    Temporal queries (with neither argument the behaviour is unchanged).
    `as_of` ("10m") evaluates as of that long ago, up to 30d. `mode` is "at"
    (default when as_of is set): the latest sample at that instant, which must
    be fresh relative to it. "within": the worst case over the `time_window`
    ending at that instant (now if no as_of), so a breach anywhere in it counts.
    The goals are read now from the TMF921 intent and the broker judges the past
    telemetry itself: nothing is pushed to the server, no high-water mark moves
    and a question about the past never updates the intent's current fulfilment
    state (see assess_tmf921_as_of)."""
    if source != "tmf921":
        raise ClientInputError(f"Unknown source: {source!r}")
    if mode not in (None, "at", "within"):
        raise ClientInputError(f"Unknown mode: {mode!r}")
    if as_of is not None:
        window_seconds(as_of, "as_of")
    if as_of is not None or mode is not None:
        return assess_tmf921_as_of(intent_id, time_window, as_of=as_of, mode=mode,
                                   join=join)
    return assess_via_tmf921(intent_id, time_window, join=join)
