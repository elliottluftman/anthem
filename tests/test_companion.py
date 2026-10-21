"""Tests for the Anthem companion: pure core tests (message dicts) plus asyncio integration
tests with a fake relay and a fake Remote Script. Runs under unittest or pytest:

    python3 -m unittest tests.test_companion -v
    python3 -m pytest tests/test_companion.py -q
"""
import asyncio
import json
import os
import sys
import time
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
os.environ.setdefault("ANTHEM_HOME", str(ROOT / "tests" / ".anthem_test_home"))

from anthem_core import protocol  # noqa: E402
from anthem_core.session import Session  # noqa: E402
from companion import core  # noqa: E402
from companion import anthem  # noqa: E402

ME, PEER, SECRET = "p-me", "p-peer", "s" * 24


def note(i, pitch=60, start=0.0):
    return {"id": i, "pitch": pitch, "start": start, "dur": 0.25, "vel": 100, "mute": False,
            "prob": 1.0, "vel_dev": 0.0, "rel_vel": 64.0}


def welcome(me=ME, peer=PEER, revs=None, notes=None, seq=0, ops=None, resync=False, role="host"):
    revs = revs or {"drums": 0, "bass": 0}
    notes = notes or {"drums": [], "bass": []}
    return {"v": 1, "type": "welcome", "session": "S1", "code": "ABCD1234", "participant": me, "role": role,
            "seq": seq, "resync": resync, "ops": ops, "snapshot": {
                "clips": [{"clip": "drums", "name": "Drums", "track_name": "Anthem Drums", "slot": 0, "owner": me},
                          {"clip": "bass", "name": "Bass", "track_name": "Anthem Bass", "slot": 0, "owner": peer}],
                "revs": revs, "notes": notes}}


def hello(path="/tmp/a.als", token=None):
    msg = {"v": 1, "type": "script_hello", "live_version": "12.1", "set_name": "a", "set_path": path,
           "script_version": "1"}
    if token is not None:
        msg["token"] = token
    return msg


def baseline(clip, notes=None):
    """The script's local_snapshot for a non-owned clip (after bind / set_owned)."""
    return {"v": 1, "type": "local_snapshot", "clip": clip, "notes": notes or [], "owned": False}


def script_token(port=anthem.DEFAULT_SCRIPT_PORT):
    return anthem.token_path(port).read_text().strip()


def inventory(found=("drums", "bass")):
    return {"v": 1, "type": "inventory", "tracks": ["Anthem Drums", "Anthem Bass"], "clips": [
        {"clip": c, "found": c in found, "track_name": "Anthem %s" % c.title(), "slot": 0, "note_count": 0}
        for c in ("drums", "bass")]}


def of(actions, kind, mtype=None):
    return [a[1] for a in actions if a[0] == kind and (mtype is None or a[1].get("type") == mtype)]


def prints(actions):
    return "\n".join(a[1] for a in actions if a[0] == "print")


class Clock(object):
    def __init__(self):
        self.t = 1000.0

    def __call__(self):
        return self.t


def bound_companion(notes=None, revs=None):
    """Host companion: owns drums, bass is the peer's. Welcomed, script connected, bound, inventory done
    and the script's (empty) baseline for bass delivered, as the real script does after bind."""
    clock = Clock()
    c = core.Companion(ME, SECRET, create_clips=core.TEMPLATE_CLIPS, clock=clock)
    c.on_relay_connected()
    c.on_relay(welcome(notes=notes, revs=revs))
    c.on_script_connected()
    c.on_script(hello())
    acts = c.on_script(inventory())
    c.on_script(baseline("bass"))
    return c, clock, acts


# ============================================================== core: binding
class BindTests(unittest.TestCase):
    def test_bind_requires_welcome_and_hello(self):
        c = core.Companion(ME, SECRET, create_clips=core.TEMPLATE_CLIPS)
        acts = c.on_relay_connected()
        self.assertEqual(of(acts, "relay")[0]["type"], "create")
        c.on_script_connected()
        self.assertEqual(of(c.on_script(hello()), "script", "bind"), [])
        acts = c.on_relay(welcome())
        binds = of(acts, "script", "bind")
        self.assertEqual(len(binds), 1)
        owned = {x["clip"]: x["owned"] for x in binds[0]["clips"]}
        self.assertEqual(owned, {"drums": True, "bass": False})
        self.assertIn("ABCD1234", prints(acts))
        self.assertTrue(c.bound)

    def test_bind_other_order(self):
        c = core.Companion(ME, SECRET, create_clips=core.TEMPLATE_CLIPS)
        c.on_relay_connected()
        self.assertEqual(of(c.on_relay(welcome()), "script", "bind"), [])
        c.on_script_connected()
        self.assertEqual(len(of(c.on_script(hello()), "script", "bind")), 1)

    def test_set_path_mismatch_blocks_until_rebind(self):
        c = core.Companion(ME, SECRET, session="S1", set_path="/tmp/old.als", last_seq=0)
        acts = c.on_relay_connected()
        self.assertEqual(of(acts, "relay")[0]["type"], "hello")
        c.on_relay(welcome())
        c.on_script_connected()
        acts = c.on_script(hello("/tmp/new.als"))
        self.assertEqual(of(acts, "script", "bind"), [])
        self.assertIn("WARNING", prints(acts))
        self.assertFalse(c.bound)
        acts = c.on_command("rebind")
        self.assertEqual(len(of(acts, "script", "bind")), 1)
        self.assertEqual(c.set_path, "/tmp/new.als")
        self.assertTrue(any(a[0] == "persist" and a[1]["set_path"] == "/tmp/new.als" for a in acts))

    def test_hello_with_new_path_after_bound_unbinds(self):
        c, _, _ = bound_companion()
        acts = c.on_script(hello("/tmp/other.als"))
        self.assertFalse(c.bound)
        self.assertIn("rebind", prints(acts))
        # relayed ops are no longer forwarded
        acts = c.on_relay({"v": 1, "type": "op", "seq": 5, "rev": 1, "participant": PEER, "op_id": "x1",
                           "clip": "bass", "kind": "snapshot", "payload": {"notes": [note(1)]}})
        self.assertEqual(of(acts, "script"), [])
        self.assertEqual(len(c.clips["bass"].notes), 1)  # replica still updated

    def test_persist_set_path_at_first_bind(self):
        c = core.Companion(ME, SECRET, create_clips=core.TEMPLATE_CLIPS)
        c.on_relay_connected()
        c.on_relay(welcome())
        c.on_script_connected()
        acts = c.on_script(hello("/x.als"))
        self.assertTrue(any(a[0] == "persist" and a[1]["set_path"] == "/x.als" for a in acts))

    def test_unsaved_set_is_unknown_project(self):
        c = core.Companion(ME, SECRET, create_clips=core.TEMPLATE_CLIPS, set_path="")
        self.assertIsNone(c.set_path)
        c.on_relay_connected()
        c.on_relay(welcome())
        c.on_script_connected()
        acts = c.on_script(hello(""))
        self.assertEqual(len(of(acts, "script", "bind")), 1)
        self.assertIn("unsaved Live Set", prints(acts))
        self.assertIsNone(c.set_path)
        self.assertFalse(any(a[0] == "persist" and a[1]["set_path"] == "" for a in acts))
        self.assertIsNone(c.identity()["set_path"])
        # script re-instantiates, still unsaved: could be another set -> mismatch until rebind
        acts = c.on_script(hello(""))
        self.assertFalse(c.bound)
        self.assertIn("rebind", prints(acts))
        self.assertEqual(of(acts, "script", "bind"), [])
        acts = c.on_command("rebind")
        self.assertEqual(len(of(acts, "script", "bind")), 1)
        self.assertIsNone(c.set_path)
        # a saved path shows up later while bound: the unsaved first set can not be proven to be
        # this project -> mismatch; rebind records the path, which then guards as usual
        acts = c.on_script(hello("/saved.als"))
        self.assertFalse(c.bound)
        self.assertIn("rebind", prints(acts))
        acts = c.on_command("rebind")
        self.assertTrue(c.bound)
        self.assertEqual(c.set_path, "/saved.als")
        self.assertTrue(any(a[0] == "persist" and a[1]["set_path"] == "/saved.als" for a in acts))
        acts = c.on_script(hello("/other.als"))
        self.assertFalse(c.bound)
        self.assertIn("WARNING", prints(acts))

    def test_first_bind_with_saved_path_records_it(self):
        c = core.Companion(ME, SECRET, create_clips=core.TEMPLATE_CLIPS)
        c.on_relay_connected()
        c.on_relay(welcome())
        c.on_script_connected()
        acts = c.on_script(hello("/first.als"))
        self.assertTrue(c.bound)
        self.assertEqual(c.set_path, "/first.als")
        self.assertNotIn("WARNING", prints(acts))
        # the same path again (script re-instantiated in the same project) binds again
        self.assertEqual(len(of(c.on_script(hello("/first.als")), "script", "bind")), 1)

    def test_create_token_only_in_create_message(self):
        c = core.Companion(ME, SECRET, create_clips=core.TEMPLATE_CLIPS, create_token="T0K")
        msg = of(c.on_relay_connected(), "relay")[0]
        self.assertEqual((msg["type"], msg["token"]), ("create", "T0K"))
        self.assertNotIn("token", c.identity())
        self.assertNotIn("T0K", anthem._summarize(msg))
        c = core.Companion(ME, SECRET, create_clips=core.TEMPLATE_CLIPS)
        self.assertNotIn("token", of(c.on_relay_connected(), "relay")[0])
        c = core.Companion(ME, SECRET, join_code="ABC", create_token="T0K")
        self.assertNotIn("token", of(c.on_relay_connected(), "relay")[0])


