"""Pure session logic used by the relay (and by tests, without any networking).

A Session holds:
  * participants: id -> {secret_hash, online}
  * clips: collaboration clip id -> {name, track_name, slot, owner}
  * revs:  clip id -> current revision (int, increments per accepted op)
  * log:   accepted ops in sequence order, each stamped with ``seq``
  * state: clip id -> {note_id: note} (authoritative notes, replayed from ops)

Ordering is by relay-assigned ``seq`` only; wall-clock is never used.
"""
import hashlib
import hmac
import secrets

from . import protocol

MAX_LOG = 5000          # bounded; older ops are dropped, late joiners get a snapshot instead
MAX_LOG_NOTES = 20000   # ...and by total notes in the log, so a resume backlog stays small
MAX_PARTICIPANTS = 2
# The welcome (full snapshot of every clip) must always fit one wire line, or the session can
# never be resumed. Above this many notes in total, every op is checked by actually encoding
# the candidate welcome; below it, the check is skipped (it cannot come close to the limit).
WELCOME_CHECK_NOTES = 800
WELCOME_HEADROOM = 4096  # bytes kept free below protocol.MAX_LINE_BYTES for seq/rev/id growth
CODE_ALPHABET = "ABCDEFGHJKMNPQRSTVWXYZ23456789"   # base32 without ambiguous 0/O/1/I/L/U
CODE_LEN = 12


class Rejected(Exception):
    def __init__(self, reason):
        Exception.__init__(self, reason)
        self.reason = reason


def _hash(secret):
    return hashlib.sha256(secret.encode("utf-8")).hexdigest()


def new_code():
    """Human-typeable invite code: 12 chars from a 30-letter alphabet (~59 bits), case-insensitive."""
    return "".join(secrets.choice(CODE_ALPHABET) for _ in range(CODE_LEN))


def _note_count(entry):
    p = entry["payload"]
    return len(p["notes"]) if entry["kind"] == "snapshot" else \
        len(p["added"]) + len(p["modified"]) + len(p["removed"])


ROLES = ("host", "guest")


