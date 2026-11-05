"""Render flow with two SIMULATED clients (real relay, two real companions with the real
upload/download glue, the real Remote Script code headless on MOCKED songs) and the surface
wiring for render_capture / apply_render. Not Ableton."""
import asyncio
import hashlib
import math
import os
import struct
import sys
import tempfile
import time

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
for p in (ROOT, os.path.join(ROOT, "tests", "live_stubs"), os.path.join(ROOT, "remote_script"), os.path.join(ROOT, "tests")):
    if p not in sys.path:
        sys.path.insert(0, p)

import Live  # noqa: E402
import mock_live as ml  # noqa: E402
from companion import anthem as capp  # noqa: E402
from companion.core import Companion  # noqa: E402
from test_e2e_sim import World, wait_for  # noqa: E402
from test_headless_surface import start, run, CLIPS  # noqa: E402


def write_aifc(path, frames=48000 * 2, rate=48000):
    """A 2-second 32-bit float stereo AIFC like Live's recorder writes (sine, not silence)."""
    vals = []
    for i in range(frames):
        v = math.sin(i * 2 * math.pi * 220 / rate) * 0.5
        vals += [v, v]
    raw = struct.pack(">%df" % len(vals), *vals)

    def ext80(r):
        e = 16383 + (r.bit_length() - 1)
        return struct.pack(">HQ", e, r << (64 - r.bit_length()))
    comm = struct.pack(">hLh", 2, frames, 32) + ext80(rate) + b"fl32" + b"\x00\x00"
    ssnd = struct.pack(">LL", 0, 0) + raw
    body = b"AIFC" + b"COMM" + struct.pack(">L", len(comm)) + comm + b"SSND" + struct.pack(">L", len(ssnd)) + ssnd
    with open(path, "wb") as f:
        f.write(b"FORM" + struct.pack(">L", len(body)) + body)


@pytest.fixture
def tmpdir_():
    with tempfile.TemporaryDirectory() as d:
        yield d


def until(pred, surface, song, comp, max_ticks=60):
    """Tick the headless surface until pred() holds (socket delivery is asynchronous)."""
    for _ in range(max_ticks):
        if pred():
            return True
        run(surface, song, comp, ticks=1, dt=0.01)
    return pred()


def test_surface_render_capture_and_apply_wiring(tmp_path):
    song = ml.MockSong()
    song.tracks[0].clip_slots[0].create_clip(8.0)
    song.tracks[0].devices = [ml.MockDevice("Serum 2")]
    song.record_file = str(tmp_path / "rec.aif")
    song.flush()
    surface, comp = start(song)
    try:
        comp.send({"type": "bind", "clips": CLIPS})
        invs = lambda: [m for m in comp.inbox if m["type"] == "inventory" and m["clips"] and m["clips"][0].get("devices")]
        assert until(invs, surface, song, comp)
        inv = invs()[-1]
        assert inv["clips"][0]["devices"] == [{"name": "Serum 2", "cls": "PluginDevice", "plugin": True}]
        assert inv["plugins"] == []            # the stub Live has no browser
        comp.send({"type": "render_capture", "clip": "drums", "render_id": "rid1"})
        assert until(lambda: song.tracks[-1].name == "Anthem Render Bus", surface, song, comp)
        # a second capture while one runs is refused, the first continues
        comp.send({"type": "render_capture", "clip": "drums", "render_id": "rid2"})
        refused = lambda: [m for m in comp.inbox if m["type"] == "render_captured" and m["render_id"] == "rid2"]
        assert until(refused, surface, song, comp)
        assert not refused()[0]["ok"] and "in progress" in refused()[0]["error"]
        song.finish_recording()
        import Anthem.render as rmod
        old = rmod.SETTLE_SECONDS
        rmod.SETTLE_SECONDS = 0.0
        done = lambda: [m for m in comp.inbox if m["type"] == "render_captured" and m["render_id"] == "rid1"]
        try:
            assert until(done, surface, song, comp)
        finally:
            rmod.SETTLE_SECONDS = old
        assert done()[0]["ok"] and done()[0]["path"] == song.record_file and done()[0]["beats"] == 8.0
        assert all(t.name != "Anthem Render Bus" for t in song.tracks)
        # unbound clip -> error; apply_render needs an existing file
        comp.send({"type": "render_capture", "clip": "nope", "render_id": "rid3"})
        comp.send({"type": "apply_render", "clip": "bass", "render_id": "r9", "path": str(tmp_path / "missing.wav"),
                   "track_name": "Anthem Bass", "slot": 0, "beats": 4.0})
        wav = tmp_path / "r.wav"
        wav.write_bytes(b"RIFF....WAVE")
        comp.send({"type": "apply_render", "clip": "bass", "render_id": "r10", "path": str(wav),
                   "track_name": "Anthem Bass", "slot": 0, "beats": 4.0})
        assert until(lambda: len([m for m in comp.inbox if m["type"] == "render_applied"]) >= 2
                     and any(m["type"] == "render_captured" and m["render_id"] == "rid3" for m in comp.inbox),
                     surface, song, comp)
        by = {(m["type"], m.get("render_id")): m for m in comp.inbox}
        assert by[("render_captured", "rid3")]["ok"] is False
        assert by[("render_applied", "r9")]["ok"] is False and "missing" in by[("render_applied", "r9")]["error"]
        assert by[("render_applied", "r10")]["ok"] is True
        t = [x for x in song.tracks if x.name == "Anthem Bass (render)"][0]
        assert t.clip_slots[0].clip.file_path == str(wav)
    finally:
        surface.disconnect()
        comp.close()


