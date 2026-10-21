# Capability matrix — Ableton Live Remote Script API (Milestone 1)

Environment where this was measured: **Ableton Live 12.4.1 Suite, macOS 26.6.2**
(Live auto-updated from 12.3.8 → 12.4 → 12.4.1 during the day; the script loaded on all
three). Embedded Python 3.11.6. No Max for Live used.

Legend — verification levels, strictly as defined in the brief:
* **mock** = passes against `tests/mock_live.py` (not Ableton)
* **Live/API** = verified inside one real Live instance with the edit made through the API
  (`tools/live_verify.py` phase A)
* **Live/human** = verified inside one real Live instance with a person editing in the
  piano roll (`tools/live_verify.py` phase B, 2026-10-16, log in `tools/live_verify_phaseB.txt`)
* **2×Live** = verified between two real Ableton installations — **not yet done**

| Operation | Read | Detect native edit | Apply | Verified in Live |
|---|---|---|---|---|
| Add MIDI note | yes (`get_all_notes_extended`) | yes — `notes` listener; drawn note arrived as `added` 194 ms after the edit (100 ms debounce + 50 ms tick + loopback) | yes (`add_new_notes`, ids returned) | Live/human (detect), Live/API (apply) |
| Move MIDI note | yes | yes — arrives as `modified` with the **same note_id** (two drags observed) | yes (`apply_note_modifications`) | Live/human (detect), Live/API (apply) |
| Resize MIDI note | yes | yes — `modified`, same id (0.25 → 0.75 beats observed) | yes | Live/human, Live/API |
| Transpose (drag pitch) | yes | yes — `modified`, same id (50 → 51 observed) | yes — pitch accepted by `apply_note_modifications` in 12.4.1 (fallback remove+add exists and is mock-tested) | Live/human, Live/API |
| Delete MIDI note | yes | yes — `removed` with the id, 151 ms | yes (`remove_notes_by_id`) | Live/human, Live/API |
| Change velocity | yes | listener path identical to move/resize; **not yet exercised by a human** (tester did not find the velocity lane) | yes (`velocity` via `apply_note_modifications`, verified read-back 100 → 64) | Live/API only |
| Mute note / probability / vel. deviation / release vel. | yes | same listener (not human-tested) | yes for mute (verified); others set on the same call, not asserted separately | Live/API (mute) |
| Duplicate / copy-paste notes | yes | yes — Cmd+D ×3 arrived as three `added` deltas | yes (as adds) | Live/human |
| Create MIDI clip | yes (`clip_slot.has_clip`) | yes — `has_clip` listener → `clip_found` | yes (`clip_slot.create_clip(len)`) used by `setup` | Live/API (create), mock (listener) |
| Delete MIDI clip | yes | yes — `has_clip` listener → `clip_lost`; sync for that clip pauses | **not applied remotely by design** (deletion is reported, never mirrored) | mock |
| Rename track | yes (`track.name`) | yes — `name` listener re-resolves; rename away = `clip_lost`, back = `clip_found` | not synchronized (template names are the binding key) | mock |
| Create MIDI track | yes (`song.tracks`) | yes — `tracks` listener re-resolves bindings | yes (`song.create_midi_track(-1)` + `name`) used by `setup` | Live/API (create), mock (listener) |
| Move arrangement clip | readable (`track.arrangement_clips`) | not implemented | not implemented | — (out of scope) |

## Other verified facts (Live/API unless noted)

* `Live.Base.Timer(callback, interval=50, repeat=True)` fires on the main thread every
  50 ms; errors in the callback would stop it, so every tick is wrapped.
* The `notes` listener fires asynchronously ~43 ms after an API edit (one coalesced
  notification), and for human edits once per gesture end (a drag produced one event).
* Apply cost inside Live: 0.2–7 ms per op (`applied.ms`), i.e. negligible vs. audio.
* An unchanged snapshot results in **zero** Live writes (content matching) — no undo spam.
* Our own writes do not produce a conflict/echo: the content check after the listener
  fires sees the expected state (Live/API, 1.5 s observation window).
* Live merges overlapping same-pitch notes; the script now verifies the clip against the
  expected content after every apply and reports `ok:false` so the companion re-sends a
  snapshot rather than silently diverging (mock-tested; observed once in Live when a
  stale test clip was used).
* Opening another Live Set re-instantiates the Remote Script (`disconnect` + fresh
  `create_instance`), observed in Live 12.3.8 while a user opened their own project.
* `song.file_path` is `""` for an unsaved set; `song.name` likewise. The companion treats an
  unsaved set as an unknown project (warning at bind; a later re-instantiation with an unsaved
  or newly saved set is treated as a possible different project and requires `rebind`).
  Save the disposable set once so the guard can compare real paths.

## Full path with one real Live (2026-10-16, `tools/demo_one_live.py`)

Real Live 12.4.1 (host, Drums) ↔ companion ↔ relay ↔ companion ↔ headless script on a
mocked set (guest, Bass). Observed: the guest's six bass notes and a velocity change
appeared in Live's `Anthem Bass` clip (confirmed by the tester looking at Live); the
tester's drum edits (3 → 6 → 5 notes: draws and a delete) arrived in the guest's mock clip
with the exact pitches/positions. No conflicts, no echoes. Log: `tools/demo_one_live.txt`.
Re-run with the final build (`tools/demo_one_live_final.txt`): Live's bass clip still held two
notes from an earlier test, so the companion raised a CONFLICT instead of overwriting them;
after `revert bass` the guest's six notes and a velocity change were applied in Live
(8 applies, median 1.2 ms, p95 5.7 ms).

## Not verified yet

* Two real Live installations exchanging edits (the relay/companion path is proven with
  real sockets and one real Live on one side; the other side was the Remote Script code
  running headless, not a second Live).
* Live 12 Standard / Live 11 / Windows.
* Velocity and note-expression edits made by a human.
* Audio dropout measurement under load (CPU meter unchanged in casual observation only).
