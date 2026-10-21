"""Tests for the Remote Script logic against MOCKED Live objects (tests/mock_live.py).

These prove the diff/apply/feedback-loop/conflict logic, not Live itself. Behaviour
inside real Ableton is recorded separately in docs/CAPABILITY_MATRIX.md.
"""
import os
import socket
import sys
import threading
import time

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "remote_script"))
sys.path.insert(0, ROOT)

from Anthem import clipsync  # noqa: E402
from Anthem.clipbinding import ClipBinding  # noqa: E402
from Anthem.bridge import Bridge  # noqa: E402
import mock_live as ml  # noqa: E402


def make_binding(song, clip_id="drums", track="Anthem Drums", owned=True):
    sent = []
    b = ClipBinding(clip_id, track, 0, owned, sent.append, ml.MidiNoteSpecification, lambda m: None)
    b.resolve(song)
    return b, sent


def setup_pair():
    """Machine A owns 'drums'; machine B receives it."""
    sa, sb = ml.MockSong(), ml.MockSong()
    sa.tracks[0].clip_slots[0].create_clip(4.0)
    sb.tracks[0].clip_slots[0].create_clip(4.0)
    sa.flush(), sb.flush()
    a, sent_a = make_binding(sa, owned=True)
    b, sent_b = make_binding(sb, owned=False)
    sent_a.clear(), sent_b.clear()
    return sa, sb, a, b, sent_a, sent_b


def pump(song, binding, now_offset=1.0):
    song.flush()
    binding.tick(time.time() + now_offset)


def deltas(sent):
    return [m for m in sent if m["type"] == "local_delta"]


def apply_all(sent, receiver, start=0):
    """Forward owner messages to the receiver binding like the companion/relay would."""
    for i, m in enumerate(sent[start:]):
        if m["type"] == "local_delta":
            receiver.apply_delta("op%d" % i, m["added"], m["modified"], m["removed"])
        elif m["type"] == "local_snapshot":
            receiver.apply_snapshot("snap%d" % i, m["notes"])


# --- clipsync (pure) ----------------------------------------------------------------
def test_diff_add_modify_remove():
    n1 = {"id": 1, "pitch": 60, "start": 0.0, "dur": 1.0, "vel": 100.0, "mute": False, "prob": 1.0, "vel_dev": 0.0, "rel_vel": 64.0}
    n2 = dict(n1, id=2, pitch=62)
    n2m = dict(n2, start=0.5)
    n3 = dict(n1, id=3, pitch=64)
    assert clipsync.diff({1: n1, 2: n2}, {1: n1, 2: n2}) is None
    added, modified, removed = clipsync.diff({1: n1, 2: n2}, {2: n2m, 3: n3})
    assert added == [n3] and modified == [n2m] and removed == [1]


def test_same_content_ignores_ids():
    n = {"id": 1, "pitch": 60, "start": 0.0, "dur": 1.0, "vel": 100.0, "mute": False, "prob": 1.0, "vel_dev": 0.0, "rel_vel": 64.0}
    assert clipsync.same_content({1: n}, {7: dict(n, id=7)})
    assert not clipsync.same_content({1: n}, {7: dict(n, id=7, vel=90.0)})


# --- owner side ---------------------------------------------------------------------
def test_owner_sends_snapshot_on_bind_and_delta_on_edit():
    song = ml.MockSong()
    clip = song.tracks[0].clip_slots[0].create_clip(4.0)
    song.flush()
    b, sent = make_binding(song)
    assert sent[0]["type"] == "clip_found" and sent[1]["type"] == "local_snapshot" and sent[1]["notes"] == []
    nid = clip.user_draw(36, 0.0, 0.25)
    pump(song, b)
    d = deltas(sent)
    assert len(d) == 1 and [n["id"] for n in d[0]["added"]] == [nid] and d[0]["modified"] == [] and d[0]["removed"] == []
    clip.user_modify(nid, start_time=1.0, velocity=80.0)
    pump(song, b)
    d = deltas(sent)
    assert len(d) == 2 and d[1]["modified"][0]["start"] == 1.0 and d[1]["modified"][0]["vel"] == 80.0
    clip.user_delete(nid)
    pump(song, b)
    assert deltas(sent)[2]["removed"] == [nid]


