"""Pure tests for anthem_core: protocol validation and session/ownership/ordering logic."""
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from anthem_core import protocol, session  # noqa: E402

N = {"id": 1, "pitch": 60, "start": 0.0, "dur": 1.0, "vel": 100.0, "mute": False}
CLIPS = [{"clip": "drums", "track_name": "Anthem Drums", "slot": 0, "owner": "host"},
         {"clip": "bass", "track_name": "Anthem Bass", "slot": 0, "owner": "guest"}]


def op(op_id, clip="drums", kind="delta", base_rev=0, **payload):
    if kind == "delta":
        payload = {"added": payload.get("added", []), "modified": payload.get("modified", []),
                   "removed": payload.get("removed", [])}
    return protocol.validate_op({"op_id": op_id, "clip": clip, "kind": kind, "base_rev": base_rev, "payload": payload})


def make_session():
    s = session.Session("s1", session.new_code(), CLIPS)
    s.add_participant("host1", "h" * 20, "host")
    s.add_participant("guest1", "g" * 20, "guest")
    return s


# --- protocol -------------------------------------------------------------------
def test_encode_decode_roundtrip_and_version():
    line = protocol.encode({"type": "ping"})
    assert line.endswith(b"\n") and protocol.decode(line, protocol.RELAY_TYPES)["type"] == "ping"
    with pytest.raises(protocol.ProtocolError):
        protocol.decode(b'{"v":2,"type":"ping"}', protocol.RELAY_TYPES)
    with pytest.raises(protocol.ProtocolError):
        protocol.decode(b'{"v":1,"type":"exec"}', protocol.RELAY_TYPES)
    with pytest.raises(protocol.ProtocolError):
        protocol.decode(b'[1,2]', protocol.RELAY_TYPES)
    with pytest.raises(protocol.ProtocolError):
        protocol.decode(b'\xff\xfe', protocol.RELAY_TYPES)


def test_oversized_line_rejected_both_ways():
    big = {"type": "op", "payload": "x" * protocol.MAX_LINE_BYTES}
    with pytest.raises(protocol.ProtocolError):
        protocol.encode(big)
    with pytest.raises(protocol.ProtocolError):
        protocol.decode(b"{" + b" " * protocol.MAX_LINE_BYTES + b"}", protocol.RELAY_TYPES)


@pytest.mark.parametrize("bad", [
    dict(N, pitch=128), dict(N, pitch=-1), dict(N, pitch=60.5), dict(N, dur=0), dict(N, dur=-1),
    dict(N, start=float("nan")), dict(N, vel=200), dict(N, mute="no"), dict(N, prob=2), dict(N, id=-1),
    dict(N, id="1"), "not a dict", dict(N, start=1e9),
])
def test_invalid_notes(bad):
    with pytest.raises(protocol.ProtocolError):
        protocol.validate_note(bad)


def test_note_defaults_normalized():
    n = protocol.validate_note(N)
    assert n == {"id": 1, "pitch": 60, "start": 0.0, "dur": 1.0, "vel": 100.0, "mute": False,
                 "prob": 1.0, "vel_dev": 0.0, "rel_vel": 64.0}


def test_validate_op_rules():
    with pytest.raises(protocol.ProtocolError):
        op("o1")                                   # empty delta
    with pytest.raises(protocol.ProtocolError):
        op("bad id!", added=[N])
    with pytest.raises(protocol.ProtocolError):
        protocol.validate_op({"op_id": "o", "clip": "drums", "kind": "exec", "base_rev": 0, "payload": {}})
    with pytest.raises(protocol.ProtocolError):
        op("o1", kind="snapshot", notes=[N, N])    # duplicate ids
    with pytest.raises(protocol.ProtocolError):
        op("o1", kind="snapshot", notes=[N] * 0 + [dict(N, id=i) for i in range(protocol.MAX_NOTES_PER_OP + 1)])
    with pytest.raises(protocol.ProtocolError):
        op("o1", removed=["1"])
    assert op("o1", removed=[1])["payload"] == {"added": [], "modified": [], "removed": [1]}