# ============================================================ core: routing
class RoutingTests(unittest.TestCase):
    def test_init_snapshot_pushed_on_empty_baseline_not_on_inventory(self):
        c, _, acts = bound_companion(notes={"drums": [note(1)], "bass": [note(7, 36)]}, revs={"drums": 1, "bass": 1})
        self.assertEqual(of(acts, "script", "apply_snapshot"), [])   # inventory only prints the table
        self.assertIn("Supported parts", prints(acts))
        acts = c.on_script(baseline("bass"))                         # empty local clip: nothing to lose
        snaps = of(acts, "script", "apply_snapshot")
        self.assertEqual([s["clip"] for s in snaps], ["bass"])
        self.assertEqual(snaps[0]["notes"][0]["id"], 7)
        self.assertTrue(snaps[0]["op_id"].startswith("init-bass-"))
        self.assertNotIn("force", snaps[0])
        # the owned clip's baseline is authoritative, never compared
        acts = c.on_script({"v": 1, "type": "local_snapshot", "clip": "drums", "notes": [], "owned": True})
        self.assertEqual(of(acts, "relay", "op")[0]["kind"], "snapshot")

    def test_baseline_is_conflict_aware(self):
        c, _, _ = bound_companion(notes={"drums": [], "bass": [note(7, 36, 1.0)]}, revs={"drums": 0, "bass": 1})
        # content-equal (different ids, same pitch/start/dur/vel/mute): push the init snapshot
        acts = c.on_script(baseline("bass", [note(123, 36, 1.0)]))
        self.assertEqual(len(of(acts, "script", "apply_snapshot")), 1)
        self.assertFalse(c.clips["bass"].conflict)
        # differing local content: CONFLICT, no push, instructions shown
        acts = c.on_script(baseline("bass", [note(5, 40)]))
        self.assertEqual(of(acts, "script", "apply_snapshot"), [])
        self.assertTrue(c.clips["bass"].conflict)
        self.assertIn("revert bass", prints(acts))
        self.assertIn("differs from the session", of(acts, "script", "show_message")[0]["msg"])
        # relayed ops stay paused until revert, which pushes with force
        acts = c.on_relay({"v": 1, "type": "op", "seq": 2, "rev": 2, "participant": PEER, "op_id": "p2", "clip": "bass",
                           "kind": "delta", "payload": {"added": [note(2)], "modified": [], "removed": []}})
        self.assertEqual(of(acts, "script"), [])
        snap = of(c.on_command("revert bass"), "script", "apply_snapshot")[0]
        self.assertTrue(snap["force"])
        self.assertEqual(sorted(n["id"] for n in snap["notes"]), [2, 7])
        # owner still a role string and replica empty: nothing to push yet
        c2 = core.Companion(ME, SECRET, create_clips=core.TEMPLATE_CLIPS)
        c2.on_relay_connected()
        c2.on_relay(welcome(peer="guest"))
        c2.on_script_connected()
        c2.on_script(hello())
        c2.on_script(inventory())
        self.assertEqual(c2.on_script(baseline("bass")), [])
        self.assertEqual(len(of(c2.on_script(baseline("bass", [note(1)])), "print")), 1)  # local notes -> conflict
        self.assertTrue(c2.clips["bass"].conflict)

    def test_note_content_compares_expression_fields(self):
        a = note(1)
        for k, v in (("prob", 0.5), ("vel_dev", 10.0), ("rel_vel", 20.0)):
            self.assertNotEqual(core.note_content([a]), core.note_content([dict(a, id=2, **{k: v})]), k)
        self.assertEqual(core.note_content([a]), core.note_content([dict(a, id=2, prob=1.00001)]))
        # a baseline that only differs in probability is a real local edit -> conflict, not an init push
        c, _, _ = bound_companion(notes={"drums": [], "bass": [note(7)]}, revs={"drums": 0, "bass": 1})
        acts = c.on_script(baseline("bass", [dict(note(9), prob=0.3)]))
        self.assertTrue(c.clips["bass"].conflict)
        self.assertEqual(of(acts, "script", "apply_snapshot"), [])

    def test_local_snapshot_then_delta_base_rev(self):
        c, _, _ = bound_companion()
        acts = c.on_script({"v": 1, "type": "local_snapshot", "clip": "drums", "notes": [note(1)]})
        ops = of(acts, "relay", "op")
        self.assertEqual(len(ops), 1)
        self.assertEqual((ops[0]["kind"], ops[0]["base_rev"]), ("snapshot", 0))
        self.assertEqual(len(ops[0]["op_id"]), 32)
        c.on_relay({"v": 1, "type": "op_ack", "op_id": ops[0]["op_id"], "seq": 1, "rev": 1, "duplicate": False})
        self.assertEqual(c.clips["drums"].rev, 1)
        acts = c.on_script({"v": 1, "type": "local_delta", "clip": "drums", "added": [note(2, 62)], "modified": [],
                            "removed": []})
        op = of(acts, "relay", "op")[0]
        self.assertEqual((op["kind"], op["base_rev"]), ("delta", 1))
        self.assertEqual(op["payload"]["added"][0]["id"], 2)
        self.assertEqual(set(c.clips["drums"].notes), {1, 2})
        # second delta before the ack uses the optimistic rev
        acts = c.on_script({"v": 1, "type": "local_delta", "clip": "drums", "added": [], "modified": [],
                            "removed": [1]})
        self.assertEqual(of(acts, "relay", "op")[0]["base_rev"], 2)

    def test_op_ack_keeps_optimistic_rev_while_ops_pending(self):
        c, _, _ = bound_companion()
        ops = []
        for i in (1, 2):
            acts = c.on_script({"v": 1, "type": "local_delta", "clip": "drums", "added": [note(i)], "modified": [],
                                "removed": []})
            ops.append(of(acts, "relay", "op")[0])
        self.assertEqual([o["base_rev"] for o in ops], [0, 1])
        c.on_relay({"v": 1, "type": "op_ack", "op_id": ops[0]["op_id"], "seq": 1, "rev": 1, "duplicate": False})
        self.assertEqual(c.clips["drums"].rev, 2)        # not regressed to 1: the second delta is still in flight
        acts = c.on_script({"v": 1, "type": "local_delta", "clip": "drums", "added": [note(3)], "modified": [],
                            "removed": []})
        self.assertEqual(of(acts, "relay", "op")[0]["base_rev"], 2)
        # once nothing is pending for the clip the relay's rev is authoritative
        c.on_relay({"v": 1, "type": "op_ack", "op_id": ops[1]["op_id"], "seq": 2, "rev": 2, "duplicate": False})
        third = [k for k in c.pending][0]
        c.on_relay({"v": 1, "type": "op_ack", "op_id": third, "seq": 3, "rev": 3, "duplicate": False})
        self.assertEqual(c.clips["drums"].rev, 3)
        self.assertEqual(len(c.pending), 0)

    def test_local_messages_for_non_owned_clip(self):
        c, _, _ = bound_companion()
        acts = c.on_script({"v": 1, "type": "local_snapshot", "clip": "bass", "notes": [note(1)]})
        self.assertEqual(of(acts, "relay"), [])          # never an op; local notes differ -> conflict
        self.assertTrue(c.clips["bass"].conflict)
        c.clips["bass"].conflict = False
        acts = c.on_script({"v": 1, "type": "local_delta", "clip": "bass", "added": [note(1)], "modified": [],
                            "removed": []})
        self.assertEqual(of(acts, "relay"), [])
        self.assertTrue(c.clips["bass"].conflict)

    def test_invalid_local_delta_dropped(self):
        c, _, _ = bound_companion()
        acts = c.on_script({"v": 1, "type": "local_delta", "clip": "drums", "added": [{"id": -1}], "modified": [],
                            "removed": []})
        self.assertEqual(of(acts, "relay"), [])
        self.assertIn("dropped", prints(acts))
        self.assertEqual(len(c.pending), 0)

    def test_oversized_delta_and_healing_rejects_request_snapshot(self):
        c, _, _ = bound_companion()
        big = [note(i) for i in range(protocol.MAX_NOTES_PER_OP + 1)]
        acts = c.on_script({"v": 1, "type": "local_delta", "clip": "drums", "added": big, "modified": [], "removed": []})
        self.assertEqual(of(acts, "relay"), [])
        self.assertIn("delta too large", prints(acts))
        self.assertEqual(of(acts, "script", "request_snapshot"), [{"type": "request_snapshot", "clip": "drums"}])
        self.assertFalse(c.clips["drums"].dirty)
        # the clip is unavailable: remembered as dirty instead
        c.clips["drums"].found = False
        acts = c.on_script({"v": 1, "type": "local_delta", "clip": "drums", "added": big, "modified": [], "removed": []})
        self.assertEqual(of(acts, "script"), [])
        self.assertTrue(c.clips["drums"].dirty)
        c.clips["drums"].found = True
        c.clips["drums"].dirty = False
        # relay rejects that a snapshot heals: modify of unknown note, clip too large
        for reason in ("modify of unknown note 7", "clip too large"):
            acts = c.on_script({"v": 1, "type": "local_delta", "clip": "drums", "added": [note(1)], "modified": [],
                                "removed": []})
            op_id = of(acts, "relay", "op")[0]["op_id"]
            acts = c.on_relay({"v": 1, "type": "reject", "op_id": op_id, "reason": reason})
            self.assertEqual(of(acts, "script", "request_snapshot"), [{"type": "request_snapshot", "clip": "drums"}], reason)
            self.assertNotIn(op_id, c.pending)
        self.assertIn("%d-note limit" % protocol.MAX_NOTES_PER_CLIP, prints(acts))

    def test_op_ack_latency_recorded(self):
        c, clock, _ = bound_companion()
        acts = c.on_script({"v": 1, "type": "local_snapshot", "clip": "drums", "notes": []})
        op_id = of(acts, "relay", "op")[0]["op_id"]
        self.assertIn(op_id, c.pending)
        clock.t += 0.120
        c.on_relay({"v": 1, "type": "op_ack", "op_id": op_id, "seq": 1, "rev": 1, "duplicate": False})
        self.assertNotIn(op_id, c.pending)
        self.assertEqual(len(c.ack_latencies), 1)
        self.assertAlmostEqual(c.ack_latencies[0], 0.120, places=3)
        self.assertEqual(c.last_seq, 1)
        st = c.latency_stats()["op_ack_s"]
        self.assertAlmostEqual(st["median"], 0.120, places=3)
        self.assertIn("median=120ms", c.status_text())

    def test_duplicate_ack_dropped(self):
        c, _, _ = bound_companion()
        acts = c.on_script({"v": 1, "type": "local_snapshot", "clip": "drums", "notes": []})
        op_id = of(acts, "relay", "op")[0]["op_id"]
        c.on_relay({"v": 1, "type": "op_ack", "op_id": op_id, "seq": 1, "rev": 1, "duplicate": True})
        self.assertEqual(len(c.ack_latencies), 0)
        self.assertNotIn(op_id, c.pending)

    def test_reject_stale_requests_snapshot(self):
        c, _, _ = bound_companion()
        acts = c.on_script({"v": 1, "type": "local_delta", "clip": "drums", "added": [note(1)], "modified": [],
                            "removed": []})
        op_id = of(acts, "relay", "op")[0]["op_id"]
        acts = c.on_relay({"v": 1, "type": "reject", "op_id": op_id, "reason": "stale base_rev 0 (current 3)"})
        self.assertEqual(of(acts, "script", "request_snapshot"), [{"type": "request_snapshot", "clip": "drums"}])
        self.assertNotIn(op_id, c.pending)
        # other reject reasons are printed
        acts = c.on_script({"v": 1, "type": "local_snapshot", "clip": "drums", "notes": []})
        op_id = of(acts, "relay", "op")[0]["op_id"]
        acts = c.on_relay({"v": 1, "type": "reject", "op_id": op_id, "reason": "not owner of clip drums"})
        self.assertIn("not owner", prints(acts))
        self.assertEqual(of(acts, "script"), [])

    def test_relayed_op_forwarded_and_replica_updated(self):
        c, _, _ = bound_companion()
        op = {"v": 1, "type": "op", "seq": 3, "rev": 1, "participant": PEER, "op_id": "peer-1", "clip": "bass",
              "kind": "delta", "payload": {"added": [note(5, 40)], "modified": [], "removed": []}}
        acts = c.on_relay(op)
        fwd = of(acts, "script", "apply_delta")
        self.assertEqual(len(fwd), 1)
        self.assertEqual((fwd[0]["clip"], fwd[0]["op_id"], fwd[0]["added"][0]["id"]), ("bass", "peer-1", 5))
        self.assertEqual(c.clips["bass"].rev, 1)
        self.assertEqual(c.last_seq, 3)
        self.assertEqual(set(c.clips["bass"].notes), {5})
        # duplicate (same op_id / seq) applied only once
        acts = c.on_relay(op)
        self.assertEqual(acts, [])
        acts = c.on_relay(dict(op, op_id="peer-1b"))  # same seq, different id -> still a dup
        self.assertEqual(acts, [])
        self.assertEqual(len(c.clips["bass"].notes), 1)
        # removal
        acts = c.on_relay(dict(op, seq=4, rev=2, op_id="peer-2",
                               payload={"added": [], "modified": [], "removed": [5]}))
        self.assertEqual(of(acts, "script", "apply_delta")[0]["removed"], [5])
        self.assertEqual(c.clips["bass"].notes, {})

    def test_relayed_op_for_owned_clip_not_forwarded(self):
        c, _, _ = bound_companion()
        acts = c.on_relay({"v": 1, "type": "op", "seq": 1, "rev": 1, "participant": ME, "op_id": "mine", "clip": "drums",
                           "kind": "snapshot", "payload": {"notes": [note(1)]}})
        self.assertEqual(of(acts, "script"), [])
        self.assertEqual(c.clips["drums"].rev, 1)

    def test_relayed_op_with_bad_notes_ignored(self):
        c, _, _ = bound_companion()
        acts = c.on_relay({"v": 1, "type": "op", "seq": 9, "rev": 1, "participant": PEER, "op_id": "bad", "clip": "bass",
                           "kind": "snapshot", "payload": {"notes": [{"id": 1, "pitch": 999}]}})
        self.assertEqual(of(acts, "script"), [])
        self.assertIn("invalid", prints(acts))
        self.assertEqual(c.clips["bass"].notes, {})

    def test_applied_failure_self_heals_then_pauses(self):
        c, _, _ = bound_companion(notes={"drums": [], "bass": [note(1)]}, revs={"drums": 0, "bass": 1})
        acts = c.on_script({"v": 1, "type": "applied", "clip": "bass", "op_id": "init-bass-1", "ok": False,
                            "error": "boom", "ms": 3.5})
        heal = of(acts, "script", "apply_snapshot")
        self.assertEqual(len(heal), 1)
        self.assertEqual(heal[0]["notes"][0]["id"], 1)
        self.assertEqual(list(c.applied_ms), [3.5])
        acts = c.on_script({"v": 1, "type": "applied", "clip": "bass", "op_id": heal[0]["op_id"], "ok": False,
                            "error": "boom", "ms": 1})
        self.assertEqual(of(acts, "script"), [])
        self.assertIn("ERROR", prints(acts))
        self.assertTrue(c.clips["bass"].paused)
        # paused: relayed ops not forwarded; resync re-applies and un-pauses
        acts = c.on_relay({"v": 1, "type": "op", "seq": 2, "rev": 2, "participant": PEER, "op_id": "p2", "clip": "bass",
                           "kind": "delta", "payload": {"added": [note(2)], "modified": [], "removed": []}})
        self.assertEqual(of(acts, "script"), [])
        acts = c.on_command("resync")
        self.assertFalse(c.clips["bass"].paused)
        snaps = of(acts, "script", "apply_snapshot")
        self.assertEqual(sorted(n["id"] for n in snaps[0]["notes"]), [1, 2])
        self.assertTrue(snaps[0]["force"])
        self.assertEqual(of(acts, "script", "request_snapshot"), [{"type": "request_snapshot", "clip": "drums"}])

    def test_applied_failure_never_discards_a_local_edit(self):
        c, _, _ = bound_companion(notes={"drums": [], "bass": [note(1)]}, revs={"drums": 0, "bass": 1})
        # error "conflict" == the script's conflict message
        acts = c.on_script({"v": 1, "type": "applied", "clip": "bass", "op_id": "p1", "ok": False, "error": "conflict"})
        self.assertTrue(c.clips["bass"].conflict)
        self.assertEqual(of(acts, "script", "apply_snapshot"), [])
        self.assertIn("revert bass", prints(acts))
        self.assertEqual(len(of(acts, "script", "show_message")), 1)
        # while in conflict a failure only logs, no heal
        acts = c.on_script({"v": 1, "type": "applied", "clip": "bass", "op_id": "p2", "ok": False, "error": "boom"})
        self.assertEqual(of(acts, "script"), [])
        self.assertIn("not healing", prints(acts))
        c.clips["bass"].conflict = False
        c.clips["bass"].paused = True
        acts = c.on_script({"v": 1, "type": "applied", "clip": "bass", "op_id": "p3", "ok": False, "error": "boom"})
        self.assertEqual(of(acts, "script"), [])
        c.clips["bass"].paused = False
        c.clips["bass"].found = False
        acts = c.on_script({"v": 1, "type": "applied", "clip": "bass", "op_id": "p4", "ok": False, "error": "boom"})
        self.assertEqual(of(acts, "script"), [])
        # a re-found non-owned clip clears paused/heal state; the script's baseline then re-pushes
        c.clips["bass"].paused = True
        c.heal_sent["bass"] = "stale"
        acts = c.on_script({"v": 1, "type": "clip_found", "clip": "bass"})
        self.assertFalse(c.clips["bass"].paused)
        self.assertNotIn("bass", c.heal_sent)
        self.assertEqual(of(acts, "script", "apply_snapshot"), [])
        self.assertEqual(len(of(c.on_script(baseline("bass")), "script", "apply_snapshot")), 1)
        # the one-time heal is still sent in the healthy case
        acts = c.on_script({"v": 1, "type": "applied", "clip": "bass", "op_id": "p5", "ok": False,
                            "error": "content differs after apply"})
        heal = of(acts, "script", "apply_snapshot")
        self.assertEqual(len(heal), 1)
        self.assertNotIn("force", heal[0])

    def test_detect_lag_recorded(self):
        c, _, _ = bound_companion()
        wall = [5000.0]
        c.wall = lambda: wall[0]
        for lag in (0.010, 0.030, 0.020):
            c.on_script({"v": 1, "type": "local_delta", "clip": "drums", "added": [note(1)], "modified": [],
                         "removed": [], "t_detect": wall[0] - lag})
        self.assertEqual(len(c.detect_lag), 3)
        st = c.latency_stats()["detect_lag_s"]
        self.assertAlmostEqual(st["median"], 0.020, places=3)
        self.assertIn("detect→companion lag: n=3 median=20ms p95=30ms", c.status_text())
        # missing/invalid t_detect is simply not sampled
        c.on_script({"v": 1, "type": "local_delta", "clip": "drums", "added": [note(2)], "modified": [], "removed": []})
        self.assertEqual(len(c.detect_lag), 3)