def test_owner_debounce_coalesces_burst():
    song = ml.MockSong()
    clip = song.tracks[0].clip_slots[0].create_clip(4.0)
    song.flush()
    b, sent = make_binding(song)
    t0 = time.time()
    for i in range(50):
        clip.user_draw(36 + (i % 12), i * 0.25, 0.25)
        song.flush()
        b.tick(t0 + 0.01)          # within debounce window: nothing sent yet
    assert deltas(sent) == []
    b.tick(t0 + 1.0)
    d = deltas(sent)
    assert len(d) == 1 and len(d[0]["added"]) == 50


def test_owner_unchanged_notification_sends_nothing():
    song = ml.MockSong()
    clip = song.tracks[0].clip_slots[0].create_clip(4.0)
    song.flush()
    b, sent = make_binding(song)
    clip._notify("notes")     # e.g. a no-op edit
    pump(song, b)
    assert deltas(sent) == []


# --- receiver side + two simulated machines -------------------------------------------
def test_two_machines_add_move_resize_velocity_delete():
    sa, sb, a, b, sent_a, sent_b = setup_pair()
    ca, cb = sa.tracks[0].clip_slots[0].clip, sb.tracks[0].clip_slots[0].clip
    nid = ca.user_draw(36, 0.0, 0.25)
    pump(sa, a)
    apply_all(sent_a, b)
    assert cb.content() == ca.content()
    # move, resize, velocity in one go
    ca.user_modify(nid, start_time=2.0, duration=0.5, velocity=64.0)
    pump(sa, a)
    apply_all(sent_a, b, start=1)
    assert cb.content() == ca.content() == [(36, 2.0, 0.5, 64.0, False)]
    ca.user_delete(nid)
    pump(sa, a)
    apply_all(sent_a, b, start=2)
    assert cb.content() == [] and b.idmap.r2l == {}
    # receiver reported each apply
    applied = [m for m in sent_b if m["type"] == "applied"]
    assert len(applied) == 3 and all(m["ok"] for m in applied) and all("ms" in m for m in applied)
    assert sb.undo_steps == 3


def test_receiver_does_not_echo_or_conflict_on_own_writes():
    sa, sb, a, b, sent_a, sent_b = setup_pair()
    ca = sa.tracks[0].clip_slots[0].clip
    for i in range(4):
        ca.user_draw(40 + i, i * 1.0, 0.5)
    pump(sa, a)
    apply_all(sent_a, b)
    sb.flush()   # Live's async notes notification after our own write
    types = [m["type"] for m in sent_b]
    assert "conflict" not in types and "local_delta" not in types and "local_snapshot" not in types


def test_receiver_local_edit_raises_conflict_and_revert_fixes_it():
    sa, sb, a, b, sent_a, sent_b = setup_pair()
    ca, cb = sa.tracks[0].clip_slots[0].clip, sb.tracks[0].clip_slots[0].clip
    ca.user_draw(36, 0.0, 0.25)
    pump(sa, a)
    apply_all(sent_a, b)
    sb.flush()
    cb.user_draw(60, 1.0, 1.0)   # B edits a clip it does not own
    sb.flush()
    assert [m["type"] for m in sent_b].count("conflict") == 1 and b.conflict
    cb.user_draw(61, 2.0, 1.0)
    sb.flush()
    assert [m["type"] for m in sent_b].count("conflict") == 1   # reported once until resolved
    # companion decides "revert": re-apply authoritative snapshot
    snap = [m for m in sent_a if m["type"] == "local_snapshot"]
    a.snapshot_now()
    snap = sent_a[-1]["notes"]
    assert b.apply_snapshot("rev0", snap) is False and sent_b[-1]["error"] == "conflict"   # not without force
    assert cb.content() != ca.content()
    b.apply_snapshot("rev1", snap, force=True)
    sb.flush()
    assert cb.content() == ca.content() and not b.conflict


