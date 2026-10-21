#!/usr/bin/env python3
"""One REAL Ableton Live + one SIMULATED peer, through the real relay and two real companions.

  Live (host, owns 'Anthem Drums')  <-> companion A <-> relay <-> companion B <-> headless script + mock set
                                                                     (guest, owns 'Anthem Bass')

The simulated guest writes a bass line into its mock clip every few seconds; those notes
must appear in Live's 'Anthem Bass' clip. Anything you draw in Live's 'Anthem Drums' clip
is printed here as received by the simulated guest. Ctrl-C to stop.

Usage: python3 tools/demo_one_live.py [--script-port 9800] [--seconds 120]
Requires Live running with the Anthem control surface selected and the template tracks
present (run tools/live_verify.py once, or type `setup` in a companion).
"""
import argparse
import asyncio
import os
import sys
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
for p in (ROOT, os.path.join(ROOT, "tests", "live_stubs"), os.path.join(ROOT, "remote_script"),
          os.path.join(ROOT, "tests")):
    sys.path.insert(0, p)

import Live  # noqa: E402  (stub, for the simulated peer only)
import mock_live as ml  # noqa: E402
from Anthem import create_instance  # noqa: E402
from Anthem.bridge import Bridge  # noqa: E402
from companion import anthem as capp  # noqa: E402
from companion.core import Companion  # noqa: E402
from relay.relay import RelayServer  # noqa: E402

BASS = [(36, 0.0, 0.5), (36, 1.0, 0.5), (43, 1.5, 0.25), (41, 2.0, 0.75), (36, 3.0, 0.5), (38, 3.5, 0.5)]


class CInstance(object):
    def __init__(self, song):
        self._song = song

    def song(self):
        return self._song


def ts():
    return time.strftime("%H:%M:%S")


async def main(args):
    relay = RelayServer(port=0)
    port = await relay.start()
    print("[%s] relay on 127.0.0.1:%d" % (ts(), port))

    outA, outB = [], []

    def mk_out(tag, store):
        def f(s):
            store.append(s)
            print("[%s] %s: %s" % (ts(), tag, s))
        return f

    a = Companion("p-live-host", "host-secret-" + "x" * 20)
    qa, qb = asyncio.Queue(), asyncio.Queue()
    ra, rb = [], []
    ta = asyncio.ensure_future(capp.run_session(a, ("127.0.0.1", port), script_port=args.script_port,
                                                stdin=qa, out=mk_out("A(Live)", outA), persist=None, runner=ra))
    while a.code is None:
        await asyncio.sleep(0.05)
    print("[%s] session %s code %s" % (ts(), a.session, a.code))

    b = Companion("p-sim-guest", "guest-secret-" + "y" * 20, join_code=a.code)
    sim_port = 9810
    tb = asyncio.ensure_future(capp.run_session(b, ("127.0.0.1", port), script_port=sim_port,
                                                stdin=qb, out=mk_out("B(sim)", outB), persist=None, runner=rb))
    while not rb or not rb[0].ready.is_set():
        await asyncio.sleep(0.05)
    song = ml.MockSong(file_path="/tmp/SimGuest.als")
    song.tracks[0].clip_slots[0].create_clip(4.0)
    song.tracks[1].clip_slots[0].create_clip(4.0)
    song.flush()
    import Anthem.anthem as mod
    mod.TOKEN_PATH = str(capp.token_path(sim_port))
    surface = create_instance(CInstance(song))
    surface._bridge.close()
    surface._bridge = Bridge(port=sim_port, log=lambda m: None)

    async def pump():
        while True:
            song.flush()
            Live.Base.fire_all()
            await asyncio.sleep(0.02)
    pt = asyncio.ensure_future(pump())

    t0 = time.monotonic()
    while not (a.bound and b.bound) and time.monotonic() - t0 < 20:
        await asyncio.sleep(0.1)
    if not a.bound:
        print("Live side not bound — is Live running with Anthem selected and the template tracks present?")
        print("Typing `setup` for the Live side now.")
        await qa.put("setup\n")
        while not a.bound and time.monotonic() - t0 < 40:
            await asyncio.sleep(0.1)
    print("[%s] bound: Live=%s sim=%s" % (ts(), a.bound, b.bound))

    # If Live's Anthem Bass clip already holds notes (e.g. from tools/live_verify.py), the host
    # companion raises a CONFLICT instead of overwriting them; the demo answers as the user would.
    await asyncio.sleep(2.0)   # give the baseline comparison time to run
    if any("CONFLICT: your local bass clip" in line for line in outA):
        print("[%s] Live's bass clip was not empty -> conflict raised; typing 'revert bass' for the host" % ts())
        await qa.put("revert bass\n")
        await asyncio.sleep(1.0)

    bass = song.tracks[1].clip_slots[0].clip
    drums = song.tracks[0].clip_slots[0].clip
    last_drums = None
    step = 0
    end = time.monotonic() + args.seconds
    print("\n*** Draw/move/delete notes in Live's 'Anthem Drums' clip now. ***\n")
    try:
        while time.monotonic() < end:
            if step < len(BASS):
                p, s, d = BASS[step]
                bass.user_draw(p, s, d)
                print("[%s] sim guest drew bass note %s -> should appear in Live's Anthem Bass" % (ts(), (p, s, d)))
                step += 1
            elif step == len(BASS):
                for nid in list(bass._notes)[:2]:
                    bass.user_modify(nid, velocity=70.0)
                print("[%s] sim guest changed velocity of two bass notes" % ts())
                step += 1
            c = drums.content()
            if c != last_drums:
                print("[%s] sim guest's DRUMS clip now has %d notes (from Live): %s" % (ts(), len(c), c[:8]))
                last_drums = c
            await asyncio.sleep(3.0)
    except KeyboardInterrupt:
        pass
    finally:
        await qa.put("status\n")
        await asyncio.sleep(0.3)
        pt.cancel()
        for r in ra + rb:
            r.stop()
        await asyncio.gather(ta, tb, return_exceptions=True)
        surface.disconnect()
        await relay.stop()


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--script-port", type=int, default=9800)
    ap.add_argument("--seconds", type=int, default=120)
    asyncio.run(main(ap.parse_args()))