# =========================================================== core: conflicts
class ConflictTests(unittest.TestCase):
    def test_conflict_pauses_forwarding_and_revert_reapplies(self):
        c, _, _ = bound_companion(notes={"drums": [], "bass": [note(1)]}, revs={"drums": 0, "bass": 1})
        acts = c.on_script({"v": 1, "type": "conflict", "clip": "bass"})
        self.assertTrue(c.clips["bass"].conflict)
        self.assertIn("owned by the other participant", of(acts, "script", "show_message")[0]["msg"])
        self.assertIn("revert bass", prints(acts))
        acts = c.on_relay({"v": 1, "type": "op", "seq": 2, "rev": 2, "participant": PEER, "op_id": "p2", "clip": "bass",
                           "kind": "delta", "payload": {"added": [note(2)], "modified": [], "removed": []}})
        self.assertEqual(of(acts, "script"), [])            # paused
        self.assertEqual(set(c.clips["bass"].notes), {1, 2})  # but replica is current
        acts = c.on_command("revert bass")
        self.assertFalse(c.clips["bass"].conflict)
        snaps = of(acts, "script", "apply_snapshot")
        self.assertEqual(len(snaps), 1)
        self.assertEqual(sorted(n["id"] for n in snaps[0]["notes"]), [1, 2])
        self.assertIs(snaps[0]["force"], True)     # only revert/resync overwrite a local edit

    def test_conflict_on_owned_clip_corrects_the_script(self):
        c, _, _ = bound_companion()
        acts = c.on_script({"v": 1, "type": "conflict", "clip": "drums"})
        self.assertEqual(acts, [("script", {"type": "set_owned", "clip": "drums", "owned": True})])
        self.assertFalse(c.clips["drums"].conflict)

    def test_conflict_is_idempotent(self):
        c, _, _ = bound_companion(notes={"drums": [], "bass": [note(1)]}, revs={"drums": 0, "bass": 1})
        acts = c.on_script({"v": 1, "type": "conflict", "clip": "bass"})
        self.assertEqual(len(of(acts, "script", "show_message")), 1)
        self.assertTrue(c.clips["bass"].conflict)
        self.assertEqual(c.on_script({"v": 1, "type": "conflict", "clip": "bass"}), [])
        self.assertEqual(c.on_script({"v": 1, "type": "applied", "clip": "bass", "op_id": "p1", "ok": False,
                                      "error": "conflict"}), [])
        # a local_delta on the non-owned clip keeps correcting the script's ownership, but says nothing new
        acts = c.on_script({"v": 1, "type": "local_delta", "clip": "bass", "added": [note(2)], "modified": [],
                            "removed": []})
        self.assertEqual(acts, [("script", {"type": "set_owned", "clip": "bass", "owned": False})])

    def test_set_owned_self_correction(self):
        c, _, _ = bound_companion(notes={"drums": [], "bass": [note(1)]}, revs={"drums": 0, "bass": 1})
        # the script reports the wrong owned flag with its snapshot: corrected first, content still used
        acts = c.on_script({"v": 1, "type": "local_snapshot", "clip": "drums", "notes": [note(5)], "owned": False})
        self.assertEqual(of(acts, "script"), [{"type": "set_owned", "clip": "drums", "owned": True}])
        self.assertEqual(of(acts, "relay", "op")[0]["kind"], "snapshot")
        acts = c.on_script({"v": 1, "type": "local_snapshot", "clip": "bass", "notes": [], "owned": True})
        self.assertEqual([m["type"] for m in of(acts, "script")], ["set_owned", "apply_snapshot"])
        self.assertEqual(of(acts, "script")[0]["owned"], False)
        self.assertEqual(of(acts, "relay"), [])
        # a local_delta for a clip we do not own: set_owned(False) + conflict
        acts = c.on_script({"v": 1, "type": "local_delta", "clip": "bass", "added": [note(2)], "modified": [],
                            "removed": []})
        self.assertEqual(of(acts, "script")[0], {"type": "set_owned", "clip": "bass", "owned": False})
        self.assertTrue(c.clips["bass"].conflict)
        # clip_found always restates ownership; a non-owned clip is not pushed (baseline drives it)
        acts = c.on_script({"v": 1, "type": "clip_found", "clip": "bass"})
        self.assertEqual(of(acts, "script"), [{"type": "set_owned", "clip": "bass", "owned": False}])
        acts = c.on_script({"v": 1, "type": "clip_found", "clip": "drums"})
        self.assertEqual(of(acts, "script"), [{"type": "set_owned", "clip": "drums", "owned": True}])
        # without a bound script nothing is sent and the owned clip is remembered as dirty
        c.on_script_disconnected()
        self.assertEqual(c.on_relay({"v": 1, "type": "ownership", "clip": "bass", "owner": ME}), [
            ("print", "ownership: bass -> you")])
        self.assertTrue(c.clips["bass"].dirty)

    def test_claim_flow(self):
        c, _, _ = bound_companion()
        c.on_script({"v": 1, "type": "conflict", "clip": "bass"})
        acts = c.on_command("claim bass")
        self.assertEqual(of(acts, "relay"), [{"type": "claim", "clip": "bass"}])
        acts = c.on_relay({"v": 1, "type": "ownership", "clip": "bass", "owner": ME})
        self.assertTrue(c.clips["bass"].owned)
        self.assertFalse(c.clips["bass"].conflict)
        # the script learns about it first and answers set_owned with its own local_snapshot
        self.assertEqual(of(acts, "script"), [{"type": "set_owned", "clip": "bass", "owned": True}])
        acts = c.on_script({"v": 1, "type": "local_snapshot", "clip": "bass", "notes": [note(9)], "owned": True})
        self.assertEqual(of(acts, "relay", "op")[0]["kind"], "snapshot")
        # ownership moves away -> set_owned only; the script answers with its baseline and _baseline
        # decides (equal content -> harmless init push, no conflict; differing -> CONFLICT)
        acts = c.on_relay({"v": 1, "type": "ownership", "clip": "bass", "owner": PEER})
        self.assertFalse(c.clips["bass"].owned)
        self.assertEqual(of(acts, "script"), [{"type": "set_owned", "clip": "bass", "owned": False}])
        acts = c.on_script(baseline("bass", [note(77)]))
        self.assertFalse(c.clips["bass"].conflict)
        snaps = of(acts, "script", "apply_snapshot")
        self.assertEqual(len(snaps), 1)
        self.assertEqual(snaps[0]["notes"][0]["id"], 9)
        self.assertNotIn("force", snaps[0])
        acts = c.on_script(baseline("bass", [note(77, 40)]))
        self.assertTrue(c.clips["bass"].conflict)
        self.assertEqual(of(acts, "script", "apply_snapshot"), [])

    def test_ownership_gained_while_clip_unavailable_still_tells_the_script(self):
        c, _, _ = bound_companion()
        c.on_script({"v": 1, "type": "clip_lost", "clip": "bass", "reason": "deleted"})
        acts = c.on_relay({"v": 1, "type": "ownership", "clip": "bass", "owner": ME})
        # the script keeps the flag and snapshots on its own when the clip re-appears
        self.assertEqual(of(acts, "script"), [{"type": "set_owned", "clip": "bass", "owned": True}])
        self.assertFalse(c.clips["bass"].dirty)
        acts = c.on_script({"v": 1, "type": "clip_found", "clip": "bass"})
        self.assertEqual(of(acts, "script"), [{"type": "set_owned", "clip": "bass", "owned": True}])
        # with unsent edits the snapshot is requested explicitly
        c.clips["bass"].dirty = True
        acts = c.on_script({"v": 1, "type": "clip_found", "clip": "bass"})
        self.assertEqual(of(acts, "script", "request_snapshot"), [{"type": "request_snapshot", "clip": "bass"}])
        self.assertFalse(c.clips["bass"].dirty)

    def test_setup_command(self):
        c = core.Companion(ME, SECRET, create_clips=core.TEMPLATE_CLIPS)
        self.assertIn("needs", prints(c.on_command("setup")))
        c.on_relay_connected()
        c.on_relay(welcome())
        c.on_script_connected()
        c.on_script(hello("/tmp/s.als"))
        acts = c.on_command("setup")
        st = of(acts, "script", "setup_template")
        self.assertEqual(len(st), 1)
        self.assertEqual({x["clip"]: x["owned"] for x in st[0]["clips"]}, {"drums": True, "bass": False})
        self.assertEqual({x["length"] for x in st[0]["clips"]}, {16.0})   # 4 bars in 4/4
        # a different set refuses setup until rebind
        c.on_script(hello("/tmp/other.als"))
        acts = c.on_command("setup")
        self.assertEqual(of(acts, "script"), [])
        self.assertIn("rebind", prints(acts))

    def test_release(self):
        c, _, _ = bound_companion()
        self.assertEqual(of(c.on_command("release drums"), "relay"), [{"type": "release", "clip": "drums"}])
        self.assertIn("usage", prints(c.on_command("claim nope")))


