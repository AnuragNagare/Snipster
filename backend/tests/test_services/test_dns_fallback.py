"""DNS fallback: a hand-made DNS question and answer parser, the cache, and the worker's database connection."""

import socket
import struct

import pytest

from app.core import dns_fallback as dns


@pytest.fixture(autouse=True)
def clean_cache():
    dns.clear_cache()
    yield
    dns.clear_cache()


def reply(query_id, answers, rcode=0, question=b"\x03abc\x03com\x00"):
    """A DNS answer: header, the question, then the answer records (each already encoded)."""
    header = struct.pack("!HHHHHH", query_id, 0x8180 | rcode, 1, len(answers), 0, 0)
    return header + question + struct.pack("!HH", 1, 1) + b"".join(answers)


def a_record(ip, name=b"\xc0\x0c"):        # a pointer to the name in the question
    return name + struct.pack("!HHIH", 1, 1, 60, 4) + bytes(int(p) for p in ip.split("."))


def cname_record(target=b"\x05other\x03com\x00", name=b"\xc0\x0c"):
    return name + struct.pack("!HHIH", 5, 1, 60, len(target)) + target


# ─── The question ────────────────────────────────────────────────────────────

def test_the_question_asks_for_the_a_record_of_the_name():
    q = dns.build_query("db.example.com", 0x1234)
    assert q[:2] == b"\x12\x34" and q[2:4] == b"\x01\x00"            # id, recursion desired
    assert q[4:6] == b"\x00\x01"                                      # one question
    assert q[12:] == b"\x02db\x07example\x03com\x00\x00\x01\x00\x01"  # labels, type A, class IN


def test_a_trailing_dot_is_ignored():
    assert dns.build_query("a.b.", 1) == dns.build_query("a.b", 1)


# ─── The answer ──────────────────────────────────────────────────────────────

def test_addresses_are_read_from_the_answer():
    data = reply(7, [a_record("18.189.49.143"), a_record("3.18.239.121")])
    assert dns.parse_a_records(data, 7) == ["18.189.49.143", "3.18.239.121"]


def test_a_cname_before_the_addresses_is_skipped():
    data = reply(7, [cname_record(), a_record("1.2.3.4")])
    assert dns.parse_a_records(data, 7) == ["1.2.3.4"]


def test_an_error_reply_gives_nothing():
    assert dns.parse_a_records(reply(7, [], rcode=3), 7) == []        # NXDOMAIN
    assert dns.parse_a_records(reply(7, [], rcode=2), 7) == []        # server failure


def test_an_error_flag_wins_even_if_the_reply_carries_a_record():
    """Some resolvers hand back a block-page address with an error code: never use it."""
    assert dns.parse_a_records(reply(7, [a_record("10.0.0.1")], rcode=3), 7) == []


def test_a_reply_for_another_question_is_ignored():
    assert dns.parse_a_records(reply(8, [a_record("1.2.3.4")]), 7) == []


def test_garbage_does_not_crash_the_parser():
    assert dns.parse_a_records(b"", 7) == []
    assert dns.parse_a_records(b"\x00\x07", 7) == []


# ─── Asking public DNS ───────────────────────────────────────────────────────

class FakeSocket:
    """Stands in for a UDP socket: answers from a script keyed by resolver address."""

    sent = []
    scripts = {}

    def __init__(self, *a, **k):
        self.server = None

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def settimeout(self, t):
        pass

    def sendto(self, data, addr):
        FakeSocket.sent.append(addr[0])
        self.server, self.query = addr[0], data

    def recvfrom(self, n):
        outcome = FakeSocket.scripts[self.server]
        if isinstance(outcome, Exception):
            raise outcome
        qid = struct.unpack("!H", self.query[:2])[0]
        return outcome(qid), (self.server, 53)


@pytest.fixture
def udp(monkeypatch):
    FakeSocket.sent, FakeSocket.scripts = [], {}
    monkeypatch.setattr(dns.socket, "socket", FakeSocket)
    return FakeSocket


def test_the_first_resolver_that_answers_wins(udp):
    udp.scripts = {"8.8.8.8": lambda qid: reply(qid, [a_record("9.9.9.9")]),
                   "1.1.1.1": lambda qid: reply(qid, [a_record("5.5.5.5")])}
    assert dns.query_public_dns("db.example.com") == "9.9.9.9" and udp.sent == ["8.8.8.8"]


def test_the_second_resolver_is_tried_when_the_first_times_out(udp):
    udp.scripts = {"8.8.8.8": socket.timeout("slow"), "1.1.1.1": lambda qid: reply(qid, [a_record("5.5.5.5")])}
    assert dns.query_public_dns("db.example.com") == "5.5.5.5" and udp.sent == ["8.8.8.8", "1.1.1.1"]


