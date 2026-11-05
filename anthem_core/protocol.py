"""Wire protocol: newline-delimited JSON (NDJSON), one object per line, UTF-8.

Used on two links:
  * Remote Script <-> Companion (loopback TCP, never leaves the machine)
  * Companion     <-> Relay     (TCP, TLS when the relay is on the internet)

Every message carries ``v`` (protocol version) and ``type``. Validation is strict:
unknown types, oversized lines and malformed payloads are rejected, never executed.
"""
import json
import re

PROTOCOL_VERSION = 1
MAX_LINE_BYTES = 256 * 1024       # hard cap per message on the wire
MAX_NOTES_PER_OP = 2000           # ~115 bytes per wire note: 2000 notes always fit in MAX_LINE_BYTES
MAX_NOTES_PER_CLIP = 2000         # relay rejects ops that would push a clip above this
# Soft cap on the notes of ALL clips of a session together. A resuming client receives the whole
# session as one ``welcome`` line (snapshot of every clip), so the per-clip cap alone is not
# enough: two 1200-note clips already do not fit MAX_LINE_BYTES, and a session whose welcome
# does not fit can never be resumed again. The relay rejects an op that would push the session
# above this count (reason "clip too large") and, above session.WELCOME_CHECK_NOTES, additionally
# measures the welcome line itself. ~115 bytes per wire note: 1500 notes leave ample room.
MAX_NOTES_PER_SESSION = 1500
MAX_TRACKS = 16

# Renders: one loop of a track's audio output, shipped as a 24-bit (or 16-bit) PCM WAV so a
# participant who lacks a plug-in can still hear it. Blobs travel in base64 chunks of
# BLOB_CHUNK_BYTES raw bytes, one chunk per wire line, flow-controlled by the receiver.
MAX_RENDER_BEATS = 256.0
MAX_BLOB_BYTES = 32 * 1024 * 1024      # one render file
BLOB_CHUNK_BYTES = 160 * 1024          # raw bytes per chunk (~214 KiB base64 + envelope < MAX_LINE_BYTES)
MAX_PLUGIN_NAMES = 4096                # installed plug-ins one participant may announce (a real collection had 1084)
MAX_NAME_LEN = 128

ID_RE = re.compile(r"^[A-Za-z0-9_-]{1,64}$")
SHA_RE = re.compile(r"^[0-9a-f]{64}$")

# --- relay <-> companion message types ---------------------------------------
RELAY_TYPES = {
    # companion -> relay
    "create",     # {participant, secret, clips:[{clip, name, track_name, slot, owner:"host"|"guest"}]}
    "join",       # {code, participant, secret}           first-time join by invite code
    "hello",      # {session, participant, secret, last_seq} resume an existing identity
    "op",         # {op_id, clip, kind, base_rev, payload}
    "claim",      # {clip}
    "release",    # {clip}
    "ping",
    "info",       # {plugins:[name], tracks:{clip:[plugin name]}}  what this participant has installed / uses
    "blob_put",   # {blob, idx, n, size, data(b64)}  upload one chunk of a render file
    "blob_get",   # {blob, idx}                       download one chunk (receiver paces itself)
    # relay -> companion
    "welcome",    # {session, code, participant, seq, snapshot:{clips, revs, notes, renders}, ops:[...]|null, resync:bool}
    "op",         # relayed op: {seq, rev, participant, op_id, clip, kind, payload}
    "op_ack",     # {op_id, seq, rev, duplicate}
    "reject",     # {op_id, reason}
    "ownership",  # {clip, owner}
    "presence",   # {participants:[{id, online}]}
    "error",      # {reason}
    "pong",
    "info",       # relayed: {participant, plugins, tracks}
    "blob_ack",   # {blob, idx, ok, error?, complete}
    "blob_chunk", # {blob, idx, n, size, data, ok} or {blob, ok:false, error}
}

# --- remote script <-> companion message types -------------------------------
LOCAL_TYPES = {
    # script -> companion
    "script_hello",   # {live_version, set_name, set_path, script_version, token}
    "inventory",      # {clips:[{clip, found, track_name, slot, owned, note_count}], tracks:[names]}
    "local_delta",    # {clip, added:[note], modified:[note], removed:[note_id], t_detect}  owned clips only
    "local_snapshot", # {clip, notes:[note], owned}  owned clips: authoritative; non-owned: sent once at bind
                      #   (and on request) so the companion can detect pre-existing local edits
    "clip_lost",      # {clip, reason}
    "clip_found",     # {clip}
    "conflict",       # {clip} local edit detected on a clip this participant does not own
    "applied",        # {clip, op_id, ok, error?, ms}  error "conflict" = local edit present, write skipped
    "render_captured",  # {clip, render_id, ok, error?, path, beats, ms}  owner: one loop recorded to a file
    "render_applied",   # {clip, render_id, ok, error?, ms}  receiver: file placed as an audio clip
    "log",            # {level, msg}
    # companion -> script
    "bind",           # {clips:[{clip, track_name, slot, owned}]}
    "set_owned",      # {clip, owned}  ownership changed at runtime (claim/release/reconnect)
    "apply_delta",    # {clip, op_id, added, modified, removed}
    "apply_snapshot", # {clip, op_id, notes, force?}  force=true (revert/resync) overwrites a local edit on purpose
    "request_snapshot",  # {clip}
    "setup_template", # {clips:[{clip, track_name, slot, owned, length?}]} create missing tracks/clips, then bind
    "render_capture", # {clip, render_id}  owner: record one loop of the clip's track output (API capture)
    "apply_render",   # {clip, render_id, path, beats}  receiver: put the file in the "<track> (render)" track
    "reload_code",    # dev only: reload the script's pure modules and re-bind (loopback, no payload)
    "show_message",   # {msg}
}

