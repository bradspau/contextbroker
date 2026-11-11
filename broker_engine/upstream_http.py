"""One reused HTTP session per upstream (Fuseki, VictoriaMetrics, TMF921).

Reusing a session keeps the TCP connection to an upstream open between calls.
Three rules a reader must not break:

1. Keyword arguments pass through unchanged. Fakes in the tests have fixed
   signatures such as `(url, params=None, timeout=None)`, so this module never
   adds a keyword of its own.
2. Credentials and timeouts stay per call. A session carries no auth, header,
   params or timeout, so a rotated Fuseki credential takes effect on the next
   request and every call site keeps its own `REQUEST_TIMEOUT_S`.
3. A session never retries (`max_retries=0`) and keeps no cookie. A repeated
   observation POST would push twice, and nothing one reply sets may be sent on
   a later call.

Sessions live here and nowhere else: some modules are reloaded by tests, and a
session kept in a reloaded module would be forgotten.
"""
import threading
from http.cookiejar import DefaultCookiePolicy

import requests
from requests.adapters import HTTPAdapter

UPSTREAMS = ("fuseki", "victoriametrics", "tmf921")

_lock = threading.Lock()
_sessions: dict[str, requests.Session] = {}


def _new_session() -> requests.Session:
    s = requests.Session()
    adapter = HTTPAdapter(pool_connections=2, pool_maxsize=32, max_retries=0)
    s.mount("http://", adapter)
    s.mount("https://", adapter)
    # An empty allow-list accepts no cookie and returns none.
    s.cookies.set_policy(DefaultCookiePolicy(allowed_domains=[]))
    return s


def session(upstream: str) -> requests.Session:
    """The process-wide session for `upstream`; any other name is refused."""
    if upstream not in UPSTREAMS:
        raise ValueError("unknown upstream")
    with _lock:
        s = _sessions.get(upstream)
        if s is None:
            s = _sessions[upstream] = _new_session()
        return s


def get(upstream: str, url: str, **kwargs):
    return session(upstream).get(url, **kwargs)


def post(upstream: str, url: str, **kwargs):
    return session(upstream).post(url, **kwargs)


def reset_sessions() -> None:
    """Close every session and forget it; the next call makes a new one."""
    with _lock:
        sessions = list(_sessions.values())
        _sessions.clear()
    for s in sessions:
        s.close()