# =========================================================== core: reconnect
class ReconnectTests(unittest.TestCase):
    def test_disconnect_coalesces_local_edits(self):
        c, _, _ = bound_companion()
        c.on_relay_disconnected()
        for i in range(100):
            acts = c.on_script({"v": 1, "type": "local_delta", "clip": "drums", "added": [note(i)], "modified": [],
                                "removed": []})
            self.assertEqual(of(acts, "relay"), [])
        self.assertEqual(len(c.pending), 0)
        self.assertTrue(c.clips["drums"].dirty)
        acts = c.on_relay_connected()
        self.assertEqual(of(acts, "relay")[0]["type"], "hello")
        self.assertEqual(of(acts, "relay")[0]["last_seq"], c.last_seq)
        acts = c.on_relay(welcome(seq=0, ops=[]))
        reqs = of(acts, "script", "request_snapshot")
        self.assertEqual(reqs, [{"type": "request_snapshot", "clip": "drums"}])   # exactly one per owned clip
        self.assertEqual(len(of(acts, "script", "apply_snapshot")), 1)            # bass re-pushed from replica
        self.assertLessEqual(len(c.pending), core.MAX_PENDING)

    def test_lost_ownership_offline_with_unsent_edits_enters_conflict(self):
        c, _, _ = bound_companion(notes={"drums": [note(1)], "bass": []}, revs={"drums": 1, "bass": 0})
        c.on_relay_disconnected()
        c.on_script({"v": 1, "type": "local_delta", "clip": "drums", "added": [note(2)], "modified": [], "removed": []})
        self.assertTrue(c.clips["drums"].dirty)
        c.on_relay_connected()
        # the peer claimed drums while we were offline
        w = welcome(seq=3, ops=None, resync=True, notes={"drums": [note(1)], "bass": []}, revs={"drums": 1, "bass": 0})
        w["snapshot"]["clips"][0]["owner"] = PEER
        acts = c.on_relay(w)
        d = c.clips["drums"]
        self.assertFalse(d.owned)
        self.assertTrue(d.conflict)
        self.assertFalse(d.dirty)
        msgs = of(acts, "script")
        self.assertEqual(msgs[0], {"type": "set_owned", "clip": "drums", "owned": False})
        self.assertEqual([m["clip"] for m in msgs if m["type"] == "apply_snapshot"], ["bass"])   # drums NOT pushed
        self.assertEqual(of(acts, "script", "request_snapshot"), [])
        self.assertIn("unsent local edits", prints(acts))
        self.assertIn("revert drums", prints(acts))
        self.assertIn("now owned by the other participant", of(acts, "script", "show_message")[0]["msg"])
        # the user decides: revert pushes the session version with force
        snap = of(c.on_command("revert drums"), "script", "apply_snapshot")[0]
        self.assertEqual(([n["id"] for n in snap["notes"]], snap["force"]), ([1], True))
        # ownership gained while offline (bound, clip found): set_owned, no request_snapshot
        w = welcome(seq=4, ops=None, resync=True)
        acts = c.on_relay(w)
        msgs = of(acts, "script")
        self.assertIn({"type": "set_owned", "clip": "drums", "owned": True}, msgs)
        self.assertEqual([m for m in msgs if m["type"] == "request_snapshot"], [])
        self.assertTrue(c.clips["drums"].owned)
        self.assertFalse(c.clips["drums"].conflict)
        # ownership lost at runtime with nothing unsent: plain push (covered in test_claim_flow);
        # lost at runtime while dirty (relay unreachable then back): conflict, no push
        c.on_relay_disconnected()
        c.on_script({"v": 1, "type": "local_delta", "clip": "drums", "added": [note(3)], "modified": [], "removed": []})
        c.on_relay_connected()
        c.on_relay(welcome(seq=5, ops=[]))     # still ours after this welcome
        c.clips["drums"].dirty = True
        acts = c.on_relay({"v": 1, "type": "ownership", "clip": "drums", "owner": PEER})
        self.assertTrue(c.clips["drums"].conflict)
        self.assertEqual([m["type"] for m in of(acts, "script")], ["set_owned", "show_message"])

    def test_invalid_welcome_ignored(self):
        c = core.Companion(ME, SECRET, create_clips=core.TEMPLATE_CLIPS)
        c.on_relay_connected()
        for bad in (dict(session="../../x"), dict(session="/tmp/evil"), dict(session=None), dict(session=["S1"]),
                    dict(participant=5), dict(role=None), dict(code=7)):
            acts = c.on_relay(dict(welcome(), **bad))
            self.assertEqual(acts, [("print", "relay sent an invalid welcome; ignoring")], bad)
            self.assertFalse(c.welcomed)
            self.assertIsNone(c.session)
            self.assertEqual(c.clips, {})
        acts = c.on_relay(dict(welcome(), code=None))     # code may be absent (guest after the code is retired)
        self.assertTrue(c.welcomed)
        self.assertEqual(c.session, "S1")
        self.assertTrue(any(a[0] == "persist" for a in acts))

    def test_welcome_seq_zero_after_relay_restart(self):
        c, _, _ = bound_companion()
        c.on_relay({"v": 1, "type": "op", "seq": 7, "rev": 1, "participant": PEER, "op_id": "old-7", "clip": "bass",
                    "kind": "snapshot", "payload": {"notes": [note(1)]}})
        self.assertEqual(c.last_seq, 7)
        c.on_relay_disconnected()
        c.on_relay_connected()
        acts = c.on_relay(welcome(seq=0, ops=None, resync=True))
        self.assertEqual(c.last_seq, 0)
        self.assertEqual(len(c.seen_ops), 0)
        self.assertTrue(any(a[0] == "persist" and a[1]["last_seq"] == 0 for a in acts))
        # ops from the restarted relay (low seq, reused op_id) are applied again
        acts = c.on_relay({"v": 1, "type": "op", "seq": 1, "rev": 1, "participant": PEER, "op_id": "old-7", "clip": "bass",
                           "kind": "snapshot", "payload": {"notes": [note(2)]}})
        self.assertEqual(len(of(acts, "script", "apply_snapshot")), 1)
        self.assertEqual(c.last_seq, 1)

    def test_pending_bounded(self):
        c, _, _ = bound_companion()
        for i in range(core.MAX_PENDING + 50):
            c.on_script({"v": 1, "type": "local_delta", "clip": "drums", "added": [note(i)], "modified": [],
                         "removed": []})
        self.assertEqual(len(c.pending), core.MAX_PENDING)

    def test_welcome_reconnect_with_ops(self):
        c, _, _ = bound_companion()
        c.on_relay_disconnected()
        c.on_relay_connected()
        ops = [{"seq": 1, "rev": 1, "participant": PEER, "op_id": "r1", "clip": "bass", "kind": "snapshot",
                "payload": {"notes": [note(1)]}},
               {"seq": 2, "rev": 2, "participant": PEER, "op_id": "r2", "clip": "bass", "kind": "delta",
                "payload": {"added": [note(2)], "modified": [], "removed": []}}]
        acts = c.on_relay(welcome(seq=2, ops=ops, notes={"drums": [], "bass": [note(1), note(2)]},
                                  revs={"drums": 0, "bass": 2}))
        self.assertEqual(c.last_seq, 2)
        self.assertEqual(c.clips["bass"].rev, 2)
        self.assertEqual(set(c.clips["bass"].notes), {1, 2})
        self.assertIn("applied 2 missed ops", prints(acts))
        self.assertEqual(of(acts, "script", "apply_delta"), [])
        snaps = of(acts, "script", "apply_snapshot")
        self.assertEqual([s["clip"] for s in snaps], ["bass"])
        self.assertEqual(sorted(n["id"] for n in snaps[0]["notes"]), [1, 2])
        # a later live op with an already-seen seq is dropped
        self.assertEqual(c.on_relay(dict(ops[1], v=1, type="op")), [])

    def test_welcome_reconnect_resync_uses_snapshot(self):
        c, _, _ = bound_companion()
        c.clips["bass"].notes = {99: note(99)}
        c.on_relay_disconnected()
        c.on_relay_connected()
        acts = c.on_relay(welcome(seq=40, ops=None, resync=True, notes={"drums": [note(3)], "bass": [note(1)]},
                                  revs={"drums": 5, "bass": 7}))
        self.assertIn("full resync", prints(acts))
        self.assertEqual(c.last_seq, 40)
        self.assertEqual(set(c.clips["bass"].notes), {1})
        self.assertEqual((c.clips["drums"].rev, c.clips["bass"].rev), (5, 7))
        self.assertTrue(any(a[0] == "persist" and a[1]["last_seq"] == 40 for a in acts))

    def test_first_welcome_on_resume_uses_snapshot_even_with_ops(self):
        c = core.Companion(ME, SECRET, session="S1", last_seq=3)
        c.on_relay_connected()
        ops = [{"seq": 4, "rev": 1, "participant": PEER, "op_id": "r1", "clip": "bass", "kind": "delta",
                "payload": {"added": [note(2)], "modified": [], "removed": []}}]
        c.on_relay(welcome(seq=4, ops=ops, notes={"drums": [], "bass": [note(1), note(2)]}, revs={"drums": 0, "bass": 1}))
        self.assertEqual(set(c.clips["bass"].notes), {1, 2})
        self.assertEqual(c.last_seq, 4)

    def test_script_disconnect_marks_lost_and_rebinds_on_new_hello(self):
        c, _, _ = bound_companion()
        c.on_script_disconnected()
        self.assertFalse(c.bound)
        self.assertFalse(any(x.found for x in c.clips.values()))
        c.on_script_connected()
        acts = c.on_script(hello())
        self.assertEqual(len(of(acts, "script", "bind")), 1)

    def test_clip_lost_found(self):
        c, _, _ = bound_companion()
        c.on_script({"v": 1, "type": "clip_lost", "clip": "bass", "reason": "deleted"})
        self.assertFalse(c.clips["bass"].found)
        acts = c.on_relay({"v": 1, "type": "op", "seq": 1, "rev": 1, "participant": PEER, "op_id": "p1", "clip": "bass",
                           "kind": "snapshot", "payload": {"notes": [note(1)]}})
        self.assertEqual(of(acts, "script"), [])
        acts = c.on_script({"v": 1, "type": "clip_found", "clip": "bass"})
        self.assertEqual(of(acts, "script"), [{"type": "set_owned", "clip": "bass", "owned": False}])
        acts = c.on_script(baseline("bass"))     # the script's baseline after re-binding drives the push
        self.assertEqual(of(acts, "script", "apply_snapshot")[0]["notes"][0]["id"], 1)
        acts = c.on_script({"v": 1, "type": "clip_found", "clip": "drums"})
        self.assertEqual(of(acts, "script"), [{"type": "set_owned", "clip": "drums", "owned": True}])

    def test_tick_pings_and_times_out_pending(self):
        c, clock, _ = bound_companion()
        self.assertEqual(c.on_tick(clock.t + 1), [])
        self.assertEqual(of(c.on_tick(clock.t + 31), "relay"), [{"type": "ping"}])
        c.on_script({"v": 1, "type": "local_snapshot", "clip": "drums", "notes": []})
        acts = c.on_tick(clock.t + 100)
        self.assertEqual(len(c.pending), 0)
        self.assertIn("no ack", prints(acts))

    def test_status_and_unknown_messages(self):
        c, _, _ = bound_companion()
        self.assertEqual(c.on_relay({"v": 1, "type": "pong"}), [])
        self.assertEqual(c.on_relay({"v": 1, "type": "bogus"}), [])
        self.assertEqual(c.on_script({"v": 1, "type": "nonsense", "clip": "drums"}), [])
        self.assertIn("relay: connected", prints(c.on_command("status")))
        self.assertEqual(c.on_command("quit"), [("quit",)])
        self.assertIn("unknown command", prints(c.on_command("frobnicate")))


