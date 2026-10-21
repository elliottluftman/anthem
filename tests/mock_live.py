"""MOCKED Live objects for unit tests. These are NOT Ableton; they imitate the subset of the
Live Python API that remote_script/Anthem uses, with deferred listener notification
(call ``song.flush()`` to deliver pending notifications, like Live does asynchronously).
"""


class MidiNoteSpecification(object):
    def __init__(self, pitch, start_time, duration, velocity=100.0, mute=False, probability=1.0,
                 velocity_deviation=0.0, release_velocity=64.0):
        self.pitch, self.start_time, self.duration = pitch, start_time, duration
        self.velocity, self.mute, self.probability = velocity, mute, probability
        self.velocity_deviation, self.release_velocity = velocity_deviation, release_velocity


class MockNote(object):
    FIELDS = ("pitch", "start_time", "duration", "velocity", "mute", "probability",
              "velocity_deviation", "release_velocity")

    def __init__(self, note_id, spec):
        self.note_id = note_id
        for f in self.FIELDS:
            setattr(self, f, getattr(spec, f))

    def copy(self):
        return MockNote(self.note_id, self)


class _Listenable(object):
    def __init__(self, song):
        self._song = song
        self._ls = {}

    def _add(self, prop, fn):
        self._ls.setdefault(prop, []).append(fn)

    def _remove(self, prop, fn):
        self._ls.get(prop, []).remove(fn)

    def _has(self, prop, fn):
        return fn in self._ls.get(prop, [])

    def _notify(self, prop):
        for fn in list(self._ls.get(prop, [])):
            self._song.pending.append(fn)

    def __getattr__(self, name):
        for prefix, impl in (("add_", self._add), ("remove_", self._remove)):
            if name.startswith(prefix) and name.endswith("_listener"):
                prop = name[len(prefix):-len("_listener")]
                return lambda fn: impl(prop, fn)
        if name.endswith("_has_listener"):
            prop = name[:-len("_has_listener")]
            return lambda fn: self._has(prop, fn)
        raise AttributeError(name)


class MockClip(_Listenable):
    is_midi_clip = True

    def __init__(self, song, length=4.0, pitch_locked=False):
        _Listenable.__init__(self, song)
        self.length = length
        self.name = "clip"
        self._notes = {}
        self._next = 1
        self.writes = 0
        self.pitch_locked = pitch_locked  # emulate an API that ignores pitch in apply_note_modifications

    def get_all_notes_extended(self):
        return [n.copy() for n in self._notes.values()]

    def get_notes_by_id(self, ids):
        return [self._notes[i].copy() for i in ids if i in self._notes]

    def add_new_notes(self, specs):
        ids = []
        for s in specs:
            nid = self._next
            self._next += 1
            self._notes[nid] = MockNote(nid, s)
            ids.append(nid)
        self.writes += 1
        self._notify("notes")
        return ids

    def apply_note_modifications(self, vec):
        for n in vec:
            cur = self._notes.get(n.note_id)
            if cur is None:
                continue
            for f in MockNote.FIELDS:
                if f == "pitch" and self.pitch_locked:
                    continue
                setattr(cur, f, getattr(n, f))
        self.writes += 1
        self._notify("notes")

    def remove_notes_by_id(self, ids):
        for i in ids:
            self._notes.pop(i, None)
        self.writes += 1
        self._notify("notes")

    # --- "manual" edits made by the user in the piano roll (test helpers) ---
    def user_draw(self, pitch, start, dur, vel=100.0):
        return self.add_new_notes([MidiNoteSpecification(pitch, start, dur, vel)])[0]

    def user_modify(self, nid, **fields):
        for k, v in fields.items():
            setattr(self._notes[nid], k, v)
        self._notify("notes")

    def user_delete(self, nid):
        del self._notes[nid]
        self._notify("notes")

    def content(self):
        return sorted((n.pitch, n.start_time, n.duration, n.velocity, n.mute) for n in self._notes.values())


class MockSlot(_Listenable):
    def __init__(self, song, track):
        _Listenable.__init__(self, song)
        self.track = track
        self.clip = None

    @property
    def has_clip(self):
        return self.clip is not None

    def create_clip(self, length):
        assert self.clip is None and self.track.has_midi_input
        self.clip = MockClip(self._song, length)
        self._notify("has_clip")
        return self.clip

    def delete_clip(self):
        assert self.clip is not None
        self.clip = None
        self._notify("has_clip")


class MockTrack(_Listenable):
    def __init__(self, song, name, midi=True, slots=8):
        _Listenable.__init__(self, song)
        self._name = name
        self.has_midi_input = midi
        self.clip_slots = [MockSlot(song, self) for _ in range(slots)]

    @property
    def name(self):
        return self._name

    @name.setter
    def name(self, v):
        self._name = v
        self._notify("name")


class MockSong(_Listenable):
    def __init__(self, track_names=("Anthem Drums", "Anthem Bass"), file_path="/tmp/Test.als"):
        self.pending = []
        _Listenable.__init__(self, self)
        self.tracks = [MockTrack(self, n) for n in track_names]
        self.file_path = file_path
        self.name = "Test"
        self.undo_steps = 0

    def begin_undo_step(self):
        self.undo_steps += 1

    def end_undo_step(self):
        pass

    def create_midi_track(self, index=-1):
        t = MockTrack(self, "MIDI")
        self.tracks.append(t)
        self._notify("tracks")
        return t

    def delete_track(self, index):
        del self.tracks[index]
        self._notify("tracks")

    def flush(self):
        """Deliver pending listener notifications (Live does this asynchronously)."""
        n = 0
        while self.pending:
            fns, self.pending = self.pending, []
            for fn in fns:
                fn()
                n += 1
        return n