def test_snapshot_is_minimal_and_idempotent():
    sa, sb, a, b, sent_a, sent_b = setup_pair()
    ca, cb = sa.tracks[0].clip_slots[0].clip, sb.tracks[0].clip_slots[0].clip
    for i in range(3):
        ca.user_draw(36 + i, i, 0.25)
    cb.user_draw(36, 0.0, 0.25)     # B already has one matching note and one stray
    cb.user_draw(99, 3.0, 0.25)
    a.snapshot_now()
    notes = sent_a[-1]["notes"]
    w0 = cb.writes
    b.apply_snapshot("s1", notes, force=True)    # explicit revert semantics
    assert cb.content() == ca.content()
    assert cb.writes - w0 == 2            # one remove call + one add call, matching note kept
    w1 = cb.writes
    b.apply_snapshot("s2", notes)         # unchanged snapshot -> no writes at all
    assert cb.writes == w1 and sb.undo_steps == 2
    assert len(b.idmap.r2l) == 3


def test_modify_falls_back_when_api_ignores_pitch():
    sa, sb, a, b, sent_a, sent_b = setup_pair()
    ca, cb = sa.tracks[0].clip_slots[0].clip, sb.tracks[0].clip_slots[0].clip
    cb.pitch_locked = True
    nid = ca.user_draw(36, 0.0, 0.25)
    pump(sa, a)
    apply_all(sent_a, b)
    ca.user_modify(nid, pitch=48)
    pump(sa, a)
    apply_all(sent_a, b, start=1)
    assert cb.content() == ca.content() == [(48, 0.0, 0.25, 100.0, False)]
    assert b.idmap.r2l[nid] in cb._notes


def test_modify_of_unknown_remote_id_is_treated_as_add():
    sa, sb, a, b, sent_a, sent_b = setup_pair()
    cb = sb.tracks[0].clip_slots[0].clip
    note = {"id": 77, "pitch": 50, "start": 1.0, "dur": 0.5, "vel": 90.0, "mute": False, "prob": 1.0, "vel_dev": 0.0, "rel_vel": 64.0}
    assert b.apply_delta("x", [], [note], [])
    assert cb.content() == [(50, 1.0, 0.5, 90.0, False)] and b.idmap.r2l[77] in cb._notes


def test_duplicate_delta_applied_twice_is_detected_by_companion_not_script():
    # The script applies whatever it is told; de-duplication lives in the relay/companion.
    sa, sb, a, b, sent_a, sent_b = setup_pair()
    cb = sb.tracks[0].clip_slots[0].clip
    note = {"id": 5, "pitch": 50, "start": 1.0, "dur": 0.5, "vel": 90.0, "mute": False, "prob": 1.0, "vel_dev": 0.0, "rel_vel": 64.0}
    b.apply_delta("o1", [note], [], [])
    b.apply_delta("o1", [], [dict(note, vel=70.0)], [])   # same id modified -> still one note
    assert cb.content() == [(50, 1.0, 0.5, 70.0, False)]


def test_apply_reports_failure_when_live_changes_the_result():
    sa, sb, a, b, sent_a, sent_b = setup_pair()
    cb = sb.tracks[0].clip_slots[0].clip
    n1 = {"id": 1, "pitch": 60, "start": 0.0, "dur": 1.0, "vel": 100.0, "mute": False}
    n2 = dict(n1, id=2, start=2.0)
    assert b.apply_snapshot("init", [n1, n2])     # receivers always start from a snapshot
    # emulate Live merging an overlapping same-pitch note: the second note silently disappears
    orig = cb.apply_note_modifications

    def merging(vec):
        orig(vec)
        cb._notes.pop(b.idmap.r2l[1], None)
    cb.apply_note_modifications = merging
    assert b.apply_delta("o2", [], [dict(n2, start=0.0)], []) is False
    assert "content differs" in sent_b[-1]["error"]
    cb.apply_note_modifications = orig
    # the companion's answer, a snapshot, heals it
    assert b.apply_snapshot("s", [n1, dict(n2, start=3.0)])
    assert cb.content() == [(60, 0.0, 1.0, 100.0, False), (60, 3.0, 1.0, 100.0, False)]