# ================================================ core: baseline / ownership races
def relayed(seq, clip, kind, payload, op_id=None):
    return {"v": 1, "type": "op", "seq": seq, "rev": seq, "participant": PEER, "op_id": op_id or "p%d" % seq,
            "clip": clip, "kind": kind, "payload": payload}


def added(seq, clip, *notes):
    return relayed(seq, clip, "delta", {"added": list(notes), "modified": [], "removed": []})


def applied_ok(clip, op_id="x"):
    return {"v": 1, "type": "applied", "clip": clip, "op_id": op_id, "ok": True, "ms": 1}


class RaceTests(unittest.TestCase):
    def test_baseline_tolerates_a_lagging_clip_and_waits_before_forwarding(self):
        c, _, _ = bound_companion(notes={"drums": [], "bass": [note(7)]}, revs={"drums": 0, "bass": 1})
        c.on_script(applied_ok("bass"))                       # the init push landed: Live holds [7]
        b = c.clips["bass"]
        # (a) clip lost while the owner keeps editing: the replica advances, Live cannot follow
        c.on_script({"v": 1, "type": "clip_lost", "clip": "bass", "reason": "deleted"})
        self.assertEqual(of(c.on_relay(added(2, "bass", note(8))), "script"), [])
        acts = c.on_script({"v": 1, "type": "clip_found", "clip": "bass"})
        self.assertEqual(of(acts, "script"), [{"type": "set_owned", "clip": "bass", "owned": False}])
        self.assertTrue(b.awaiting_baseline)
        # (b) an op relayed before the baseline arrives is applied to the replica but NOT forwarded
        acts = c.on_relay(added(3, "bass", note(9)))
        self.assertEqual(of(acts, "script"), [])
        self.assertEqual(set(b.notes), {7, 8, 9})
        # the baseline shows the OLD content (same content, Live's own ids): no edit happened -> init push
        acts = c.on_script(baseline("bass", [note(70)]))
        self.assertFalse(b.conflict)
        self.assertFalse(b.awaiting_baseline)
        snaps = of(acts, "script", "apply_snapshot")
        self.assertEqual(sorted(n["id"] for n in snaps[0]["notes"]), [7, 8, 9])
        self.assertNotIn("force", snaps[0])
        c.on_script(applied_ok("bass", snaps[0]["op_id"]))
        # forwarding resumes
        self.assertEqual(len(of(c.on_relay(added(4, "bass", note(10))), "script", "apply_delta")), 1)
        c.on_script(applied_ok("bass"))
        # (c) Live re-instantiates the script: same race through inventory
        self.assertEqual(len(of(c.on_script(hello()), "script", "bind")), 1)
        c.on_relay(added(5, "bass", note(11)))
        c.on_script(inventory())
        self.assertTrue(b.awaiting_baseline)
        self.assertEqual(of(c.on_relay(added(6, "bass", note(12))), "script"), [])
        acts = c.on_script(baseline("bass", [note(7), note(8), note(9), note(10)]))
        self.assertFalse(b.conflict)
        self.assertEqual(sorted(n["id"] for n in of(acts, "script", "apply_snapshot")[0]["notes"]), [7, 8, 9, 10, 11, 12])
        c.on_script(applied_ok("bass"))
        # Live restarted altogether
        c.on_script_disconnected()
        c.on_relay(added(7, "bass", note(13)))
        c.on_script_connected()
        c.on_script(hello())
        c.on_script(inventory())
        acts = c.on_script(baseline("bass", [note(i) for i in range(7, 13)]))
        self.assertFalse(b.conflict)
        self.assertEqual(len(of(acts, "script", "apply_snapshot")), 1)
        # a baseline that matches neither the replica nor what Live was known to hold is a real edit
        acts = c.on_script(baseline("bass", [note(5, 40)]))
        self.assertTrue(b.conflict)
        self.assertEqual(of(acts, "script", "apply_snapshot"), [])

    def test_unsent_delete_all_survives_an_empty_baseline_after_ownership_loss(self):
        c, _, _ = bound_companion(notes={"drums": [note(1)], "bass": []}, revs={"drums": 1, "bass": 0})
        c.on_relay_disconnected()
        c.on_script({"v": 1, "type": "local_delta", "clip": "drums", "added": [], "modified": [], "removed": [1]})
        c.on_relay_connected()
        w = welcome(seq=3, ops=None, resync=True, notes={"drums": [note(1)], "bass": []}, revs={"drums": 1, "bass": 0})
        w["snapshot"]["clips"][0]["owner"] = PEER      # the peer took drums while we were offline
        c.on_relay(w)
        d = c.clips["drums"]
        self.assertTrue(d.conflict and d.unsent_conflict)
        # the script's baseline is the emptied clip: still our unsent edit, not "nothing to lose"
        acts = c.on_script(baseline("drums", []))
        self.assertEqual(of(acts, "script", "apply_snapshot"), [])
        self.assertTrue(d.conflict)
        self.assertEqual(of(c.on_relay(relayed(4, "drums", "snapshot", {"notes": [note(1), note(2)]})), "script"), [])
        # local content equal to the replica: nothing unsent any more
        acts = c.on_script(baseline("drums", [note(1), note(2)]))
        self.assertFalse(d.conflict or d.unsent_conflict)
        self.assertEqual(len(of(acts, "script", "apply_snapshot")), 1)
        # revert drops the flag too
        d.conflict = d.unsent_conflict = True
        snap = of(c.on_command("revert drums"), "script", "apply_snapshot")[0]
        self.assertTrue(snap["force"])
        self.assertFalse(d.unsent_conflict)

    def test_revert_typed_while_clip_unavailable_is_applied_later(self):
        c, _, _ = bound_companion(notes={"drums": [], "bass": [note(1)]}, revs={"drums": 0, "bass": 1})
        c.on_script({"v": 1, "type": "conflict", "clip": "bass"})
        c.on_script({"v": 1, "type": "clip_lost", "clip": "bass", "reason": "deleted"})
        acts = c.on_command("revert bass")
        self.assertIn("will re-apply", prints(acts))
        self.assertEqual(of(acts, "script"), [])
        b = c.clips["bass"]
        self.assertTrue(b.revert_pending)
        c.on_relay(added(2, "bass", note(2)))
        c.on_script({"v": 1, "type": "clip_found", "clip": "bass"})
        # differing local content would normally be a conflict; the user already chose revert
        acts = c.on_script(baseline("bass", [note(5, 40)]))
        snaps = of(acts, "script", "apply_snapshot")
        self.assertEqual(len(snaps), 1)
        self.assertIs(snaps[0]["force"], True)
        self.assertTrue(snaps[0]["op_id"].startswith("revert-bass-"))
        self.assertEqual(sorted(n["id"] for n in snaps[0]["notes"]), [1, 2])
        self.assertFalse(b.conflict or b.revert_pending)
        # resync while unavailable is remembered the same way; gaining ownership forgets it
        c.on_script({"v": 1, "type": "clip_lost", "clip": "bass", "reason": "deleted"})
        c.on_command("resync")
        self.assertTrue(b.revert_pending)
        c.on_relay({"v": 1, "type": "ownership", "clip": "bass", "owner": ME})
        self.assertFalse(b.revert_pending)

    def test_op_rejected_after_ownership_change_rolls_back_and_conflicts(self):
        c, _, _ = bound_companion(notes={"drums": [note(1)], "bass": []}, revs={"drums": 1, "bass": 0})
        d = c.clips["drums"]
        op1 = of(c.on_script({"v": 1, "type": "local_delta", "clip": "drums", "added": [note(2)], "modified": [],
                              "removed": []}), "relay", "op")[0]["op_id"]
        op2 = of(c.on_script({"v": 1, "type": "local_delta", "clip": "drums", "added": [note(3)], "modified": [],
                              "removed": []}), "relay", "op")[0]["op_id"]
        self.assertEqual((set(d.notes), d.rev), ({1, 2, 3}, 3))
        c.on_relay({"v": 1, "type": "ownership", "clip": "drums", "owner": PEER})   # release/claim raced our edits
        self.assertFalse(d.owned or d.conflict)
        acts = c.on_relay({"v": 1, "type": "reject", "op_id": op1, "reason": "not owner of clip drums"})
        self.assertEqual((set(d.notes), d.rev), ({1}, 1))          # the relay never saw the edits
        self.assertEqual(len(c.pending), 0)                         # op2 is doomed the same way
        self.assertTrue(d.conflict and d.unsent_conflict)
        self.assertIn("ownership changed", prints(acts))
        self.assertIn("revert drums", prints(acts))
        self.assertEqual(len(of(acts, "script", "show_message")), 1)
        acts = c.on_relay({"v": 1, "type": "reject", "op_id": op2, "reason": "not owner of clip drums"})
        self.assertEqual(of(acts, "script"), [])
        # the baseline the script sends after set_owned(False) keeps the edit; the new owner's
        # snapshot is held instead of wiping it
        acts = c.on_script(baseline("drums", [note(1), note(2), note(3)]))
        self.assertEqual(of(acts, "script", "apply_snapshot"), [])
        self.assertTrue(d.conflict)
        self.assertEqual(of(c.on_relay(relayed(5, "drums", "snapshot", {"notes": [note(9)]})), "script"), [])
        # reject arriving before the ownership message: rolled back and remembered as unsent
        c2, _, _ = bound_companion()
        op = of(c2.on_script({"v": 1, "type": "local_delta", "clip": "drums", "added": [note(1)], "modified": [],
                              "removed": []}), "relay", "op")[0]["op_id"]
        acts = c2.on_relay({"v": 1, "type": "reject", "op_id": op, "reason": "not owner of clip drums"})
        self.assertEqual(c2.clips["drums"].notes, {})
        self.assertTrue(c2.clips["drums"].dirty)
        self.assertFalse(c2.clips["drums"].conflict)
        self.assertIn("not owner", prints(acts))
        acts = c2.on_relay({"v": 1, "type": "ownership", "clip": "drums", "owner": PEER})
        self.assertTrue(c2.clips["drums"].conflict and c2.clips["drums"].unsent_conflict)

    def test_unsendable_snapshot_leaves_replica_untouched_and_pauses(self):
        c, _, _ = bound_companion()
        d = c.clips["drums"]
        big = [dict(note(i), start=i / 3.0, dur=1 / 3.0, vel=100 / 3.0, prob=1 / 3.0, vel_dev=1 / 3.0, rel_vel=1 / 3.0)
               for i in range(protocol.MAX_NOTES_PER_OP)]
        with self.assertRaises(protocol.ProtocolError):     # valid notes, yet over the line limit
            protocol.encode({"type": "op", "payload": protocol.validate_op_payload("snapshot", {"notes": big})})
        acts = c.on_script({"v": 1, "type": "local_snapshot", "clip": "drums", "notes": big})
        self.assertEqual(of(acts, "relay"), [])
        self.assertEqual((d.notes, d.rev, len(c.pending)), ({}, 0, 0))
        self.assertTrue(d.dirty and d.paused)
        self.assertEqual(d.pause_reason, "too large")
        self.assertEqual(len(of(acts, "print")), 1)
        self.assertIn("too large to send", prints(acts))
        self.assertIn("PAUSED (too large)", c.status_text())
        # a snapshot that fits resumes syncing
        acts = c.on_script({"v": 1, "type": "local_snapshot", "clip": "drums", "notes": [note(1)]})
        self.assertEqual(of(acts, "relay", "op")[0]["kind"], "snapshot")
        self.assertFalse(d.paused or d.dirty)
        # the glue tells the user when a connected relay link refuses a message
        printed = []
        runner = anthem.SessionRunner(c, ("127.0.0.1", 1), out=printed.append)
        runner.relay = type("Dead", (), {"send": lambda self, msg: False})()
        runner.dispatch([("relay", {"type": "op"})])
        self.assertTrue(any(p.startswith("ERROR: could not send op") for p in printed), printed)

    def test_dead_relay_link_is_reported_once(self):
        c, clock, _ = bound_companion()
        drops = lambda acts: [a for a in acts if a[0] == "relay_drop"]
        clock.t += 50
        c.on_relay({"v": 1, "type": "pong"})                      # any inbound line counts
        self.assertEqual(drops(c.on_tick(clock.t + 89)), [])
        acts = c.on_tick(clock.t + 91)
        self.assertEqual(len(drops(acts)), 1)
        self.assertEqual(drops(acts)[0][1], "no data from relay for 91s")
        self.assertEqual(drops(c.on_tick(clock.t + 92)), [])       # said once; the glue closes the link
        c.on_relay_disconnected()
        self.assertEqual(drops(c.on_tick(clock.t + 1000)), [])


