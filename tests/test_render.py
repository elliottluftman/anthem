"""Renders (audio for a participant lacking a plug-in): protocol, relay blob store, companion
state machine, Remote Script capture/apply against MOCKED Live objects (not Ableton), and the
audio converter."""
import asyncio
import base64
import hashlib
import math
import os
import struct
import sys
import wave

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
for p in (ROOT, os.path.join(ROOT, "tests", "live_stubs"), os.path.join(ROOT, "remote_script"), os.path.join(ROOT, "tests")):
    if p not in sys.path:
        sys.path.insert(0, p)

import mock_live as ml  # noqa: E402
from anthem_core import protocol, session as sessmod  # noqa: E402
from anthem_core.session import Rejected, Session  # noqa: E402
from companion import audio  # noqa: E402
from companion.core import Companion, TEMPLATE_CLIPS  # noqa: E402
from relay.relay import BlobStore, RelayServer  # noqa: E402
from Anthem import render as rendermod  # noqa: E402
from Anthem.clipbinding import ClipBinding  # noqa: E402


def meta(size=1000, rid="r1", beats=16.0, sha=None):
    return {"render_id": rid, "beats": beats, "sample_rate": 48000, "channels": 2, "bits": 24,
            "size": size, "sha256": sha or ("a" * 64), "n_chunks": -(-size // protocol.BLOB_CHUNK_BYTES)}


# ---------------------------------------------------------------- protocol
def test_validate_render_and_info():
    m = protocol.validate_render(meta())
    assert m["n_chunks"] == 1 and m["beats"] == 16.0
    for bad in ({"render_id": "x y"}, dict(meta(), beats=0), dict(meta(), bits=32), dict(meta(), n_chunks=3),
                dict(meta(), sha256="zz"), dict(meta(), size=protocol.MAX_BLOB_BYTES + 1)):
        with pytest.raises(protocol.ProtocolError):
            protocol.validate_render(dict(meta(), **bad))
    info = protocol.validate_info({"plugins": ["Serum 2", "Serum 2"], "tracks": {"drums": ["Serum 2"]}})
    assert info == {"plugins": ["Serum 2"], "tracks": {"drums": ["Serum 2"]}}
    with pytest.raises(protocol.ProtocolError):
        protocol.validate_info({"plugins": ["x"] * (protocol.MAX_PLUGIN_NAMES + 1)})
    with pytest.raises(protocol.ProtocolError):
        protocol.validate_info({"tracks": {"bad id!": []}})
    op = protocol.validate_op({"op_id": "o1", "clip": "drums", "kind": "render", "payload": meta()})
    assert op["kind"] == "render"


# ---------------------------------------------------------------- session / blob store
def test_session_render_op_replaces_and_snapshots():
    s = Session("s", "CODE", TEMPLATE_CLIPS)
    s.add_participant("h", "x" * 20, "host")
    s.add_participant("g", "y" * 20, "guest")
    op = protocol.validate_op({"op_id": "o1", "clip": "drums", "kind": "render", "payload": meta(rid="r1")})
    seq, rev, dup = s.apply_op("h", op)
    assert (seq, rev, dup) == (1, 1, False) and s.renders["drums"]["render_id"] == "r1"
    with pytest.raises(Rejected):
        s.apply_op("g", protocol.validate_op({"op_id": "o2", "clip": "drums", "kind": "render", "payload": meta(rid="r2")}))
    s.apply_op("h", protocol.validate_op({"op_id": "o3", "clip": "drums", "kind": "render", "payload": meta(rid="r3")}))
    assert s.renders["drums"]["render_id"] == "r3" and s.state["drums"] == {}
    snap = s.snapshot(lambda bid: bid == "r3")
    assert snap["renders"]["drums"]["available"] is True
    s2 = Session.from_json(s.to_json())
    assert s2.renders["drums"]["render_id"] == "r3" and s2.snapshot()["renders"]["drums"]["available"] is False


def test_blob_store_chunks_eviction_and_meta():
    C = protocol.BLOB_CHUNK_BYTES
    st = BlobStore(limit=3 * C)
    data = bytes(range(256)) * ((C + 100) // 256 + 1)
    data = data[:C + 100]                       # 2 chunks
    assert st.put("b1", 0, 2, len(data), data[:C], "h") is False
    with pytest.raises(ValueError):             # wrong length for the last chunk
        st.put("b1", 1, 2, len(data), data[C:C + 5], "h")
    with pytest.raises(ValueError):             # another owner cannot write into it
        st.put("b1", 1, 2, len(data), data[C:], "g")
    assert st.put("b1", 1, 2, len(data), data[C:], "h") is True
    assert st.complete("b1") and st.meta("b1") == (len(data), hashlib.sha256(data).hexdigest())
    assert st.get("b1", 1)[2] == data[C:] and st.get("b1", 7) is None
    # a second blob that does not fit evicts the first (complete, reproducible by its owner)
    big = b"z" * (2 * C)
    st.put("b2", 0, 2, len(big), big[:C], "h")
    assert not st.complete("b1") and st.get("b1", 0) is None
    with pytest.raises(ValueError):
        st.put("huge", 0, 4, 4 * C, b"q" * C, "h")


# ---------------------------------------------------------------- relay end to end (real sockets)
class Client(object):
    def __init__(self, reader, writer):
        self.r, self.w = reader, writer

    async def send(self, msg):
        self.w.write(protocol.encode(msg))
        await self.w.drain()

    async def recv(self, want=None, timeout=5):
        while True:
            line = await asyncio.wait_for(self.r.readline(), timeout)
            assert line, "connection closed"
            m = protocol.decode(line.rstrip(b"\n"), protocol.RELAY_TYPES)
            if want is None or m["type"] == want:
                return m


async def connect(port):
    r, w = await asyncio.open_connection("127.0.0.1", port, limit=protocol.MAX_LINE_BYTES + 1024)
    return Client(r, w)


def test_relay_info_blob_and_render_flow(tmp_path):
    async def run():
        relay = RelayServer(port=0, state_path=str(tmp_path / "state.json"))
        port = await relay.start()
        try:
            h = await connect(port)
            await h.send({"type": "create", "participant": "host1", "secret": "s" * 20, "clips": TEMPLATE_CLIPS})
            w = await h.recv("welcome")
            assert w["snapshot"]["renders"] == {}
            g = await connect(port)
            await g.send({"type": "join", "code": w["code"], "participant": "guest1", "secret": "t" * 20})
            await g.recv("welcome")
            # info is relayed, remembered, and handed to a participant that connects later
            await h.send({"type": "info", "plugins": ["Serum 2"], "tracks": {"drums": ["Serum 2"]}})
            m = await g.recv("info")
            assert m["participant"] == "host1" and m["plugins"] == ["Serum 2"]
            # render op without a blob is rejected
            await h.send({"type": "op", "op_id": "op0", "clip": "drums", "kind": "render", "base_rev": 0, "payload": meta()})
            rj = await h.recv("reject")
            assert "blob" in rj["reason"]
            # upload a 2-chunk blob
            C = protocol.BLOB_CHUNK_BYTES
            data = os.urandom(C + 1234)
            n = 2
            for idx, raw in enumerate((data[:C], data[C:])):
                await h.send({"type": "blob_put", "blob": "r1", "idx": idx, "n": n, "size": len(data),
                              "data": base64.b64encode(raw).decode()})
                ack = await h.recv("blob_ack")
                assert ack["ok"] and ack["complete"] == (idx == 1), ack
            # guest may not upload (owns bass, so it may - use a non-owner: nobody here; check mismatch instead)
            bad = dict(meta(size=len(data), sha="b" * 64), n_chunks=n)
            await h.send({"type": "op", "op_id": "op1", "clip": "drums", "kind": "render", "base_rev": 0, "payload": bad})
            assert "mismatch" in (await h.recv("reject"))["reason"]
            good = meta(size=len(data), sha=hashlib.sha256(data).hexdigest())
            await h.send({"type": "op", "op_id": "op2", "clip": "drums", "kind": "render", "base_rev": 0, "payload": good})
            ack = await h.recv("op_ack")
            assert ack["rev"] == 1
            relayed = await g.recv("op")
            assert relayed["kind"] == "render" and relayed["payload"]["render_id"] == "r1"
            # guest downloads chunk by chunk
            got = b""
            for idx in range(n):
                await g.send({"type": "blob_get", "blob": "r1", "idx": idx})
                ch = await g.recv("blob_chunk")
                assert ch["ok"]
                got += base64.b64decode(ch["data"])
            assert got == data
            await g.send({"type": "blob_get", "blob": "nope", "idx": 0})
            assert (await g.recv("blob_chunk"))["ok"] is False
            # a late hello sees the render as available and receives the host's info
            g.w.close()
            g2 = await connect(port)
            await g2.send({"type": "hello", "session": w["session"], "participant": "guest1", "secret": "t" * 20, "last_seq": 0})
            w2 = await g2.recv("welcome")
            assert w2["snapshot"]["renders"]["drums"]["available"] is True
            assert (await g2.recv("info"))["plugins"] == ["Serum 2"]
            # persistence keeps the metadata, not the file
            relay._persist_now()
        finally:
            await relay.stop()
        relay2 = RelayServer(port=0, state_path=str(tmp_path / "state.json"))
        await relay2.start()
        try:
            s = list(relay2.sessions.values())[0]
            assert s.renders["drums"]["render_id"] == "r1"
            assert relay2._snapshot(s)["renders"]["drums"]["available"] is False
        finally:
            await relay2.stop()
    asyncio.run(run())


# ---------------------------------------------------------------- companion core
def host_comp():
    c = Companion("host1", "s" * 20)
    acts = c.on_relay_connected()
    c.on_relay(welcome("host1", "host"))
    c.on_script_connected()
    c.on_script({"type": "script_hello", "live_version": "12", "set_name": "T", "set_path": "/tmp/T.als", "v": 1})
    return c


def welcome(pid, role, renders=None, owner_h="host1", owner_g="guest1"):
    clips = [dict(TEMPLATE_CLIPS[0], owner=owner_h), dict(TEMPLATE_CLIPS[1], owner=owner_g)]
    return {"type": "welcome", "session": "sess1", "code": "C", "participant": pid, "role": role, "seq": 0,
            "snapshot": {"clips": clips, "revs": {"drums": 0, "bass": 0}, "notes": {"drums": [], "bass": []},
                         "renders": renders or {}}, "ops": [], "resync": True}


def inventory(plugins, drums_devices):
    return {"type": "inventory", "plugins": plugins,
            "clips": [{"clip": "drums", "found": True, "track_name": "Anthem Drums", "slot": 0, "owned": True,
                       "note_count": 0, "devices": [{"name": d, "cls": "PluginDevice", "plugin": True} for d in drums_devices]},
                      {"clip": "bass", "found": True, "track_name": "Anthem Bass", "slot": 0, "owned": False, "note_count": 0, "devices": []}],
            "tracks": ["Anthem Drums", "Anthem Bass"]}


def kinds(acts):
    return [(a[0], a[1].get("type")) if len(a) > 1 and isinstance(a[1], dict) else a[0] for a in acts]


def test_core_info_hints_and_render_command():
    c = host_comp()
    acts = c.on_script(inventory(["Serum 2", "Drift"], ["Serum 2"]))
    rel = [a[1] for a in acts if a[0] == "relay" and a[1]["type"] == "info"]
    assert rel and rel[0]["plugins"] == ["Drift", "Serum 2"] and rel[0]["tracks"] == {"drums": ["Serum 2"]}
    # peer lacks Serum 2 -> hint, printed once
    acts = c.on_relay({"type": "info", "participant": "guest1", "plugins": ["Drift"], "tracks": {"bass": ["Massive"]}})
    prints = [a[1] for a in acts if a[0] == "print"]
    assert any("does not have Serum 2" in p and "render drums" in p for p in prints)
    assert any("Massive" in p and "not installed here" in p for p in prints)
    assert not [a for a in c.on_relay({"type": "info", "participant": "guest1", "plugins": ["Drift"], "tracks": {"bass": ["Massive"]}}) if a[0] == "print"]
    # render command: owner only, then capture -> upload -> op
    assert "owned by the other" in c.on_command("render bass")[0][1]
    acts = c.on_command("render drums")
    cap = [a[1] for a in acts if a[0] == "script"][0]
    assert cap["type"] == "render_capture" and c.clips["drums"].render_state == "capturing"
    assert "already in progress" in c.on_command("render drums")[0][1]
    acts = c.on_script({"type": "render_captured", "clip": "drums", "render_id": cap["render_id"], "ok": True,
                        "path": "/tmp/x.aif", "beats": 16.0, "ms": 9000})
    up = [a[1] for a in acts if a[0] == "upload"][0]
    assert up["render_id"] == cap["render_id"] and up["converted"] is False
    m = meta(size=5000, rid=cap["render_id"])
    acts = c.on_transfer({"kind": "upload", "clip": "drums", "render_id": cap["render_id"], "ok": True,
                          "path": "/tmp/r.wav", "beats": 16.0, "meta": {k: m[k] for k in ("sample_rate", "channels", "bits", "size", "sha256", "n_chunks")}})
    op = [a[1] for a in acts if a[0] == "relay"][0]
    assert op["kind"] == "render" and op["payload"]["render_id"] == cap["render_id"] and op["payload"]["beats"] == 16.0
    assert c.clips["drums"].render_state == "shared" and c.clips["drums"].render_path == "/tmp/r.wav"
    # failure paths
    acts = c.on_script({"type": "render_captured", "clip": "drums", "render_id": "zz", "ok": False, "error": "timeout"})
    assert "failed in Live" in acts[0][1] and c.clips["drums"].render_state == "failed"


def test_core_receiver_download_apply_and_welcome_renders():
    c = Companion("guest1", "t" * 20)
    c.on_relay_connected()
    c.on_relay(welcome("guest1", "guest"))
    # render op for a clip we do not own -> download
    acts = c.on_relay({"type": "op", "seq": 1, "rev": 1, "participant": "host1", "op_id": "op1", "clip": "drums",
                       "kind": "render", "payload": meta(rid="r1", size=3000)})
    dl = [a[1] for a in acts if a[0] == "download"]
    assert dl and dl[0]["render_id"] == "r1" and c.clips["drums"].render_state == "downloading"
    # downloaded before Live is connected: apply is deferred
    acts = c.on_transfer({"kind": "download", "clip": "drums", "render_id": "r1", "ok": True, "path": "/tmp/r1.wav"})
    assert not [a for a in acts if a[0] == "script"] and c.clips["drums"].render_apply_pending
    c.on_script_connected()
    c.on_script({"type": "script_hello", "live_version": "12", "set_name": "T", "set_path": "/tmp/T.als", "v": 1})
    inv = inventory(["Drift"], [])
    inv["clips"][0]["owned"] = False
    acts = c.on_script(inv)
    ap = [a[1] for a in acts if a[0] == "script" and a[1]["type"] == "apply_render"]
    assert ap and ap[0]["path"] == "/tmp/r1.wav" and ap[0]["track_name"] == "Anthem Drums" and ap[0]["slot"] == 0
    acts = c.on_script({"type": "render_applied", "clip": "drums", "render_id": "r1", "ok": True, "ms": 12})
    assert c.clips["drums"].render_state == "applied"
    # a superseded render id after a late download is ignored; a stale op is deduped
    assert c.on_transfer({"kind": "download", "clip": "drums", "render_id": "old", "ok": True, "path": "/tmp/old.wav"}) == []
    # welcome (reconnect) listing a newer available render -> download; unavailable -> message only
    acts = c.on_relay(welcome("guest1", "guest", renders={"drums": dict(meta(rid="r2"), available=True)}))
    assert [a for a in acts if a[0] == "download"]
    acts = c.on_relay(welcome("guest1", "guest", renders={"drums": dict(meta(rid="r3"), available=False)}))
    assert not [a for a in acts if a[0] == "download"] and any("lost its file" in a[1] for a in acts if a[0] == "print")
    assert "render=r3" in c.status_text()


def test_core_owner_reuploads_when_relay_lost_the_file():
    c = host_comp()
    c.on_script(inventory([], []))
    d = c.clips["drums"]
    d.render = meta(rid="r9")
    d.render_path = "/tmp/r9.wav"
    acts = c.on_relay(welcome("host1", "host", renders={"drums": dict(meta(rid="r9"), available=False)}))
    up = [a[1] for a in acts if a[0] == "upload"]
    assert up and up[0]["converted"] is True and up[0]["path"] == "/tmp/r9.wav"
    # a fresh companion (nothing remembered) still re-offers: the glue looks for the file on disk
    d.render = None
    d.render_path = None
    acts = c.on_relay(welcome("host1", "host", renders={"drums": dict(meta(rid="r10"), available=False)}))
    up = [a[1] for a in acts if a[0] == "upload"]
    assert up and up[0]["path"] is None and up[0]["render_id"] == "r10"
    # relay offline: a finished upload is not marked dirty (no request_snapshot storm), just reported
    c.on_relay_disconnected()
    acts = c.on_transfer({"kind": "upload", "clip": "drums", "render_id": "r9", "ok": True, "path": "/tmp/r9.wav",
                          "beats": 16.0, "meta": {k: meta()[k] for k in ("sample_rate", "channels", "bits", "size", "sha256", "n_chunks")}})
    assert not [a for a in acts if a[0] == "relay"] and d.dirty is False


# ---------------------------------------------------------------- Remote Script (mocked Live)
def make_binding(song, owned=True):
    sent = []
    b = ClipBinding("drums", "Anthem Drums", 0, owned, sent.append, ml.MidiNoteSpecification, lambda m: None)
    b.resolve(song, announce=False)
    return b, sent


def test_render_capture_records_one_loop_and_cleans_up(tmp_path):
    song = ml.MockSong()
    song.tracks[0].clip_slots[0].create_clip(8.0)
    song.tracks[1].arm = True                         # another armed track must be un-armed during capture
    song.record_file = str(tmp_path / "rec.aif")
    song.flush()
    b, sent = make_binding(song)
    cap = rendermod.RenderCapture(song, "drums", "rid1", b, sent.append, lambda m: None)
    cap.start()
    assert cap.state == "route" and song.tracks[-1].name == rendermod.BUS_NAME and song.undo_steps == 1
    cap.tick(1.0)                                     # routing list empty until Live's next tick
    assert cap.state == "route"
    song.flush()
    cap.tick(1.1)
    bus = song.tracks[-1]
    assert cap.state == "record" and bus.input_routing_type.display_name == "Anthem Drums"
    assert bus.input_routing_channel.display_name == "Post FX" and bus.arm and song.session_record
    assert song.tracks[1].arm is False and song.tracks[0].clip_slots[0].playing and bus.clip_slots[0].is_recording
    cap.tick(1.5)
    assert cap.state == "record"
    song.finish_recording()
    cap.tick(2.0)
    assert cap.state == "settle" and song.session_record is False
    cap.tick(2.0 + rendermod.SETTLE_SECONDS)
    assert cap.done
    msg = sent[-1]
    assert msg["type"] == "render_captured" and msg["ok"] and msg["beats"] == 8.0 and msg["path"] == song.record_file
    assert all(t.name != rendermod.BUS_NAME for t in song.tracks)      # bus deleted
    assert song.tracks[1].arm is True and song.is_playing is False     # arms restored, transport stopped


def test_render_capture_failure_paths():
    song = ml.MockSong()
    b, sent = make_binding(song)                       # no clip in the slot
    cap = rendermod.RenderCapture(song, "drums", "rid2", b, sent.append, lambda m: None)
    cap.start()
    assert cap.done and sent[-1]["ok"] is False and "not found" in sent[-1]["error"]
    song.tracks[0].clip_slots[0].create_clip(4.0)
    song.flush()
    b, sent = make_binding(song)
    cap = rendermod.RenderCapture(song, "drums", "rid3", b, sent.append, lambda m: None)
    cap.start()
    song.flush()
    cap.tick(10.0)
    cap.tick(10.0 + cap.deadline + 1)                  # never finishes recording -> timeout, cleaned up
    assert cap.done and sent[-1]["ok"] is False and "did not finish" in sent[-1]["error"]
    assert all(t.name != rendermod.BUS_NAME for t in song.tracks) and song.session_record is False


def test_apply_render_creates_locked_track_and_replaces_clip(tmp_path):
    song = ml.MockSong()
    f = tmp_path / "r.wav"
    f.write_bytes(b"RIFF")
    ok, err = rendermod.apply_render(song, "Anthem Drums", 0, str(f), "abcdef1234", lambda m: None)
    assert ok and err is None
    t = song.tracks[-1]
    assert t.name == "Anthem Drums (render)" and t.has_audio_input and t.clip_slots[0].clip.file_path == str(f)
    assert t.clip_slots[0].clip.looping and t.clip_slots[0].clip.name == "render abcdef12"
    ok, _ = rendermod.apply_render(song, "Anthem Drums", 0, str(f), "second", lambda m: None)
    assert ok and len([x for x in song.tracks if x.name == "Anthem Drums (render)"]) == 1
    assert song.tracks[-1].clip_slots[0].clip.name == "render second"
    ok, err = rendermod.apply_render(song, "Anthem Drums", 99, str(f), "x", lambda m: None)
    assert not ok and "slot" in err


def test_track_devices_and_installed_plugins_helpers():
    t = ml.MockTrack(ml.MockSong(), "T")
    t.devices = [ml.MockDevice("Serum 2"), ml.MockDevice("Drift", "InstrumentVector"), ml.MockDevice("Kontakt 8", "AuPluginDevice")]
    d = rendermod.track_devices(t)
    assert [x["name"] for x in d if x["plugin"]] == ["Serum 2", "Kontakt 8"]

    class Item(object):
        def __init__(self, name, loadable, kids=()):
            self.name, self.is_loadable, self.iter_children = name, loadable, list(kids)

    class App(object):
        class browser(object):
            plugins = Item("Plug-Ins", False, [Item("VST3", False, [Item("Xfer", False, [Item("Serum 2", True)])]),
                                               Item("AUv2", False, [Item("Xfer", False, [Item("Serum 2", True), Item("Serum 2 FX", True)])])])
    assert rendermod.installed_plugins(App()) == ["Serum 2", "Serum 2 FX"]
    assert rendermod.installed_plugins(object()) == []


# ---------------------------------------------------------------- audio
def aifc_fl32(path, frames=500, rate=48000):
    vals = []
    for i in range(frames):
        v = math.sin(i / 10.0) * 0.5
        vals += [v, -v]
    raw = struct.pack(">%df" % len(vals), *vals)

    def ext80(r):
        e = 16383 + (r.bit_length() - 1)
        return struct.pack(">HQ", e, r << (64 - r.bit_length()))
    comm = struct.pack(">hLh", 2, frames, 32) + ext80(rate) + b"fl32" + b"\x00\x00"
    ssnd = struct.pack(">LL", 0, 0) + raw
    body = b"AIFC" + b"COMM" + struct.pack(">L", len(comm)) + comm + b"SSND" + struct.pack(">L", len(ssnd)) + ssnd
    with open(path, "wb") as f:
        f.write(b"FORM" + struct.pack(">L", len(body)) + body)
    return vals


def test_audio_conversions(tmp_path):
    vals = aifc_fl32(str(tmp_path / "a.aif"))
    m = audio.to_wav(str(tmp_path / "a.aif"), str(tmp_path / "a.wav"))
    assert m["bits"] == 24 and m["channels"] == 2 and m["sample_rate"] == 48000 and m["n_chunks"] == 1
    with wave.open(str(tmp_path / "a.wav")) as w:
        assert w.getnframes() == 500 and w.getsampwidth() == 3
        first = w.readframes(1)
    s = int.from_bytes(first[:3], "little", signed=True)
    assert abs(s / 8388607.0 - vals[0]) < 1e-5
    # 16-bit AIFF stays 16-bit; byte order swapped
    comm = struct.pack(">hLh", 1, 2, 16) + b"\x40\x0e\xac\x44\x00\x00\x00\x00\x00\x00"
    ssnd = struct.pack(">LL", 0, 0) + struct.pack(">hh", 1000, -1000)
    body = b"AIFF" + b"COMM" + struct.pack(">L", len(comm)) + comm + b"SSND" + struct.pack(">L", len(ssnd)) + ssnd
    (tmp_path / "b.aif").write_bytes(b"FORM" + struct.pack(">L", len(body)) + body)
    rate, ch, bits, pcm = audio.read_pcm(str(tmp_path / "b.aif"))
    assert (rate, ch, bits) == (44100, 1, 16) and struct.unpack("<hh", pcm) == (1000, -1000)
    # WAV float -> 24-bit, WAV 24 -> unchanged
    fmt = struct.pack("<HHIIHH", 3, 2, 44100, 44100 * 8, 8, 32)
    data = struct.pack("<4f", 0.5, -0.5, 1.5, -1.5)
    riff = b"WAVE" + b"fmt " + struct.pack("<L", len(fmt)) + fmt + b"data" + struct.pack("<L", len(data)) + data
    (tmp_path / "c.wav").write_bytes(b"RIFF" + struct.pack("<L", len(riff)) + riff)
    rate, ch, bits, pcm = audio.read_pcm(str(tmp_path / "c.wav"))
    assert bits == 24 and int.from_bytes(pcm[6:9], "little", signed=True) == 8388607   # clipped
    with pytest.raises(audio.AudioError):
        audio.read_pcm(str(tmp_path / "a.aif.missing")) if False else audio.read_pcm(__file__)
    assert audio.read_chunk(str(tmp_path / "a.wav"), 0) == (tmp_path / "a.wav").read_bytes()
    assert audio.sha256_file(str(tmp_path / "a.wav")) == m["sha256"]
