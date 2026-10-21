"""Runs the real Anthem ControlSurface class headless: STUB Live/_Framework modules
(tests/live_stubs) + MOCKED song (tests/mock_live.py) + a real loopback socket standing
in for the companion. This is not Ableton; it proves the surface's wiring.
"""
import json
import os
import socket
import sys
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "tests", "live_stubs"))
sys.path.insert(0, os.path.join(ROOT, "remote_script"))
sys.path.insert(0, os.path.join(ROOT, "tests"))

import Live  # noqa: E402  (stub)
import mock_live as ml  # noqa: E402
from Anthem import create_instance  # noqa: E402

CLIPS = [{"clip": "drums", "track_name": "Anthem Drums", "slot": 0, "owned": True},
         {"clip": "bass", "track_name": "Anthem Bass", "slot": 0, "owned": False}]


class CInstance(object):
    def __init__(self, song):
        self._song = song

    def song(self):
        return self._song


class FakeCompanion(object):
    """Accepts the script's loopback connection and speaks NDJSON synchronously."""

    def __init__(self):
        self.srv = socket.socket()
        self.srv.bind(("127.0.0.1", 0))
        self.srv.listen(1)
        self.port = self.srv.getsockname()[1]
        self.conn = None
        self.buf = b""
        self.inbox = []

    def accept(self):
        self.srv.settimeout(3)
        self.conn, _ = self.srv.accept()
        self.conn.settimeout(0.05)

    def send(self, msg):
        msg.setdefault("v", 1)
        self.conn.sendall(json.dumps(msg).encode() + b"\n")

    def read(self):
        if self.conn is None:
            return self.inbox
        try:
            while True:
                d = self.conn.recv(65536)
                if not d:
                    break
                self.buf += d
        except socket.timeout:
            pass
        while b"\n" in self.buf:
            line, self.buf = self.buf.split(b"\n", 1)
            self.inbox.append(json.loads(line))
        return self.inbox

    def close(self):
        if self.conn:
            self.conn.close()
        self.srv.close()


def run(surface, song, comp, ticks=6, dt=0.0):
    for _ in range(ticks):
        song.flush()
        Live.Base.fire_all()
        if dt:
            time.sleep(dt)
    comp.read()


def start(song):
    comp = FakeCompanion()
    import Anthem.anthem as mod
    mod.Bridge.__init__.__defaults__ = ("127.0.0.1", comp.port, None)
    surface = create_instance(CInstance(song))
    run(surface, song, comp, ticks=3)
    comp.accept()
    run(surface, song, comp, ticks=3, dt=0.01)
    return surface, comp


def types(comp):
    return [m["type"] for m in comp.inbox]


def test_surface_hello_bind_inventory_apply_and_detect():
    song = ml.MockSong()
    song.tracks[0].clip_slots[0].create_clip(4.0)
    song.tracks[1].clip_slots[0].create_clip(4.0)
    song.flush()
    surface, comp = start(song)
    hello = comp.inbox[0]
    assert hello["type"] == "script_hello" and hello["live_version"] == "12.3.8" and hello["set_path"] == "/tmp/Test.als"

    comp.send({"type": "bind", "clips": CLIPS})
    run(surface, song, comp, dt=0.01)
    inv = [m for m in comp.inbox if m["type"] == "inventory"][0]
    assert {c["clip"]: (c["found"], c["owned"]) for c in inv["clips"]} == {"drums": (True, True), "bass": (True, False)}
    assert inv["tracks"] == ["Anthem Drums", "Anthem Bass"]
    assert [m for m in comp.inbox if m["type"] == "local_snapshot"][0]["clip"] == "drums"

    # remote edit lands in the non-owned clip, and is acknowledged
    note = {"id": 3, "pitch": 40, "start": 1.0, "dur": 0.5, "vel": 90, "mute": False}
    comp.send({"type": "apply_delta", "clip": "bass", "op_id": "r1", "added": [note], "modified": [], "removed": []})
    run(surface, song, comp, dt=0.01)
    assert song.tracks[1].clip_slots[0].clip.content() == [(40, 1.0, 0.5, 90.0, False)]
    applied = [m for m in comp.inbox if m["type"] == "applied"]
    assert applied[-1]["op_id"] == "r1" and applied[-1]["ok"] and "conflict" not in types(comp)

    # native edit on the owned clip is detected after the debounce and reported once
    song.tracks[0].clip_slots[0].clip.user_draw(36, 0.0, 0.25)
    run(surface, song, comp, ticks=2)
    time.sleep(0.15)
    run(surface, song, comp, ticks=2)
    d = [m for m in comp.inbox if m["type"] == "local_delta"]
    assert len(d) == 1 and d[0]["clip"] == "drums" and d[0]["added"][0]["pitch"] == 36 and "t_detect" in d[0]

    # request_snapshot and show_message
    comp.send({"type": "request_snapshot", "clip": "drums"})
    comp.send({"type": "show_message", "msg": "hi"})
    run(surface, song, comp, dt=0.01)
    assert [m for m in comp.inbox if m["type"] == "local_snapshot"][-1]["notes"][0]["pitch"] == 36
    assert "hi" in surface.messages

    # unknown clip apply -> explicit failure, unknown type -> ignored, garbage -> ignored
    comp.send({"type": "apply_snapshot", "clip": "nope", "op_id": "z", "notes": []})
    comp.send({"type": "exec", "code": "1"})
    comp.conn.sendall(b"not json\n")
    run(surface, song, comp, dt=0.01)
    assert comp.inbox[-1] == {"v": 1, "type": "applied", "clip": "nope", "op_id": "z", "ok": False,
                              "error": "clip not bound", "ms": 0}
    assert any("ignoring message type" in l for l in surface.logs)

    # a track deleted -> clip_lost via the song-level tracks listener
    song.delete_track(1)
    run(surface, song, comp, dt=0.01)
    assert comp.inbox[-1]["type"] == "clip_lost" and comp.inbox[-1]["clip"] == "bass"

    surface.disconnect()
    assert surface.disconnected and not surface._bridge.connected
    comp.close()