def test_apply_on_owned_clip_is_reported_to_others_via_listener():
    song = ml.MockSong()
    song.tracks[0].clip_slots[0].create_clip(4.0)
    song.flush()
    b, sent = make_binding(song, owned=True)
    n = {"id": 1, "pitch": 60, "start": 0.0, "dur": 1.0, "vel": 100.0, "mute": False}
    assert b.apply_delta("handover", [n], [], [])
    pump(song, b)
    d = deltas(sent)
    assert len(d) == 1 and d[0]["added"][0]["pitch"] == 60


def test_apply_refuses_to_overwrite_pending_local_edit():
    sa, sb, a, b, sent_a, sent_b = setup_pair()
    cb = sb.tracks[0].clip_slots[0].clip
    n = {"id": 1, "pitch": 60, "start": 0.0, "dur": 1.0, "vel": 100.0, "mute": False}
    cb.user_draw(50, 2.0, 0.5)          # listener NOT yet delivered (song.flush not called)
    assert b.apply_delta("o1", [n], [], []) is False
    assert sent_b[-1]["error"] == "conflict" and b.conflict        # one signal only
    assert cb.content() == [(50, 2.0, 0.5, 100.0, False)]      # nothing written
    sb.flush()
    assert [m["type"] for m in sent_b].count("conflict") == 0     # listener does not double-report
    assert b.apply_snapshot("heal", [n]) is False               # heal does not overwrite either
    assert b.apply_snapshot("revert", [n], force=True) and cb.content() == [(60, 0.0, 1.0, 100.0, False)]
    assert not b.conflict


def test_set_owned_switches_roles_at_runtime():
    sa, sb, a, b, sent_a, sent_b = setup_pair()
    ca, cb = sa.tracks[0].clip_slots[0].clip, sb.tracks[0].clip_slots[0].clip
    ca.user_draw(36, 0, 0.25)
    pump(sa, a)
    apply_all(sent_a, b)
    # host releases, guest claims: B becomes owner, A becomes receiver
    b.set_owned(True)
    assert sent_b[-1]["type"] == "local_snapshot" and sent_b[-1]["owned"] is True and len(sent_b[-1]["notes"]) == 1
    a.set_owned(False)
    assert sent_a[-1]["type"] == "local_snapshot" and sent_a[-1]["owned"] is False
    assert a.apply_snapshot("init", sent_b[-1]["notes"])     # companion pushes the new owner's state
    assert cb.writes and ca.content() == cb.content()
    cb.user_draw(40, 1.0, 0.5)
    pump(sb, b)
    d = deltas(sent_b)
    assert len(d) == 1 and d[0]["added"][0]["pitch"] == 40 and "conflict" not in [m["type"] for m in sent_b]
    assert a.apply_delta("x", d[0]["added"], d[0]["modified"], d[0]["removed"])
    sa.flush()
    assert ca.content() == cb.content() and not a.conflict
    b.set_owned(True)       # idempotent
    assert sent_b[-1]["type"] == "local_delta"


def test_oversized_clip_is_reported_not_silently_dropped(monkeypatch):
    from Anthem import clipbinding
    monkeypatch.setattr(clipbinding, "MAX_NOTES", 5)
    song = ml.MockSong()
    clip = song.tracks[0].clip_slots[0].create_clip(4.0)
    song.flush()
    b, sent = make_binding(song)
    for i in range(6):
        clip.user_draw(36 + i, i * 0.25, 0.25)
    pump(song, b)
    assert sent[-1]["type"] == "clip_lost" and "too large" in sent[-1]["reason"]
    clip.user_draw(50, 3.0, 0.25)
    pump(song, b)
    assert sent[-1]["type"] == "clip_lost"                  # reported once
    for nid in list(clip._notes)[:3]:
        clip.user_delete(nid)
    pump(song, b)
    assert [m["type"] for m in sent[-2:]] == ["clip_found", "local_snapshot"] and len(sent[-1]["notes"]) == 4


