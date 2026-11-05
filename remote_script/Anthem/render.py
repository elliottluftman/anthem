"""Renders: audio of a collaboration clip's track, for a participant who lacks a plug-in.

Owner side (``RenderCapture``): record exactly one loop of the clip's track output into a
temporary audio track ("Anthem Render Bus") with Live's own session recording, through the
API only: input routing = the track (Post FX), arm, session_record, ``fire(record_length)``.
It runs across timer ticks (routing lists are populated one tick after a track is created)
and never blocks. The recording is real time: the transport runs for one loop.

Receiver side (``apply_render``): put the shipped file into the Session slot of an audio track
named "<track> (render)" with ``ClipSlot.create_audio_clip(path)``.

Only the small Live API subset used below is touched, so the module runs unchanged against
tests/mock_live.py (mocked, not Live).
"""
import time
import traceback

BUS_NAME = "Anthem Render Bus"
RENDER_SUFFIX = " (render)"
MAX_BEATS = 256.0
ROUTING_WAIT = 3.0        # seconds to wait for the bus track's routing list to appear
SETTLE_SECONDS = 1.0      # after recording stops: let Live finish writing the file
EXTRA_TIMEOUT = 20.0      # on top of the loop's duration


def _plugin_name(d):
    try:
        if d.class_name in ("PluginDevice", "AuPluginDevice"):
            return str(d.class_display_name or d.name)
    except Exception:  # noqa
        pass
    return None


def track_devices(track):
    """[{name, cls, plugin}] for a Live track (best effort; mocks may have no devices)."""
    out = []
    try:
        for d in track.devices:
            out.append({"name": str(d.class_display_name), "cls": str(d.class_name),
                        "plugin": _plugin_name(d) is not None})
    except Exception:  # noqa
        pass
    return out


def installed_plugins(app, limit=512):
    """Names of the plug-ins Live's browser lists (AU/VST/VST3), as the user sees them."""
    names = set()

    def walk(item, depth):
        if len(names) >= limit or depth > 5:
            return
        try:
            kids = list(item.iter_children)
        except Exception:  # noqa
            return
        for k in kids:
            try:
                if k.is_loadable:
                    names.add(str(k.name))
                else:
                    walk(k, depth + 1)
            except Exception:  # noqa
                continue
    try:
        walk(app.browser.plugins, 0)
    except Exception:  # noqa
        pass
    return sorted(names)