def test_setup_template_creates_missing_tracks_and_clips_then_binds():
    song = ml.MockSong(track_names=("Anthem Drums",))
    surface, comp = start(song)
    comp.send({"type": "setup_template", "clips": CLIPS})
    run(surface, song, comp, dt=0.01)
    names = [t.name for t in song.tracks]
    assert names == ["Anthem Drums", "Anthem Bass"]
    assert all(t.clip_slots[0].has_clip and t.clip_slots[0].clip.length == 4.0 for t in song.tracks)
    inv = [m for m in comp.inbox if m["type"] == "inventory"][-1]
    assert all(c["found"] for c in inv["clips"])
    # idempotent: a second setup creates nothing new
    comp.send({"type": "setup_template", "clips": CLIPS})
    run(surface, song, comp, dt=0.01)
    assert [t.name for t in song.tracks] == names and any("created: nothing" in l for l in surface.logs)
    surface.disconnect()
    comp.close()


def test_set_owned_token_and_force_passthrough(tmp_path, monkeypatch):
    import Anthem.anthem as mod
    tok = tmp_path / "script.token"
    tok.write_text("sekret-token\n")
    monkeypatch.setattr(mod, "TOKEN_PATH", str(tok))
    song = ml.MockSong()
    song.tracks[0].clip_slots[0].create_clip(4.0)
    song.tracks[1].clip_slots[0].create_clip(4.0)
    song.flush()
    surface, comp = start(song)
    assert comp.inbox[0]["token"] == "sekret-token"
    comp.send({"type": "bind", "clips": CLIPS})
    run(surface, song, comp, dt=0.01)
    # non-owned bass sends a baseline snapshot at bind, owned drums an authoritative one
    snaps = {m["clip"]: m["owned"] for m in comp.inbox if m["type"] == "local_snapshot"}
    assert snaps == {"drums": True, "bass": False}
    # ownership flips at runtime
    comp.send({"type": "set_owned", "clip": "bass", "owned": True})
    run(surface, song, comp, dt=0.01)
    assert surface._bindings["bass"].owned and comp.inbox[-1] == {"v": 1, "type": "local_snapshot", "clip": "bass",
                                                                  "owned": True, "notes": []}
    song.tracks[1].clip_slots[0].clip.user_draw(40, 0, 0.5)
    run(surface, song, comp, ticks=2)
    time.sleep(0.15)
    run(surface, song, comp, ticks=2)
    assert comp.inbox[-1]["type"] == "local_delta" and comp.inbox[-1]["clip"] == "bass"
    # force flag reaches the binding: a pending local edit on drums (now non-owned) is overwritten only with force
    comp.send({"type": "set_owned", "clip": "drums", "owned": False})
    run(surface, song, comp, dt=0.01)
    comp.send({"type": "apply_snapshot", "clip": "drums", "op_id": "i", "notes": []})
    run(surface, song, comp, dt=0.01)
    song.tracks[0].clip_slots[0].clip.user_draw(36, 0, 0.25)
    comp.send({"type": "apply_snapshot", "clip": "drums", "op_id": "h", "notes": []})
    run(surface, song, comp, dt=0.01)
    assert [m for m in comp.inbox if m["type"] == "applied"][-1]["error"] == "conflict"
    comp.send({"type": "apply_snapshot", "clip": "drums", "op_id": "r", "notes": [], "force": True})
    run(surface, song, comp, dt=0.01)
    assert [m for m in comp.inbox if m["type"] == "applied"][-1]["ok"] and song.tracks[0].clip_slots[0].clip.content() == []
    surface.disconnect()
    comp.close()


def test_reload_code_rebinds_with_fresh_modules():
    song = ml.MockSong()
    song.tracks[0].clip_slots[0].create_clip(4.0)
    song.tracks[1].clip_slots[0].create_clip(4.0)
    song.flush()
    surface, comp = start(song)
    comp.send({"type": "bind", "clips": CLIPS})
    run(surface, song, comp, dt=0.01)
    old = surface._bindings["drums"]
    comp.send({"type": "reload_code"})
    run(surface, song, comp, dt=0.01)
    new = surface._bindings["drums"]
    assert new is not old and type(new) is not type(old) and new.found and new.owned
    assert any("code reloaded" in l for l in surface.logs)
    assert [m["type"] for m in comp.inbox].count("inventory") == 2
    # old binding's listeners are gone: a user edit is reported exactly once
    song.tracks[0].clip_slots[0].clip.user_draw(36, 0, 0.25)
    run(surface, song, comp, ticks=2)
    time.sleep(0.15)
    run(surface, song, comp, ticks=2)
    assert [m["type"] for m in comp.inbox].count("local_delta") == 1
    surface.disconnect()
    comp.close()


def test_timer_survives_exceptions_and_companion_absence():
    song = ml.MockSong()
    comp = FakeCompanion()
    import Anthem.anthem as mod
    mod.Bridge.__init__.__defaults__ = ("127.0.0.1", comp.port, None)
    surface = create_instance(CInstance(song))
    comp.srv.close()      # companion not running
    t0 = time.time()
    for _ in range(100):
        Live.Base.fire_all()
    assert time.time() - t0 < 0.5 and surface._timer.running
    surface._bindings["x"] = object()   # something broken inside a tick must not kill the timer
    Live.Base.fire_all()
    assert surface._timer.running and any("tick error" in l for l in surface.logs)
    del surface._bindings["x"]
    surface.disconnect()