# --- session --------------------------------------------------------------------
def test_roles_resolve_owners_and_limits():
    s = make_session()
    assert s.owner_of("drums") == "host1" and s.owner_of("bass") == "guest1"
    with pytest.raises(session.Rejected):
        s.add_participant("third", "t" * 20, "guest")
    with pytest.raises(session.Rejected):
        session.Session("s2", "C0DE", CLIPS).add_participant("x", "short", "host")
    s.authenticate("host1", "h" * 20)
    with pytest.raises(session.Rejected):
        s.authenticate("host1", "wrong" * 5)
    with pytest.raises(session.Rejected):
        s.authenticate("nobody", "h" * 20)


def test_ownership_enforced_and_state_preserved_on_reject():
    s = make_session()
    s.apply_op("host1", op("o1", added=[N]))
    before = s.snapshot()
    with pytest.raises(session.Rejected, match="not owner"):
        s.apply_op("guest1", op("o2", added=[dict(N, id=2)]))
    with pytest.raises(session.Rejected, match="unknown clip"):
        s.apply_op("host1", op("o3", clip="nope", base_rev=1, added=[N]))
    with pytest.raises(session.Rejected, match="stale"):
        s.apply_op("host1", op("o4", base_rev=0, added=[dict(N, id=2)]))
    with pytest.raises(session.Rejected, match="unknown note"):
        s.apply_op("host1", op("o5", base_rev=1, added=[dict(N, id=3)], modified=[dict(N, id=9)]))
    assert s.snapshot() == before and s.seq == 1


def test_sequence_revision_dedupe_and_ordering():
    s = make_session()
    seqs = []
    for i in range(1, 51):
        seq, rev, dup = s.apply_op("host1", op("o%d" % i, base_rev=i - 1, added=[dict(N, id=i)]))
        seqs.append(seq)
        assert rev == i and not dup
    assert seqs == list(range(1, 51))
    seq, rev, dup = s.apply_op("host1", op("o7", base_rev=99, added=[dict(N, id=7)]))  # duplicate wins over validation
    assert (seq, rev, dup) == (7, 7, True) and s.seq == 50
    g = s.apply_op("guest1", op("g1", clip="bass", added=[dict(N, id=1)]))
    assert g == (51, 1, False) and s.revs == {"drums": 50, "bass": 1}
    assert [e["seq"] for e in s.ops_since(48)] == [49, 50, 51]
    assert s.ops_since(51) == []


def test_snapshot_op_resets_clip_and_delta_semantics():
    s = make_session()
    s.apply_op("host1", op("a", added=[dict(N, id=1), dict(N, id=2, pitch=62)]))
    s.apply_op("host1", op("b", base_rev=1, modified=[dict(N, id=1, vel=50.0)], removed=[2]))
    notes = s.snapshot()["notes"]["drums"]
    assert [(n["id"], n["vel"]) for n in notes] == [(1, 50.0)]
    s.apply_op("host1", op("c", kind="snapshot", base_rev=0, notes=[dict(N, id=9)]))   # snapshot ignores base_rev
    assert [n["id"] for n in s.snapshot()["notes"]["drums"]] == [9] and s.revs["drums"] == 3


def test_log_bounded_and_resync_signalled(monkeypatch):
    monkeypatch.setattr(session, "MAX_LOG", 10)
    s = make_session()
    for i in range(1, 31):
        s.apply_op("host1", op("o%d" % i, base_rev=i - 1, added=[dict(N, id=i)]))
    assert len(s.log) == 10 and s.ops_since(5) is None and len(s.ops_since(25)) == 5
    assert len(s.seen_ops) <= 20