# ===================================================== integration (asyncio)
class FakeRelay(object):
    """Minimal relay speaking PROTOCOL.md, backed by anthem_core.session.Session."""

    def __init__(self, fail_hello=None):
        self.session = None
        self.server = None
        self.port = None
        self.conns = {}      # participant -> writer
        self.received = []   # all messages received, in order
        self.got = asyncio.Event()
        self.fail_hello = fail_hello   # error reason to answer every hello with

    async def start(self):
        self.server = await asyncio.start_server(self.handle, "127.0.0.1", 0)
        self.port = self.server.sockets[0].getsockname()[1]

    async def stop(self):
        self.server.close()
        await self.server.wait_closed()

    def send(self, writer, msg):
        writer.write(protocol.encode(msg))

    async def handle(self, reader, writer):
        pid = None
        while True:
            try:
                line = await reader.readline()
            except (ConnectionError, OSError):
                break
            if not line:
                break
            msg = protocol.decode(line.strip(), protocol.RELAY_TYPES)
            self.received.append(msg)
            self.got.set()
            t = msg["type"]
            if t == "create":
                self.session = Session("S-int", "FEEDBEEF", msg["clips"])
                self.session.add_participant(msg["participant"], msg["secret"], "host")
                pid = msg["participant"]
                self.session.set_online(pid, True)
                self.conns[pid] = writer
                self.send(writer, self.welcome(pid, 0))
            elif t == "hello" and self.fail_hello:
                self.send(writer, {"type": "error", "reason": self.fail_hello})
                break
            elif t == "hello":
                pid = msg["participant"]
                self.session.set_online(pid, True)
                self.conns[pid] = writer
                self.send(writer, self.welcome(pid, msg["last_seq"]))
            elif t == "op":
                op = protocol.validate_op(msg)
                try:
                    seq, rev, dup = self.session.apply_op(pid, op)
                except Exception as e:  # anthem_core.session.Rejected
                    self.send(writer, {"type": "reject", "op_id": op["op_id"], "reason": str(e)})
                    continue
                self.send(writer, {"type": "op_ack", "op_id": op["op_id"], "seq": seq, "rev": rev, "duplicate": dup})
            elif t == "ping":
                self.send(writer, {"type": "pong"})
        writer.close()

    def welcome(self, pid, last_seq):
        s = self.session
        ops = s.ops_since(last_seq)
        return {"type": "welcome", "session": s.id, "code": s.code, "participant": pid,
                "role": s.participants[pid]["role"], "seq": s.seq, "snapshot": s.snapshot(),
                "ops": ops, "resync": ops is None}

    def peer_op(self, pid, writer_pid, op_id, clip, kind, payload):
        """Simulate the other participant's op: apply at the relay and broadcast to writer_pid."""
        s = self.session
        seq, rev, _ = s.apply_op(pid, protocol.validate_op({"op_id": op_id, "clip": clip, "kind": kind,
                                                            "base_rev": s.revs[clip], "payload": payload}))
        self.send(self.conns[writer_pid], {"type": "op", "seq": seq, "rev": rev, "participant": pid, "op_id": op_id,
                                           "clip": clip, "kind": kind, "payload": payload})


