"""REST client for the TMF921 intent server (BBF_921_java).

SECURITY: the base URL comes from the environment (TMF921_BASE_URL) at import
time or from a trusted constructor argument. Callers (REST handlers, the agent
harness) must NEVER pass a base_url derived from request or model data: that
would be an SSRF vector. Identifiers going into the URL path or JSON body are
validated with allow-list regexes before any HTTP call.

Errors are generic: Tmf921Error never carries the URL, headers or response
body, and nothing sensitive is logged.
"""
import math
import os
import re
from datetime import datetime, timezone
from decimal import Decimal

import requests
from rdflib import BNode, Graph, Literal, URIRef
from rdflib.namespace import RDF

from broker_engine.validation import REQUEST_TIMEOUT_S, ClientInputError
from data_contracts.metric_map import ALLOWED_UNITS, broker_metric_for, check_uri

TMF921_BASE_URL = os.environ.get(
    "TMF921_BASE_URL", "http://localhost:8000/tmf-api/intentManagement/v5"
)

_MAX_TTL_CHARS = 64 * 1024
# Predicates that merely declare a metric (metric as subject); any other use
# of a metric URI outside its condition list is unreadable -> fail closed.
_DECLARATION_PREDICATES = frozenset({
    RDF.type,
    URIRef("http://tio.models.tmforum.org/tio/v3.6.0/IntentManagementOntology/associatedValueType"),
})
_INTENT_ID = re.compile(r"[A-Za-z0-9-]{1,64}")


class Tmf921Error(Exception):
    """TMF921 server unavailable or returned something unusable (generic text)."""


def _check_intent_id(intent_id) -> str:
    if not isinstance(intent_id, str) or not _INTENT_ID.fullmatch(intent_id):
        raise ClientInputError("Invalid intent_id")
    return intent_id


def intent_bounds(intent) -> dict[str, float]:
    """Metric URI -> numeric bound from an intent's Turtle expression.

    Finds every RDF list of exactly two items (URI, node with rdf:value).
    Other URI-headed lists (log:allOf, log:match, ...) are skipped; a list
    headed by a known metric or carrying rdf:value that is not exactly that
    shape is unreadable and gives {}.
    Returns {} on anything unreadable, non-finite, or ambiguous (same metric
    with differing values); never raises.
    """
    try:
        expr = intent.get("expression")
        ttl = expr.get("expressionValue")
        if not isinstance(ttl, str) or len(ttl) > _MAX_TTL_CHARS:
            return {}
        g = Graph()
        g.parse(data=ttl, format="turtle")
        out: dict[str, float] = {}
        heads: set = set()
        for _s, p, head in g:
            if p == RDF.rest or not isinstance(head, BNode) or (head, RDF.first, None) not in g:
                continue
            items = list(g.items(head))
            if not items or not isinstance(items[0], URIRef):
                continue  # not a condition list (e.g. literal-first)
            # A condition is a list headed by a known metric or carrying an
            # rdf:value. It must be exactly (URI, node-with-one-rdf:value);
            # anything else is unreadable -> fail closed. Other lists
            # (log:allOf, log:match, ...) are structure and are skipped.
            if not (broker_metric_for(str(items[0])) is not None
                    or any(g.value(i, RDF.value) is not None for i in items[1:])):
                continue
            if len(items) != 2:
                return {}
            vals = list(g.objects(items[1], RDF.value))
            if len(vals) != 1 or not isinstance(vals[0], Literal):
                return {}
            py = vals[0].toPython()
            if isinstance(py, bool) or not isinstance(py, (int, float, Decimal)):
                return {}
            num = float(py)
            if not math.isfinite(num):
                return {}
            uri = str(items[0])
            if uri in out and out[uri] != num:
                return {}
            out[uri] = num
            heads.add(head)
        # A recognised metric URI may appear nowhere else in the expression.
        for s_, p_, o_ in g:
            if str(p_) in out or (str(s_) in out and p_ not in _DECLARATION_PREDICATES):
                return {}
            if isinstance(o_, URIRef) and str(o_) in out and not (
                    p_ == RDF.first and s_ in heads):
                return {}
        return out
    except Exception:
        return {}


class Tmf921Client:
    def __init__(self, base_url: str = TMF921_BASE_URL):
        self.base_url = base_url.rstrip("/")

    def _get_json(self, path: str):
        try:
            r = requests.get(f"{self.base_url}{path}", timeout=REQUEST_TIMEOUT_S,
                             allow_redirects=False)
            if not (200 <= r.status_code < 300):
                raise Tmf921Error("TMF921 request failed")
            return r.json()
        except Tmf921Error:
            raise
        except Exception:
            raise Tmf921Error("TMF921 request failed") from None

    def get_intent(self, intent_id: str) -> dict:
        data = self._get_json(f"/intent/{_check_intent_id(intent_id)}")
        if not isinstance(data, dict):
            raise Tmf921Error("TMF921 response unusable")
        return data

    def list_reports(self, intent_id: str) -> list[dict]:
        data = self._get_json(f"/intent/{_check_intent_id(intent_id)}/intentReport")
        if not isinstance(data, list) or not all(isinstance(d, dict) for d in data):
            raise Tmf921Error("TMF921 response unusable")
        return data

    def post_observation(self, intent_id: str, metric_uri: str, value: float,
                         unit: str | None = None,
                         obtained_at: datetime | None = None) -> None:
        _check_intent_id(intent_id)
        try:
            check_uri(metric_uri)
        except ValueError:
            raise ClientInputError("Invalid metric_uri") from None
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise ClientInputError("Invalid value")
        try:
            value = float(value)
            finite = math.isfinite(value)
        except (OverflowError, TypeError, ValueError):
            raise ClientInputError("Invalid value") from None
        if not finite:
            raise ClientInputError("Invalid value")
        if unit is not None and (not isinstance(unit, str) or unit not in ALLOWED_UNITS):
            raise ClientInputError("Invalid unit")
        body: dict = {"metricUri": metric_uri, "value": float(value)}
        if unit is not None:
            body["unit"] = unit
        if obtained_at is not None:
            if not isinstance(obtained_at, datetime) or obtained_at.tzinfo is None \
                    or obtained_at.utcoffset() is None:
                raise ClientInputError("obtained_at must be timezone-aware")
            try:
                utc = obtained_at.astimezone(timezone.utc).replace(tzinfo=None)
            except (OverflowError, ValueError):
                raise ClientInputError("Invalid obtained_at") from None
            body["obtainedAt"] = utc.isoformat(timespec="microseconds" if utc.microsecond else "seconds") + "Z"
        try:
            r = requests.post(f"{self.base_url}/intent/{intent_id}/observation",
                              json=body, timeout=REQUEST_TIMEOUT_S,
                              allow_redirects=False)
            ok = r.status_code in (200, 201)
        except Exception:
            raise Tmf921Error("TMF921 request failed") from None
        if not ok:
            raise Tmf921Error("TMF921 request failed")