def test_log_bounded_by_total_notes(monkeypatch):
    monkeypatch.setattr(session, "MAX_LOG_NOTES", 5)
    s = make_session()
    for i in range(1, 11):   # 2 notes per op: at most 2 ops (4 notes) stay in the log
        s.apply_op("host1", op("o%d" % i, base_rev=i - 1, added=[dict(N, id=2 * i), dict(N, id=2 * i + 1)]))
    assert len(s.log) == 2 and s.log_notes == 4 and s.ops_since(7) is None and len(s.ops_since(8)) == 2
    assert s.seq == 10 and len(s.snapshot()["notes"]["drums"]) == 20


def test_clip_note_cap(monkeypatch):
    monkeypatch.setattr(protocol, "MAX_NOTES_PER_CLIP", 3)
    s = make_session()
    with pytest.raises(session.Rejected, match="clip too large"):
        s.apply_op("host1", op("a", kind="snapshot", notes=[dict(N, id=i) for i in range(4)]))
    s.apply_op("host1", op("b", kind="snapshot", notes=[dict(N, id=i) for i in range(3)]))
    before = s.snapshot()
    with pytest.raises(session.Rejected, match="clip too large"):
        s.apply_op("host1", op("c", base_rev=1, added=[dict(N, id=9)]))
    with pytest.raises(session.Rejected, match="clip too large"):
        s.apply_op("host1", op("d", base_rev=1, added=[dict(N, id=9), dict(N, id=10)], removed=[0]))
    assert s.snapshot() == before and s.seq == 1 and s.revs["drums"] == 1
    # replacing one note for another, or modifying existing ones, stays within the cap
    s.apply_op("host1", op("e", base_rev=1, added=[dict(N, id=9)], modified=[dict(N, id=1, vel=1.0)], removed=[0]))
    assert sorted(n["id"] for n in s.snapshot()["notes"]["drums"]) == [1, 2, 9]


def test_session_note_cap(monkeypatch):
    monkeypatch.setattr(protocol, "MAX_NOTES_PER_SESSION", 5)
    s = make_session()
    s.apply_op("host1", op("a", kind="snapshot", notes=[dict(N, id=i) for i in range(3)]))
    before = (s.seq, dict(s.revs), s.snapshot())
    # the per-clip cap (2000) is nowhere near, but drums(3) + bass(3) exceeds the session cap
    with pytest.raises(session.Rejected, match="clip too large"):
        s.apply_op("guest1", op("b", clip="bass", kind="snapshot", notes=[dict(N, id=i) for i in range(3)]))
    assert (s.seq, s.revs, s.snapshot()) == before
    s.apply_op("guest1", op("c", clip="bass", kind="snapshot", notes=[dict(N, id=i) for i in range(2)]))
    with pytest.raises(session.Rejected, match="clip too large"):
        s.apply_op("guest1", op("d", clip="bass", base_rev=1, added=[dict(N, id=9)]))
    assert sorted(n["id"] for n in s.snapshot()["notes"]["bass"]) == [0, 1] and s.seq == 2
    # replacing notes keeps the total constant and stays allowed
    s.apply_op("guest1", op("e", clip="bass", base_rev=1, added=[dict(N, id=9)], removed=[0]))
    assert sorted(n["id"] for n in s.snapshot()["notes"]["bass"]) == [1, 9]


def welcome_len(s):
    return len(protocol.encode({"type": "welcome", "session": s.id, "code": s.code, "participant": "p" * 64,
                                "role": "guest", "seq": s.seq, "snapshot": s.snapshot(), "ops": None,
                                "resync": True}))


