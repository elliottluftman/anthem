"""End-to-end tests for relay/relay.py: in-process relay on 127.0.0.1:0, real asyncio TCP clients.

Run:  python3 -m unittest tests.test_relay        or        python3 -m pytest tests/test_relay.py -q
"""
import asyncio
import json
import logging
import os
import shutil
import ssl
import subprocess
import sys
import tempfile
import time
import unittest
from unittest import mock

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from anthem_core import protocol, session as sessmod  # noqa: E402
from relay import relay as relaymod  # noqa: E402
from relay.relay import RelayServer  # noqa: E402

HOST_ID, HOST_SECRET = "host-participant-1", "host-secret-0123456789abcdef"
GUEST_ID, GUEST_SECRET = "guest-participant-1", "guest-secret-0123456789abcdef"
CLIPS = [
    {"clip": "drums", "name": "Drums", "track_name": "Drums", "slot": 0, "owner": "host"},
    {"clip": "bass", "name": "Bass", "track_name": "Bass", "slot": 0, "owner": "guest"},
]


def note(nid, pitch=60, start=0.0, dur=0.25, vel=100):
    return {"id": nid, "pitch": pitch, "start": start, "dur": dur, "vel": vel}


def delta_op(op_id, clip, base_rev, added=(), modified=(), removed=()):
    return {"type": "op", "op_id": op_id, "clip": clip, "kind": "delta", "base_rev": base_rev,
            "payload": {"added": list(added), "modified": list(modified), "removed": list(removed)}}


def snapshot_op(op_id, clip, notes):
    return {"type": "op", "op_id": op_id, "clip": clip, "kind": "snapshot", "base_rev": 0,
            "payload": {"notes": list(notes)}}


class Client(object):
    def __init__(self, reader, writer):
        self.reader, self.writer = reader, writer

    @classmethod
    async def connect(cls, port, ssl_ctx=None):
        kw = {"ssl": ssl_ctx, "server_hostname": "localhost"} if ssl_ctx else {}
        r, w = await asyncio.open_connection("127.0.0.1", port, limit=protocol.MAX_LINE_BYTES, **kw)
        return cls(r, w)

    async def send(self, msg):
        self.writer.write(protocol.encode(msg))
        await self.writer.drain()

    async def send_raw(self, data):
        try:
            self.writer.write(data)
            await self.writer.drain()
        except (ConnectionError, OSError):
            pass  # server may close mid-write; that is what the test asserts anyway

    async def recv(self, timeout=3.0):
        """Next message, or None on EOF/connection reset."""
        try:
            line = await asyncio.wait_for(self.reader.readline(), timeout)
        except (ConnectionError, OSError):
            return None
        return json.loads(line.decode("utf-8")) if line else None

    async def recv_match(self, pred, what="message", timeout=3.0):
        """Skip messages until ``pred(msg)`` holds (presence/ownership chatter is dropped)."""
        while True:
            m = await self.recv(timeout)
            if m is None:
                raise AssertionError("connection closed while waiting for %s" % what)
            if pred(m):
                return m

    async def recv_type(self, t, timeout=3.0):
        return await self.recv_match(lambda m: m["type"] == t, t, timeout)

    async def wait_presence(self, online):
        """Wait for a presence broadcast whose {id: online} map equals ``online``."""
        return await self.recv_match(
            lambda m: m["type"] == "presence" and {p["id"]: p["online"] for p in m["participants"]} == online,
            "presence %r" % (online,))

    async def wait_ownership(self, clip, owner):
        return await self.recv_match(
            lambda m: m["type"] == "ownership" and m["clip"] == clip and m["owner"] == owner,
            "ownership %s=%r" % (clip, owner))

    async def expect_closed(self, timeout=3.0):
        while True:
            m = await self.recv(timeout)
            if m is None:
                return

    async def ping_pong(self):
        """Send ping and return every message received before the pong (ordering probe)."""
        await self.send({"type": "ping"})
        got = []
        while True:
            m = await self.recv()
            if m is None or m["type"] == "pong":
                return got
            got.append(m)

    async def op(self, msg):
        await self.send(msg)
        while True:
            m = await self.recv()
            if m is None:
                raise AssertionError("closed while waiting for op_ack/reject")
            if m["type"] in ("op_ack", "reject"):
                return m

    def close(self):
        self.writer.close()