class FakeScript(object):
    def __init__(self, port):
        self.port = port
        self.reader = self.writer = None
        self.inbox = []

    async def connect(self):
        self.reader, self.writer = await asyncio.open_connection("127.0.0.1", self.port)

    def send(self, msg):
        self.writer.write(protocol.encode(msg))

    def send_raw(self, data):
        self.writer.write(data)

    async def recv(self, mtype=None, timeout=3.0):
        async def inner():
            while True:
                line = await self.reader.readline()
                if not line:
                    raise ConnectionError("script link closed")
                msg = protocol.decode(line.strip(), protocol.LOCAL_TYPES)
                self.inbox.append(msg)
                if mtype is None or msg["type"] == mtype:
                    return msg
        return await asyncio.wait_for(inner(), timeout)

    async def close(self):
        self.writer.close()
        await self.writer.wait_closed()


async def wait_for(pred, timeout=3.0):
    async def inner():
        while not pred():
            await asyncio.sleep(0.01)
    await asyncio.wait_for(inner(), timeout)


def free_port():
    import socket
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    p = s.getsockname()[1]
    s.close()
    return p


class IntegrationTests(unittest.TestCase):
    def run_async(self, coro):
        return asyncio.run(asyncio.wait_for(coro, 20))

    def test_end_to_end_session_loop(self):
        self.run_async(self._end_to_end())

    async def _end_to_end(self):
        relay = FakeRelay()
        await relay.start()
        port = free_port()
        comp = core.Companion(ME, SECRET, create_clips=core.TEMPLATE_CLIPS)
        stdin = asyncio.Queue()
        persisted = []
        printed = []
        holder = []
        task = asyncio.ensure_future(anthem.run_session(
            comp, ("127.0.0.1", relay.port), port, stdin=stdin, persist=persisted.append,
            out=printed.append, runner=holder))
        try:
            await wait_for(lambda: holder and holder[0].ready.is_set())
            await wait_for(lambda: comp.welcomed)
            self.assertEqual(comp.session, "S-int")
            self.assertTrue(any("FEEDBEEF" in p for p in printed))

            self.assertEqual(oct(anthem.token_path(port).stat().st_mode & 0o777), oct(0o600))
            script = FakeScript(port)
            await script.connect()
            script.send(hello("/tmp/int.als", script_token(port)))
            bind = await script.recv("bind")
            self.assertEqual({x["clip"]: x["owned"] for x in bind["clips"]}, {"drums": True, "bass": False})
            script.send(inventory())
            script.send(baseline("bass"))     # owner still the "guest" role and replica empty: no init push

            # owned clip: local snapshot -> relay op -> ack -> rev 1
            script.send({"type": "local_snapshot", "clip": "drums", "notes": [note(1)]})
            await wait_for(lambda: comp.clips["drums"].rev == 1 and not comp.pending)
            self.assertEqual(relay.session.revs["drums"], 1)
            self.assertEqual(len(comp.ack_latencies), 1)
            script.send({"type": "local_delta", "clip": "drums", "added": [note(2, 64)], "modified": [], "removed": []})
            await wait_for(lambda: relay.session.revs["drums"] == 2 and not comp.pending)
            self.assertEqual(set(relay.session.state["drums"]), {1, 2})

            # malformed script message is ignored, loop survives
            script.send_raw(b"this is not json\n")
            script.send_raw(b'{"v":1,"type":"local_delta","clip":"drums","added":[{"id":-5}],"modified":[],"removed":[]}\n')
            await asyncio.sleep(0.05)

            # peer edits bass at the relay -> forwarded to Live
            relay.session.add_participant(PEER, "p" * 24, "guest")
            relay.peer_op(PEER, ME, "peer-1", "bass", "snapshot", {"notes": [note(7, 36)]})
            fwd = await script.recv("apply_snapshot")
            self.assertEqual((fwd["clip"], fwd["op_id"], fwd["notes"][0]["id"]), ("bass", "peer-1", 7))
            self.assertEqual(comp.clips["bass"].rev, 1)
            self.assertEqual([m for m in script.inbox if m["type"] == "apply_snapshot"], [fwd])
            self.assertNotIn("force", fwd)

            # status command via stdin
            stdin.put_nowait("status\n")
            await wait_for(lambda: any(p.startswith("relay: connected") for p in printed))
            self.assertTrue(persisted and persisted[0]["session"] == "S-int")

            stdin.put_nowait("quit\n")
            await asyncio.wait_for(task, 5)
            # debounced persist is flushed on shutdown
            self.assertEqual(persisted[-1]["set_path"], "/tmp/int.als")
            self.assertEqual(persisted[-1]["last_seq"], comp.last_seq)
            await script.close()
        finally:
            if not task.done():
                holder[0].stop()
                await asyncio.wait_for(task, 5)
            await relay.stop()

    def test_relay_reconnect_resends_snapshot(self):
        self.run_async(self._reconnect())

    async def _reconnect(self):
        relay = FakeRelay()
        await relay.start()
        port = free_port()
        comp = core.Companion(ME, SECRET, create_clips=core.TEMPLATE_CLIPS)
        holder = []
        printed = []
        task = asyncio.ensure_future(anthem.run_session(
            comp, ("127.0.0.1", relay.port), port, stdin=asyncio.Queue(), out=printed.append, runner=holder))
        try:
            await wait_for(lambda: comp.welcomed)
            script = FakeScript(port)
            await script.connect()
            script.send(hello("/tmp/r.als", script_token(port)))
            await script.recv("bind")
            script.send(inventory())
            script.send({"type": "local_snapshot", "clip": "drums", "notes": [note(1)]})
            await wait_for(lambda: relay.session.revs["drums"] == 1)

            # kill the relay connection from the relay side; edits while down are coalesced
            relay.conns[ME].close()
            await wait_for(lambda: not comp.relay_connected)
            for i in range(20):
                script.send({"type": "local_delta", "clip": "drums", "added": [note(10 + i)], "modified": [],
                             "removed": []})
            await wait_for(lambda: comp.clips["drums"].dirty)
            self.assertEqual(len(comp.pending), 0)

            # backoff reconnect (1 s) -> hello -> welcome -> exactly one request_snapshot for drums
            req = await script.recv("request_snapshot", timeout=6)
            self.assertEqual(req["clip"], "drums")
            self.assertTrue(comp.relay_connected and comp.welcomed)
            hellos = [m for m in relay.received if m["type"] == "hello"]
            self.assertEqual(len(hellos), 1)
            self.assertEqual([m["clip"] for m in script.inbox if m["type"] == "request_snapshot"], ["drums"])
            script.send({"type": "local_snapshot", "clip": "drums", "notes": [note(i) for i in range(5)]})
            await wait_for(lambda: relay.session.revs["drums"] == 2)
            self.assertEqual(len(relay.session.state["drums"]), 5)
            await script.close()
        finally:
            holder[0].stop()
            await asyncio.wait_for(task, 5)
            await relay.stop()

    async def _start(self, relay, port=None, comp=None, **kw):
        """Start a runner against `relay`; returns (comp, task, holder, printed, port)."""
        port = port or free_port()
        comp = comp or core.Companion(ME, SECRET, create_clips=core.TEMPLATE_CLIPS)
        holder, printed = [], []
        task = asyncio.ensure_future(anthem.run_session(
            comp, ("127.0.0.1", relay.port if relay else 1), port, stdin=asyncio.Queue(),
            out=printed.append, runner=holder, **kw))
        return comp, task, holder, printed, port

    async def _stop(self, task, holder, relay):
        if holder and not task.done():
            holder[0].stop()
        if holder:
            await asyncio.wait_for(task, 5)
        if relay:
            await relay.stop()

    def test_permanent_relay_error_stops_reconnecting(self):
        self.run_async(self._permanent_error())

    async def _permanent_error(self):
        relay = FakeRelay(fail_hello="unknown session")
        await relay.start()
        comp = core.Companion(ME, SECRET, session="S-gone", last_seq=0)
        comp, task, holder, printed, _ = await self._start(relay, comp=comp)
        try:
            await asyncio.wait_for(task, 5)      # stops by itself, no retry
            self.assertEqual(len([m for m in relay.received if m["type"] == "hello"]), 1)
            self.assertTrue(any("giving up (unknown session)" in p and "create" in p for p in printed), printed)
        finally:
            await self._stop(task, holder, relay)
        # the retrying class of errors keeps the loop alive
        self.assertIsNone(anthem.permanent_error_advice("participant already connected"))
        for r in ("unknown participant", "bad secret", "session too large", "create not allowed",
                  "unknown code", "session full", "role 'guest' unavailable"):
            self.assertIsNotNone(anthem.permanent_error_advice(r), r)
        self.assertIn("check the code", anthem.permanent_error_advice("unknown code"))

    def test_script_token_required(self):
        self.run_async(self._token())

    async def _token(self):
        relay = FakeRelay()
        await relay.start()
        comp, task, holder, printed, port = await self._start(relay)
        try:
            await wait_for(lambda: comp.welcomed)
            # no token / wrong token / other first message: closed, nothing processed
            for first in (hello("/tmp/t.als"), hello("/tmp/t.als", "wrong"), inventory()):
                bad = FakeScript(port)
                await bad.connect()
                bad.send(first)
                with self.assertRaises(ConnectionError):
                    await bad.recv()
                self.assertFalse(comp.script_connected)
            # the real script authenticates and binds
            good = FakeScript(port)
            await good.connect()
            good.send(hello("/tmp/t.als", script_token(port)))
            await good.recv("bind")
            self.assertTrue(comp.bound)
            # an unauthenticated newcomer does not displace the authenticated link
            bad = FakeScript(port)
            await bad.connect()
            bad.send(hello("/tmp/t.als", "nope"))
            with self.assertRaises(ConnectionError):
                await bad.recv()
            self.assertTrue(comp.script_connected and comp.bound)
            self.assertIs(holder[0].script is not None, True)
            good.send({"type": "log", "level": "info", "msg": "still here"})
            await wait_for(lambda: any("still here" in p for p in printed))
            await good.close()
        finally:
            await self._stop(task, holder, relay)

    def test_handler_bug_and_oversized_line_do_not_kill_loops(self):
        self.run_async(self._robust())

    async def _robust(self):
        relay = FakeRelay()
        await relay.start()
        comp, task, holder, printed, port = await self._start(relay)
        try:
            await wait_for(lambda: comp.welcomed)
            script = FakeScript(port)
            await script.connect()
            script.send(hello("/tmp/x.als", script_token(port)))
            await script.recv("bind")
            # a handler that raises must not drop the connection or the relay loop
            orig = comp.on_script
            comp.on_script = lambda m: (_ for _ in ()).throw(RuntimeError("bug")) if m["type"] == "clip_lost" else orig(m)
            script.send({"type": "clip_lost", "clip": "drums", "reason": "x"})
            script.send({"type": "log", "level": "info", "msg": "after-bug"})
            await wait_for(lambda: any("after-bug" in p for p in printed))
            self.assertIs(holder[0].script is not None, True)
            orig_relay = comp.on_relay
            comp.on_relay = lambda m: (_ for _ in ()).throw(RuntimeError("bug")) if m["type"] == "pong" else orig_relay(m)
            relay.send(relay.conns[ME], {"type": "pong"})
            relay.send(relay.conns[ME], {"type": "presence", "participants": [{"id": ME, "role": "host", "online": True}]})
            await wait_for(lambda: any(p.startswith("presence:") for p in printed))
            self.assertTrue(comp.relay_connected)
            # a line over the reader limit ends that link cleanly (no traceback, runner keeps going)
            script.send_raw(b'{"v":1,"type":"log","msg":"' + b"x" * (protocol.MAX_LINE_BYTES + 2048) + b'"}\n')
            await wait_for(lambda: not comp.script_connected)
            self.assertIsNone(holder[0].script)
            self.assertFalse(task.done())
            again = FakeScript(port)
            await again.connect()
            again.send(hello("/tmp/x.als", script_token(port)))
            await again.recv("bind")
            await again.close()
        finally:
            await self._stop(task, holder, relay)

    def test_dead_relay_link_is_closed_and_reconnected(self):
        self.run_async(self._dead_link())

    async def _dead_link(self):
        relay = FakeRelay()
        await relay.start()
        comp, task, holder, printed, _ = await self._start(relay)
        try:
            await wait_for(lambda: comp.welcomed)
            comp.last_rx = time.monotonic() - 10 * core.PING_INTERVAL    # the relay went silent (half-open TCP)
            await wait_for(lambda: not comp.relay_connected)
            self.assertTrue(any("no data from relay" in p for p in printed), printed)
            # the normal backoff path (1 s) reconnects with a hello
            await wait_for(lambda: comp.relay_connected and comp.welcomed, timeout=6)
            self.assertEqual(len([m for m in relay.received if m["type"] == "hello"]), 1)
            self.assertFalse(task.done())
        finally:
            await self._stop(task, holder, relay)

    def test_script_port_in_use_exits_cleanly(self):
        self.run_async(self._port_in_use())

    async def _port_in_use(self):
        import socket
        blocker = socket.socket()
        blocker.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        blocker.bind(("127.0.0.1", 0))
        blocker.listen(1)
        port = blocker.getsockname()[1]
        try:
            comp, task, holder, printed, _ = await self._start(None, port=port)
            ok = await asyncio.wait_for(task, 5)
            self.assertIs(ok, False)
            self.assertTrue(any(p == "port %d already in use — run the second companion with --script-port %d"
                                % (port, port + 10) for p in printed), printed)
            self.assertFalse(holder[0].ready.is_set())
        finally:
            blocker.close()


