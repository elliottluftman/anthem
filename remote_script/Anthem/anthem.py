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
from . import render as rendermod

SCRIPT_VERSION = "0.1.0"
TICK_MS = 50
TOKEN_PATH = "~/.anthem/script.token"


class Anthem(ControlSurface):
    def __init__(self, c_instance):
        ControlSurface.__init__(self, c_instance)
        self._bindings = {}
        self._captures = {}       # render_id -> render.RenderCapture in progress (owner side)
        self._device_tracks = []  # tracks whose device lists we watch (plug-ins added/removed)
        self._reinventory = False
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
        for cap in list(self._captures.values()):
            try:
                cap._cleanup()
            except Exception:  # noqa
                pass
        self._captures = {}
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
            for rid, cap in list(self._captures.items()):
                cap.tick(now)
                if cap.done:
                    del self._captures[rid]
            if self._reinventory and not self._captures:
                self._reinventory = False
                self._send_inventory()
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
        elif t == "render_capture":
            self._render_capture(m)
        elif t == "apply_render":
            self._apply_render(m)
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
        self._watch_devices()
        self._send_inventory()
        for b in self._bindings.values():
            if b.found:
                b._on_bound()
        self.show_message("Anthem: bound %d/%d clips" % (
            sum(1 for b in self._bindings.values() if b.found), len(self._bindings)))

    def _send_inventory(self):
        inv = []
        for b in self._bindings.values():
            e = b.inventory()
            e["devices"] = rendermod.track_devices(b.track) if b.track is not None else []
            inv.append(e)
        self._send({"type": "inventory", "clips": inv,
                    "tracks": [t.name for t in self._song.tracks],
                    "plugins": self._installed_plugins()})

    def _watch_devices(self):
        """Re-send the inventory when a plug-in is added to / removed from a bound track."""
        for t in self._device_tracks:
            try:
                if t.devices_has_listener(self._on_devices):
                    t.remove_devices_listener(self._on_devices)
            except Exception:  # noqa
                pass
        self._device_tracks = []
        for b in self._bindings.values():
            if b.track is None:
                continue
            try:
                b.track.add_devices_listener(self._on_devices)
                self._device_tracks.append(b.track)
            except Exception:  # noqa
                pass

    def _on_devices(self):
        self._reinventory = True      # coalesced; sent from the next tick

    def _installed_plugins(self):
        try:
            return rendermod.installed_plugins(Live.Application.get_application())
        except Exception:  # noqa
            return []

    # --- renders -------------------------------------------------------------------
    def _render_capture(self, m):
        clip_id, rid = str(m.get("clip")), str(m.get("render_id"))
        b = self._bindings.get(clip_id)

        def fail(why):
            self._send({"type": "render_captured", "clip": clip_id, "render_id": rid, "ok": False,
                        "error": why, "ms": 0})
        if b is None:
            return fail("clip not bound")
        if any(not c.done for c in self._captures.values()):
            return fail("another render is in progress")
        cap = rendermod.RenderCapture(self._song, clip_id, rid, b, self._send, self.log)
        cap.start()
        if not cap.done:
            self._captures[rid] = cap
            self.show_message("Anthem: rendering %s (one loop plays while it records)" % clip_id)

    def _apply_render(self, m):
        clip_id, rid = str(m.get("clip")), str(m.get("render_id"))
        t0 = time.time()
        b = self._bindings.get(clip_id)
        track_name = b.track_name if b is not None else str(m.get("track_name") or clip_id)
        slot = b.slot if b is not None else int(m.get("slot") or 0)
        path = str(m.get("path") or "")
        if not path or not os.path.isfile(path):
            ok, err = False, "render file missing"
        else:
            ok, err = rendermod.apply_render(self._song, track_name, slot, path, rid, self.log)
        out = {"type": "render_applied", "clip": clip_id, "render_id": rid, "ok": ok,
               "ms": int((time.time() - t0) * 1000)}
        if not ok:
            out["error"] = err
        else:
            self.show_message("Anthem: render of %s placed in '%s%s'" % (clip_id, track_name, rendermod.RENDER_SUFFIX))
        self._send(out)

    def _reload_code(self):
        """Developer convenience (loopback only): reload the pure modules and re-bind, so a
        code change does not require restarting Live. The surface class itself is not reloaded."""
        import importlib
        from . import clipsync, bridge, clipbinding, render
        spec = [{"clip": b.clip_id, "track_name": b.track_name, "slot": b.slot, "owned": b.owned}
                for b in self._bindings.values()]
        try:
            for mod in (clipsync, bridge, clipbinding, render):
                importlib.reload(mod)
            global ClipBinding, rendermod
            ClipBinding = clipbinding.ClipBinding
            rendermod = render
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
        self._watch_devices()


def create_instance(c_instance):
    return Anthem(c_instance)
