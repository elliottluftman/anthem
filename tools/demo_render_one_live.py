#!/usr/bin/env python3
"""Render flow with one REAL Ableton Live (host) and one SIMULATED guest, through the real
relay and two real companions:

  Live (host, 'Anthem Drums' with YOUR instrument/plug-in) <-> companion A <-> relay <-> companion B <-> headless script + mock set

What it does: creates the session, binds both sides (`setup` if needed), prints the PLUG-IN
hints, then types `render drums` for the Live side. Live plays the drums clip once while
recording it; the file is converted, uploaded, downloaded by the simulated guest and "placed"
in its mock set. The script prints every step with timestamps and verifies the file's hash.

Before running: Live open with the Anthem control surface selected (restart Live after
`anthem.py install` so the current script code is loaded), a disposable set, and - for a
meaningful render - an instrument or plug-in on 'Anthem Drums' plus a few notes in its
first clip (the companion's `setup` creates the empty tracks/clips if they are missing).

Usage: python3 tools/demo_render_one_live.py [--script-port 9800] [--timeout 120]
"""
import argparse
import asyncio
import hashlib
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


class CInstance(object):
    def __init__(self, song):
        self._song = song

    def song(self):
        return self._song


def ts():
    return time.strftime("%H:%M:%S")


async def wait(pred, timeout, what):
    t0 = time.monotonic()
    while time.monotonic() - t0 < timeout:
        if pred():
            return time.monotonic() - t0
        await asyncio.sleep(0.05)
    raise SystemExit("TIMEOUT waiting for %s" % what)


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
    await wait(lambda: a.code is not None, 20, "host welcome")
    b = Companion("p-sim-guest", "guest-secret-" + "y" * 20, join_code=a.code)
    sim_port = 9810
    tb = asyncio.ensure_future(capp.run_session(b, ("127.0.0.1", port), script_port=sim_port,
                                                stdin=qb, out=mk_out("B(sim)", outB), persist=None, runner=rb))
    await wait(lambda: rb and rb[0].ready.is_set(), 20, "guest companion")
    song = ml.MockSong(file_path="/tmp/SimGuest.als")
    song.tracks[0].clip_slots[0].create_clip(16.0)
    song.tracks[1].clip_slots[0].create_clip(16.0)
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
    try:
        try:
            await wait(lambda: a.bound and b.bound, 15, "both bound")
        except SystemExit:
            print("[%s] Live side not bound - typing `setup` for it (creates the template tracks/clips)" % ts())
            await qa.put("setup\n")
            await wait(lambda: a.bound and b.bound, 30, "both bound after setup")
        await wait(lambda: a.clips["drums"].found, 15, "Live's Anthem Drums clip")
        await asyncio.sleep(2.0)
        if any("CONFLICT: your local bass clip" in line for line in outA):
            await qa.put("revert bass\n")
        print("[%s] plug-ins on Live's Anthem Drums: %s | installed in Live: %s" % (
            ts(), a.clips["drums"].devices or "none", len(a.plugins or [])))
        if not a.clips["drums"].devices:
            print("[%s] NOTE: no plug-in on Anthem Drums; the render still works (stock instrument or silence)" % ts())
        print("\n*** Typing `render drums` for the Live side: Live will play the drums clip once and record it. ***\n")
        t0 = time.monotonic()
        await qa.put("render drums\n")
        await wait(lambda: a.clips["drums"].render_state in ("uploading", "shared", "failed"), args.timeout, "capture")
        print("[%s] capture finished after %.1fs -> %s" % (ts(), time.monotonic() - t0, a.clips["drums"].render_state))
        if a.clips["drums"].render_state == "failed":
            raise SystemExit("render failed on the Live side (see messages above)")
        await wait(lambda: a.clips["drums"].render_state == "shared", args.timeout, "upload + op_ack")
        print("[%s] shared after %.1fs: %s" % (ts(), time.monotonic() - t0, a.clips["drums"].render))
        await wait(lambda: b.clips["drums"].render_state == "applied", args.timeout, "guest download + placement")
        rt = [t for t in song.tracks if t.name == "Anthem Drums (render)"][0]
        path = rt.clip_slots[0].clip.file_path
        meta = b.clips["drums"].render
        sha = hashlib.sha256(open(path, "rb").read()).hexdigest()
        print("[%s] guest placed %s (%d KiB, %d-bit, %d Hz, %.0f beats) hash %s after %.1fs total" % (
            ts(), os.path.basename(path), meta["size"] // 1024, meta["bits"], meta["sample_rate"], meta["beats"],
            "OK" if sha == meta["sha256"] else "MISMATCH", time.monotonic() - t0))
        await qa.put("status\n")
        await asyncio.sleep(0.5)
        print("\nRESULT: render verified end to end with a real Live as the owner (%s)." % ("hash OK" if sha == meta["sha256"] else "HASH MISMATCH"))
    finally:
        pt.cancel()
        for r in ra + rb:
            r.stop()
        await asyncio.gather(ta, tb, return_exceptions=True)
        surface.disconnect()
        await relay.stop()


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--script-port", type=int, default=9800)
    ap.add_argument("--timeout", type=int, default=120)
    asyncio.run(main(ap.parse_args()))