class RenderCapture(object):
    """State machine: start() -> tick() until done. Reports via send(render_captured)."""

    def __init__(self, song, clip_id, render_id, binding, send, log):
        self.song = song
        self.clip_id = clip_id
        self.render_id = render_id
        self.binding = binding
        self._send = send
        self._log = log
        self.state = "new"
        self.bus = None
        self.beats = 0.0
        self.t0 = time.time()
        self.t_state = self.t0
        self.deadline = None
        self.was_playing = False
        self.arms = []           # (track, previous arm) for tracks we un-armed
        self.done = False
        self._undo_open = False

    # --- lifecycle ------------------------------------------------------------
    def start(self):
        try:
            b = self.binding
            if not (b.found and b.clip is not None and b.track is not None):
                return self._fail("clip not found in Live")
            self.beats = float(b.clip.length)
            if not 0 < self.beats <= MAX_BEATS:
                return self._fail("clip length %.1f beats out of range" % self.beats)
            self.was_playing = bool(self.song.is_playing)
            self.song.begin_undo_step()
            self._undo_open = True
            self.song.create_audio_track(-1)
            self.bus = self.song.tracks[len(self.song.tracks) - 1]
            self.bus.name = BUS_NAME
            self.state = "route"
            self.t_state = time.time()
        except Exception:  # noqa
            self._fail("start failed: %s" % traceback.format_exc().splitlines()[-1])

    def tick(self, now):
        if self.done:
            return
        try:
            if self.state == "route":
                self._route(now)
            elif self.state == "record":
                self._poll(now)
            elif self.state == "settle":
                if now - self.t_state >= SETTLE_SECONDS:
                    self._finish()
        except Exception:  # noqa
            self._fail("capture failed: %s" % traceback.format_exc().splitlines()[-1])

    # --- steps ----------------------------------------------------------------
    def _route(self, now):
        src = None
        for r in self.bus.available_input_routing_types:
            if r.display_name == self.binding.track.name:
                src = r
                break
        if src is None:
            if now - self.t_state < ROUTING_WAIT:
                return                      # routing list not populated yet
            return self._fail("track %r not offered as an input" % self.binding.track.name)
        self.bus.input_routing_type = src
        try:
            for ch in self.bus.available_input_routing_channels:
                if ch.display_name == "Post FX":
                    self.bus.input_routing_channel = ch
                    break
        except Exception:  # noqa
            pass
        for t in self.song.tracks:
            try:
                if t is not self.bus and t.can_be_armed and t.arm:
                    self.arms.append((t, True))
                    t.arm = False           # session record must only record into the bus
            except Exception:  # noqa
                continue
        self.bus.arm = True
        self.song.session_record = True
        # Both launch at the same quantization point: the source clip plays, the bus records it.
        self.binding.slot_obj.fire()
        self.bus.clip_slots[0].fire(record_length=self.beats)
        tempo = float(getattr(self.song, "tempo", 120.0) or 120.0)
        self.deadline = now + self.beats * 60.0 / tempo + EXTRA_TIMEOUT
        self.state = "record"
        self.t_state = now

    def _poll(self, now):
        slot = self.bus.clip_slots[0]
        if slot.has_clip and not slot.is_recording:
            self.song.session_record = False
            self.state = "settle"
            self.t_state = now
            return
        if now > self.deadline:
            self._fail("recording did not finish in time")

    def _finish(self):
        slot = self.bus.clip_slots[0]
        clip = slot.clip
        path = str(clip.file_path or "")
        beats = float(clip.length or self.beats)
        if not path:
            return self._fail("Live reported no file for the recording")
        self._cleanup()
        self.done = True
        self._send({"type": "render_captured", "clip": self.clip_id, "render_id": self.render_id,
                    "ok": True, "path": path, "beats": beats, "ms": int((time.time() - self.t0) * 1000)})

    def _fail(self, why):
        self._log("render %s: %s" % (self.render_id, why))
        try:
            self._cleanup()
        except Exception:  # noqa
            pass
        self.done = True
        self._send({"type": "render_captured", "clip": self.clip_id, "render_id": self.render_id,
                    "ok": False, "error": why, "ms": int((time.time() - self.t0) * 1000)})

    def _cleanup(self):
        try:
            self.song.session_record = False
        except Exception:  # noqa
            pass
        if self.bus is not None:
            try:
                self.bus.arm = False
            except Exception:  # noqa
                pass
            try:
                self.binding.slot_obj.stop()
            except Exception:  # noqa
                pass
            for i, t in enumerate(self.song.tracks):
                if t is self.bus or (t == self.bus):
                    self.song.delete_track(i)
                    break
            self.bus = None
        for t, prev in self.arms:
            try:
                t.arm = prev
            except Exception:  # noqa
                pass
        self.arms = []
        try:
            if not self.was_playing:
                self.song.stop_playing()
        except Exception:  # noqa
            pass
        if self._undo_open:
            self._undo_open = False
            try:
                self.song.end_undo_step()
            except Exception:  # noqa
                pass


def find_render_track(song, track_name):
    name = track_name + RENDER_SUFFIX
    for t in song.tracks:
        try:
            if t.name == name and t.has_audio_input:
                return t
        except Exception:  # noqa
            continue
    return None


def apply_render(song, track_name, slot_index, path, render_id, log):
    """Place ``path`` in slot ``slot_index`` of the "<track_name> (render)" audio track
    (created at the end of the set if missing). Returns (ok, error)."""
    try:
        song.begin_undo_step()
        t = find_render_track(song, track_name)
        if t is None:
            song.create_audio_track(-1)
            t = song.tracks[len(song.tracks) - 1]
            t.name = track_name + RENDER_SUFFIX
        if slot_index >= len(t.clip_slots):
            return False, "slot %d does not exist" % slot_index
        slot = t.clip_slots[slot_index]
        if slot.has_clip:
            slot.delete_clip()
        slot.create_audio_clip(path)
        clip = slot.clip
        try:
            clip.name = "render %s" % render_id[:8]
            clip.looping = True
        except Exception:  # noqa
            pass
        return True, None
    except Exception:  # noqa
        log("apply_render failed: %s" % traceback.format_exc())
        return False, traceback.format_exc().splitlines()[-1][:200]
    finally:
        try:
            song.end_undo_step()
        except Exception:  # noqa
            pass