def test_welcome_of_all_clips_must_fit_one_line(monkeypatch):
    monkeypatch.setattr(protocol, "MAX_NOTES_PER_SESSION", 10 ** 6)   # exercise the encode check itself
    s = make_session()
    notes = [dict(N, id=i, pitch=36 + i % 60, start=i * 0.25) for i in range(1200)]   # < MAX_NOTES_PER_CLIP
    s.apply_op("host1", op("a", kind="snapshot", notes=notes))
    before = (s.seq, dict(s.revs), s.snapshot())
    assert welcome_len(s) <= protocol.MAX_LINE_BYTES - session.WELCOME_HEADROOM
    # the second clip passes the per-clip cap, but both snapshots no longer fit one welcome line
    with pytest.raises(session.Rejected, match="clip too large"):
        s.apply_op("guest1", op("b", clip="bass", kind="snapshot", notes=notes))
    assert (s.seq, s.revs, s.snapshot()) == before
    assert welcome_len(s) <= protocol.MAX_LINE_BYTES - session.WELCOME_HEADROOM   # still resumable
    # a smaller second clip fits; a delta that would push the welcome over the limit does not
    s.apply_op("guest1", op("c", clip="bass", kind="snapshot", notes=notes[:1000]))
    assert welcome_len(s) <= protocol.MAX_LINE_BYTES - session.WELCOME_HEADROOM
    before = (s.seq, dict(s.revs), s.snapshot())
    with pytest.raises(session.Rejected, match="clip too large"):
        s.apply_op("guest1", op("d", clip="bass", base_rev=1,
                                added=[dict(N, id=5000 + i, start=i * 0.25) for i in range(150)]))
    assert (s.seq, s.revs, s.snapshot()) == before
    s.apply_op("guest1", op("e", clip="bass", base_rev=1, added=[dict(N, id=5000)]))
    assert len(s.snapshot()["notes"]["bass"]) == 1001 and s.seq == 3
    assert welcome_len(s) <= protocol.MAX_LINE_BYTES - session.WELCOME_HEADROOM


def test_welcome_check_skipped_below_threshold(monkeypatch):
    # Below WELCOME_CHECK_NOTES the encode probe is not run at all (it cannot come close to the limit).
    s = make_session()
    calls = []
    monkeypatch.setattr(session.Session, "_snapshot_of", lambda self, st, _o=session.Session._snapshot_of:
                        calls.append(len(st)) or _o(self, st))
    s.apply_op("host1", op("a", kind="snapshot", notes=[dict(N, id=i) for i in range(session.WELCOME_CHECK_NOTES)]))
    assert calls == []
    s.apply_op("guest1", op("b", clip="bass", kind="snapshot", notes=[dict(N, id=0)]))
    assert calls == [2]


def test_new_code_format():
    codes = {session.new_code() for _ in range(200)}
    assert len(codes) == 200
    assert all(len(c) == 12 and set(c) <= set(session.CODE_ALPHABET) for c in codes)
    assert not set("01IOLU") & set(session.CODE_ALPHABET)


def test_claim_release_rules():
    s = make_session()
    s.set_online("host1", True)
    s.set_online("guest1", True)
    with pytest.raises(session.Rejected):
        s.claim("guest1", "drums")
    s.set_online("host1", False)
    s.claim("guest1", "drums")
    assert s.owner_of("drums") == "guest1"
    s.release("guest1", "drums")
    assert s.owner_of("drums") is None
    s.release("host1", "bass")     # not owner: no-op
    assert s.owner_of("bass") == "guest1"
    s.claim("host1", "drums")      # unowned: anyone can claim
    assert s.owner_of("drums") == "host1"
    with pytest.raises(session.Rejected):
        s.claim("host1", "nope")


def test_persistence_roundtrip_keeps_notes_owners_and_auth():
    s = make_session()
    s.apply_op("host1", op("a", added=[N]))
    s2 = session.Session.from_json(s.to_json())
    assert s2.snapshot() == s.snapshot() and s2.seq == 1 and s2.owner_of("bass") == "guest1"
    assert type(session.Session.from_json(dict(s.to_json(), seq=0)).seq) is int
    s2.authenticate("guest1", "g" * 20)
    assert s2.ops_since(0) is None       # the op log is not persisted -> resync via snapshot
    assert "secret" not in str(s.to_json()).replace("secret_hash", "")