OP_KINDS = {"delta", "snapshot", "render"}
RENDER_FIELDS = ("render_id", "beats", "sample_rate", "channels", "bits", "size", "sha256", "n_chunks")

NOTE_FIELDS = ("id", "pitch", "start", "dur", "vel", "mute", "prob", "vel_dev", "rel_vel")


class ProtocolError(ValueError):
    pass


def encode(msg):
    """Serialize one message to a single NDJSON line (bytes, trailing newline)."""
    msg = dict(msg)
    msg.setdefault("v", PROTOCOL_VERSION)
    line = json.dumps(msg, separators=(",", ":"), ensure_ascii=True).encode("utf-8") + b"\n"
    if len(line) > MAX_LINE_BYTES:
        raise ProtocolError("message too large (%d bytes)" % len(line))
    return line


def decode(line, allowed_types):
    """Parse one line; raise ProtocolError on anything suspicious."""
    if len(line) > MAX_LINE_BYTES:
        raise ProtocolError("line too large")
    try:
        msg = json.loads(line.decode("utf-8"))
    except (UnicodeDecodeError, ValueError) as e:
        raise ProtocolError("bad json: %s" % e)
    if not isinstance(msg, dict):
        raise ProtocolError("message must be an object")
    if msg.get("v") != PROTOCOL_VERSION:
        raise ProtocolError("unsupported protocol version %r" % (msg.get("v"),))
    t = msg.get("type")
    if t not in allowed_types:
        raise ProtocolError("unknown message type %r" % (t,))
    return msg


def valid_id(s):
    return isinstance(s, str) and bool(ID_RE.match(s))


def validate_note(n):
    """A note as it travels on the wire. ``id`` is the owner's Live note_id (int)."""
    if not isinstance(n, dict):
        raise ProtocolError("note must be an object")
    if not isinstance(n.get("id"), int) or n["id"] < 0:
        raise ProtocolError("note.id must be a non-negative int")
    if not isinstance(n.get("pitch"), int) or not 0 <= n["pitch"] <= 127:
        raise ProtocolError("note.pitch out of range")
    for k in ("start", "dur"):
        v = n.get(k)
        if not isinstance(v, (int, float)) or v != v or abs(v) > 1e7:
            raise ProtocolError("note.%s invalid" % k)
    if n["dur"] <= 0:
        raise ProtocolError("note.dur must be > 0")
    vel = n.get("vel", 100)
    if not isinstance(vel, (int, float)) or not 0 <= vel <= 127:
        raise ProtocolError("note.vel out of range")
    if not isinstance(n.get("mute", False), bool):
        raise ProtocolError("note.mute must be bool")
    prob = n.get("prob", 1.0)
    if not isinstance(prob, (int, float)) or not 0 <= prob <= 1:
        raise ProtocolError("note.prob out of range")
    vd = n.get("vel_dev", 0.0)
    if not isinstance(vd, (int, float)) or not -127 <= vd <= 127:
        raise ProtocolError("note.vel_dev out of range")
    rv = n.get("rel_vel", 64.0)
    if not isinstance(rv, (int, float)) or not 0 <= rv <= 127:
        raise ProtocolError("note.rel_vel out of range")
    return {
        "id": n["id"], "pitch": n["pitch"], "start": float(n["start"]), "dur": float(n["dur"]),
        "vel": float(vel), "mute": bool(n.get("mute", False)), "prob": float(prob),
        "vel_dev": float(vd), "rel_vel": float(rv),
    }