def test_both_resolvers_failing_gives_none(udp):
    udp.scripts = {"8.8.8.8": socket.timeout("x"), "1.1.1.1": OSError("down")}
    assert dns.query_public_dns("db.example.com") is None


def test_a_resolver_that_says_no_such_name_is_skipped(udp):
    udp.scripts = {"8.8.8.8": lambda qid: reply(qid, [], rcode=3), "1.1.1.1": lambda qid: reply(qid, [a_record("5.5.5.5")])}
    assert dns.query_public_dns("db.example.com") == "5.5.5.5"


# ─── When the fallback is used ───────────────────────────────────────────────

def test_no_fallback_when_the_system_resolver_works(monkeypatch):
    monkeypatch.setattr(dns, "system_resolves", lambda h: True)
    monkeypatch.setattr(dns, "query_public_dns", lambda h: pytest.fail("must not be asked"))
    assert dns.fallback_address("db.example.com") is None


def test_fallback_address_when_the_system_resolver_fails(monkeypatch):
    monkeypatch.setattr(dns, "system_resolves", lambda h: False)
    monkeypatch.setattr(dns, "query_public_dns", lambda h: "18.189.49.143")
    assert dns.fallback_address("db.example.com") == "18.189.49.143"


def test_none_when_nothing_can_resolve_it(monkeypatch):
    monkeypatch.setattr(dns, "system_resolves", lambda h: False)
    monkeypatch.setattr(dns, "query_public_dns", lambda h: None)
    assert dns.fallback_address("db.example.com") is None


def test_the_decision_is_cached_for_five_minutes(monkeypatch):
    calls = []
    monkeypatch.setattr(dns, "system_resolves", lambda h: calls.append(h) or False)
    monkeypatch.setattr(dns, "query_public_dns", lambda h: "1.2.3.4")
    now = [1000.0]
    for _ in range(3):
        assert dns.fallback_address("h.example.com", clock=lambda: now[0]) == "1.2.3.4"
    assert calls == ["h.example.com"]                                   # asked once
    now[0] += dns.CACHE_SECONDS + 1
    dns.fallback_address("h.example.com", clock=lambda: now[0])
    assert len(calls) == 2                                              # asked again after it expired


# ─── The worker's database connection ────────────────────────────────────────

URL = "postgresql://user:pw@db.example.com/neondb?sslmode=require&channel_binding=require"


@pytest.fixture
def db(monkeypatch):
    from app.core.config import settings
    monkeypatch.setattr(settings, "DATABASE_URL", URL)


def test_a_normal_network_connects_by_name(db, monkeypatch):
    from app.db import sync_engine
    monkeypatch.setattr(sync_engine.dns_fallback, "fallback_address", lambda h: None)
    kw = sync_engine.connect_kwargs()
    assert kw["host"] == "db.example.com" and "hostaddr" not in kw
    assert (kw["user"], kw["password"], kw["dbname"], kw["port"]) == ("user", "pw", "neondb", 5432)
    assert kw["sslmode"] == "require" and kw["channel_binding"] == "require"


def test_a_broken_resolver_connects_to_the_ip_but_keeps_the_name_for_tls(db, monkeypatch):
    from app.db import sync_engine
    monkeypatch.setattr(sync_engine.dns_fallback, "fallback_address", lambda h: "18.189.49.143")
    kw = sync_engine.connect_kwargs()
    assert kw["hostaddr"] == "18.189.49.143" and kw["host"] == "db.example.com"   # name kept: TLS and routing


def test_the_engine_asks_for_the_address_on_every_new_connection(db, monkeypatch):
    """A changed address is picked up when a pooled connection is replaced."""
    from app.db import sync_engine
    seen = []
    monkeypatch.setattr(sync_engine.dns_fallback, "fallback_address", lambda h: seen.append(h) or "1.1.1.1")
    monkeypatch.setattr(sync_engine.psycopg2, "connect", lambda **kw: (_ for _ in ()).throw(RuntimeError(kw["hostaddr"])))
    engine = sync_engine.make_sync_engine()
    for _ in range(2):
        with pytest.raises(Exception, match="1.1.1.1"):
            engine.connect()
    assert seen == ["db.example.com", "db.example.com"]


def test_the_worker_session_uses_this_engine(monkeypatch):
    from app.workers import tasks
    from app.db import sync_engine

    made = []
    monkeypatch.setattr(sync_engine, "make_sync_engine", lambda: made.append(1) or __import__("sqlalchemy").create_engine("sqlite://"))
    monkeypatch.setattr(tasks, "_SessionFactory", None)
    session = tasks._get_sync_session()
    session.close()
    assert made == [1]
    tasks._SessionFactory = None
