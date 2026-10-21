"""Pure companion state machine. No I/O, no clocks of its own, no sockets.

Every ``on_*`` method takes an inbound message/event and returns a list of actions:

    ("relay", msg)      send msg to the relay (dropped by the glue if disconnected)
    ("script", msg)     send msg to the connected Remote Script
    ("print", text)     show text to the user
    ("persist", dict)   identity fields changed (session, code, role, set_path, last_seq)
    ("relay_drop", why) the relay link looks dead: the glue closes it so the reconnect path runs
    ("quit",)           user asked to exit

Clip replica semantics mirror ``anthem_core.session.Session.apply_op``: a snapshot
replaces the note map, a delta adds/modifies/removes by owner note id.
"""
import collections
import statistics
import time
import uuid

from anthem_core import protocol

MAX_PENDING = 512          # outstanding ops awaiting op_ack (oldest dropped beyond this)
MAX_SAMPLES = 256          # latency samples kept for diagnostics
MAX_SEEN_OPS = 4096        # relayed op_ids remembered for duplicate suppression
PING_INTERVAL = 30.0
PENDING_TIMEOUT = 60.0

TEMPLATE_CLIPS = [
    {"clip": "drums", "name": "Drums", "track_name": "Anthem Drums", "slot": 0, "owner": "host"},
    {"clip": "bass", "name": "Bass", "track_name": "Anthem Bass", "slot": 0, "owner": "guest"},
]


def new_participant_id():
    return "p-" + uuid.uuid4().hex[:16]


def _pct(samples, q):
    if not samples:
        return None
    s = sorted(samples)
    idx = min(len(s) - 1, int(round(q * (len(s) - 1))))
    return s[idx]


def note_content(notes):
    """Content of a note list without ids (for comparing a local clip with the replica).
    Same fields as the script's ``clipsync.notes_equal``."""
    return sorted((n["pitch"], round(float(n["start"]), 4), round(float(n["dur"]), 4),
                   round(float(n.get("vel", 100)), 2), bool(n.get("mute", False)),
                   round(float(n.get("prob", 1.0)), 4), round(float(n.get("vel_dev", 0.0)), 2),
                   round(float(n.get("rel_vel", 64.0)), 2)) for n in notes)


class Clip(object):
    __slots__ = ("clip", "name", "track_name", "slot", "owner", "owned", "found", "rev",
                 "notes", "conflict", "paused", "pause_reason", "dirty", "note_count",
                 "awaiting_baseline", "expected_local", "unsent_conflict", "revert_pending")

    def __init__(self, clip, name, track_name, slot, owner):
        self.clip = clip
        self.name = name
        self.track_name = track_name
        self.slot = slot
        self.owner = owner
        self.owned = False
        self.found = False
        self.rev = 0
        self.notes = {}         # owner note id -> note (replica of the relay's state)
        self.conflict = False
        self.paused = False
        self.pause_reason = None
        self.dirty = False      # owned: local edits happened while relay was unreachable
        self.note_count = None  # as reported by the script's inventory
        # Non-owned clips: no remote op is forwarded between (re)found / set_owned(False) and the
        # script's baseline local_snapshot, so the baseline is compared with what it was read against.
        self.awaiting_baseline = False
        self.expected_local = None    # note_content the local clip is known to hold (may lag the replica)
        self.unsent_conflict = False  # conflict raised for unsent edits: an empty baseline is still an edit
        self.revert_pending = False   # revert/resync typed while the clip was unavailable

    def pause(self, reason=None):
        self.paused = True
        self.pause_reason = reason

    def unpause(self):
        self.paused = False
        self.pause_reason = None

    def apply(self, kind, payload):
        if kind == "snapshot":
            self.notes = {n["id"]: n for n in payload["notes"]}
        else:
            for n in payload.get("added", []):
                self.notes[n["id"]] = n
            for n in payload.get("modified", []):
                self.notes[n["id"]] = n
            for nid in payload.get("removed", []):
                self.notes.pop(nid, None)

    def sorted_notes(self):
        return sorted(self.notes.values(), key=lambda n: (n["start"], n["pitch"], n["id"]))


