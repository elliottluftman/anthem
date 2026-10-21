"""Anthem Remote Script: the Ableton Live side of the collaboration prototype.

Responsibilities (and nothing more):
  * keep a non-blocking loopback connection to the companion (bridge.py)
  * bind the collaboration clips the companion asks for (clipbinding.py)
  * observe native note edits on owned clips and report them
  * apply remote edits to non-owned clips, on Live's main thread, from a Live timer
Network to other machines is the companion's job; this module never blocks.
"""
import os
import time
import traceback

import Live
from _Framework.ControlSurface import ControlSurface

from .bridge import Bridge
from .clipbinding import ClipBinding

SCRIPT_VERSION = "0.1.0"
TICK_MS = 50
TOKEN_PATH = "~/.anthem/script.token"


class Anthem(ControlSurface):
    def __init__(self, c_instance):
        ControlSurface.__init__(self, c_instance)
        self._bindings = {}
        self._token_path = TOKEN_PATH
        self._bridge = Bridge(log=self.log)
        self._song = self.song()
        self._song.add_tracks_listener(self._on_tracks)
        self._timer = Live.Base.Timer(callback=self._tick, interval=TICK_MS, repeat=True)
        self._timer.start()
        self.log("Anthem %s loaded; set=%r" % (SCRIPT_VERSION, self._set_path()))
        self.show_message("Anthem: waiting for companion (127.0.0.1:9800)")

    # --- plumbing ----------------------------------------------------------------
    def log(self, msg):
        self.log_message("Anthem: %s" % msg)

    def _set_path(self):
        try:
            return self._song.file_path or ""
        except Exception:  # noqa
            return ""

    def _send(self, msg):
        return self._bridge.send(msg)

    def disconnect(self):
        try:
            self._timer.stop()
        except Exception:  # noqa
            pass
        for b in self._bindings.values():
            b.close()
        self._bindings = {}
        try:
            if self._song.tracks_has_listener(self._on_tracks):
                self._song.remove_tracks_listener(self._on_tracks)
        except Exception:  # noqa
            pass
        self._bridge.close()
        self.log("disconnected")
        ControlSurface.disconnect(self)

    # --- timer -------------------------------------------------------------------
    def _tick(self):
        # Errors in a Live.Base.Timer callback stop the timer, so nothing may escape here.
        try:
            msgs = self._bridge.poll()
            if self._bridge.just_connected:
                self._hello()
            if self._bridge.just_disconnected:
                self.show_message("Anthem: companion disconnected")
            for m in msgs:
                self._handle(m)
            now = time.time()
            for b in self._bindings.values():
                b.tick(now)
            if self._bridge.dropped:
                self._bridge.dropped = False
                for b in self._bindings.values():
                    if b.owned:
                        b.snapshot_now()
        except Exception:  # noqa
            self.log("tick error: %s" % traceback.format_exc())

    def _token(self):
        """Shared secret written by the companion so only it can drive this script."""
        try:
            with open(os.path.expanduser(self._token_path)) as f:
                return f.read().strip()[:128]
        except (IOError, OSError):
            return ""

    def _hello(self):
        app = Live.Application.get_application()
        self._send({
            "type": "script_hello", "script_version": SCRIPT_VERSION,
            "live_version": "%d.%d.%d" % (app.get_major_version(), app.get_minor_version(), app.get_bugfix_version()),
            "set_name": self._song.name, "set_path": self._set_path(), "token": self._token(),
        })
        self.show_message("Anthem: companion connected")

    # --- inbound -----------------------------------------------------------------
    def _handle(self, m):
        t = m.get("type")
        if t == "bind":
            self._bind(m.get("clips") or [])
        elif t in ("apply_delta", "apply_snapshot", "request_snapshot"):
            b = self._bindings.get(m.get("clip"))
            if b is None:
                if t != "request_snapshot":
                    self._send({"type": "applied", "clip": m.get("clip"), "op_id": m.get("op_id"),
                                "ok": False, "error": "clip not bound", "ms": 0})
                return
            if t == "apply_delta":
                b.apply_delta(m.get("op_id"), m.get("added") or [], m.get("modified") or [], m.get("removed") or [])
            elif t == "apply_snapshot":
                b.apply_snapshot(m.get("op_id"), m.get("notes") or [], force=bool(m.get("force")))
            else:
                b.snapshot_now()
        elif t == "set_owned":
            b = self._bindings.get(m.get("clip"))
            if b is not None:
                b.set_owned(m.get("owned"))
        elif t == "setup_template":
            self._setup_template(m.get("clips") or [])
        elif t == "reload_code":
            self._reload_code()
        elif t == "show_message":
            self.show_message(str(m.get("msg", ""))[:200])
        else:
            self.log("ignoring message type %r" % (t,))

    def _bind(self, clips):
        for b in self._bindings.values():
            b.close()
        self._bindings = {}
        for c in clips[:16]:
            try:
                b = ClipBinding(str(c["clip"]), str(c["track_name"]), int(c.get("slot", 0)), bool(c.get("owned")),
                                self._send, Live.Clip.MidiNoteSpecification, self.log)
                b.resolve(self._song, announce=False)
                self._bindings[b.clip_id] = b
            except Exception:  # noqa
                self.log("bind failed for %r: %s" % (c, traceback.format_exc()))
        self._send({"type": "inventory",
                    "clips": [b.inventory() for b in self._bindings.values()],
                    "tracks": [t.name for t in self._song.tracks]})
        for b in self._bindings.values():
            if b.found:
                b._on_bound()
        self.show_message("Anthem: bound %d/%d clips" % (
            sum(1 for b in self._bindings.values() if b.found), len(self._bindings)))

    def _reload_code(self):
        """Developer convenience (loopback only): reload the pure modules and re-bind, so a
        code change does not require restarting Live. The surface class itself is not reloaded."""
        import importlib
        from . import clipsync, bridge, clipbinding
        spec = [{"clip": b.clip_id, "track_name": b.track_name, "slot": b.slot, "owned": b.owned}
                for b in self._bindings.values()]
        try:
            for mod in (clipsync, bridge, clipbinding):
                importlib.reload(mod)
            global ClipBinding
            ClipBinding = clipbinding.ClipBinding
            self.log("code reloaded")
        except Exception:  # noqa
            self.log("reload failed: %s" % traceback.format_exc())
        self._bind(spec)

    def _setup_template(self, clips):
        """Create the missing template tracks/clips in the CURRENT set (explicit user command only)."""
        created = []
        try:
            for c in clips[:16]:
                name, slot = str(c["track_name"]), int(c.get("slot", 0))
                track = None
                for t in self._song.tracks:
                    if t.name == name and t.has_midi_input:
                        track = t
                        break
                if track is None:
                    track = self._song.create_midi_track(-1)
                    track.name = name
                    created.append("track %s" % name)
                if slot < len(track.clip_slots) and not track.clip_slots[slot].has_clip:
                    track.clip_slots[slot].create_clip(float(c.get("length", 4.0)))
                    created.append("clip %s[%d]" % (name, slot))
        except Exception:  # noqa
            self.log("setup_template failed: %s" % traceback.format_exc())
        self.log("setup_template created: %s" % (created or "nothing"))
        self._bind(clips)

    def _on_tracks(self):
        for b in self._bindings.values():
            try:
                b.resolve(self._song)
            except Exception:  # noqa
                self.log("resolve failed: %s" % traceback.format_exc())


def create_instance(c_instance):
    return Anthem(c_instance)
