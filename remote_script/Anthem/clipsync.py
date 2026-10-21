"""Pure note-diff logic for the Remote Script. No Live imports, so it is unit-testable.

Notes are plain dicts in wire form (see anthem_core.protocol.NOTE_FIELDS):
  {"id", "pitch", "start", "dur", "vel", "mute", "prob", "vel_dev", "rel_vel"}
``id`` is Live's per-clip note_id on the machine that owns the clip.
"""

EPS = 1e-6
OVERSIZED = "oversized"   # returned by Bridge.send when a message exceeds the line limit


def note_from_live(n):
    return {
        "id": int(n.note_id), "pitch": int(n.pitch), "start": float(n.start_time),
        "dur": float(n.duration), "vel": float(n.velocity), "mute": bool(n.mute),
        "prob": float(n.probability), "vel_dev": float(n.velocity_deviation),
        "rel_vel": float(n.release_velocity),
    }


def notes_equal(a, b):
    if a["pitch"] != b["pitch"] or a["mute"] != b["mute"]:
        return False
    for k in ("start", "dur", "vel", "prob", "vel_dev", "rel_vel"):
        if abs(a[k] - b[k]) > EPS:
            return False
    return True


def diff(old, new):
    """old/new: dict id -> note. Returns (added, modified, removed_ids) or None if identical."""
    added, modified, removed = [], [], []
    for nid, n in new.items():
        o = old.get(nid)
        if o is None:
            added.append(n)
        elif not notes_equal(o, n):
            modified.append(n)
    for nid in old:
        if nid not in new:
            removed.append(nid)
    if not (added or modified or removed):
        return None
    added.sort(key=lambda n: (n["start"], n["pitch"], n["id"]))
    modified.sort(key=lambda n: n["id"])
    removed.sort()
    return added, modified, removed


def same_content(a, b):
    """Compare two id->note maps ignoring ids (used for feedback-loop / conflict checks)."""
    if len(a) != len(b):
        return False
    ka = sorted(a.values(), key=_content_key)
    kb = sorted(b.values(), key=_content_key)
    return all(notes_equal(x, y) for x, y in zip(ka, kb))


def _content_key(n):
    return (round(n["start"], 5), n["pitch"], round(n["dur"], 5), round(n["vel"], 3), n["mute"])


class IdMap(object):
    """Maps the owner's note ids (remote) <-> this machine's Live note ids (local)."""

    def __init__(self):
        self.r2l = {}
        self.l2r = {}

    def bind(self, remote_id, local_id):
        self.r2l[remote_id] = local_id
        self.l2r[local_id] = remote_id

    def unbind_remote(self, remote_id):
        lid = self.r2l.pop(remote_id, None)
        if lid is not None:
            self.l2r.pop(lid, None)
        return lid

    def clear(self):
        self.r2l.clear()
        self.l2r.clear()