def validate_render(p):
    """Metadata of one render blob (no audio in it). Returns the normalized dict."""
    if not isinstance(p, dict):
        raise ProtocolError("render must be an object")
    if not valid_id(p.get("render_id")):
        raise ProtocolError("render.render_id invalid")
    beats = p.get("beats")
    if not isinstance(beats, (int, float)) or beats != beats or not 0 < beats <= MAX_RENDER_BEATS:
        raise ProtocolError("render.beats invalid")
    sr = p.get("sample_rate")
    if not isinstance(sr, int) or isinstance(sr, bool) or not 8000 <= sr <= 192000:
        raise ProtocolError("render.sample_rate invalid")
    ch = p.get("channels")
    if ch not in (1, 2) or isinstance(ch, bool):
        raise ProtocolError("render.channels invalid")
    bits = p.get("bits")
    if bits not in (16, 24) or isinstance(bits, bool):
        raise ProtocolError("render.bits invalid")
    size = p.get("size")
    if not isinstance(size, int) or isinstance(size, bool) or not 0 < size <= MAX_BLOB_BYTES:
        raise ProtocolError("render.size invalid")
    sha = p.get("sha256")
    if not isinstance(sha, str) or not SHA_RE.match(sha):
        raise ProtocolError("render.sha256 invalid")
    n = p.get("n_chunks")
    if not isinstance(n, int) or isinstance(n, bool) or n != -(-size // BLOB_CHUNK_BYTES):
        raise ProtocolError("render.n_chunks invalid")
    return {"render_id": p["render_id"], "beats": float(beats), "sample_rate": sr, "channels": ch,
            "bits": bits, "size": size, "sha256": sha, "n_chunks": n}


def validate_info(msg):
    """``info``: installed plug-in names and, per clip, the plug-ins on that clip's track."""
    plugins = msg.get("plugins", [])
    tracks = msg.get("tracks", {})
    if not isinstance(plugins, list) or len(plugins) > MAX_PLUGIN_NAMES:
        raise ProtocolError("info.plugins invalid")
    if not all(isinstance(x, str) and 0 < len(x) <= MAX_NAME_LEN for x in plugins):
        raise ProtocolError("info.plugins invalid")
    if not isinstance(tracks, dict) or len(tracks) > MAX_TRACKS:
        raise ProtocolError("info.tracks invalid")
    out = {}
    for clip, names in tracks.items():
        if not valid_id(clip) or not isinstance(names, list) or len(names) > 64:
            raise ProtocolError("info.tracks invalid")
        if not all(isinstance(x, str) and 0 < len(x) <= MAX_NAME_LEN for x in names):
            raise ProtocolError("info.tracks invalid")
        out[clip] = list(names)
    return {"plugins": sorted(set(plugins)), "tracks": out}


def validate_blob_chunk(msg, need_data=True):
    """Shape of a ``blob_put`` (and relayed ``blob_chunk``) message. Returns (blob, idx, n, size, data)."""
    blob = msg.get("blob")
    if not valid_id(blob):
        raise ProtocolError("blob id invalid")
    idx, n, size = msg.get("idx"), msg.get("n"), msg.get("size")
    for v in (idx, n, size):
        if not isinstance(v, int) or isinstance(v, bool) or v < 0:
            raise ProtocolError("blob chunk fields invalid")
    if not 0 < size <= MAX_BLOB_BYTES or n != -(-size // BLOB_CHUNK_BYTES) or idx >= n:
        raise ProtocolError("blob chunk fields invalid")
    data = msg.get("data")
    if need_data:
        if not isinstance(data, str) or len(data) > (BLOB_CHUNK_BYTES * 4) // 3 + 8:
            raise ProtocolError("blob chunk data invalid")
    return blob, idx, n, size, data


def validate_op_payload(kind, payload):
    """Validate and normalize an op payload. Returns the normalized payload."""
    if not isinstance(payload, dict):
        raise ProtocolError("payload must be an object")
    if kind == "render":
        return validate_render(payload)
    if kind == "snapshot":
        notes = payload.get("notes")
        if not isinstance(notes, list) or len(notes) > MAX_NOTES_PER_OP:
            raise ProtocolError("snapshot.notes invalid")
        out = [validate_note(n) for n in notes]
        if len({n["id"] for n in out}) != len(out):
            raise ProtocolError("duplicate note ids in snapshot")
        return {"notes": out}
    if kind == "delta":
        added = payload.get("added", [])
        modified = payload.get("modified", [])
        removed = payload.get("removed", [])
        if not all(isinstance(x, list) for x in (added, modified, removed)):
            raise ProtocolError("delta lists invalid")
        if len(added) + len(modified) + len(removed) > MAX_NOTES_PER_OP:
            raise ProtocolError("delta too large")
        if len(added) + len(modified) + len(removed) == 0:
            raise ProtocolError("empty delta")
        if not all(isinstance(i, int) and i >= 0 for i in removed):
            raise ProtocolError("delta.removed must be ints")
        return {
            "added": [validate_note(n) for n in added],
            "modified": [validate_note(n) for n in modified],
            "removed": list(removed),
        }
    raise ProtocolError("unknown op kind %r" % (kind,))


def validate_op(msg):
    """Validate a companion->relay ``op`` message. Returns normalized op dict."""
    for k in ("op_id", "clip"):
        if not valid_id(msg.get(k)):
            raise ProtocolError("op.%s invalid" % k)
    kind = msg.get("kind")
    if kind not in OP_KINDS:
        raise ProtocolError("op.kind invalid")
    base_rev = msg.get("base_rev", 0)
    if not isinstance(base_rev, int) or base_rev < 0:
        raise ProtocolError("op.base_rev invalid")
    return {
        "op_id": msg["op_id"], "clip": msg["clip"], "kind": kind,
        "base_rev": base_rev, "payload": validate_op_payload(kind, msg.get("payload")),
    }