def test_oversized_bytes_reported_once_recovers_only_when_it_fits(monkeypatch):
    from Anthem import clipbinding
    monkeypatch.setattr(clipbinding, "MAX_LINE_BYTES", 700)     # ~5 notes fit
    song = ml.MockSong()
    clip = song.tracks[0].clip_slots[0].create_clip(4.0)
    song.flush()
    b, sent = make_binding(song)
    for i in range(3):
        clip.user_draw(36 + i, i * 0.25, 0.25)
    pump(song, b)
    assert sent[-1]["type"] == "local_delta"
    for i in range(10):
        clip.user_draw(50 + i, i * 0.25, 0.25)
    pump(song, b)
    assert sent[-1]["type"] == "clip_lost" and "size limit" in sent[-1]["reason"] and b.too_large
    n = len(sent)
    clip.user_draw(70, 3.0, 0.25)
    pump(song, b)
    assert len(sent) == n                                   # no flapping: nothing sent while too large
    for nid in list(clip._notes)[:11]:
        clip.user_delete(nid)
    pump(song, b)
    assert [m["type"] for m in sent[-2:]] == ["clip_found", "local_snapshot"]   # found announced first
    assert len(sent[-1]["notes"]) == len(clip._notes) and not b.too_large and b.known


def test_receiver_baseline_is_truncated_never_lost(monkeypatch):
    from Anthem import clipbinding
    monkeypatch.setattr(clipbinding, "MAX_LINE_BYTES", 700)
    song = ml.MockSong()
    clip = song.tracks[0].clip_slots[0].create_clip(4.0)
    for i in range(12):
        clip.user_draw(36 + i, i * 0.25, 0.25)
    song.flush()
    b, sent = make_binding(song, owned=False)
    assert [m["type"] for m in sent] == ["clip_found", "local_snapshot"]
    assert sent[-1]["truncated"] is True and 0 < len(sent[-1]["notes"]) < 12 and b.found


def test_set_owned_after_too_large_announces_found():
    from Anthem import clipbinding
    song = ml.MockSong()
    clip = song.tracks[0].clip_slots[0].create_clip(4.0)
    song.flush()
    b, sent = make_binding(song)
    b.too_large = True          # as if the owner clip had been reported too large
    b.set_owned(False)
    assert [m["type"] for m in sent[-2:]] == ["clip_found", "local_snapshot"] and not b.too_large


def test_closed_binding_is_inert():
    song = ml.MockSong()
    slot = song.tracks[0].clip_slots[0]
    slot.create_clip(4.0)
    song.flush()
    b, sent = make_binding(song)
    slot.delete_clip()      # notification pending
    b.close()
    n = len(sent)
    song.flush()
    assert len(sent) == n and not b.found and b._listeners == []
    slot.create_clip(4.0)
    song.flush()
    assert len(sent) == n


# --- clip / track lifecycle -------------------------------------------------------------
def test_clip_deleted_and_recreated():
    sa, sb, a, b, sent_a, sent_b = setup_pair()
    slot = sb.tracks[0].clip_slots[0]
    slot.delete_clip()
    sb.flush()
    assert sent_b[-1] == {"type": "clip_lost", "clip": "drums", "reason": "clip missing"} and not b.found
    assert b.apply_delta("x", [{"id": 1, "pitch": 60, "start": 0, "dur": 1, "vel": 100, "mute": False}], [], []) is False
    assert sent_b[-1]["error"] == "clip not found"
    slot.create_clip(4.0)
    sb.flush()
    assert [m["type"] for m in sent_b[-2:]] == ["clip_found", "local_snapshot"] and b.found
    assert sent_b[-1]["owned"] is False and sent_b[-1]["notes"] == []
    assert b.apply_delta("y", [{"id": 1, "pitch": 60, "start": 0, "dur": 1, "vel": 100, "mute": False}], [], [])
    assert slot.clip.content() == [(60, 0, 1, 100, False)]