def test_two_simulated_clients_render_flow(tmpdir_):
    asyncio.run(_render_flow(tmpdir_))


async def _render_flow(tmp):
    os.environ.setdefault("ANTHEM_HOME", tmp)
    capp.RENDERS_DIR = capp.Path(tmp) / "renders"
    capp.CHUNK_PACE = 0.0
    import Anthem.render as rmod
    rmod.SETTLE_SECONDS = 0.0
    w = World(tmp)
    pump_task = asyncio.ensure_future(w.pump())
    try:
        await w.start_relay()
        a = Companion("p-host-aaaaaaaa", "s" * 32)
        await w.start_companion("A", a)
        await wait_for(lambda: a.code is not None, what="host welcome")
        b = Companion("p-guest-bbbbbbbb", "t" * 32, join_code=a.code)
        await w.start_companion("B", b)
        await wait_for(lambda: a.bound and b.bound, what="both bound")
        await wait_for(lambda: a.clips["drums"].owned and b.clips["bass"].owned, what="ownership")
        song_a, song_b = w.songs["A"], w.songs["B"]
        # A's drums track has a plug-in B lacks: both sides get told (info exchange via relay)
        song_a.tracks[0].set_devices([ml.MockDevice("Serum 2")])       # devices listener -> new inventory
        await wait_for(lambda: b.peer_tracks.get("drums") == ["Serum 2"], what="info relayed")
        song_a.tracks[1].set_devices([ml.MockDevice("Kontakt 8", "AuPluginDevice")])   # B's clip, seen from A: no hint
        w.stdin["B"].put_nowait("rebind\n")                            # explicit rebind re-sends B's inventory
        await wait_for(lambda: a.peer_plugins is not None and w.printed("B", "rebind: session"), what="B rebind")
        assert w.printed("B", "not installed here") and w.printed("A", "does not have Serum 2")
        # A renders: the mock recorder produces a real AIFC file
        rec = os.path.join(tmp, "Anthem Drums 0001.aif")
        write_aifc(rec)
        song_a.record_file = rec
        w.stdin["A"].put_nowait("render drums\n")
        await wait_for(lambda: song_a.recordings, what="capture started (bus recording)")
        assert song_a.tracks[-1].name == "Anthem Render Bus" and song_a.session_record
        song_a.finish_recording()
        await wait_for(lambda: a.clips["drums"].render_state == "shared", timeout=15, what="render uploaded + op acked")
        assert all(t.name != "Anthem Render Bus" for t in song_a.tracks)
        await wait_for(lambda: b.clips["drums"].render_state == "applied", timeout=15, what="render applied on B")
        rt = [t for t in song_b.tracks if t.name == "Anthem Drums (render)"]
        assert rt and rt[0].has_audio_input
        path = rt[0].clip_slots[0].clip.file_path
        assert os.path.isfile(path) and path.endswith(".wav")
        meta = b.clips["drums"].render
        assert hashlib.sha256(open(path, "rb").read()).hexdigest() == meta["sha256"]
        assert meta["bits"] == 24 and meta["sample_rate"] == 48000 and meta["beats"] == 4.0
        assert os.path.getsize(path) > 500000 and meta["n_chunks"] >= 3   # multi-chunk transfer happened
        # guest cannot render the host's clip; status shows the render
        w.stdin["B"].put_nowait("render drums\n")
        await wait_for(lambda: w.printed("B", "only the owner can render"), what="refusal")
        w.stdin["B"].put_nowait("status\n")
        await wait_for(lambda: any("render=" + meta["render_id"][:8] in l for l in w.out["B"]), what="status")
        # relay restart: metadata survives, the file does not; the owner re-uploads, B keeps its copy
        await w.relay.stop()
        await wait_for(lambda: not a.relay_connected and not b.relay_connected, timeout=10, what="both offline")
        await w.start_relay()
        await wait_for(lambda: a.welcomed and b.welcomed, timeout=40, what="both back")
        await wait_for(lambda: w.printed("A", "re-uploading"), timeout=10, what="re-upload started")
        await wait_for(lambda: w.relay.blobs and list(w.relay.blobs.values())[0].complete(meta["render_id"]),
                       timeout=20, what="blob back on relay")
        assert b.clips["drums"].render_path == path
    except AssertionError:
        print("A OUT:", w.out["A"][-15:])
        print("B OUT:", w.out["B"][-15:])
        raise
    finally:
        pump_task.cancel()
        await w.stop()
