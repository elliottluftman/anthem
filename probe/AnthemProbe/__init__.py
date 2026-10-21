"""AnthemProbe - throwaway Remote Script that introspects the real Live Python API.

It writes a report to ~/AnthemProbe_report.txt and runs capability experiments on the
first MIDI track's first clip slot. Disposable; not part of the product.
"""
import os
import sys
import time
import traceback

import Live
from _Framework.ControlSurface import ControlSurface

REPORT = os.path.expanduser("~/AnthemProbe_report.txt")


def _w(f, *parts):
    f.write(" ".join(str(p) for p in parts) + "\n")
    f.flush()


def dump_class(f, cls, name):
    _w(f, "\n==== %s ====" % name)
    for attr in sorted(dir(cls)):
        if attr.startswith("__"):
            continue
        try:
            obj = getattr(cls, attr)
            doc = (getattr(obj, "__doc__", "") or "").strip().replace("\n", "\n      ")
        except Exception as e:  # noqa
            doc = "<err %s>" % e
        _w(f, "  %s :: %s" % (attr, doc[:600]))


class AnthemProbe(ControlSurface):
    def __init__(self, c_instance):
        ControlSurface.__init__(self, c_instance)
        self._f = open(REPORT, "w")
        self._ticks = 0
        self._notes_events = []
        self._clip = None
        self._timer = None
        try:
            self.run()
        except Exception:
            _w(self._f, traceback.format_exc())
        self.show_message("AnthemProbe loaded; report at %s" % REPORT)

    def log(self, *p):
        _w(self._f, "[%.3f]" % time.time(), *p)
        self.log_message("AnthemProbe: " + " ".join(str(x) for x in p))

    def run(self):
        f = self._f
        app = Live.Application.get_application()
        _w(f, "Live version: %d.%d.%d" % (app.get_major_version(), app.get_minor_version(), app.get_bugfix_version()))
        _w(f, "Python:", sys.version)
        _w(f, "Thread support:", "threading" in sys.modules or "importable")
        dump_class(f, Live.Clip.Clip, "Live.Clip.Clip")
        dump_class(f, Live.Clip.MidiNote, "Live.Clip.MidiNote")
        dump_class(f, Live.Clip.MidiNoteSpecification, "Live.Clip.MidiNoteSpecification")
        dump_class(f, Live.ClipSlot.ClipSlot, "Live.ClipSlot.ClipSlot")
        dump_class(f, Live.Track.Track, "Live.Track.Track")
        dump_class(f, Live.Song.Song, "Live.Song.Song")
        dump_class(f, Live.Song.Song.View, "Live.Song.Song.View")
        dump_class(f, Live.Base, "Live.Base")
        if hasattr(Live.Base, "Timer"):
            dump_class(f, Live.Base.Timer, "Live.Base.Timer")
        dump_class(f, Live.Application.Application, "Live.Application.Application")
        dump_class(f, self, "ControlSurface(self)")

        song = self.song()
        _w(f, "\nSong file_path:", getattr(song, "file_path", "<none>"), "name:", getattr(song, "name", "<none>"))
        _w(f, "Tracks:")
        for i, t in enumerate(song.tracks):
            _w(f, "  ", i, t.name, "midi=%s" % t.has_midi_input, "slots=%d" % len(t.clip_slots),
               "arr_clips=%d" % len(t.arrangement_clips))

        # Timer experiment: Live.Base.Timer vs schedule_message
        try:
            self._timer = Live.Base.Timer(callback=self._tick, interval=50, repeat=True)
            self._timer.start()
            _w(f, "Live.Base.Timer started (50ms)")
        except Exception:
            _w(f, "Live.Base.Timer failed:", traceback.format_exc())
            self.schedule_message(1, self._tick)

        # Note listener experiment on first MIDI track's first slot
        for t in song.tracks:
            if t.has_midi_input:
                slot = t.clip_slots[0]
                if not slot.has_clip:
                    slot.create_clip(4.0)
                    _w(f, "created clip in", t.name)
                self._clip = slot.clip
                self._clip.add_notes_listener(self._on_notes)
                self._clip.add_name_listener(self._on_name)
                slot.add_has_clip_listener(self._on_has_clip)
                self._dump_notes("initial")
                # Apply experiment: add a note, modify it, remove it
                ids = self._clip.add_new_notes((Live.Clip.MidiNoteSpecification(pitch=60, start_time=0.0, duration=0.5, velocity=90),))
                _w(f, "add_new_notes returned:", type(ids), list(ids))
                self._dump_notes("after add")
                notes = self._clip.get_notes_by_id(ids)
                for n in notes:
                    n.start_time = 1.0
                    n.velocity = 50
                self._clip.apply_note_modifications(notes)
                self._dump_notes("after modify")
                self._clip.remove_notes_by_id(ids)
                self._dump_notes("after remove")
                break
        _w(f, "setup done; draw notes in the clip now")

    def _dump_notes(self, label):
        try:
            notes = self._clip.get_all_notes_extended()
            self.log(label, "n=%d" % len(notes), [(n.note_id, n.pitch, n.start_time, n.duration, n.velocity, n.mute) for n in notes])
        except Exception:
            self.log(label, traceback.format_exc())

    def _on_notes(self):
        self.log("NOTES LISTENER fired")
        self._dump_notes("listener")

    def _on_name(self):
        self.log("name listener:", self._clip.name)

    def _on_has_clip(self):
        self.log("has_clip listener fired")

    def _tick(self):
        self._ticks += 1
        if self._ticks % 100 == 0:
            self.log("tick", self._ticks, "song playing:", self.song().is_playing)

    def disconnect(self):
        try:
            if self._timer:
                self._timer.stop()
        except Exception:
            pass
        self.log("disconnect")
        self._f.close()
        ControlSurface.disconnect(self)


def create_instance(c_instance):
    return AnthemProbe(c_instance)