def test_track_renamed_then_restored_and_track_deleted():
    sa, sb, a, b, sent_a, sent_b = setup_pair()
    sb.tracks[0].name = "Something else"
    sb.flush()
    assert sent_b[-1]["type"] == "clip_lost" and sent_b[-1]["reason"] == "track missing"
    sb.tracks[0].name = "Anthem Drums"
    sb.flush()
    assert sent_b[-2]["type"] == "clip_found"
    sb.delete_track(0)
    sb.flush()
    # the song-level tracks listener is wired by the ControlSurface; emulate it
    b.resolve(sb)
    assert sent_b[-1]["type"] == "clip_lost" and not b.found


def test_owner_rebinding_after_clip_recreated_sends_fresh_snapshot():
    song = ml.MockSong()
    slot = song.tracks[0].clip_slots[0]
    slot.create_clip(4.0)
    song.flush()
    b, sent = make_binding(song)
    slot.delete_clip()
    song.flush()
    slot.create_clip(8.0)
    slot.clip.user_draw(36, 0, 1)
    song.flush()
    assert [m["type"] for m in sent][-2:] == ["clip_found", "local_snapshot"]
    assert len(sent[-1]["notes"]) == 1


def test_resolve_ignores_audio_track_with_same_name():
    song = ml.MockSong(track_names=("Anthem Drums",))
    song.tracks[0].has_midi_input = False
    b, sent = make_binding(song)
    assert not b.found and sent == []


# --- bridge (real loopback sockets, no Live) ------------------------------------------------
class _Server(threading.Thread):
    def __init__(self):
        threading.Thread.__init__(self, daemon=True)
        self.srv = socket.socket()
        self.srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self.srv.bind(("127.0.0.1", 0))
        self.srv.listen(1)
        self.port = self.srv.getsockname()[1]
        self.received = []
        self.conn = None
        self.start()

    def run(self):
        while True:
            try:
                c, _ = self.srv.accept()
            except OSError:
                return
            self.conn = c
            buf = b""
            while True:
                try:
                    d = c.recv(65536)
                except OSError:
                    break
                if not d:
                    break
                buf += d
                while b"\n" in buf:
                    line, buf = buf.split(b"\n", 1)
                    self.received.append(line)
            c.close()


def _poll_until(bridge, pred, timeout=3.0):
    out = []
    t0 = time.time()
    while time.time() - t0 < timeout:
        out += bridge.poll()
        if pred(out):
            return out
        time.sleep(0.01)
    return out


def test_bridge_connect_send_receive_and_reconnect():
    srv = _Server()
    br = Bridge(port=srv.port)
    _poll_until(br, lambda _: br.connected)
    assert br.connected
    assert br.send({"type": "script_hello", "x": 1})
    _poll_until(br, lambda _: srv.received)
    assert srv.received and b'"type":"script_hello"' in srv.received[0]
    srv.conn.sendall(b'{"v":1,"type":"bind","clips":[]}\n{"garbage"\n{"v":2,"type":"bind"}\n')
    msgs = _poll_until(br, lambda out: len(out) >= 1)
    assert msgs == [{"v": 1, "type": "bind", "clips": []}]   # malformed and wrong-version lines dropped
    srv.conn.close()
    _poll_until(br, lambda _: not br.connected)
    assert not br.connected and not br.send({"type": "x"})   # nothing is queued while disconnected
    _poll_until(br, lambda _: br.connected, timeout=6.0)
    assert br.connected
    br.close()
    srv.srv.close()


def test_bridge_never_blocks_when_no_companion():
    br = Bridge(port=1)   # nothing listens here
    t0 = time.time()
    for _ in range(200):
        br.poll()
    assert time.time() - t0 < 0.5 and not br.connected


def test_bridge_outbox_is_bounded():
    srv = _Server()
    br = Bridge(port=srv.port)
    _poll_until(br, lambda _: br.connected)
    br._flush = lambda: None   # simulate a stalled kernel buffer
    for i in range(1000):
        br.send({"type": "local_delta", "i": i})
    assert len(br._outbox) <= 256 and br.dropped
    br.close()
    srv.srv.close()