class RelayTestCase(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.mkdtemp(prefix="anthem-relay-test-")
        self.servers = []
        self.clients = []
        self.srv = await self.start_server()

    async def asyncTearDown(self):
        for c in self.clients:
            c.close()
        for s in self.servers:
            await s.stop()
        shutil.rmtree(self.tmp, ignore_errors=True)

    async def start_server(self, **kw):
        s = RelayServer("127.0.0.1", 0, **kw)
        await s.start()
        self.servers.append(s)
        return s

    async def connect(self, srv=None, ssl_ctx=None):
        c = await Client.connect((srv or self.srv).port, ssl_ctx)
        self.clients.append(c)
        return c

    async def create(self, clips=CLIPS, srv=None, **extra):
        c = await self.connect(srv)
        await c.send(dict({"type": "create", "participant": HOST_ID, "secret": HOST_SECRET, "clips": clips}, **extra))
        w = await c.recv_type("welcome")
        return c, w

    async def join(self, code, pid=GUEST_ID, secret=GUEST_SECRET, srv=None):
        c = await self.connect(srv)
        await c.send({"type": "join", "code": code, "participant": pid, "secret": secret})
        return c, await c.recv()

    async def hello(self, session, pid, secret, last_seq=0, srv=None):
        c = await self.connect(srv)
        await c.send({"type": "hello", "session": session, "participant": pid, "secret": secret,
                      "last_seq": last_seq})
        return c, await c.recv()

    async def pair(self, clips=CLIPS):
        host, hw = await self.create(clips)
        guest, gw = await self.join(hw["code"])
        self.assertEqual(gw["type"], "welcome")
        return host, hw, guest, gw

    async def wait_offline(self, session, pid, srv=None):
        """Wait until the relay has processed ``pid``'s disconnect (deterministic, in-process)."""
        srv = srv or self.srv
        for _ in range(300):
            if (session, pid) not in srv.online:
                return
            await asyncio.sleep(0.01)
        raise AssertionError("%s never went offline" % pid)

    async def snapshot_via_hello(self, session, pid, secret):
        c, w = await self.hello(session, pid, secret)
        self.assertEqual(w["type"], "welcome", w)
        c.close()
        await self.wait_offline(session, pid)
        return w["snapshot"]

    # 1 ---------------------------------------------------------------------------
    async def test_create_and_join(self):
        host, hw = await self.create()
        self.assertEqual(hw["type"], "welcome")
        self.assertEqual(len(hw["code"]), 12)
        self.assertTrue(set(hw["code"]) <= set(sessmod.CODE_ALPHABET), hw["code"])
        self.assertEqual(hw["role"], "host")
        self.assertEqual(hw["participant"], HOST_ID)
        self.assertEqual(hw["seq"], 0)
        self.assertEqual(hw["ops"], [])
        self.assertFalse(hw["resync"])
        owners = {c["clip"]: c["owner"] for c in hw["snapshot"]["clips"]}
        self.assertEqual(owners, {"drums": HOST_ID, "bass": "guest"})  # guest not yet resolved
        guest, gw = await self.join(hw["code"].lower())  # codes are case-insensitive
        self.assertEqual(gw["type"], "welcome")
        self.assertEqual(gw["role"], "guest")
        self.assertEqual(gw["session"], hw["session"])
        owners = {c["clip"]: c["owner"] for c in gw["snapshot"]["clips"]}
        self.assertEqual(owners, {"drums": HOST_ID, "bass": GUEST_ID})
        self.assertEqual(gw["snapshot"]["revs"], {"drums": 0, "bass": 0})
        pres = await host.wait_presence({HOST_ID: True, GUEST_ID: True})
        self.assertEqual({p["id"]: p["role"] for p in pres["participants"]}, {HOST_ID: "host", GUEST_ID: "guest"})
        # host is told the guest's clip now has a concrete owner
        await host.wait_ownership("bass", GUEST_ID)

    # 2 ---------------------------------------------------------------------------
    async def test_ownership_enforcement_and_relay(self):
        host, hw, guest, gw = await self.pair()
        r = await guest.op(delta_op("g1", "drums", 0, added=[note(1)]))
        self.assertEqual(r["type"], "reject")
        self.assertEqual(r["op_id"], "g1")
        self.assertIn("not owner", r["reason"])
        ack = await host.op(delta_op("h1", "drums", 0, added=[note(1, 36)]))
        self.assertEqual(ack, {"v": 1, "type": "op_ack", "op_id": "h1", "seq": 1, "rev": 1, "duplicate": False})
        relayed = await guest.recv_type("op")
        self.assertEqual(relayed["seq"], 1)
        self.assertEqual(relayed["rev"], 1)
        self.assertEqual(relayed["participant"], HOST_ID)
        self.assertEqual(relayed["op_id"], "h1")
        self.assertEqual(relayed["clip"], "drums")
        self.assertEqual(relayed["kind"], "delta")
        self.assertEqual(relayed["payload"]["added"][0]["pitch"], 36)
        self.assertEqual(relayed["payload"]["added"][0]["id"], 1)
        # host must not get a relayed copy of its own op
        self.assertEqual([m["type"] for m in await host.ping_pong() if m["type"] == "op"], [])

    # 3 ---------------------------------------------------------------------------
    async def test_duplicate_op_id(self):
        host, hw, guest, gw = await self.pair()
        a1 = await host.op(delta_op("dup", "drums", 0, added=[note(1)]))
        self.assertEqual((a1["seq"], a1["rev"], a1["duplicate"]), (1, 1, False))
        await guest.recv_type("op")
        a2 = await host.op(delta_op("dup", "drums", 0, added=[note(1)]))
        self.assertEqual((a2["seq"], a2["rev"], a2["duplicate"]), (1, 1, True))
        self.assertEqual([m for m in await guest.ping_pong() if m["type"] == "op"], [])
        self.assertEqual(self.srv.sessions[hw["session"]].seq, 1)

    # 4 ---------------------------------------------------------------------------
    async def test_ordering_50_ops(self):
        clips = [dict(c, owner="host") for c in CLIPS]
        host, hw, guest, gw = await self.pair(clips)
        revs = {"drums": 0, "bass": 0}
        for i in range(50):
            clip = "drums" if i % 2 == 0 else "bass"
            await host.send(delta_op("op%d" % i, clip, revs[clip], added=[note(i, 40 + i % 40)]))
            revs[clip] += 1
        acks = []
        while len(acks) < 50:
            acks.append(await host.recv_type("op_ack"))
        self.assertEqual([a["seq"] for a in acks], list(range(1, 51)))
        self.assertTrue(all(not a["duplicate"] for a in acks))
        got = []
        while len(got) < 50:
            got.append(await guest.recv_type("op"))
        self.assertEqual([m["seq"] for m in got], list(range(1, 51)))
        self.assertEqual([m["op_id"] for m in got], ["op%d" % i for i in range(50)])
        self.assertEqual([m["rev"] for m in got if m["clip"] == "drums"], list(range(1, 26)))
        self.assertEqual([m["rev"] for m in got if m["clip"] == "bass"], list(range(1, 26)))

    # 5 ---------------------------------------------------------------------------
    async def test_stale_base_rev(self):
        host, hw, guest, gw = await self.pair()
        a = await host.op(delta_op("a", "drums", 0, added=[note(1)]))
        self.assertEqual(a["rev"], 1)
        r = await host.op(delta_op("b", "drums", 0, added=[note(2)]))
        self.assertEqual(r["type"], "reject")
        self.assertIn("stale", r["reason"])
        s = await host.op(snapshot_op("c", "drums", [note(1), note(2, 62)]))
        self.assertEqual((s["type"], s["seq"], s["rev"]), ("op_ack", 2, 2))
        d = await host.op(delta_op("d", "drums", 2, modified=[note(2, 64)]))
        self.assertEqual((d["type"], d["rev"]), ("op_ack", 3))
        self.assertEqual([m["seq"] for m in [await guest.recv_type("op") for _ in range(3)]], [1, 2, 3])
        guest.close()
        await self.wait_offline(hw["session"], GUEST_ID)
        snap = await self.snapshot_via_hello(hw["session"], GUEST_ID, GUEST_SECRET)
        self.assertEqual([(n["id"], n["pitch"]) for n in snap["notes"]["drums"]], [(1, 60), (2, 64)])

    # 6 ---------------------------------------------------------------------------
    async def test_hello_resume(self):
        host, hw, guest, gw = await self.pair()
        guest.close()
        await host.wait_presence({HOST_ID: True, GUEST_ID: False})
        for i in range(3):
            await host.op(delta_op("r%d" % i, "drums", i, added=[note(i)]))
        guest2, w = await self.hello(hw["session"], GUEST_ID, GUEST_SECRET, last_seq=0)
        self.assertEqual(w["type"], "welcome")
        self.assertFalse(w["resync"])
        self.assertEqual([o["seq"] for o in w["ops"]], [1, 2, 3])
        self.assertEqual([o["op_id"] for o in w["ops"]], ["r0", "r1", "r2"])
        self.assertEqual(w["seq"], 3)
        self.assertEqual(w["ops"][0]["payload"]["added"][0]["id"], 0)
        # partial resume
        guest2.close()
        await self.wait_offline(hw["session"], GUEST_ID)
        guest3, w = await self.hello(hw["session"], GUEST_ID, GUEST_SECRET, last_seq=2)
        self.assertEqual([o["seq"] for o in w["ops"]], [3])
        guest3.close()
        await self.wait_offline(hw["session"], GUEST_ID)
        # log eviction -> ops null, resync true
        old = sessmod.MAX_LOG
        sessmod.MAX_LOG = 2
        try:
            for i in range(3, 8):
                await host.op(delta_op("r%d" % i, "drums", i, added=[note(i)]))
        finally:
            sessmod.MAX_LOG = old
        guest4, w = await self.hello(hw["session"], GUEST_ID, GUEST_SECRET, last_seq=0)
        self.assertEqual(w["type"], "welcome")
        self.assertIsNone(w["ops"])
        self.assertTrue(w["resync"])
        self.assertEqual(w["seq"], 8)
        self.assertEqual(len(w["snapshot"]["notes"]["drums"]), 8)
        # a client claiming to be ahead of the relay also gets a resync
        guest4.close()
        await self.wait_offline(hw["session"], GUEST_ID)
        guest5, w = await self.hello(hw["session"], GUEST_ID, GUEST_SECRET, last_seq=999)
        self.assertTrue(w["resync"])
        self.assertIsNone(w["ops"])

    # 7 ---------------------------------------------------------------------------
    async def test_identity_already_online(self):
        host, hw, guest, gw = await self.pair()
        c, m = await self.hello(hw["session"], HOST_ID, HOST_SECRET)
        self.assertEqual(m["type"], "error")
        self.assertIn("already connected", m["reason"])
        await c.expect_closed()
        # original host still works
        self.assertEqual((await host.ping_pong()) is not None, True)
        host.close()
        await guest.wait_presence({HOST_ID: False, GUEST_ID: True})
        host2, m = await self.hello(hw["session"], HOST_ID, HOST_SECRET)
        self.assertEqual(m["type"], "welcome")
        self.assertEqual(m["role"], "host")

    # 8 ---------------------------------------------------------------------------
    async def test_auth_failures(self):
        host, hw = await self.create()
        # first message must be an auth message
        c = await self.connect()
        await c.send({"type": "ping"})
        m = await c.recv()
        self.assertEqual(m["type"], "error")
        await c.expect_closed()
        c, m = await self.join(hw["code"], pid="short", secret="tiny")
        self.assertEqual(m["type"], "error")
        await c.expect_closed()
        c, m = await self.hello(hw["session"], HOST_ID, "wrong-secret-0123456789")
        self.assertEqual(m["type"], "error")
        self.assertEqual(m["reason"], "bad secret")
        await c.expect_closed()
        c, m = await self.hello(hw["session"], "nobody", GUEST_SECRET)
        self.assertEqual((m["type"], m["reason"]), ("error", "unknown participant"))
        await c.expect_closed()
        c, m = await self.hello("no-such-session", GUEST_ID, GUEST_SECRET)
        self.assertEqual((m["type"], m["reason"]), ("error", "unknown session"))
        await c.expect_closed()
        c, m = await self.join("ZZZZZZZZZZZZ")
        self.assertEqual((m["type"], m["reason"]), ("error", "unknown code"))
        await c.expect_closed()
        guest, gw = await self.join(hw["code"])
        self.assertEqual(gw["type"], "welcome")
        # the invite code is single use: once the guest is in it no longer resolves
        c, m = await self.join(hw["code"], pid="third-participant", secret="third-secret-0123456789")
        self.assertEqual((m["type"], m["reason"]), ("error", "unknown code"))
        await c.expect_closed()
        self.assertNotIn(hw["code"], self.srv.codes)
        # re-auth on an authenticated connection is an error
        await host.send({"type": "hello", "session": hw["session"], "participant": HOST_ID,
                         "secret": HOST_SECRET, "last_seq": 0})
        self.assertEqual((await host.recv_type("error"))["reason"], "already authenticated")
        await host.expect_closed()
        self.assertEqual(len(self.srv.sessions[hw["session"]].participants), 2)

    # 9 ---------------------------------------------------------------------------
    async def test_invalid_payloads(self):
        host, hw, guest, gw = await self.pair()
        guest.close()
        await host.wait_presence({HOST_ID: True, GUEST_ID: False})
        await host.op(delta_op("ok", "drums", 0, added=[note(1)]))
        expected = await self.snapshot_via_hello(hw["session"], GUEST_ID, GUEST_SECRET)
        await host.ping_pong()  # drain presence/ownership chatter from the probe hello

        r = await host.op(delta_op("bad-pitch", "drums", 1, added=[note(2, pitch=200)]))
        self.assertEqual(r["type"], "reject")
        self.assertEqual(r["op_id"], "bad-pitch")
        self.assertIn("pitch", r["reason"])
        r = await host.op(delta_op("no-clip", "nope", 0, added=[note(2)]))
        self.assertEqual((r["type"], r["reason"]), ("reject", "unknown clip"))
        r = await host.op(delta_op("empty", "drums", 1))
        self.assertEqual(r["type"], "reject")
        r = await host.op(delta_op("mod-unknown", "drums", 1, added=[note(5)], modified=[note(99)]))
        self.assertEqual(r["type"], "reject")
        self.assertIn("unknown note", r["reason"])
        r = await host.op({"type": "op", "op_id": "kind", "clip": "drums", "kind": "evil", "base_rev": 1,
                           "payload": {}})
        self.assertEqual(r["type"], "reject")
        await host.send({"type": "claim", "clip": "bad clip!"})
        self.assertEqual((await host.recv())["type"], "reject")  # nothing else is in flight here
        self.assertEqual(await self.snapshot_via_hello(hw["session"], GUEST_ID, GUEST_SECRET), expected)
        self.assertEqual([m for m in await host.ping_pong() if m["type"] not in ("presence", "ownership")], [])

        # unknown type
        await host.send({"type": "bogus"})
        m = await host.recv()
        self.assertEqual(m["type"], "error")
        self.assertIn("unknown message type", m["reason"])
        await host.expect_closed()
        # non-JSON
        c, _ = await self.hello(hw["session"], HOST_ID, HOST_SECRET)
        await c.send_raw(b"{not json\n")
        m = await c.recv_type("error")  # own presence/ownership broadcasts precede it
        self.assertIn("bad json", m["reason"])
        await c.expect_closed()
        # wrong protocol version
        c, _ = await self.hello(hw["session"], HOST_ID, HOST_SECRET)
        await c.send_raw(b'{"v": 2, "type": "ping"}\n')
        m = await c.recv_type("error")
        self.assertIn("version", m["reason"])
        await c.expect_closed()
        c = await self.connect()
        await c.send_raw(b'{"v": 2, "type": "hello"}\n')
        self.assertEqual((await c.recv())["type"], "error")
        await c.expect_closed()
        # oversized line (after auth and as first message)
        big = json.dumps({"v": 1, "type": "op", "op_id": "x", "clip": "drums", "kind": "snapshot",
                          "base_rev": 0, "payload": {"pad": "x" * (protocol.MAX_LINE_BYTES + 100)}})
        c, _ = await self.hello(hw["session"], HOST_ID, HOST_SECRET)
        await c.send_raw(big.encode() + b"\n")
        await c.expect_closed()
        c = await self.connect()
        await c.send_raw(b"x" * (protocol.MAX_LINE_BYTES + 1) + b"\n")
        await c.expect_closed()
        self.assertEqual(await self.snapshot_via_hello(hw["session"], GUEST_ID, GUEST_SECRET), expected)
        self.assertEqual(self.srv.sessions[hw["session"]].seq, 1)

    # 10 --------------------------------------------------------------------------
    async def test_claim(self):
        host, hw, guest, gw = await self.pair()
        await host.send({"type": "claim", "clip": "drums"})  # already mine: ok
        own = await host.recv_type("ownership")
        self.assertEqual(own, {"v": 1, "type": "ownership", "clip": "drums", "owner": HOST_ID})
        await guest.send({"type": "claim", "clip": "drums"})
        r = await guest.recv_type("reject")
        self.assertIsNone(r["op_id"])
        self.assertIn("owned by", r["reason"])
        await guest.send({"type": "claim", "clip": "no-such"})
        self.assertEqual((await guest.recv_type("reject"))["reason"], "unknown clip")
        host.close()
        await guest.wait_presence({HOST_ID: False, GUEST_ID: True})
        self.assertEqual(await guest.ping_pong(), [])  # nothing else queued: the claim reply comes next
        await guest.send({"type": "claim", "clip": "drums"})
        own = await guest.recv()
        self.assertEqual(own, {"v": 1, "type": "ownership", "clip": "drums", "owner": GUEST_ID})
        ack = await guest.op(delta_op("g-drums", "drums", 0, added=[note(7, 38)]))
        self.assertEqual((ack["type"], ack["seq"], ack["rev"]), ("op_ack", 1, 1))
        # host reconnects, sees the new owner, and can no longer write
        host2, w = await self.hello(hw["session"], HOST_ID, HOST_SECRET, last_seq=0)
        self.assertEqual({c["clip"]: c["owner"] for c in w["snapshot"]["clips"]}["drums"], GUEST_ID)
        self.assertEqual(w["ops"][0]["op_id"], "g-drums")
        r = await host2.op(delta_op("h-late", "drums", 1, added=[note(8)]))
        self.assertEqual(r["type"], "reject")
        self.assertIn("not owner", r["reason"])
        await guest.wait_ownership("drums", GUEST_ID)  # re-broadcast on host2's hello
        # release -> unowned -> host claims it back; both sides see each change
        await guest.send({"type": "release", "clip": "drums"})
        await guest.wait_ownership("drums", None)
        await host2.wait_ownership("drums", None)
        await host2.send({"type": "claim", "clip": "drums"})
        await host2.wait_ownership("drums", HOST_ID)
        await guest.wait_ownership("drums", HOST_ID)
        self.assertEqual(self.srv.sessions[hw["session"]].owner_of("drums"), HOST_ID)

    # 11 --------------------------------------------------------------------------
    async def test_persistence(self):
        state = os.path.join(self.tmp, "sub", "relay-state.json")
        srv1 = await self.start_server(state_path=state)
        host, hw = await self.create(srv=srv1)
        for i in range(3):
            await host.op(delta_op("p%d" % i, "drums", i, added=[note(i, 50 + i)]))
        host.close()
        await srv1.stop()
        self.assertTrue(os.path.exists(state))
        with open(state) as f:
            data = json.load(f)
        self.assertEqual(len(data["sessions"]), 1)
        self.assertNotIn(HOST_SECRET, json.dumps(data))
        srv2 = await self.start_server(state_path=state)
        self.assertIn(hw["session"], srv2.sessions)
        c, w = await self.hello(hw["session"], HOST_ID, HOST_SECRET, last_seq=1, srv=srv2)
        self.assertEqual(w["type"], "welcome")
        self.assertEqual(w["code"], hw["code"])
        self.assertTrue(w["resync"])
        self.assertIsNone(w["ops"])
        self.assertEqual(w["seq"], 3)
        self.assertEqual(w["snapshot"]["revs"]["drums"], 3)
        self.assertEqual([(n["id"], n["pitch"]) for n in w["snapshot"]["notes"]["drums"]],
                         [(0, 50), (1, 51), (2, 52)])
        self.assertEqual({cl["clip"]: cl["owner"] for cl in w["snapshot"]["clips"]}["drums"], HOST_ID)
        # the restored session keeps working: join by the same code, host continues at rev 3
        g, gw = await self.join(hw["code"], srv=srv2)
        self.assertEqual(gw["type"], "welcome")
        ack = await c.op(delta_op("p3", "drums", 3, added=[note(3)]))
        self.assertEqual((ack["seq"], ack["rev"]), (4, 4))
        # a duplicate op_id from before the restart is not remembered (log not persisted) -> stale
        r = await c.op(delta_op("p0", "drums", 0, added=[note(0)]))
        self.assertEqual(r["type"], "reject")
        c.close()
        g.close()
        await srv2.stop()
        # a full session is restored without its (retired) invite code
        srv3 = await self.start_server(state_path=state)
        self.assertNotIn(hw["code"], srv3.codes)
        self.assertEqual(len(srv3.sessions[hw["session"]].participants), 2)

    async def test_persist_only_when_dirty_and_never_raises(self):
        state = os.path.join(self.tmp, "clean", "relay-state.json")
        srv = await self.start_server(state_path=state)
        await srv.stop()
        self.assertFalse(os.path.exists(state))      # nothing changed: nothing written
        srv = await self.start_server(state_path=state)
        host, hw = await self.create(srv=srv)
        await asyncio.sleep(relaymod.PERSIST_DEBOUNCE + 0.3)
        mtime = os.stat(state).st_mtime_ns
        self.assertFalse(srv._dirty)
        await srv.stop()
        self.assertEqual(os.stat(state).st_mtime_ns, mtime)   # stop() skips an unchanged state
        # an unwritable path is logged, not raised, and the state stays dirty for a retry
        blocker = os.path.join(self.tmp, "blocker")
        with open(blocker, "w") as f:
            f.write("x")
        srv = await self.start_server(state_path=os.path.join(blocker, "state.json"))
        host, hw = await self.create(srv=srv)
        with self.assertLogs("anthem.relay", level="ERROR") as cm:
            srv._persist_now()
        self.assertIn("persist failed", cm.output[0])
        self.assertTrue(srv._dirty)
        await srv.stop()   # must not raise either

    async def test_restart_at_seq_zero_resyncs_client_ahead(self):
        state = os.path.join(self.tmp, "relay-state.json")
        srv1 = await self.start_server(state_path=state)
        host, hw = await self.create(srv=srv1)
        host.close()
        await srv1.stop()
        srv2 = await self.start_server(state_path=state)
        c, w = await self.hello(hw["session"], HOST_ID, HOST_SECRET, last_seq=5, srv=srv2)
        self.assertEqual(w["type"], "welcome")
        self.assertIs(w["resync"], True)
        self.assertIsNone(w["ops"])
        self.assertIs(type(w["seq"]), int)
        self.assertEqual(w["seq"], 0)
        ack = await c.op(delta_op("after", "drums", 0, added=[note(1)]))
        self.assertEqual((ack["type"], ack["seq"], ack["rev"]), ("op_ack", 1, 1))

    # 12 --------------------------------------------------------------------------
    async def test_tls(self):
        if not shutil.which("openssl"):
            self.skipTest("openssl not available")
        cert, key = os.path.join(self.tmp, "cert.pem"), os.path.join(self.tmp, "key.pem")
        base = ["openssl", "req", "-x509", "-newkey", "rsa:2048", "-nodes", "-subj", "/CN=localhost",
                "-days", "1", "-keyout", key, "-out", cert]
        p = subprocess.run(base + ["-addext", "subjectAltName=DNS:localhost,IP:127.0.0.1"],
                           capture_output=True)
        if p.returncode != 0:
            p = subprocess.run(base, capture_output=True)
        if p.returncode != 0:
            self.skipTest("openssl failed: %s" % p.stderr.decode(errors="replace"))
        from relay.relay import make_ssl_context
        srv = await self.start_server(ssl_context=make_ssl_context(cert, key))
        client_ctx = ssl.create_default_context(cafile=cert)
        c = await self.connect(srv, ssl_ctx=client_ctx)
        self.assertIsNotNone(c.writer.get_extra_info("sslcontext"))
        await c.send({"type": "create", "participant": HOST_ID, "secret": HOST_SECRET, "clips": CLIPS})
        w = await c.recv_type("welcome")
        self.assertEqual(w["role"], "host")
        self.assertEqual(len(w["code"]), 12)
        # plaintext against a TLS relay must not get a welcome (asyncio logs the failed handshake; mute it)
        asyncio_log = logging.getLogger("asyncio")
        level = asyncio_log.level
        asyncio_log.setLevel(logging.CRITICAL)
        try:
            plain = await self.connect(srv)
            await plain.send_raw(protocol.encode({"type": "ping"}))
            self.assertIsNone(await plain.recv(timeout=2.0))
        finally:
            asyncio_log.setLevel(level)


    # extras -----------------------------------------------------------------------
    async def test_rate_limit(self):
        host, hw = await self.create()
        for _ in range(201):
            host.writer.write(protocol.encode({"type": "ping"}))
        await host.writer.drain()
        m = await host.recv_match(lambda m: m["type"] == "error", "error")
        self.assertEqual(m["reason"], "rate limit exceeded")
        await host.expect_closed()
        self.assertEqual(len(self.srv.sessions), 1)  # session survives the dropped connection

    async def test_auth_timeout_and_connection_limit(self):
        srv = await self.start_server(auth_timeout=0.2)
        c = await self.connect(srv)
        self.assertIsNone(await c.recv(timeout=2.0))  # silently closed without a first message
        self.assertEqual(len(srv.sessions), 0)
        # a non-auth first message is refused
        c = await self.connect(srv)
        await c.send({"type": "claim", "clip": "drums"})
        self.assertEqual((await c.recv())["reason"], "first message must be create, join or hello")
        await c.expect_closed()

    async def test_welcome_too_big_falls_back_to_snapshot(self):
        host, hw, guest, gw = await self.pair()
        guest.close()
        await host.wait_presence({HOST_ID: True, GUEST_ID: False})
        # three ~130 KB snapshot ops: the backlog (~390 KB) exceeds MAX_LINE_BYTES, one snapshot does not
        notes = [note(i, 36 + i % 60, start=i * 0.25) for i in range(1200)]
        for i in range(3):
            ack = await host.op(snapshot_op("big%d" % i, "drums", notes))
            self.assertEqual(ack["type"], "op_ack", ack)
        s = self.srv.sessions[hw["session"]]
        self.assertEqual(len(s.ops_since(0)), 3)
        with self.assertRaises(protocol.ProtocolError):
            protocol.encode({"type": "welcome", "ops": s.ops_since(0), "snapshot": s.snapshot()})
        guest2, w = await self.hello(hw["session"], GUEST_ID, GUEST_SECRET, last_seq=0)
        self.assertEqual(w["type"], "welcome", w)
        self.assertIs(w["resync"], True)
        self.assertIsNone(w["ops"])
        self.assertEqual(w["seq"], 3)
        self.assertEqual(len(w["snapshot"]["notes"]["drums"]), 1200)
        # a small backlog is still delivered as ops
        await host.op(delta_op("small", "drums", 3, added=[note(5000)]))
        await guest2.recv_type("op")
        guest2.close()
        await self.wait_offline(hw["session"], GUEST_ID)
        guest3, w = await self.hello(hw["session"], GUEST_ID, GUEST_SECRET, last_seq=3)
        self.assertEqual([o["op_id"] for o in w["ops"]], ["small"])
        self.assertFalse(w["resync"])

    async def test_session_too_large_is_an_explicit_error(self):
        # Ops can no longer build such a session (see test_op_that_would_break_the_welcome_is_rejected),
        # but state that did not come through apply_op (an old state file) must still fail loudly.
        clips = [dict(CLIPS[0], clip="c%d" % i, name="c%d" % i) for i in range(3)]
        host, hw = await self.create(clips)
        s = self.srv.sessions[hw["session"]]
        notes = [protocol.validate_note(note(i, 36 + i % 60, start=i * 0.25)) for i in range(1200)]
        for c in clips:
            s.state[c["clip"]] = {n["id"]: n for n in notes}
        with self.assertRaises(protocol.ProtocolError):
            protocol.encode({"type": "welcome", "snapshot": s.snapshot()})
        host.close()
        await self.wait_offline(hw["session"], HOST_ID)
        c, m = await self.hello(hw["session"], HOST_ID, HOST_SECRET, last_seq=0)
        self.assertEqual((m["type"], m["reason"]), ("error", "session too large"))
        await c.expect_closed()
        self.assertNotIn((hw["session"], HOST_ID), self.srv.online)

    async def test_op_that_would_break_the_welcome_is_rejected(self):
        """Per-clip caps pass, but the welcome carries the snapshot of ALL clips in one line: an op
        that would make it exceed MAX_LINE_BYTES is rejected, so the session stays resumable."""
        host, hw, guest, gw = await self.pair()
        sid = hw["session"]
        notes = [note(i, 36 + i % 60, start=i * 0.25) for i in range(1200)]   # < MAX_NOTES_PER_CLIP
        with mock.patch.object(protocol, "MAX_NOTES_PER_SESSION", 10 ** 6):   # exercise the encode check
            ack = await host.op(snapshot_op("drums-big", "drums", notes))
            self.assertEqual(ack["type"], "op_ack", ack)
            await guest.recv_type("op")
            s = self.srv.sessions[sid]
            before = (s.seq, dict(s.revs), s.snapshot())
            rej = await guest.op(snapshot_op("bass-big", "bass", notes))
            self.assertEqual((rej["type"], rej["reason"]), ("reject", "clip too large"), rej)
            self.assertEqual((s.seq, s.revs, s.snapshot()), before)
            self.assertEqual(await host.ping_pong(), [])      # nothing was broadcast
            # the session is still resumable: the welcome fits
            guest.close()
            await self.wait_offline(sid, GUEST_ID)
            guest2, w = await self.hello(sid, GUEST_ID, GUEST_SECRET, last_seq=0)
            self.assertEqual(w["type"], "welcome", w)
            self.assertEqual(len(w["snapshot"]["notes"]["drums"]), 1200)
            self.assertEqual(w["snapshot"]["notes"]["bass"], [])
            # a smaller clip that keeps the welcome within the line limit is accepted
            ack = await guest2.op(snapshot_op("bass-ok", "bass", notes[:300]))
            self.assertEqual(ack["type"], "op_ack", ack)
            await host.recv_type("op")
        # without the patch the documented soft cap applies outright
        rej = await host.op(delta_op("over-cap", "drums", 1, added=[note(9000)]))
        self.assertEqual((rej["type"], rej["reason"]), ("reject", "clip too large"), rej)
        self.assertEqual(len(s.state["drums"]), 1200)

    async def test_op_whose_relayed_form_exceeds_the_line_limit_is_rejected(self):
        """The relayed op carries seq/rev/participant on top of the incoming line. An op that fits
        on the way in but not on the way out must be rejected (not acked, not broadcast) instead
        of silently closing the receiver."""
        host, hw, guest, gw = await self.pair()
        s = self.srv.sessions[hw["session"]]

        def fat_note(i, start):   # every field present with long float reprs: normalization adds nothing
            return {"id": 10 ** 9 + i, "pitch": 127, "start": start, "dur": 0.123456789012345,
                    "vel": 100.12345678901, "mute": False, "prob": 0.123456789012345,
                    "vel_dev": -12.1234567890123, "rel_vel": 64.123456789012}

        def wire_len(msg):
            return len(json.dumps(dict(msg, v=1), separators=(",", ":"))) + 1

        def relayed(msg):
            return dict(msg, seq=s.seq + 1, rev=s.revs["drums"] + 1, participant=HOST_ID)

        target = protocol.MAX_LINE_BYTES - 4
        per_note = len(json.dumps(fat_note(0, 0.0), separators=(",", ":"))) + 1   # + separating comma
        notes = [fat_note(i, 0.0) for i in range((target - wire_len(snapshot_op("huge", "drums", []))) // per_note)]
        for i in range(len(notes)):   # fine-tune to the byte by lengthening a few ``start`` reprs
            deficit = target - wire_len(snapshot_op("huge", "drums", notes))
            if deficit <= 0:
                break
            notes[i]["start"] = float("0." + "1" * (min(deficit, 14) + 1))
        msg = snapshot_op("huge", "drums", notes)
        self.assertLessEqual(len(notes), protocol.MAX_NOTES_PER_OP)
        self.assertEqual(wire_len(msg), target)
        self.assertEqual(protocol.validate_op(msg)["payload"], msg["payload"])   # same bytes relayed
        self.assertGreater(wire_len(relayed(msg)), protocol.MAX_LINE_BYTES)

        await guest.ping_pong()   # drain the join-time presence/ownership chatter
        real_apply = sessmod.Session.apply_op
        with mock.patch.object(sessmod.Session, "apply_op", autospec=True, side_effect=real_apply) as applied:
            rej = await host.op(msg)
            self.assertEqual((rej["type"], rej["reason"]), ("reject", "clip too large"), rej)
            applied.assert_not_called()          # rejected before the session was touched
        self.assertEqual((s.seq, s.revs["drums"], s.state["drums"]), (0, 0, {}))
        self.assertEqual(await guest.ping_pong(), [])   # receiver: nothing arrived, still connected
        # the link keeps working for ops that do fit
        ack = await host.op(snapshot_op("fits", "drums", notes[:100]))
        self.assertEqual(ack["type"], "op_ack", ack)
        m = await guest.recv_type("op")
        self.assertEqual((m["op_id"], m["seq"], m["rev"], len(m["payload"]["notes"])), ("fits", 1, 1, 100))

    async def test_failed_auth_throttle(self):
        with mock.patch.multiple(relaymod, FAIL_DELAY_AFTER=2, FAIL_DELAY=0.4, FAIL_BLOCK_AFTER=4):
            host, hw = await self.create()
            t0 = time.monotonic()
            for _ in range(2):
                c, m = await self.join("ZZZZZZZZZZZZ")
                self.assertEqual(m["reason"], "unknown code")
            self.assertLess(time.monotonic() - t0, 0.3)   # below the threshold: answered at once
            t0 = time.monotonic()
            c, m = await self.hello(hw["session"], HOST_ID, "wrong-secret-0123456789")
            self.assertEqual(m["reason"], "bad secret")
            self.assertGreaterEqual(time.monotonic() - t0, 0.4)   # tarpit
            t0 = time.monotonic()
            c, m = await self.hello(hw["session"], "nobody", HOST_SECRET)   # 4th failure -> blocked
            self.assertEqual(m["reason"], "unknown participant")
            self.assertGreaterEqual(time.monotonic() - t0, 0.4)
            c = await self.connect()
            m = await c.recv()
            self.assertEqual((m["type"], m["reason"]), ("error", "too many failed attempts"))
            await c.expect_closed()
            # exactly the four guesses were counted (the successful create was not)
            self.assertEqual(len(self.srv.failures["127.0.0.1"]), 4)
            # the block ages out with the window
            host.close()
            await self.wait_offline(hw["session"], HOST_ID)
            with mock.patch.object(relaymod, "FAIL_WINDOW", 0.0):
                await asyncio.sleep(0.01)
                c, m = await self.hello(hw["session"], HOST_ID, HOST_SECRET)
                self.assertEqual(m["type"], "welcome", m)

    async def test_per_ip_connection_limit(self):
        with mock.patch.object(relaymod, "MAX_CONNECTIONS_PER_IP", 3):
            held = [await self.connect() for _ in range(3)]
            c = await self.connect()
            m = await c.recv()
            self.assertEqual((m["type"], m["reason"]), ("error", "too many connections"))
            await c.expect_closed()
            held[0].close()
            for _ in range(300):
                if self.srv.ip_conns.get("127.0.0.1") == 2:
                    break
                await asyncio.sleep(0.01)
            host, hw = await self.create()   # a slot freed up
            self.assertEqual(hw["type"], "welcome")

    async def test_per_ip_session_creation_limit(self):
        with mock.patch.object(relaymod, "SESSION_CREATE_MAX", 2):
            await self.create()
            await self.create()
            c = await self.connect()
            await c.send({"type": "create", "participant": HOST_ID, "secret": HOST_SECRET, "clips": CLIPS})
            m = await c.recv()
            self.assertEqual((m["type"], m["reason"]), ("error", "too many sessions"))
            await c.expect_closed()
            self.assertEqual(len(self.srv.sessions), 2)
            # a rejected create (bad clips) does not count against the quota
            with mock.patch.object(relaymod, "SESSION_CREATE_MAX", 3):
                c = await self.connect()
                await c.send({"type": "create", "participant": HOST_ID, "secret": HOST_SECRET, "clips": []})
                self.assertIn("clips", (await c.recv())["reason"])
                await c.expect_closed()
                self.assertEqual(len(self.srv.created["127.0.0.1"]), 2)

    async def test_create_token(self):
        srv = await self.start_server(create_token="let-me-in")
        for bad, reason in (({}, "create not allowed"), ({"token": "nope"}, "create not allowed"),
                            ({"token": None}, "create not allowed"), ({"token": 5}, "malformed auth")):
            c = await self.connect(srv)
            await c.send(dict({"type": "create", "participant": HOST_ID, "secret": HOST_SECRET, "clips": CLIPS}, **bad))
            m = await c.recv()
            self.assertEqual((m["type"], m["reason"]), ("error", reason), bad)
            await c.expect_closed()
        self.assertEqual(len(srv.sessions), 0)
        host, hw = await self.create(srv=srv, token="let-me-in")
        self.assertEqual(hw["role"], "host")
        # join/hello never need the token
        guest, gw = await self.join(hw["code"], srv=srv)
        self.assertEqual(gw["type"], "welcome")
        guest.close()
        await self.wait_offline(hw["session"], GUEST_ID, srv=srv)
        c, w = await self.hello(hw["session"], GUEST_ID, GUEST_SECRET, srv=srv)
        self.assertEqual(w["type"], "welcome")

    async def test_create_token_guessing_and_protocol_errors_are_throttled(self):
        """A wrong --create-token, a non-auth first message and pre-auth protocol errors all count
        toward the per-IP failure window, so none of them can be hammered at wire speed."""
        with mock.patch.multiple(relaymod, FAIL_DELAY_AFTER=100, FAIL_BLOCK_AFTER=5):
            srv = await self.start_server(create_token="let-me-in")
            for i in range(2):
                c = await self.connect(srv)
                await c.send({"type": "create", "participant": HOST_ID, "secret": HOST_SECRET,
                              "clips": CLIPS, "token": "guess-%d" % i})
                m = await c.recv()
                self.assertEqual((m["type"], m["reason"]), ("error", "create not allowed"))
                await c.expect_closed()
            self.assertEqual(len(srv.failures["127.0.0.1"]), 2)
            # generic pre-auth failures count too: non-auth first message, bad JSON, bad version
            c = await self.connect(srv)
            await c.send({"type": "ping"})
            self.assertEqual((await c.recv())["reason"], "first message must be create, join or hello")
            await c.expect_closed()
            c = await self.connect(srv)
            await c.send_raw(b"this is not json\n")
            self.assertIn("bad json", (await c.recv())["reason"])
            await c.expect_closed()
            self.assertEqual(len(srv.failures["127.0.0.1"]), 4)
            c = await self.connect(srv)
            await c.send_raw(b'{"v": 99, "type": "create"}\n')
            self.assertIn("unsupported protocol version", (await c.recv())["reason"])
            await c.expect_closed()
            self.assertEqual(len(srv.failures["127.0.0.1"]), 5)
            # ... and the IP is now refused outright, even with the right token
            c = await self.connect(srv)
            m = await c.recv()
            self.assertEqual((m["type"], m["reason"]), ("error", "too many failed attempts"))
            await c.expect_closed()
            self.assertEqual(len(srv.sessions), 0)
            # the tarpit applies to those generic failures as well
            with mock.patch.multiple(relaymod, FAIL_DELAY_AFTER=1, FAIL_DELAY=0.3, FAIL_BLOCK_AFTER=100):
                t0 = time.monotonic()
                c = await self.connect(srv)
                await c.send_raw(b"garbage\n")
                self.assertIn("bad json", (await c.recv())["reason"])
                self.assertGreaterEqual(time.monotonic() - t0, 0.3)

    async def test_malformed_auth_shapes_are_rejected_not_crashed(self):
        """Wrong-typed auth fields (non-ASCII token, list participant, int secret, ...) used to raise
        TypeError inside _authenticate: no reply, no throttle count, just log.exception. They must
        be answered with ``error`` and counted like any other pre-auth failure."""
        self.enterContext(mock.patch.object(relaymod, "FAIL_DELAY_AFTER", 1000))   # no tarpit here
        srv = await self.start_server(create_token="let-me-in")
        host, hw = await self.create(srv=srv, token="let-me-in")
        host_conn = srv.online[(hw["session"], HOST_ID)]
        base_create = {"type": "create", "secret": HOST_SECRET, "clips": CLIPS, "token": "let-me-in"}
        base_hello = {"type": "hello", "session": hw["session"], "secret": HOST_SECRET, "last_seq": 0}
        cases = [
            # non-ASCII token: str/str compare_digest raises TypeError; must be a plain mismatch
            (dict(base_create, participant=HOST_ID, token="hünter2"), "create not allowed"),
            (dict(base_create, participant=HOST_ID, token="let-me-iné"), "create not allowed"),
            # participant must be a valid id before any session is touched (list is unhashable)
            (dict(base_create, participant=[HOST_ID]), "bad participant id"),
            (dict(base_hello, participant=[HOST_ID]), "bad participant id"),
            (dict(base_hello, participant={"id": HOST_ID}), "bad participant id"),
            (dict(base_hello, participant=None), "bad participant id"),
            ({"type": "join", "code": hw["code"], "participant": 42, "secret": GUEST_SECRET}, "bad participant id"),
            # secret / token / code / session must be strings
            (dict(base_hello, participant=HOST_ID, secret=12345), "malformed auth"),
            (dict(base_hello, participant=HOST_ID, secret=["a" * 20]), "malformed auth"),
            (dict(base_hello, participant=HOST_ID, session=[hw["session"]]), "malformed auth"),
            (dict(base_hello, participant=HOST_ID, session={"x": 1}), "malformed auth"),
            ({"type": "join", "code": ["ABC"], "participant": GUEST_ID, "secret": GUEST_SECRET}, "malformed auth"),
            ({"type": "join", "code": 1234, "participant": GUEST_ID, "secret": GUEST_SECRET}, "malformed auth"),
            (dict(base_create, participant=HOST_ID, token={"t": "let-me-in"}), "malformed auth"),
        ]
        with self.assertNoLogs("anthem.relay", logging.ERROR):
            for msg, reason in cases:
                c = await self.connect(srv)
                await c.send(msg)
                m = await c.recv()
                self.assertIsNotNone(m, "no reply for %r" % (msg,))
                self.assertEqual((m["type"], m["reason"]), ("error", reason), msg)
                await c.expect_closed()
        self.assertEqual(len(srv.failures["127.0.0.1"]), len(cases))
        self.assertEqual(len(srv.sessions), 1)
        self.assertEqual(len(srv.sessions[hw["session"]].participants), 1)
        # a non-ASCII secret is legitimate: hashed as UTF-8, it round-trips through join and hello
        guest, gw = await self.join(hw["code"], secret="pässwörd-0123456789", srv=srv)
        self.assertEqual(gw["type"], "welcome", gw)
        guest.close()
        await self.wait_offline(hw["session"], GUEST_ID, srv=srv)
        c, w = await self.hello(hw["session"], GUEST_ID, "pässwörd-0123456789", srv=srv)
        self.assertEqual(w["type"], "welcome", w)
        c.close()
        await self.wait_offline(hw["session"], GUEST_ID, srv=srv)
        c, m = await self.hello(hw["session"], GUEST_ID, "passwörd-0123456789", srv=srv)
        self.assertEqual((m["type"], m["reason"]), ("error", "bad secret"))
        await c.expect_closed()
        # defense in depth: an unanticipated TypeError/ValueError inside auth is still a counted,
        # answered failure rather than an unhandled exception
        n = len(srv.failures["127.0.0.1"])
        with mock.patch.object(sessmod.Session, "authenticate", side_effect=TypeError("boom")):
            with self.assertNoLogs("anthem.relay", logging.ERROR):
                c, m = await self.hello(hw["session"], HOST_ID, HOST_SECRET, srv=srv)
            self.assertEqual((m["type"], m["reason"]), ("error", "malformed auth"))
            await c.expect_closed()
        self.assertEqual(len(srv.failures["127.0.0.1"]), n + 1)
        self.assertIs(srv.online[(hw["session"], HOST_ID)], host_conn)   # the real host was untouched


if __name__ == "__main__":
    unittest.main()
