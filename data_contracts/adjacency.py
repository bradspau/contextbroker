"""The closed neighbour-discovery line formats the broker may read (SEM-01, D-62, D-65).

A stored event cannot name the far end of a link: the events table keeps the sender's device, a
two-value kind and the raw line, and a link up or down line describes only the sender's own
interface. A neighbour-discovery line names the local interface, the neighbour and the neighbour's
interface, so it is the only input from which a link hypothesis can be inferred. This module reads
such a line, and only at promotion time, from the message part of a stored raw line. Ingest, the
event kind table (``data_contracts.syslog_events``) and the stored shape are not touched by it.

The owner approved the three formats below at the plan 07-07 checkpoint on 2026-11-09 (answer
``approve-all``). The two CDP wordings follow community forum posts about Cisco IOS, not a vendor
guide. ``lldp_neighbor_up`` is a PROPOSED wording that has NOT been checked against any device: it
may never match a real line, or may need to be replaced by the line a real device emits. A new format
is added here only with a near-miss test, and removing one is a code change with a test, not a
migration.

Every format is case-sensitive and anchored: it must start at its first literal token, not preceded
by a letter or digit, and must end the message (``\\Z``), with an optional final full stop only where
the format allows it. The interface and neighbour names are character classes with a fixed upper
length, so device-authored text cannot widen a match. The names a line carries are device-authored
text: callers use them only as lookup keys against the network graph's own names and never store or
print them.

The module is pure: ``re`` and ``dataclasses`` only.
"""
import re
from dataclasses import dataclass

# The interface class of ``data_contracts.syslog_events`` (copied, not imported; a test pins them equal):
# letters, digits and / . : _ - for 1 to 64 characters.
_IFACE = r"[A-Za-z0-9/.:_-]{1,64}"
# A neighbour name: letters, digits and _ . - for 1 to 128 characters.
_PEER = r"[A-Za-z0-9_.-]{1,128}"
_NOT_ALNUM_BEFORE = r"(?<![A-Za-z0-9])"
_DUPLEX = r"(?:full|half)"
# [0-9], never \d: \d reads digits of other scripts.
_VLAN = r"[0-9]{1,4}"

_FORMATS: tuple[tuple[str, re.Pattern[str]], ...] = (
    # Cisco IOS CDP: %CDP-4-DUPLEX_MISMATCH: duplex mismatch discovered on Gi0/0/1 (not half duplex),
    # with Router-C Gi0/0/2 (half duplex).
    ("cdp_duplex_mismatch", re.compile(
        _NOT_ALNUM_BEFORE + r"%CDP-4-DUPLEX_MISMATCH: duplex mismatch discovered on "
        + r"(?P<local>" + _IFACE + r") \((?:not )?" + _DUPLEX + r" duplex\), with "
        + r"(?P<peer>" + _PEER + r") (?P<remote>" + _IFACE + r") \(" + _DUPLEX + r" duplex\)\.?\Z")),
    # Cisco IOS CDP: %CDP-4-NATIVE_VLAN_MISMATCH: Native VLAN mismatch discovered on Gi0/0/1 (1),
    # with Router-C Gi0/0/2 (10).
    ("cdp_native_vlan_mismatch", re.compile(
        _NOT_ALNUM_BEFORE + r"%CDP-4-NATIVE_VLAN_MISMATCH: Native VLAN mismatch discovered on "
        + r"(?P<local>" + _IFACE + r") \(" + _VLAN + r"\), with "
        + r"(?P<peer>" + _PEER + r") (?P<remote>" + _IFACE + r") \(" + _VLAN + r"\)\.?\Z")),
    # LLDP style (proposed wording, unverified): LLDP_NEIGHBOR_UP: LLDP neighbor up on local port
    # Gi0/0/1, remote system Router-C, remote port Gi0/0/2
    ("lldp_neighbor_up", re.compile(
        _NOT_ALNUM_BEFORE + r"LLDP_NEIGHBOR_UP: LLDP neighbor up on local port "
        + r"(?P<local>" + _IFACE + r"), remote system (?P<peer>" + _PEER + r"), remote port "
        + r"(?P<remote>" + _IFACE + r")\Z")),
)

# The approved format names, in the order they are tried.
ADJACENCY_FORMATS: tuple[str, ...] = tuple(name for name, _ in _FORMATS)


@dataclass(frozen=True)
class Adjacency:
    """What a neighbour-discovery line names, as written. These are lookup keys only."""

    local_interface: str
    peer: str
    peer_interface: str


def recognise_adjacency(message: str) -> Adjacency | None:
    """The adjacency a message names, or None for any other text.

    One leading byte order mark is ignored, as ``recognise`` does. The approved patterns are tried in
    order and the first match wins."""
    if not isinstance(message, str):
        return None
    if message.startswith("\ufeff"):
        message = message[1:]
    for _name, pattern in _FORMATS:
        found = pattern.search(message)
        if found is not None:
            return Adjacency(found.group("local"), found.group("peer"), found.group("remote"))
    return None