class Companion(object):
    def __init__(self, participant, secret, session=None, code=None, role=None, set_path=None,
                 last_seq=0, create_clips=None, join_code=None, clock=None, wall=None,
                 create_token=None):
        self.participant = participant
        self.create_token = create_token or None   # relay --create-token; never logged or persisted
        self.secret = secret
        self.session = session
        self.code = code
        self.role = role
        self.set_path = set_path or None     # '' (unsaved set) is never recorded
        self.last_seq = int(last_seq or 0)
        self.create_clips = create_clips
        self.join_code = join_code
        self.clock = clock or time.monotonic
        self.wall = wall or time.time        # wall clock: the script's t_detect is time.time() based

        self.clips = collections.OrderedDict()
        self.relay_connected = False
        self.welcomed = False
        self.ever_welcomed = False
        self.script_connected = False
        self.script_hello = None
        self.bound = False
        self.bind_blocked = False
        self.presence = []
        self.pending = collections.OrderedDict()   # op_id -> (clip, sent_at, (notes, rev) before the op)
        self.seen_ops = collections.OrderedDict()  # relayed op_id -> seq
        self.ack_latencies = collections.deque(maxlen=MAX_SAMPLES)
        self.applied_ms = collections.deque(maxlen=MAX_SAMPLES)
        self.detect_lag = collections.deque(maxlen=MAX_SAMPLES)   # script t_detect -> companion, seconds
        self.last_ping = None
        self.last_rx = None      # when the relay last sent anything (pong included)
        self._counter = 0
        self.heal_sent = {}      # clip -> op_id of the in-flight self-heal snapshot

    # ------------------------------------------------------------------ helpers
    def _now(self):
        return self.clock()

    def _next(self, prefix, clip):
        self._counter += 1
        return "%s-%s-%d" % (prefix, clip, self._counter)

    def _persist(self):
        return ("persist", {"session": self.session, "code": self.code, "role": self.role,
                            "set_path": self.set_path, "last_seq": self.last_seq})

    def _bump_seq(self, seq):
        if isinstance(seq, int) and seq > self.last_seq:
            self.last_seq = seq
            return [self._persist()]
        return []

    def identity(self):
        return {"session": self.session, "code": self.code, "participant": self.participant,
                "secret": self.secret, "role": self.role, "set_path": self.set_path,
                "last_seq": self.last_seq}

    def _apply_snapshot_msg(self, c, prefix="push", force=False):
        msg = {"type": "apply_snapshot", "clip": c.clip, "op_id": self._next(prefix, c.clip),
               "notes": c.sorted_notes()}
        if force:   # only the explicit user commands revert/resync overwrite a local edit
            msg["force"] = True
        return ("script", msg)

    def _can_forward(self, c):
        return (self.script_connected and self.bound and c.found and not c.owned
                and not c.conflict and not c.paused and not c.awaiting_baseline)

    def _set_owned(self, c):
        """Tell the bound script who owns the clip (it handles a not-yet-found clip itself).
        A clip we do not own answers with its baseline local_snapshot: wait for it."""
        if self.script_connected and self.bound:
            if not c.owned:
                c.awaiting_baseline = True
            return [("script", {"type": "set_owned", "clip": c.clip, "owned": c.owned})]
        return []

    def _remember_local(self, c):
        """The local clip holds the replica (just applied / about to go out of reach)."""
        c.expected_local = note_content(c.notes.values())

    def _show(self, msg):
        if self.script_connected:
            return [("script", {"type": "show_message", "msg": msg})]
        return []

    def _conflict_actions(self, c, why):
        c.conflict = True
        self.heal_sent.pop(c.clip, None)
        return self._show("Anthem: %s - type revert or claim in the companion" % why) + [("print",
                "CONFLICT: %s.\n"
                "  revert %s  - discard the local edit and re-apply the shared version\n"
                "  claim %s   - take ownership (only if the owner is offline / released it)"
                % (why, c.clip, c.clip))]

    def _ownership_gained(self, c):
        """Clip became ours. The script answers set_owned with a local_snapshot on its own."""
        c.conflict = False
        c.unsent_conflict = False
        c.revert_pending = False
        c.unpause()
        self.heal_sent.pop(c.clip, None)
        out = self._set_owned(c)
        if not out:
            c.dirty = True   # snapshot is requested once the script is bound again
        return out

    def _ownership_lost(self, c):
        """Clip is no longer ours. Unsent local edits are never overwritten silently. Nothing is
        pushed here: the script answers set_owned with a baseline local_snapshot (see _baseline)."""
        had_unsent = c.dirty
        c.dirty = False
        out = self._set_owned(c)
        if had_unsent:
            c.unsent_conflict = True
            out.extend(self._conflict_actions(
                c, "%s is now owned by the other participant but you have unsent local edits; "
                   "they are kept until you type 'revert %s' or 'claim %s'" % (c.clip, c.clip, c.clip)))
        return out

    # ------------------------------------------------------------- connections
    def on_relay_connected(self):
        self.relay_connected = True
        self.welcomed = False
        if self.session:
            msg = {"type": "hello", "session": self.session, "participant": self.participant,
                   "secret": self.secret, "last_seq": self.last_seq}
        elif self.join_code:
            msg = {"type": "join", "code": self.join_code, "participant": self.participant,
                   "secret": self.secret}
        else:
            msg = {"type": "create", "participant": self.participant, "secret": self.secret,
                   "clips": self.create_clips or TEMPLATE_CLIPS}
            if self.create_token:
                msg["token"] = self.create_token
        self.last_ping = self.last_rx = self._now()
        return [("relay", msg)]

    def on_relay_disconnected(self):
        self.relay_connected = False
        self.welcomed = False
        # Unacked ops are abandoned: every owned clip re-sends a snapshot after reconnect.
        for clip, _, _ in self.pending.values():
            c = self.clips.get(clip)
            if c is not None:
                c.dirty = True
        self.pending.clear()
        return [("print", "relay: disconnected (local edits are coalesced until reconnect)")]

    def on_script_connected(self):
        self.script_connected = True
        return []

    def on_script_disconnected(self):
        self.script_connected = False
        self.script_hello = None
        self.bound = False
        for c in self.clips.values():
            c.found = False
            if not c.owned:
                self._remember_local(c)   # the replica may advance while Live is away
        return [("print", "script: disconnected (waiting for Live to reconnect)")]

    # ------------------------------------------------------------------- relay
    def on_relay(self, msg):
        self.last_rx = self._now()
        handler = getattr(self, "_relay_" + msg.get("type", ""), None)
        if handler is None:
            return []
        return handler(msg)

    def _relay_pong(self, msg):
        return []

    def _relay_error(self, msg):
        return [("print", "relay error: %s" % (msg.get("reason"),))]

    def _relay_presence(self, msg):
        parts = msg.get("participants") or []
        self.presence = parts
        who = ", ".join("%s(%s) %s" % (p.get("role"), "you" if p.get("id") == self.participant else "peer",
                                        "online" if p.get("online") else "offline") for p in parts)
        return [("print", "presence: " + who)]

    def _relay_welcome(self, msg):
        snap = msg.get("snapshot") or {}
        if not isinstance(snap, dict) or not isinstance(snap.get("clips"), list):
            return [("print", "relay: malformed welcome (no snapshot) - ignored")]
        if (not protocol.valid_id(msg.get("session")) or not isinstance(msg.get("participant"), str)
                or not isinstance(msg.get("role"), str)
                or not (msg.get("code") is None or isinstance(msg.get("code"), str))):
            return [("print", "relay sent an invalid welcome; ignoring")]
        out = []
        self.session = msg.get("session") or self.session
        self.code = msg.get("code") or self.code
        self.role = msg.get("role") or self.role
        self.welcomed = True
        reconnect = self.ever_welcomed
        self.ever_welcomed = True

        # Clip metadata + ownership always come from the snapshot.
        gained, lost = [], []
        for c in snap["clips"]:
            cid = c.get("clip")
            if not protocol.valid_id(cid):
                continue
            cs = self.clips.get(cid)
            existed = cs is not None
            if not existed:
                cs = Clip(cid, c.get("name", cid), c.get("track_name", cid), int(c.get("slot", 0)), c.get("owner"))
                self.clips[cid] = cs
            was = cs.owned
            cs.owner = c.get("owner")
            cs.owned = (cs.owner == self.participant)
            if cs.owned:
                cs.conflict = False
            if existed and cs.owned and not was:
                gained.append(cs)
            elif was and not cs.owned:
                lost.append(cs)

        ops = msg.get("ops")
        resync = bool(msg.get("resync")) or ops is None or not reconnect
        if resync:
            revs = snap.get("revs") or {}
            notes = snap.get("notes") or {}
            for cid, cs in self.clips.items():
                try:
                    payload = protocol.validate_op_payload("snapshot", {"notes": notes.get(cid, [])})
                except protocol.ProtocolError as e:
                    out.append(("print", "relay: bad snapshot notes for %s: %s" % (cid, e)))
                    continue
                cs.apply("snapshot", payload)
                cs.rev = int(revs.get(cid, cs.rev) or 0)
            # The relay's seq is authoritative, even when lower than ours (relay restarted).
            if isinstance(msg.get("seq"), int):
                self.last_seq = msg["seq"]
            self.seen_ops.clear()
            out.append(("print", "relay: welcome (%s) - full resync from snapshot, seq=%d"
                        % ("reconnect" if reconnect else "session %s" % self.session, self.last_seq)))
        else:
            n = 0
            for op in ops:
                if isinstance(op, dict):
                    self._apply_relayed(op, forward=False)
                    n += 1
            self.last_seq = max(self.last_seq, int(msg.get("seq") or 0))
            out.append(("print", "relay: welcome (reconnect) - applied %d missed ops, seq=%d" % (n, self.last_seq)))
        out.append(self._persist())

        # Ownership that moved while we were away: tell the script first, then push/request.
        for cs in gained:
            out.extend(self._ownership_gained(cs))
        for cs in lost:
            out.extend(self._ownership_lost(cs))
        if reconnect and self.bound:
            for cs in self.clips.values():
                if not cs.found or cs in gained or cs in lost:
                    continue
                if cs.owned:
                    cs.dirty = False
                    out.append(("script", {"type": "request_snapshot", "clip": cs.clip}))
                elif not cs.conflict and not cs.paused:
                    out.append(self._apply_snapshot_msg(cs, "resync"))
        elif not reconnect:
            out.append(("print", "session %s  role=%s  invite code: %s" % (self.session, self.role, self.code)))
        out.extend(self._maybe_bind())
        return out

    def _relay_op_ack(self, msg):
        op_id = msg.get("op_id")
        ent = self.pending.pop(op_id, None)
        out = []
        if ent is not None and not msg.get("duplicate"):
            clip, sent_at, _ = ent
            self.ack_latencies.append(max(0.0, self._now() - sent_at))
            c = self.clips.get(clip)
            if c is not None and isinstance(msg.get("rev"), int):
                # Other ops for this clip still in flight: keep the optimistic rev they were based on.
                if any(cl == clip for cl, _, _ in self.pending.values()):
                    c.rev = max(c.rev, msg["rev"])
                else:
                    c.rev = msg["rev"]
        out.extend(self._bump_seq(msg.get("seq")))
        return out

    def _relay_reject(self, msg):
        op_id = msg.get("op_id")
        reason = str(msg.get("reason") or "")
        ent = self.pending.pop(op_id, None)
        clip = ent[0] if ent else None
        c = self.clips.get(clip) if clip else None
        # Reasons a fresh authoritative snapshot heals (the relay's clip state drifted from ours).
        heal = "stale" in reason or reason.startswith("modify of unknown note") or reason == "clip too large"
        out = []
        if c is not None and reason.startswith("not owner"):
            # Our edit never reached the session: the replica must show the relay's state again (the
            # state before this op; later ops for the clip are doomed the same way) so the local edit
            # is detected as a conflict instead of being wiped by the next owner's snapshot.
            c.notes, c.rev = ent[2]
            for k in [k for k, v in self.pending.items() if v[0] == clip]:
                self.pending.pop(k)
            if c.owned:
                c.dirty = True      # the ownership message is on its way: _ownership_lost raises the conflict
                return [("print", "relay rejected op %s (%s): %s" % (op_id, clip, reason))]
            c.unsent_conflict = True
            return self._conflict_actions(
                c, "your edit of %s was not accepted because its ownership changed; it is kept in Live "
                   "until you type 'revert %s' or 'claim %s'" % (c.clip, c.clip, c.clip))
        if c is None or not heal:
            out.append(("print", "relay rejected op %s%s: %s" % (op_id, " (%s)" % clip if clip else "", reason)))
        if c is not None and reason == "clip too large":
            out.append(("print", "%s exceeds the %d-note limit; remove notes in Live to resume syncing it"
                        % (c.clip, protocol.MAX_NOTES_PER_CLIP)))
        if c is not None and heal:
            if self.script_connected and c.found and c.owned:
                out.append(("script", {"type": "request_snapshot", "clip": clip}))
            else:
                c.dirty = True
        return out

    def _relay_op(self, msg):
        seq = msg.get("seq")
        op_id = msg.get("op_id")
        if op_id in self.seen_ops or (isinstance(seq, int) and seq <= self.last_seq):
            return []
        return self._apply_relayed(msg, forward=True)

    def _apply_relayed(self, msg, forward):
        op_id, clip, kind = msg.get("op_id"), msg.get("clip"), msg.get("kind")
        seq = msg.get("seq")
        if op_id in self.seen_ops:
            return []
        c = self.clips.get(clip)
        if c is None or kind not in protocol.OP_KINDS:
            return [("print", "relay: op %s for unknown clip %r - ignored" % (op_id, clip))]
        try:
            payload = protocol.validate_op_payload(kind, msg.get("payload"))
        except protocol.ProtocolError as e:
            return [("print", "relay: invalid op %s: %s - ignored" % (op_id, e))]
        c.apply(kind, payload)
        if isinstance(msg.get("rev"), int):
            c.rev = msg["rev"]
        if protocol.valid_id(op_id):
            self.seen_ops[op_id] = seq
            while len(self.seen_ops) > MAX_SEEN_OPS:
                self.seen_ops.popitem(last=False)
        out = self._bump_seq(seq)
        if forward and self._can_forward(c):
            fwd = {"type": "apply_" + kind, "clip": clip, "op_id": op_id}
            fwd.update(payload)
            out.append(("script", fwd))
        return out

    def _relay_ownership(self, msg):
        clip, owner = msg.get("clip"), msg.get("owner")
        c = self.clips.get(clip)
        if c is None:
            return []
        was = c.owned
        c.owner = owner
        c.owned = (owner == self.participant)
        out = [("print", "ownership: %s -> %s" % (clip, "you" if c.owned else (owner or "nobody")))]
        if c.owned and not was:
            out.extend(self._ownership_gained(c))
        elif was and not c.owned:
            out.extend(self._ownership_lost(c))
        return out

    # ------------------------------------------------------------------ script
    def on_script(self, msg):
        handler = getattr(self, "_script_" + msg.get("type", ""), None)
        if handler is None:
            return []
        return handler(msg)

    def _script_log(self, msg):
        return [("print", "script[%s]: %s" % (msg.get("level"), msg.get("msg")))]

    def _hello_path(self):
        """Live Set path from the last script_hello; '' (unsaved set) counts as unknown."""
        return (self.script_hello or {}).get("set_path") or None

    def _script_script_hello(self, msg):
        self.script_hello = msg
        path = self._hello_path()
        out = [("print", "script: hello from Live %s set=%r" % (msg.get("live_version"), msg.get("set_name")))]
        if self.bound:
            self.bound = False
            for c in self.clips.values():
                c.found = False
                if not c.owned:
                    self._remember_local(c)
            if self.set_path is None or path != self.set_path:
                # Nothing recorded (the first set was unsaved) or another path: we cannot prove
                # the re-instantiated script is in the same project.
                return out + self._mismatch(path)
        out.extend(self._maybe_bind())
        return out

    def _mismatch(self, path):
        self.bind_blocked = True
        return [("print",
                 "WARNING: Live opened a different Set.\n  recorded: %s\n  current:  %s\n"
                 "Sync is paused. Type 'rebind' to attach this session to the current Set."
                 % (self.set_path or "(unsaved)", path or "(unsaved)"))]

    def _attach_set(self):
        """Record the current Set path (first bind / setup). Returns actions."""
        path = self._hello_path()
        if self.set_path is not None and path != self.set_path:
            return None
        out = []
        if path is None:
            out.append(("print", "WARNING: unsaved Live Set: the different-project guard is inactive "
                                 "— save the set to enable it"))
        elif self.set_path is None:
            self.set_path = path
            out.append(self._persist())
        self.bind_blocked = False
        return out

    def _maybe_bind(self):
        if self.bound or not self.welcomed or self.script_hello is None or not self.script_connected:
            return []
        out = self._attach_set()
        if out is None:
            return self._mismatch(self._hello_path())
        self.bound = True
        out.append(("script", {"type": "bind", "clips": [
            {"clip": c.clip, "track_name": c.track_name, "slot": c.slot, "owned": c.owned}
            for c in self.clips.values()]}))
        return out

    def _script_inventory(self, msg):
        if not self.bound:
            return []
        seen = set()
        for e in msg.get("clips") or []:
            if not isinstance(e, dict):
                continue
            c = self.clips.get(e.get("clip"))
            if c is None:
                continue
            seen.add(c.clip)
            was = c.found
            c.found = bool(e.get("found"))
            if c.found and not was:
                c.awaiting_baseline = True    # the script's baseline local_snapshot follows
            c.note_count = e.get("note_count")
        for c in self.clips.values():
            if c.clip not in seen:
                c.found = False
        rows = ["Supported parts:",
                "  %-8s %-16s %-4s %-6s %-6s %s" % ("clip", "track", "slot", "found", "owned", "notes")]
        for c in self.clips.values():
            rows.append("  %-8s %-16s %-4d %-6s %-6s %s" % (
                c.clip, c.track_name[:16], c.slot, "yes" if c.found else "NO",
                "yes" if c.owned else "no", "-" if c.note_count is None else c.note_count))
        out = [("print", "\n".join(rows))]
        # The init push for non-owned clips happens when the script sends its local_snapshot
        # baseline (see _script_local_snapshot), so pre-existing local edits are never overwritten.
        for c in self.clips.values():
            if c.found and c.owned and c.dirty:
                c.dirty = False
                out.append(("script", {"type": "request_snapshot", "clip": c.clip}))
        return out

    def _local_op(self, c, kind, payload):
        """Owned clip changed locally: send an op now, or coalesce until the relay is back."""
        if not (self.relay_connected and self.welcomed):
            c.dirty = True
            return []
        op = {"type": "op", "op_id": uuid.uuid4().hex, "clip": c.clip, "kind": kind,
              "base_rev": c.rev, "payload": payload}
        try:
            protocol.validate_op(op)
        except protocol.ProtocolError as e:
            out = [("print", "script: invalid %s for %s dropped (%s) - re-syncing from a full snapshot"
                    % (kind, c.clip, e))]
            # A bad snapshot is not re-requested (it would just come back bad); it is retried on the
            # next inventory/clip_found/reconnect via dirty.
            if kind == "delta" and self.script_connected and c.found:
                return out + [("script", {"type": "request_snapshot", "clip": c.clip})]
            c.dirty = True
            return out
        try:
            protocol.encode(op)   # before touching the replica: an unsendable op must leave no trace
        except protocol.ProtocolError:
            c.dirty = True        # retried as a snapshot on the next inventory/clip_found/reconnect/resync
            c.pause("too large")
            return [("print", "%s is too large to send (over %d KiB on the wire); remove notes in Live "
                              "to resume syncing it" % (c.clip, protocol.MAX_LINE_BYTES // 1024))]
        prev = (dict(c.notes), c.rev)
        c.apply(kind, payload)
        c.rev += 1            # optimistic; reconciled by op_ack / reject
        c.dirty = False
        if kind == "snapshot" and c.pause_reason == "too large":
            c.unpause()       # the whole clip fits again
        self.pending[op["op_id"]] = (c.clip, self._now(), prev)
        while len(self.pending) > MAX_PENDING:
            self.pending.popitem(last=False)
        return [("relay", op)]

    def _script_local_snapshot(self, msg):
        c = self.clips.get(msg.get("clip"))
        if c is None or not self.bound:
            return []
        out = []
        if "owned" in msg and bool(msg["owned"]) != c.owned:
            out = self._set_owned(c)      # the script's view of ownership drifted: correct it first
        if c.owned:
            return out + self._local_op(c, "snapshot", {"notes": msg.get("notes")})
        return out + self._baseline(c, msg.get("notes"))

    def _baseline(self, c, notes):
        """Non-owned clip's local content (after bind / set_owned): push the session version
        over it only when that loses nothing; otherwise the user decides (revert / claim)."""
        c.awaiting_baseline = False
        try:
            local = protocol.validate_op_payload("snapshot", {"notes": notes})["notes"]
        except protocol.ProtocolError as e:
            return [("print", "script: invalid local_snapshot for %s ignored: %s" % (c.clip, e))]
        if c.revert_pending:      # the user already chose: overwrite whatever is there
            c.revert_pending = False
            c.conflict = c.unsent_conflict = False
            return [("print", "reverting %s to the shared version" % c.clip),
                    self._apply_snapshot_msg(c, "revert", force=True)]
        content = note_content(local)
        same = content == note_content(c.notes.values())
        if c.unsent_conflict and not same:
            return []             # unsent edits (even 'delete all notes') stay until revert/claim
        c.unsent_conflict = False
        # Equal to the replica, or to what the clip is known to hold (it merely lags the replica,
        # e.g. lost/restarted while the owner kept editing): no local edit.
        if local and not same and content != c.expected_local:
            return self._conflict_actions(
                c, "your local %s clip differs from the session's — revert %s to take the "
                   "session version or claim %s" % (c.clip, c.clip, c.clip))
        c.conflict = False
        resolved = c.owner and c.owner not in ("host", "guest")
        if not resolved and not c.notes:
            return []     # nothing to push yet (and no owner to have written anything)
        if not self._can_forward(c):
            return []
        return [self._apply_snapshot_msg(c, "init")]

    def _script_local_delta(self, msg):
        c = self.clips.get(msg.get("clip"))
        if c is None or not self.bound:
            return []
        t = msg.get("t_detect")
        if isinstance(t, (int, float)) and t == t:
            self.detect_lag.append(max(0.0, self.wall() - float(t)))
        if not c.owned:
            return self._set_owned(c) + self._script_conflict(msg)
        return self._local_op(c, "delta", {"added": msg.get("added", []), "modified": msg.get("modified", []),
                                           "removed": msg.get("removed", [])})

    def _script_applied(self, msg):
        c = self.clips.get(msg.get("clip"))
        ms = msg.get("ms")
        if isinstance(ms, (int, float)):
            self.applied_ms.append(float(ms))
        if c is None or msg.get("ok", True):
            if c is not None and self.heal_sent.get(c.clip) == msg.get("op_id"):
                self.heal_sent.pop(c.clip, None)
            if c is not None and not c.owned:
                self._remember_local(c)
            return []
        op_id, err = msg.get("op_id"), msg.get("error")
        if err == "conflict":
            return self._script_conflict(msg)
        if c.conflict or c.paused or not c.found:
            state = "in conflict" if c.conflict else ("paused" if c.paused else "lost")
            return [("print", "script: apply %s on %s failed (%s); clip is %s - not healing"
                     % (op_id, c.clip, err, state))]
        if self.heal_sent.get(c.clip) == op_id:
            self.heal_sent.pop(c.clip, None)
            c.pause()
            return [("print", "ERROR: Live could not apply %s (%s) even after a full snapshot; "
                              "clip paused - fix the clip in Live and type 'resync'" % (c.clip, err))]
        act = self._apply_snapshot_msg(c, "heal")
        self.heal_sent[c.clip] = act[1]["op_id"]
        return [("print", "script: apply %s on %s failed (%s) - re-sending full snapshot"
                 % (op_id, c.clip, err)), act]

    def _script_conflict(self, msg):
        c = self.clips.get(msg.get("clip"))
        if c is None:
            return []
        if c.owned:
            return self._set_owned(c)     # the script thinks it does not own the clip: correct it
        if c.conflict:
            return []                     # already shown; nothing new to say
        return self._conflict_actions(
            c, "%s is owned by the other participant and was edited locally" % c.clip)

    def _script_clip_lost(self, msg):
        c = self.clips.get(msg.get("clip"))
        if c is None:
            return []
        c.found = False
        if not c.owned:
            self._remember_local(c)   # remote ops keep advancing the replica while the clip is away
        return [("print", "script: clip %s lost (%s)" % (c.clip, msg.get("reason", "?")))]

    def _script_clip_found(self, msg):
        c = self.clips.get(msg.get("clip"))
        if c is None:
            return []
        c.found = True
        out = [("print", "script: clip %s found" % c.clip)] + self._set_owned(c)
        if c.owned:
            # The script sends its own snapshot on (re)bind; only unsent edits need a request.
            if c.dirty:
                c.dirty = False
                out.append(("script", {"type": "request_snapshot", "clip": c.clip}))
        else:
            c.unpause()           # a re-found clip gets a fresh chance
            self.heal_sent.pop(c.clip, None)
            # no push here: the script's baseline local_snapshot drives _baseline (set_owned above
            # has flagged awaiting_baseline, so no relayed op slips in before it)
        return out

    # ---------------------------------------------------------------- commands
    def on_command(self, line):
        parts = (line or "").strip().split()
        if not parts:
            return []
        cmd, args = parts[0].lower(), parts[1:]
        if cmd in ("quit", "exit"):
            return [("quit",)]
        if cmd == "status":
            return [("print", self.status_text())]
        if cmd == "help":
            return [("print", "commands: status | revert <clip> | claim <clip> | release <clip> | rebind | setup | resync | quit")]
        if cmd == "setup":
            # Create the missing template tracks/clips in the CURRENT set (script binds afterwards).
            if not (self.welcomed and self.script_connected and self.script_hello is not None):
                return [("print", "setup needs the relay session and Live to be connected")]
            out = self._attach_set()
            if out is None:
                return self._mismatch(self._hello_path()) + [("print", "setup refused: run 'rebind' first")]
            self.bound = True
            out.append(("script", {"type": "setup_template", "clips": [
                {"clip": c.clip, "track_name": c.track_name, "slot": c.slot, "owned": c.owned,
                 "length": 16.0}      # 4 bars in 4/4, as documented
                for c in self.clips.values()]}))
            return out + [("print", "setup: asked Live to create the missing Anthem tracks/clips")]
        if cmd in ("revert", "claim", "release"):
            if not args or args[0] not in self.clips:
                return [("print", "usage: %s <clip>   (clips: %s)" % (cmd, ", ".join(self.clips) or "none yet"))]
            c = self.clips[args[0]]
            if cmd == "revert":
                c.conflict = c.unsent_conflict = False
                c.unpause()
                self.heal_sent.pop(c.clip, None)
                if c.owned:
                    return [("print", "%s is owned by you; nothing to revert" % c.clip)]
                if not (self.script_connected and c.found):
                    c.revert_pending = True     # honoured by _baseline when the clip is back
                    return [("print", "%s: conflict cleared; will re-apply when the clip is available" % c.clip)]
                return [("print", "reverting %s to the shared version" % c.clip),
                        self._apply_snapshot_msg(c, "revert", force=True)]
            if not self.relay_connected:
                return [("print", "relay is disconnected; try again later")]
            return [("relay", {"type": cmd, "clip": c.clip})]
        if cmd == "rebind":
            if self.script_hello is None:
                return [("print", "no Live Set connected yet")]
            self.set_path = self._hello_path()
            self.bind_blocked = False
            out = [self._persist(), ("print", "rebind: session now attached to %s" % (self.set_path or "(unsaved set)"))]
            return out + self._maybe_bind()
        if cmd == "resync":
            out = []
            for c in self.clips.values():
                c.unpause()
                self.heal_sent.pop(c.clip, None)
                if not (self.script_connected and c.found):
                    if not c.owned and not c.conflict:
                        c.revert_pending = True   # forced re-apply once the clip is back
                    continue
                if c.owned:
                    c.dirty = False
                    out.append(("script", {"type": "request_snapshot", "clip": c.clip}))
                elif not c.conflict:
                    out.append(self._apply_snapshot_msg(c, "resync", force=True))
            return [("print", "resync: %d clip(s)" % len(out))] + out
        return [("print", "unknown command %r (try: help)" % cmd)]

    # -------------------------------------------------------------------- tick
    def on_tick(self, now):
        out = []
        if self.relay_connected:
            if self.last_ping is None or now - self.last_ping >= PING_INTERVAL:
                self.last_ping = now
                out.append(("relay", {"type": "ping"}))
            if self.last_rx is not None and now - self.last_rx > 3 * PING_INTERVAL:
                # Not even a pong: a half-open TCP link would otherwise look connected forever.
                out.append(("relay_drop", "no data from relay for %ds" % (now - self.last_rx)))
                self.last_rx = now      # said once; the glue closes the link
            expired = [k for k, (_, t, _) in self.pending.items() if now - t > PENDING_TIMEOUT]
            for k in expired:
                clip, _, _ = self.pending.pop(k)
                c = self.clips.get(clip)
                if c is not None:
                    c.dirty = True
                out.append(("print", "relay: no ack for op %s (%s) after %ds" % (k, clip, PENDING_TIMEOUT)))
        return out

    # ------------------------------------------------------------------ status
    def latency_stats(self):
        def stats(samples):
            s = list(samples)
            if not s:
                return None
            return {"n": len(s), "median": statistics.median(s), "p95": _pct(s, 0.95)}
        return {"op_ack_s": stats(self.ack_latencies), "applied_ms": stats(self.applied_ms),
                "detect_lag_s": stats(self.detect_lag)}

    def status_text(self):
        lines = ["relay: %s%s" % ("connected" if self.relay_connected else "DISCONNECTED",
                                   "" if self.welcomed or not self.relay_connected else " (awaiting welcome)"),
                 "script: %s%s" % ("connected" if self.script_connected else "not connected",
                                    " bound" if self.bound else (" (bind blocked: rebind)" if self.bind_blocked else "")),
                 "session=%s role=%s participant=%s last_seq=%d set=%s pending_ops=%d"
                 % (self.session, self.role, self.participant, self.last_seq, self.set_path, len(self.pending)),
                 "  %-8s %-6s %-6s %-5s %-9s %s" % ("clip", "owner", "found", "rev", "state", "notes")]
        for c in self.clips.values():
            paused = "PAUSED" + (" (%s)" % c.pause_reason if c.pause_reason else "")
            state = "CONFLICT" if c.conflict else (paused if c.paused else ("dirty" if c.dirty else "ok"))
            owner = "you" if c.owned else ("peer" if c.owner and c.owner not in ("host", "guest") else (c.owner or "none"))
            lines.append("  %-8s %-6s %-6s %-5d %-9s %d" % (c.clip, owner, "yes" if c.found else "no", c.rev, state, len(c.notes)))
        st = self.latency_stats()
        a, p, d = st["op_ack_s"], st["applied_ms"], st["detect_lag_s"]
        lines.append("op_ack rtt: %s" % ("n=%d median=%.0fms p95=%.0fms" % (a["n"], a["median"] * 1000, a["p95"] * 1000) if a else "no samples"))
        lines.append("applied.ms: %s" % ("n=%d median=%.1f p95=%.1f" % (p["n"], p["median"], p["p95"]) if p else "no samples"))
        lines.append("detect→companion lag: %s" % ("n=%d median=%.0fms p95=%.0fms" % (d["n"], d["median"] * 1000, d["p95"] * 1000) if d else "no samples"))
        return "\n".join(lines)