class CliTests(unittest.TestCase):
    def test_identity_roundtrip(self):
        import tempfile
        with tempfile.TemporaryDirectory() as d:
            old = anthem.SESSIONS_DIR
            anthem.SESSIONS_DIR = Path(d) / "sessions"
            try:
                anthem.save_identity({"session": "S9", "code": "C", "participant": "p-x", "secret": "s" * 24,
                                      "relay": "h:1", "tls": False, "role": "host", "set_path": None, "last_seq": 7})
                p = anthem.SESSIONS_DIR / "S9.json"
                self.assertEqual(oct(p.stat().st_mode & 0o777), oct(0o600))
                ids = anthem.load_identities()
                self.assertEqual(ids[0]["session"], "S9")
                self.assertEqual(ids[0]["last_seq"], 7)
            finally:
                anthem.SESSIONS_DIR = old

    def test_identity_path_rejects_hostile_session_ids(self):
        import tempfile
        with tempfile.TemporaryDirectory() as d:
            old = anthem.SESSIONS_DIR
            anthem.SESSIONS_DIR = Path(d) / "sessions"
            try:
                for bad in ("../../x", "/tmp/evil", "", "a" * 65, "x y", None, 3):
                    with self.assertRaises(ValueError, msg=repr(bad)):
                        anthem.identity_path(bad)
                    with self.assertRaises(ValueError, msg=repr(bad)):
                        anthem.save_identity({"session": bad, "participant": "p", "secret": "s" * 24})
                self.assertFalse(anthem.SESSIONS_DIR.exists())
                self.assertFalse((Path(d) / "x.json").exists())
                self.assertEqual(anthem.identity_path("ok_Id-1"), anthem.SESSIONS_DIR / "ok_Id-1.json")
            finally:
                anthem.SESSIONS_DIR = old

    def test_secret_files_and_dirs_are_private(self):
        import tempfile
        with tempfile.TemporaryDirectory() as d:
            old = anthem.ANTHEM_DIR, anthem.SESSIONS_DIR
            anthem.ANTHEM_DIR = Path(d) / "home" / ".anthem"
            anthem.SESSIONS_DIR = anthem.ANTHEM_DIR / "sessions"
            try:
                tok = anthem.write_token(9999)
                p = anthem.ANTHEM_DIR / "script-9999.token"
                self.assertEqual(p.read_text().strip(), tok)
                self.assertEqual(oct(p.stat().st_mode & 0o777), oct(0o600))
                self.assertEqual(oct(anthem.ANTHEM_DIR.stat().st_mode & 0o777), oct(0o700))
                os.chmod(str(anthem.ANTHEM_DIR), 0o755)          # an existing lax dir is tightened
                anthem.save_identity({"session": "S9", "participant": "p-x", "secret": "s" * 24})
                self.assertEqual(oct(anthem.ANTHEM_DIR.stat().st_mode & 0o777), oct(0o700))
                self.assertEqual(oct(anthem.SESSIONS_DIR.stat().st_mode & 0o777), oct(0o700))
                self.assertEqual(oct((anthem.SESSIONS_DIR / "S9.json").stat().st_mode & 0o777), oct(0o600))
                # a stale temp file from a crash does not block the exclusive create
                (anthem.SESSIONS_DIR / "S9.tmp").write_text("junk")
                anthem.save_identity({"session": "S9", "participant": "p-x", "secret": "t" * 24, "last_seq": 2})
                self.assertEqual(anthem.load_identities()[0]["last_seq"], 2)
                self.assertFalse((anthem.SESSIONS_DIR / "S9.tmp").exists())
            finally:
                anthem.ANTHEM_DIR, anthem.SESSIONS_DIR = old

    def test_script_token_comparison_never_raises(self):
        r = anthem.SessionRunner(core.Companion(ME, SECRET), ("h", 1))
        r.token = "secret-token"
        ok = lambda tok: r._script_authenticated(dict(hello("/a.als"), token=tok))  # noqa: E731
        self.assertTrue(ok("secret-token"))
        for bad in ("tökén", "secret-tokén", "", 123, None, ["secret-token"], b"secret-token"):
            self.assertFalse(ok(bad), repr(bad))
        self.assertFalse(r._script_authenticated(None))
        self.assertFalse(r._script_authenticated(dict(inventory(), token="secret-token")))

    def test_install_dest_safety(self):
        import contextlib
        import io
        import tempfile
        src = ROOT / "remote_script" / "Anthem"
        if not (src / "__init__.py").is_file():
            self.skipTest("Remote Script not built")
        with tempfile.TemporaryDirectory() as d:
            quiet = io.StringIO()
            # a folder with unrelated content is never deleted
            stray = Path(d) / "Remote Scripts"
            stray.mkdir()
            (stray / "Other.py").write_text("keep me")
            with self.assertRaises(SystemExit) as cm:
                anthem.main(["install", "--dest", str(stray)])
            self.assertIn("--dest %s" % (stray / "Anthem"), str(cm.exception))
            self.assertEqual((stray / "Other.py").read_text(), "keep me")
            self.assertEqual(sorted(p.name for p in stray.iterdir()), ["Other.py"])
            # a plain file is refused too
            f = Path(d) / "file"
            f.write_text("x")
            with self.assertRaises(SystemExit):
                anthem.main(["install", "--dest", str(f)])
            # new folder, empty folder and a previous install all work; stale files are cleaned
            for dest in (Path(d) / "new" / "Anthem", stray / "Anthem"):
                with contextlib.redirect_stdout(quiet):
                    anthem.main(["install", "--dest", str(dest)])
                self.assertTrue((dest / "clipbinding.py").is_file())
            (dest / "old_module.py").write_text("stale")
            with contextlib.redirect_stdout(quiet):
                anthem.main(["install", "--dest", str(dest)])
            self.assertFalse((dest / "old_module.py").exists())
            self.assertTrue((dest / "__init__.py").is_file())

    def test_parse_relay(self):
        self.assertEqual(anthem.parse_relay("relay.example.com:9801"), ("relay.example.com", 9801))
        self.assertEqual(anthem.parse_relay("[::1]:9801"), ("::1", 9801))
        with self.assertRaises(SystemExit):
            anthem.parse_relay("nope")

    def test_log_summary_never_includes_notes_or_secret(self):
        s = anthem._summarize({"type": "op", "op_id": "a", "clip": "drums", "kind": "snapshot", "base_rev": 0,
                               "payload": {"notes": [note(1)]}})
        self.assertIn("n=1", s)
        self.assertNotIn("pitch", s)
        s = anthem._summarize({"type": "hello", "participant": "p-x", "secret": "TOPSECRET", "session": "S"})
        self.assertNotIn("TOPSECRET", s)
        s = anthem._summarize({"type": "script_hello", "set_path": "/a.als", "token": "LOOPBACK"})
        self.assertNotIn("LOOPBACK", s)

    def test_create_token_cli_and_env(self):
        import unittest.mock as mock
        seen = []
        with mock.patch.object(anthem, "_run", lambda comp, relay, args, extra: seen.append(comp)):
            anthem.main(["create", "--relay", "h:1", "--create-token", "CLI-T"])
            with mock.patch.dict(os.environ, {"ANTHEM_CREATE_TOKEN": "ENV-T"}):
                anthem.main(["create", "--relay", "h:1"])
            anthem.main(["create", "--relay", "h:1"])
        self.assertEqual([c.create_token for c in seen], ["CLI-T", "ENV-T", None])


if __name__ == "__main__":
    unittest.main()
