"""Look a host name up through public DNS when this machine's own resolver cannot.

Some networks (hotspots, some routers) fail to resolve the database host even though Google's
resolver (8.8.8.8) can. The worker runs unattended on a laptop that moves between networks, so it
must not depend on the network's DNS for the database. Only the worker's database connection uses this
(see app/db/sync_engine.py); everything else keeps the normal resolver.

A plain DNS question over UDP, standard library only. Nothing secret is sent: just a host name.
"""

import socket
import struct
import time

PUBLIC_RESOLVERS = ("8.8.8.8", "1.1.1.1")
TIMEOUT_SECONDS = 3.0
CACHE_SECONDS = 300

_cache: dict[str, tuple[float, str | None]] = {}


def clear_cache() -> None:
    _cache.clear()


def build_query(host: str, query_id: int) -> bytes:
    """A DNS question for the A record of `host`."""
    header = struct.pack("!HHHHHH", query_id, 0x0100, 1, 0, 0, 0)  # recursion desired, one question
    name = b"".join(bytes([len(part)]) + part.encode("ascii") for part in host.rstrip(".").split("."))
    return header + name + b"\x00" + struct.pack("!HH", 1, 1)       # type A, class IN


def _skip_name(data: bytes, pos: int) -> int:
    while True:
        length = data[pos]
        if length == 0:
            return pos + 1
        if length & 0xC0 == 0xC0:   # compression pointer: two bytes, the name continues elsewhere
            return pos + 2
        pos += 1 + length


def parse_a_records(data: bytes, query_id: int) -> list[str]:
    """The IPv4 addresses in a DNS answer (CNAMEs and other records are skipped). Empty if it is an error."""
    if len(data) < 12:
        return []
    reply_id, flags, questions, answers, _, _ = struct.unpack("!HHHHHH", data[:12])
    if reply_id != query_id or flags & 0x000F != 0:   # wrong reply, or the server reported an error
        return []
    pos = 12
    for _ in range(questions):
        pos = _skip_name(data, pos) + 4
    found = []
    for _ in range(answers):
        pos = _skip_name(data, pos)
        rtype, _rclass, _ttl, length = struct.unpack("!HHIH", data[pos:pos + 10])
        pos += 10
        if rtype == 1 and length == 4:
            found.append(".".join(str(b) for b in data[pos:pos + 4]))
        pos += length
    return found


def query_public_dns(host: str, *, resolvers=PUBLIC_RESOLVERS, timeout: float = TIMEOUT_SECONDS) -> str | None:
    """The first IPv4 address any public resolver gives for `host`, or None."""
    query_id = int.from_bytes(__import__("os").urandom(2), "big")
    question = build_query(host, query_id)
    for server in resolvers:
        try:
            with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
                sock.settimeout(timeout)
                sock.sendto(question, (server, 53))
                data, _ = sock.recvfrom(2048)
            addresses = parse_a_records(data, query_id)
            if addresses:
                return addresses[0]
        except (OSError, struct.error, IndexError):
            continue
    return None


def system_resolves(host: str) -> bool:
    try:
        socket.getaddrinfo(host, None, socket.AF_INET)
        return True
    except OSError:
        return False


def fallback_address(host: str, *, clock=time.monotonic) -> str | None:
    """
    None when the system resolver works for `host` (connect normally).
    Otherwise the address public DNS gives for it, or None if that fails too.
    The decision is kept for 5 minutes so a broken resolver is not asked on every connection.
    """
    now = clock()
    hit = _cache.get(host)
    if hit and now - hit[0] < CACHE_SECONDS:
        return hit[1]
    address = None if system_resolves(host) else query_public_dns(host)
    _cache[host] = (now, address)
    return address
