"""One collaboration clip <-> one native Live Session clip.

Owner side : observe the clip's notes listener, diff against the last reported state,
             emit ``local_delta`` (debounced so a drag becomes one message).
Receiver   : apply ``apply_delta`` / ``apply_snapshot`` with Live's id-based note API,
             keep an owner-id -> local-id map, and detect local edits (conflicts) by
             comparing the clip's content with what we expect after our own writes.

Live objects are only touched through the small set of methods used below, so the
whole class runs unchanged against tests/mock_live.py (mocked, not real Live).
"""
import json
import time
import traceback

from . import clipsync

DEBOUNCE_SECONDS = 0.1
MAX_NOTES = 2000   # mirrors anthem_core.protocol.MAX_NOTES_PER_OP; larger clips cannot be sent
MAX_LINE_BYTES = 256 * 1024   # mirrors bridge.MAX_LINE_BYTES (kept here so this module stays pure)


def _liveobj_valid(obj):
    """Live proxies raise or compare unequal to themselves once the object is gone."""
    try:
        return obj is not None and obj != None  # noqa: E711  (Live overrides __ne__)
    except Exception:  # noqa
        return False


class ClipBinding(object):
    def __init__(self, clip_id, track_name, slot, owned, send, note_spec, log):
        self.clip_id = clip_id
        self.track_name = track_name
        self.slot = int(slot)
        self.owned = bool(owned)
        self._send = send          # callable(msg dict)
        self._note_spec = note_spec  # callable(**fields) -> Live.Clip.MidiNoteSpecification
        self._log = log
        self.song = None
        self.track = None
        self.slot_obj = None
        self.clip = None
        self.found = False
        self.known = {}            # id -> note (owner: last reported; receiver: expected, by local id)
        self.replica = {}          # receiver: owner's notes by owner id, as told by the companion
        self._replica_valid = False  # True once a snapshot has defined the full expected content
        self.idmap = clipsync.IdMap()
        self.conflict = False
        self.too_large = False
        self._baseline_sent = False   # owner: a full snapshot has gone out since (re)bind
        self._dirty_at = None
        self._applying = False
        self._closed = False
        self._listeners = []       # (obj, remove_fn_name, fn)

    # --- resolution ------------------------------------------------------------
    def resolve(self, song, announce=True):
        """(Re)locate the track/slot/clip. Emits clip_found / clip_lost on transitions."""
        if self._closed:
            return False
        self.song = song
        track = None
        for t in song.tracks:
            try:
                if t.name == self.track_name and t.has_midi_input:
                    track = t
                    break
            except Exception:  # noqa
                continue
        clip = None
        slot_obj = None
        if track is not None and self.slot < len(track.clip_slots):
            slot_obj = track.clip_slots[self.slot]
            if slot_obj.has_clip and slot_obj.clip is not None and slot_obj.clip.is_midi_clip:
                clip = slot_obj.clip
        # Always re-attach: the set of tracks (and hence name listeners) may have changed.
        self._detach()
        self.track, self.slot_obj, self.clip = track, slot_obj, clip
        self._attach()
        was_found = self.found
        self.found = clip is not None
        if announce and self.found and not was_found:
            self._send({"type": "clip_found", "clip": self.clip_id})
            self._on_bound()
        elif announce and was_found and not self.found:
            reason = "track missing" if track is None else ("slot missing" if slot_obj is None else "clip missing")
            self.idmap.clear()
            self.known = {}
            self._send({"type": "clip_lost", "clip": self.clip_id, "reason": reason})
        return self.found

    @staticmethod
    def _same(a, b):
        if a is None or b is None:
            return a is b
        try:
            return a == b
        except Exception:  # noqa
            return False

    def _attach(self):
        # Listen to every track's name: a rename away from, or back to, our name must re-resolve.
        for t in self.song.tracks:
            try:
                self._listen(t, "name", self._on_track_name)
            except Exception:  # noqa
                pass
        if self.slot_obj is not None:
            self._listen(self.slot_obj, "has_clip", self._on_has_clip)
        if self.clip is not None:
            self._listen(self.clip, "notes", self._on_notes)

    def _listen(self, obj, prop, fn):
        getattr(obj, "add_%s_listener" % prop)(fn)
        self._listeners.append((obj, prop, fn))

    def _detach(self):
        for obj, prop, fn in self._listeners:
            try:
                if getattr(obj, "%s_has_listener" % prop)(fn):
                    getattr(obj, "remove_%s_listener" % prop)(fn)
            except Exception:  # noqa: object may already be gone
                pass
        self._listeners = []

    def close(self):
        self._closed = True
        self._detach()
        self.clip = self.slot_obj = self.track = self.song = None
        self.found = False

    def _on_bound(self):
        self.idmap.clear()
        self.replica = {}
        self._replica_valid = False
        self.conflict = False
        self._baseline_sent = False
        self._dirty_at = None
        if self.owned:
            self.snapshot_now()
        else:
            # Whatever is there is "expected" until a snapshot arrives; the companion compares
            # this baseline with the session and raises a conflict instead of overwriting edits.
            self.known = self._read()
            self.snapshot_now()

    def set_owned(self, owned):
        """Ownership changed at runtime (claim/release/reconnect)."""
        owned = bool(owned)
        if owned == self.owned:
            return
        self.owned = owned
        self.known = {}
        self._baseline_sent = False
        if self.found:
            self._on_bound()

    def _on_track_name(self):
        if self._closed:
            return
        try:
            self.resolve(self.song)
        except Exception:  # noqa: never raise into Live's listener dispatch
            self._log("resolve failed: %s" % traceback.format_exc())

    def _on_has_clip(self):
        self._on_track_name()

    def inventory(self):
        return {"clip": self.clip_id, "found": self.found, "track_name": self.track_name,
                "slot": self.slot, "owned": self.owned,
                "note_count": len(self._read()) if self.found else 0}

    # --- reading ---------------------------------------------------------------
    def _read(self):
        if self.clip is None:
            return {}
        try:
            return {int(n.note_id): clipsync.note_from_live(n) for n in self.clip.get_all_notes_extended()}
        except Exception:  # noqa
            self._log("read failed for %s: %s" % (self.clip_id, traceback.format_exc()))
            return {}

    # --- owner side ------------------------------------------------------------
    def _on_notes(self):
        if self._applying or self._closed:
            return
        if self.owned:
            if self._dirty_at is None:
                self._dirty_at = time.time()
        else:
            self._check_consistency()

    def tick(self, now):
        if self.owned and self._dirty_at is not None and now - self._dirty_at >= DEBOUNCE_SECONDS:
            detected = self._dirty_at
            self._dirty_at = None
            self._emit_delta(detected)

    def _mark_too_large(self, detail):
        """Owner clips that cannot travel in one message: report loudly (once), never silently."""
        self._baseline_sent = False
        if not self.too_large:
            self.too_large = True
            self._send({"type": "clip_lost", "clip": self.clip_id,
                        "reason": "too large (%s); sync paused" % detail})

    def _mark_fits(self):
        """Called only when a full snapshot is known to fit: announce recovery first."""
        if self.too_large:
            self.too_large = False
            self._send({"type": "clip_found", "clip": self.clip_id})

    @staticmethod
    def _fits(msg):
        return len(json.dumps(msg, separators=(",", ":"))) + 64 <= MAX_LINE_BYTES

    def _emit_delta(self, detected_at):
        if not self.found:
            return
        current = self._read()
        if not self._baseline_sent or self.too_large:
            self.snapshot_now()          # (re)establish a baseline the companion actually has
            return
        d = clipsync.diff(self.known, current)
        if d is None:
            return
        added, modified, removed = d
        msg = {"type": "local_delta", "clip": self.clip_id, "added": added,
               "modified": modified, "removed": removed, "t_detect": detected_at}
        if len(current) > MAX_NOTES or not self._fits(msg):
            self.snapshot_now()          # decides between "fits as a snapshot" and "too large"
            return
        self._send(msg)
        self.known = current

    def snapshot_now(self):
        if not self.found:
            return
        self._dirty_at = None
        current = self._read()
        notes = sorted(current.values(), key=lambda n: (n["start"], n["pitch"], n["id"]))
        msg = {"type": "local_snapshot", "clip": self.clip_id, "owned": self.owned, "notes": notes}
        if self.owned:
            if len(notes) > MAX_NOTES:
                self._mark_too_large("%d notes > %d" % (len(notes), MAX_NOTES))
                return
            if not self._fits(msg):
                self._mark_too_large("%d notes exceed the message size limit" % len(notes))
                return
            self._mark_fits()
        else:
            # Receiver baseline: never mark the clip lost. Shrink until it fits; the companion
            # treats a truncated baseline as "differs" and raises a conflict, never overwrites.
            self._mark_fits()
            while notes and not self._fits(msg):
                notes = notes[: len(notes) // 2]
                msg = {"type": "local_snapshot", "clip": self.clip_id, "owned": False,
                       "notes": notes, "truncated": True}
        self._send(msg)
        self.known = current
        self._baseline_sent = True

    # --- receiver side ---------------------------------------------------------
    def _check_consistency(self):
        current = self._read()
        if clipsync.same_content(current, self.known):
            return
        if not self.conflict:
            self.conflict = True
            self._log("conflict on %s: local content differs from owner's" % self.clip_id)
            self._send({"type": "conflict", "clip": self.clip_id})

    @staticmethod
    def _norm(n):
        """Fill optional wire fields so comparisons never KeyError on a minimal note."""
        return {"id": int(n["id"]), "pitch": int(n["pitch"]), "start": float(n["start"]),
                "dur": float(n["dur"]), "vel": float(n.get("vel", 100.0)), "mute": bool(n.get("mute", False)),
                "prob": float(n.get("prob", 1.0)), "vel_dev": float(n.get("vel_dev", 0.0)),
                "rel_vel": float(n.get("rel_vel", 64.0))}

    def apply_delta(self, op_id, added, modified, removed):
        added = [self._norm(n) for n in added]
        modified = [self._norm(n) for n in modified]
        removed = [int(i) for i in removed]
        return self._apply(op_id, lambda: self._do_delta(added, modified, removed))

    def apply_snapshot(self, op_id, notes, force=False):
        """``force`` (revert/resync) overwrites a local edit on purpose; otherwise a local edit
        that is present refuses the write and raises a conflict."""
        notes = [self._norm(n) for n in notes]
        return self._apply(op_id, lambda: self._do_snapshot(notes), force=force)

    def _apply(self, op_id, fn, force=False):
        t0 = time.time()
        if not self.found:
            self._send({"type": "applied", "clip": self.clip_id, "op_id": op_id, "ok": False,
                        "error": "clip not found", "ms": 0})
            return False
        if not self.owned and not force:
            # A local edit may be sitting in the clip with its listener still pending; writing
            # over it would discard the user's work. Refuse and let the companion raise the conflict.
            if not clipsync.same_content(self._read(), self.known):
                self.conflict = True
                # one signal only: the companion treats applied/error=conflict like a conflict message
                self._send({"type": "applied", "clip": self.clip_id, "op_id": op_id, "ok": False,
                            "error": "conflict", "ms": 0})
                return False
        ok, err = True, None
        self._applying = True
        undo = self.song is not None and hasattr(self.song, "begin_undo_step")
        try:
            if undo:
                self.song.begin_undo_step()
            fn()
        except Exception:  # noqa
            ok, err = False, traceback.format_exc().strip().splitlines()[-1]
            self._log("apply failed on %s: %s" % (self.clip_id, traceback.format_exc()))
        finally:
            try:
                if undo:
                    self.song.end_undo_step()
            except Exception:  # noqa
                pass
            if not self.owned:
                self.known = self._read()   # what we expect to see; anything else is a local edit
                if ok and self._replica_valid and not clipsync.same_content(self.known, self.replica):
                    # Live changed the result (e.g. merged overlapping same-pitch notes). Say so;
                    # the companion answers with an authoritative snapshot.
                    ok, err = False, "content differs after apply (%d local vs %d expected notes)" % (
                        len(self.known), len(self.replica))
                if ok:
                    self.conflict = False   # the clip now matches the owner's content
            # On an owned clip (e.g. applied during a claim handover) the content change must
            # reach the other participant: leave ``known`` alone so the notes listener diffs it.
            self._applying = False
        self._send({"type": "applied", "clip": self.clip_id, "op_id": op_id, "ok": ok,
                    "error": err, "ms": round((time.time() - t0) * 1000.0, 2)})
        return ok

    def _spec(self, n):
        return self._note_spec(pitch=int(n["pitch"]), start_time=float(n["start"]),
                               duration=float(n["dur"]), velocity=float(n["vel"]),
                               mute=bool(n["mute"]), probability=float(n.get("prob", 1.0)),
                               velocity_deviation=float(n.get("vel_dev", 0.0)),
                               release_velocity=float(n.get("rel_vel", 64.0)))

    def _add(self, remote_notes):
        if not remote_notes:
            return
        ids = list(self.clip.add_new_notes(tuple(self._spec(n) for n in remote_notes)))
        created = {int(n.note_id): clipsync.note_from_live(n) for n in self.clip.get_notes_by_id(tuple(ids))}
        # Match created notes to remote notes by content (robust even if Live reorders ids).
        unused = dict(created)
        for rn in remote_notes:
            lid = None
            for cid, cn in unused.items():
                if clipsync.notes_equal(cn, rn):
                    lid = cid
                    break
            if lid is None and unused:
                lid = next(iter(unused))
            if lid is not None:
                del unused[lid]
                self.idmap.bind(rn["id"], lid)

    def _modify(self, remote_notes):
        todo, fallback = [], []
        for rn in remote_notes:
            lid = self.idmap.r2l.get(rn["id"])
            if lid is None:
                fallback.append(rn)
            else:
                todo.append((lid, rn))
        if todo:
            by_lid = dict(todo)
            vec = self.clip.get_notes_by_id(tuple(by_lid))
            seen = set()
            for ln in vec:
                rn = by_lid.get(int(ln.note_id))
                if rn is None:
                    continue
                seen.add(int(ln.note_id))
                ln.pitch = int(rn["pitch"])
                ln.start_time = float(rn["start"])
                ln.duration = float(rn["dur"])
                ln.velocity = float(rn["vel"])
                ln.mute = bool(rn["mute"])
                ln.probability = float(rn.get("prob", 1.0))
                ln.velocity_deviation = float(rn.get("vel_dev", 0.0))
                ln.release_velocity = float(rn.get("rel_vel", 64.0))
            self.clip.apply_note_modifications(vec)
            # Verify; anything Live did not take (or a vanished local note) is re-created.
            after = {int(n.note_id): clipsync.note_from_live(n) for n in self.clip.get_notes_by_id(tuple(by_lid))}
            for lid, rn in todo:
                got = after.get(lid)
                if got is None or not clipsync.notes_equal(got, rn):
                    if got is not None:
                        self.clip.remove_notes_by_id((lid,))
                    self.idmap.unbind_remote(rn["id"])
                    fallback.append(rn)
        if fallback:
            self._log("modify fallback (remove+add) for %d note(s) on %s" % (len(fallback), self.clip_id))
            self._add(fallback)

    def _remove(self, remote_ids):
        lids = [self.idmap.unbind_remote(rid) for rid in remote_ids]
        lids = [l for l in lids if l is not None]
        if lids:
            self.clip.remove_notes_by_id(tuple(lids))

    def _do_delta(self, added, modified, removed):
        for rid in removed:
            self.replica.pop(rid, None)
        for n in modified + added:
            self.replica[n["id"]] = n
        self._remove(removed)
        self._modify(modified)
        self._add(added)

    def _do_snapshot(self, notes):
        """Make the clip equal to ``notes`` with the fewest writes; rebuild the id map."""
        self.replica = {n["id"]: n for n in notes}
        self._replica_valid = True
        current = self._read()
        self.idmap.clear()
        unmatched_local = dict(current)
        to_add = []
        for rn in notes:
            lid = None
            for cid, cn in unmatched_local.items():
                if clipsync.notes_equal(cn, rn):
                    lid = cid
                    break
            if lid is None:
                to_add.append(rn)
            else:
                del unmatched_local[lid]
                self.idmap.bind(rn["id"], lid)
        if unmatched_local:
            self.clip.remove_notes_by_id(tuple(unmatched_local))
        self._add(to_add)
