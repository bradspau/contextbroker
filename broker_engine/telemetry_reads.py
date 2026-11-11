"""Run the per-metric telemetry reads of one request together, in the original order.

`read_in_order(fn, items)` is `[fn(i) for i in items]` with the calls spread over a
bounded thread pool. Results come back in the order of `items`, and the first failing item
in that order raises, so a caller sees the same answer and the same error as with one
worker. With one item or one worker `fn` runs on the calling thread and no pool is made.
`workers` None reads BROKER_TELEMETRY_WORKERS on each call.

The pool is made per call and sized `min(workers, len(items))`: the cap is per request
(per assess), not a process-wide limit.
"""
from concurrent.futures import ThreadPoolExecutor

from broker_engine.validation import MAX_TELEMETRY_WORKERS, telemetry_workers


def read_in_order(fn, items, workers=None) -> list:
    """`[fn(i) for i in items]`, run on at most `workers` threads (1 to 8)."""
    if workers is None:
        workers = telemetry_workers()  # read on each call
    if isinstance(workers, bool) or not isinstance(workers, int) \
            or not 1 <= workers <= MAX_TELEMETRY_WORKERS:
        raise ValueError(f"workers must be a whole number from 1 to {MAX_TELEMETRY_WORKERS}")
    items = list(items)
    if len(items) <= 1 or workers == 1:
        return [fn(i) for i in items]
    with ThreadPoolExecutor(max_workers=min(workers, len(items))) as pool:
        return list(pool.map(fn, items))
