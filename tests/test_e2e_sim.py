"""End-to-end with two SIMULATED Ableton clients: real relay (in-process, real TCP), two real
companions (run_session), and the real Remote Script code running headless against stub
Live modules + mocked songs. This is "two simulated clients", not two Ableton installations.
"""
import asyncio
import os
import socket
import sys
import tempfile
import time

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
for p in (ROOT, os.path.join(ROOT, "tests", "live_stubs"), os.path.join(ROOT, "remote_script"),
          os.path.join(ROOT, "tests")):
    sys.path.insert(0, p)

import Live  # noqa: E402 (stub)
import mock_live as ml  # noqa: E402
from Anthem import create_instance  # noqa: E402
from Anthem.bridge import Bridge  # noqa: E402
from companion import anthem as capp  # noqa: E402
from companion.core import Companion  # noqa: E402
from relay.relay import RelayServer  # noqa: E402


def free_port():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    p = s.getsockname()[1]
    s.close()
    return p


class CInstance(object):
    def __init__(self, song):
        self._song = song

    def song(self):
        return self._song


def make_surface(song, port):
    import Anthem.anthem as mod
    mod.TOKEN_PATH = str(capp.token_path(port))   # test companions write their token under ANTHEM_HOME
    surface = create_instance(CInstance(song))
    surface._bridge.close()
    surface._bridge = Bridge(port=port, log=surface.log)
    return surface


async def wait_for(pred, timeout=8.0, what="condition"):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if pred():
            return True
        await asyncio.sleep(0.02)
    raise AssertionError("timeout waiting for " + what)


def clip(song, i):
    return song.tracks[i].clip_slots[0].clip


class World(object):
    """relay + 2 companions + 2 headless Live surfaces, pumped by one asyncio task."""

    def __init__(self, tmp):
        self.tmp = tmp
        self.relay_port = free_port()
        self.relay = None
        self.songs = {}
        self.surfaces = {}
        self.comps = {}
        self.runners = {}
        self.stdin = {}
        self.out = {"A": [], "B": []}
        self.tasks = []
        self.pumping = True

    async def start_relay(self):
        self.relay = RelayServer(port=self.relay_port, state_path=os.path.join(self.tmp, "relay.json"))
        await self.relay.start()

    async def start_companion(self, name, comp):
        q = asyncio.Queue()
        holder = []
        port = free_port()
        self.stdin[name] = q
        self.comps[name] = comp
        task = asyncio.ensure_future(capp.run_session(
            comp, ("127.0.0.1", self.relay_port), script_port=port, stdin=q,
            out=self.out[name].append, persist=None, runner=holder))
        self.tasks.append(task)
        await wait_for(lambda: holder and holder[0].ready.is_set(), what="companion %s ready" % name)
        self.runners[name] = holder[0]
        song = ml.MockSong(file_path="/tmp/%s.als" % name)
        song.tracks[0].clip_slots[0].create_clip(4.0)
        song.tracks[1].clip_slots[0].create_clip(4.0)
        song.flush()
        self.songs[name] = song
        self.surfaces[name] = make_surface(song, port)

    async def pump(self):
        while self.pumping:
            for name, s in self.songs.items():
                s.flush()
            Live.Base.fire_all()
            await asyncio.sleep(0.02)

    async def stop(self):
        self.pumping = False
        for r in self.runners.values():
            r.stop()
        for t in self.tasks:
            try:
                await asyncio.wait_for(t, 5)
            except Exception:  # noqa
                pass
        for s in self.surfaces.values():
            s.disconnect()
        if self.relay:
            await self.relay.stop()

    def printed(self, name, needle):
        return any(needle in line for line in self.out[name])


@pytest.fixture
def tmpdir_():
    with tempfile.TemporaryDirectory() as d:
        yield d


def test_two_simulated_clients_full_flow(tmpdir_):
    asyncio.run(_full_flow(tmpdir_))