class Session(object):
    def __init__(self, session_id, code, clips):
        """clips: list of {"clip": id, "name":..., "track_name":..., "slot": int, "owner": role_or_participant}

        ``owner`` starts as a role ("host"/"guest"). It is resolved to a participant id by
        ``add_participant(..., role)``: the creator is "host", the first joiner is "guest".
        """
        self.id = session_id
        self.code = code
        self.participants = {}
        self.clips = {}
        self.revs = {}
        self.state = {}
        self.log = []
        self.log_notes = 0  # sum of _note_count over self.log
        self.seq = 0
        self.seen_ops = {}  # op_id -> (seq, rev) for duplicate suppression
        for c in clips:
            if not protocol.valid_id(c["clip"]):
                raise Rejected("bad clip id")
            self.clips[c["clip"]] = {
                "clip": c["clip"], "name": c.get("name", c["clip"]),
                "track_name": c["track_name"], "slot": int(c.get("slot", 0)),
                "owner": c.get("owner"),
            }
            self.revs[c["clip"]] = 0
            self.state[c["clip"]] = {}

    # --- participants ---------------------------------------------------------
    def add_participant(self, participant_id, secret, role):
        if not protocol.valid_id(participant_id):
            raise Rejected("bad participant id")
        if not isinstance(secret, str) or len(secret) < 16:
            raise Rejected("secret too short")
        if participant_id in self.participants:
            raise Rejected("participant exists")
        if len(self.participants) >= MAX_PARTICIPANTS:
            raise Rejected("session full")
        if role not in ROLES or any(p["role"] == role for p in self.participants.values()):
            raise Rejected("role %r unavailable" % (role,))
        self.participants[participant_id] = {"secret_hash": _hash(secret), "online": False, "role": role}
        for c in self.clips.values():
            if c["owner"] == role:
                c["owner"] = participant_id

    def authenticate(self, participant_id, secret):
        p = self.participants.get(participant_id)
        if p is None or not isinstance(secret, str):
            raise Rejected("unknown participant")
        if not hmac.compare_digest(p["secret_hash"], _hash(secret)):
            raise Rejected("bad secret")
        return p

    def set_online(self, participant_id, online):
        # Ownership survives a disconnect; the other side may `claim` it while the owner is offline.
        self.participants[participant_id]["online"] = online

    def presence(self):
        return [{"id": pid, "role": p["role"], "online": p["online"]}
                for pid, p in sorted(self.participants.items())]

    # --- ownership ------------------------------------------------------------
    def owner_of(self, clip):
        c = self.clips.get(clip)
        return None if c is None else c["owner"]

    def claim(self, participant_id, clip):
        c = self.clips.get(clip)
        if c is None:
            raise Rejected("unknown clip")
        owner = c["owner"]
        if owner is None or owner == participant_id:
            c["owner"] = participant_id
            return
        if self.participants.get(owner, {}).get("online"):
            raise Rejected("clip owned by %s" % owner)
        # owner is offline: ownership may be taken over explicitly
        c["owner"] = participant_id

    def release(self, participant_id, clip):
        c = self.clips.get(clip)
        if c is None:
            raise Rejected("unknown clip")
        if c["owner"] == participant_id:
            c["owner"] = None

    # --- ops ------------------------------------------------------------------
    def apply_op(self, participant_id, op):
        """op: validated via protocol.validate_op. Returns (seq, rev, duplicate: bool)."""
        dup = self.seen_ops.get(op["op_id"])
        if dup is not None:
            return dup[0], dup[1], True
        clip = op["clip"]
        if clip not in self.clips:
            raise Rejected("unknown clip")
        if self.clips[clip]["owner"] != participant_id:
            raise Rejected("not owner of clip %s" % clip)
        rev = self.revs[clip]
        if op["kind"] == "delta" and op["base_rev"] != rev:
            # Owner's view is stale (e.g. after a reconnect); it must resend a snapshot.
            raise Rejected("stale base_rev %d (current %d)" % (op["base_rev"], rev))
        notes = self.state[clip]
        p = op["payload"]
        # Build the candidate clip state first and validate it; nothing is mutated until every
        # check has passed, so a rejected op leaves the session exactly as it was.
        if op["kind"] == "snapshot":
            if len(p["notes"]) > protocol.MAX_NOTES_PER_CLIP:
                raise Rejected("clip too large")
            after = {n["id"]: n for n in p["notes"]}
        else:
            added_ids = {n["id"] for n in p["added"]}
            for n in p["modified"]:
                if n["id"] not in notes and n["id"] not in added_ids:
                    raise Rejected("modify of unknown note %d" % n["id"])
            after = dict(notes)
            for n in p["added"]:
                after[n["id"]] = n
            for n in p["modified"]:
                after[n["id"]] = n
            for nid in p["removed"]:
                after.pop(nid, None)
            if len(after) > protocol.MAX_NOTES_PER_CLIP:
                raise Rejected("clip too large")
        self._check_welcome_fits(clip, after)
        self.state[clip] = after
        self.seq += 1
        rev += 1
        self.revs[clip] = rev
        entry = {
            "seq": self.seq, "rev": rev, "participant": participant_id,
            "op_id": op["op_id"], "clip": clip, "kind": op["kind"], "payload": p,
        }
        self.log.append(entry)
        self.log_notes += _note_count(entry)
        while self.log and (len(self.log) > MAX_LOG or self.log_notes > MAX_LOG_NOTES):
            self.log_notes -= _note_count(self.log.pop(0))
        self.seen_ops[op["op_id"]] = (self.seq, rev)
        if len(self.seen_ops) > MAX_LOG * 2:
            # drop oldest half (dict preserves insertion order)
            for k in list(self.seen_ops)[: MAX_LOG]:
                del self.seen_ops[k]
        return self.seq, rev, False

    def ops_since(self, last_seq):
        """Ops after last_seq, or None if they fell out of the bounded log (caller sends snapshot)."""
        if last_seq >= self.seq:
            return []
        if not self.log or self.log[0]["seq"] > last_seq + 1:
            return None
        return [e for e in self.log if e["seq"] > last_seq]

    def _check_welcome_fits(self, clip, after):
        """Reject (``clip too large``) if ``clip`` holding ``after`` would make the session's
        welcome line (snapshot of every clip) exceed the wire limit, which would leave the
        session permanently un-resumable. State is not touched."""
        total = len(after) + sum(len(ns) for cid, ns in self.state.items() if cid != clip)
        if total > protocol.MAX_NOTES_PER_SESSION:
            raise Rejected("clip too large")
        if total <= WELCOME_CHECK_NOTES:
            return  # far below the limit: skip the (relatively costly) encode
        state = dict(self.state)
        state[clip] = after
        probe = {
            # Same shape as the relay's welcome, with worst-case placeholders for what the
            # relay fills in (longest allowed participant id, next seq, snapshot only).
            "type": "welcome", "session": self.id, "code": self.code, "participant": "x" * 64,
            "role": "guest", "seq": self.seq + 1, "snapshot": self._snapshot_of(state),
            "ops": None, "resync": True,
        }
        try:
            line = protocol.encode(probe)
        except protocol.ProtocolError:
            raise Rejected("clip too large")
        if len(line) > protocol.MAX_LINE_BYTES - WELCOME_HEADROOM:
            raise Rejected("clip too large")

    def snapshot(self):
        return self._snapshot_of(self.state)

    def _snapshot_of(self, state):
        return {
            "session": self.id,
            "clips": [dict(c) for c in self.clips.values()],
            "revs": dict(self.revs),
            "notes": {cid: sorted(ns.values(), key=lambda n: (n["start"], n["pitch"], n["id"]))
                      for cid, ns in state.items()},
        }

    # --- persistence ----------------------------------------------------------
    def to_json(self):
        return {
            "id": self.id, "code": self.code, "seq": self.seq,
            "participants": {k: {"secret_hash": v["secret_hash"], "role": v["role"]}
                             for k, v in self.participants.items()},
            "clips": list(self.clips.values()), "revs": self.revs,
            "state": {cid: list(ns.values()) for cid, ns in self.state.items()},
        }

    @classmethod
    def from_json(cls, d):
        s = cls(d["id"], d["code"], d["clips"])
        s.seq = int(d["seq"])
        for pid, p in d["participants"].items():
            s.participants[pid] = {"secret_hash": p["secret_hash"], "online": False, "role": p["role"]}
        s.revs = dict(d["revs"])
        for cid, notes in d["state"].items():
            s.state[cid] = {n["id"]: n for n in notes}
        return s