async def _full_flow(tmp):
    w = World(tmp)
    pump_task = asyncio.ensure_future(w.pump())
    try:
        await w.start_relay()
        a = Companion("p-host-aaaaaaaa", "s" * 32)
        await w.start_companion("A", a)
        await wait_for(lambda: a.code is not None, what="host welcome")
        b = Companion("p-guest-bbbbbbbb", "t" * 32, join_code=a.code)
        await w.start_companion("B", b)
        try:
            await wait_for(lambda: a.bound and b.bound, what="both bound")
        except AssertionError:
            print("A OUT:", w.out["A"][-12:])
            print("B OUT:", w.out["B"][-12:])
            print("A logs:", w.surfaces["A"].logs[-5:])
            raise
        await wait_for(lambda: a.clips["drums"].owned and not a.clips["bass"].owned
                       and b.clips["bass"].owned and not b.clips["drums"].owned, what="ownership resolved")
        drums_a, drums_b = clip(w.songs["A"], 0), clip(w.songs["B"], 0)
        bass_a, bass_b = clip(w.songs["A"], 1), clip(w.songs["B"], 1)

        # 1-4: A draws, moves, resizes, changes velocity, deletes -> B follows
        t0 = time.monotonic()
        nid = drums_a.user_draw(36, 0.0, 0.25)
        await wait_for(lambda: drums_b.content() == drums_a.content(), what="draw propagates")
        first_latency = time.monotonic() - t0
        drums_a.user_modify(nid, start_time=1.0)
        await wait_for(lambda: drums_b.content() == [(36, 1.0, 0.25, 100.0, False)], what="move")
        drums_a.user_modify(nid, duration=0.75)
        await wait_for(lambda: drums_b.content() == [(36, 1.0, 0.75, 100.0, False)], what="resize")
        drums_a.user_modify(nid, velocity=64.0)
        await wait_for(lambda: drums_b.content() == [(36, 1.0, 0.75, 64.0, False)], what="velocity")
        drums_a.user_delete(nid)
        await wait_for(lambda: drums_b.content() == [], what="delete")

        # 5-6: both directions concurrently, no feedback loop, no conflicts
        for i in range(8):
            drums_a.user_draw(36 + i, i * 0.5, 0.25)
            bass_b.user_draw(40 + i, i * 0.5, 0.5)
        await wait_for(lambda: drums_b.content() == drums_a.content() and bass_a.content() == bass_b.content(),
                       what="bidirectional convergence")
        assert len(drums_b.content()) == 8 and len(bass_a.content()) == 8
        await asyncio.sleep(0.6)
        seq_now = w.relay.sessions[a.session].seq
        await asyncio.sleep(0.6)
        assert w.relay.sessions[a.session].seq == seq_now, "ops kept flowing with no edits: feedback loop"
        assert not a.pending and not b.pending
        assert not w.printed("A", "conflict") and not w.printed("B", "conflict")

        # 7: copy/paste burst: 32 notes at once
        for i in range(32):
            drums_a.user_draw(60 + (i % 12), 2.0 + i * 0.0625, 0.0625)
        await wait_for(lambda: drums_b.content() == drums_a.content(), what="burst")

        # B edits A's clip -> conflict on B; revert restores
        bass_notes_before = bass_b.content()
        drums_b.user_draw(99, 3.9, 0.1)
        await wait_for(lambda: b.clips["drums"].conflict, what="conflict detected")
        assert w.printed("B", "revert") and drums_b.content() != drums_a.content()
        # meanwhile A keeps editing: B must not receive writes while in conflict
        drums_a.user_draw(37, 3.5, 0.25)
        await asyncio.sleep(0.5)
        assert (99, 3.9, 0.1, 100.0, False) in drums_b.content()
        await w.stdin["B"].put("revert drums\n")
        await wait_for(lambda: drums_b.content() == drums_a.content() and not b.clips["drums"].conflict, what="revert")
        assert bass_b.content() == bass_notes_before

        # 13: a second client with B's identity while B is online is rejected
        c_out = []
        dup = Companion(b.participant, b.secret, session=b.session, role="guest")
        holder = []
        dup_task = asyncio.ensure_future(capp.run_session(dup, ("127.0.0.1", w.relay_port), script_port=free_port(),
                                                          stdin=asyncio.Queue(), out=c_out.append, runner=holder))
        await wait_for(lambda: any("already connected" in l for l in c_out), what="duplicate identity rejected")
        holder[0].stop()
        await dup_task
        assert b.relay_connected and b.welcomed

        # 14: B deletes the drums clip -> lost; recreates -> snapshot re-applied
        w.songs["B"].tracks[0].clip_slots[0].delete_clip()
        await wait_for(lambda: not b.clips["drums"].found, what="clip_lost")
        drums_a.user_draw(38, 3.75, 0.25)
        await asyncio.sleep(0.4)
        w.songs["B"].tracks[0].clip_slots[0].create_clip(4.0)
        await wait_for(lambda: clip(w.songs["B"], 0).content() == drums_a.content(), what="re-created clip resynced")

        # 11-12: relay restart with persistence; companions reconnect and resync; edits flow again
        drums_snapshot = drums_a.content()
        await w.relay.stop()
        await wait_for(lambda: not a.relay_connected and not b.relay_connected, what="both see relay down")
        drums_a.user_draw(39, 0.25, 0.25)       # offline edit: coalesced, not queued
        await asyncio.sleep(0.3)
        assert not a.pending
        await w.start_relay()
        await wait_for(lambda: a.welcomed and b.welcomed, timeout=15, what="reconnect after relay restart")
        await wait_for(lambda: clip(w.songs["B"], 0).content() == drums_a.content(), timeout=10, what="post-restart resync")
        assert len(drums_a.content()) == len(drums_snapshot) + 1
        bass_b.user_draw(50, 3.0, 0.5)
        await wait_for(lambda: bass_a.content() == bass_b.content(), what="edit after restart")

        # 15: A opens a different set -> script re-instantiates -> companion refuses to bind
        other = ml.MockSong(file_path="/tmp/other.als")
        other.tracks[0].clip_slots[0].create_clip(4.0)
        other.flush()
        w.surfaces["A"].disconnect()
        old_port = w.surfaces["A"]._bridge.addr[1]
        w.songs["A"] = other
        w.surfaces["A"] = make_surface(other, old_port)
        await wait_for(lambda: a.bind_blocked, what="different set blocked")
        bass_b.user_draw(51, 3.5, 0.5)
        await asyncio.sleep(0.5)
        assert clip(other, 1) is None or clip(other, 1).content() == []
        assert clip(other, 0).content() == []        # nothing written into the new set
        assert w.printed("A", "rebind")

        # restore A on the original set for the handover test
        w.surfaces["A"].disconnect()
        w.songs["A"] = songA = ml.MockSong(file_path="/tmp/A.als")
        songA.tracks[0].clip_slots[0].create_clip(4.0)
        songA.tracks[1].clip_slots[0].create_clip(4.0)
        songA.flush()
        w.surfaces["A"] = make_surface(songA, old_port)
        await wait_for(lambda: a.bound and not a.bind_blocked, what="A rebound")
        await wait_for(lambda: clip(songA, 1).content() == bass_b.content() and a.clips["drums"].owned, what="A caught up")
        drums_a = clip(songA, 0)
        drums_a.user_draw(60, 0.0, 1.0)
        await wait_for(lambda: clip(w.songs["B"], 0).content() == drums_a.content(), what="A's drums reach B again")

        # 9: ownership handover: host goes offline, guest claims drums, guest's edits now flow
        w.runners["A"].stop()
        await wait_for(lambda: not w.relay.sessions[a.session].participants[a.participant]["online"], what="host offline")
        await w.stdin["B"].put("claim drums\n")
        await wait_for(lambda: b.clips["drums"].owned and w.surfaces["B"]._bindings["drums"].owned, what="guest owns drums in companion AND script")
        drums_b2 = clip(w.songs["B"], 0)
        rev_before = w.relay.sessions[a.session].revs["drums"]
        drums_b2.user_draw(61, 1.0, 1.0)
        await wait_for(lambda: w.relay.sessions[a.session].revs["drums"] > rev_before + 0
                       and len(w.relay.sessions[a.session].state["drums"]) == len(drums_b2.content()), what="new owner's edit reaches relay")
        assert not w.printed("B", "CONFLICT on drums") or b.clips["drums"].owned
        # host comes back with the same identity: it is no longer the owner and receives the guest's version
        a2 = Companion(a.participant, a.secret, session=a.session, role="host")
        holder = []
        qa2 = asyncio.Queue()
        ta2 = asyncio.ensure_future(capp.run_session(a2, ("127.0.0.1", w.relay_port), script_port=old_port,
                                                     stdin=qa2, out=w.out["A"].append, persist=None, runner=holder))
        w.tasks.append(ta2)
        await wait_for(lambda: holder and holder[0].ready.is_set(), what="host companion restarted")
        w.runners["A"] = holder[0]
        w.stdin["A"] = qa2
        w.surfaces["A"].disconnect()
        w.surfaces["A"] = make_surface(songA, old_port)
        await wait_for(lambda: a2.bound and not a2.clips["drums"].owned, what="host sees drums owned by guest")
        # The host's local drums differ from the session (the guest edited it meanwhile): the
        # companion must NOT overwrite silently — it raises a conflict and waits for `revert`.
        await wait_for(lambda: a2.clips["drums"].conflict, what="host warned about diverged drums")
        assert clip(songA, 0).content() == [(60, 0.0, 1.0, 100.0, False)]      # untouched so far
        await w.stdin["A"].put("revert drums\n")
        try:
            await wait_for(lambda: clip(songA, 0).content() == drums_b2.content(), what="host receives guest's drums after revert")
        except AssertionError:
            print("A OUT:", w.out["A"][-6:])
            print("A clips:", {k: (c.owned, c.found, c.conflict, c.paused, len(c.notes)) for k, c in a2.clips.items()})
            print("A drums:", clip(songA, 0).content(), "B drums:", drums_b2.content())
            print("A logs:", w.surfaces["A"].logs[-6:])
            raise
        drums_b2.user_draw(62, 2.0, 0.5)
        await wait_for(lambda: clip(songA, 0).content() == drums_b2.content(), what="handover live")

        print("first-edit latency %.0f ms" % (first_latency * 1000))
    finally:
        pump_task.cancel()
        await w.stop()
