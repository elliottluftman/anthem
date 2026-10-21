# Anthem protocol (v1)

Transport: newline-delimited JSON (one object per line, UTF-8). Every message has
`"v": 1` and `"type"`. Max line 256 KiB. Unknown types or invalid payloads are
rejected and never executed. Definitions live in `anthem_core/protocol.py`;
session/ownership/sequence logic lives in `anthem_core/session.py`.

Three processes per participant machine, plus one shared relay:

```
Ableton Live ──(Remote Script "Anthem", Live.Base.Timer poll, non-blocking loopback TCP)──┐
                                                                                            │ 127.0.0.1:9800
companion (asyncio, owns all network I/O, state, diagnostics) ◄─────────────────────────────┘
       │  TCP (TLS when on the internet)
relay (asyncio: auth, ownership, ordering, bounded op log, optional JSON persistence)
```

## Identity

* `session` id: relay-generated, `[A-Za-z0-9_-]{1,64}`.
* invite `code`: 12 characters from a 30-letter base32 alphabet (no 0/O/1/I/L/U), case-insensitive, single use (retired once the guest has joined); shown to the host, typed by the guest.
* `participant` id: companion-generated (uuid-ish, 1..64 id chars), persisted in
  `~/.anthem/sessions/<session>.json` with its random `secret` (>= 16 chars).
  A second connection presenting an identity that is already online is rejected.
* `clip` id: collaboration id, fixed per session (`drums`, `bass` in the template).
  Live object references are never sent across machines.

## Companion ⇄ relay

| type | direction | fields | notes |
|---|---|---|---|
| `create` | c→r | `participant, secret, clips:[{clip,name,track_name,slot,owner}]` | `owner` is a role: `host` or `guest`. Creator is `host`. |
| `join` | c→r | `code, participant, secret` | Registers the `guest`. Fails if the session already has a guest. |
| `hello` | c→r | `session, participant, secret, last_seq` | Resume. |
| `welcome` | r→c | `session, code, participant, role, seq, snapshot, ops, resync` | `snapshot = {clips, revs, notes:{clip:[note]}}`. `ops` = ops after `last_seq`, or `null` with `resync:true` when they fell out of the bounded log (use the snapshot). |
| `op` | c→r | `op_id, clip, kind, base_rev, payload` | `kind` ∈ `delta`, `snapshot`. See payloads below. |
| `op_ack` | r→c | `op_id, seq, rev, duplicate` | Duplicate `op_id` returns the original seq/rev and is not re-applied/re-broadcast. |
| `op` | r→c | `seq, rev, participant, op_id, clip, kind, payload` | Broadcast to the *other* participants in `seq` order. |
| `reject` | r→c | `op_id, reason` | e.g. `not owner`, `stale base_rev` (owner must resend a `snapshot` op). |
| `claim` / `release` | c→r | `clip` | Claim succeeds if unowned, already yours, or the owner is offline. |
| `ownership` | r→c | `clip, owner` | Broadcast after every ownership change. |
| `presence` | r→c | `participants:[{id, role, online}]` | Broadcast on connect/disconnect. |
| `error` | r→c | `reason` | Followed by connection close for auth failures. |
| `ping`/`pong` | both | | Keep-alive; relay drops idle connections after 90 s. |

### Note (wire form)

`{"id": int, "pitch": 0..127, "start": beats, "dur": beats > 0, "vel": 0..127, "mute": bool,
  "prob": 0..1, "vel_dev": -127..127, "rel_vel": 0..127}`

`id` is the **owner's** Live `note_id` for that clip. Receivers keep a private
mapping owner-id → local Live note_id (see `remote_script/Anthem/clipsync.py`).

### Op payloads

* `snapshot`: `{notes:[note]}` — authoritative full content of the clip. Idempotent.
* `delta`: `{added:[note], modified:[note], removed:[int]}` — must be non-empty and
  carry `base_rev` equal to the clip's current revision at the relay.

Ordering: relay-assigned `seq` (session-wide) and `rev` (per clip). No wall clocks.

## Remote Script ⇄ companion (loopback only)

| type | direction | fields |
|---|---|---|
| `script_hello` | s→c | `live_version, set_name, set_path, script_version, token` — sent on connect and whenever Live re-instantiates the script (which happens when a different Set is opened). `token` is read from `~/.anthem/script.token` (written 0600 by the companion at start); the companion ignores every other message from a connection whose token does not match, and does not displace an authenticated Live connection for an unauthenticated one. An empty `set_path` (unsaved set) is treated as *unknown project*: the companion warns that the different-project guard is inactive, and a later `script_hello` with an empty path while bound is treated as a mismatch (requires `rebind`). |
| `bind` | c→s | `clips:[{clip, track_name, slot, owned}]` |
| `set_owned` | c→s | `clip, owned` — ownership changed at runtime (after `claim`/`release`/reconnect). The script resets that binding's state; newly owned → it sends a `local_snapshot`; no longer owned → it waits for `apply_snapshot`. |
| `inventory` | s→c | `clips:[{clip, found, track_name, slot, owned, note_count}]`, `tracks:[names]` |
| `local_snapshot` | s→c | `clip, notes, owned` — after bind for every found clip and on `request_snapshot`. For owned clips it is authoritative (companion → relay `snapshot` op). For non-owned clips it is informational: the companion compares it with the replica and, if the local clip has notes that differ, enters the CONFLICT state instead of overwriting them with the init snapshot. |
| `local_delta` | s→c | `clip, added, modified, removed, t_detect` — one per notes-listener firing on an owned clip. |
| `apply_snapshot` | c→s | `clip, op_id, notes, force?` — `force:true` is sent only for the explicit `revert`/`resync` commands and overwrites a local edit on purpose. |
| `apply_delta` | c→s | `clip, op_id, added, modified, removed` |
| `applied` | s→c | `clip, op_id, ok, error?, ms` (`ms` = time spent applying). Before writing, the script compares the clip with the content it expects; if a local edit is present it does **not** write, replies `ok:false, error:"conflict"` and raises `conflict`. `error:"content differs after apply"` means Live altered the written result (e.g. merged overlapping notes); the companion answers with one authoritative snapshot unless the clip is in conflict, paused or lost. Oversized clips (> 2000 notes) cannot be sent; the script reports `clip_lost` with reason `too large` and stops syncing that clip. |
| `request_snapshot` | c→s | `clip` |
| `setup_template` | c→s | same `clips` as `bind` (+ optional `length`) — creates missing template MIDI tracks/clips in the **current** set, then binds. Only sent on an explicit user command (`setup`). |
| `clip_lost` / `clip_found` | s→c | `clip, reason?` |
| `conflict` | s→c | `clip` — the user edited a clip they do not own. |
| `show_message` | c→s | `msg` (shown in Live's status bar) |
| `log` | s→c | `level, msg` |

## Feedback-loop suppression

* A participant only ever *sends* `local_delta`/`local_snapshot` for clips it owns,
  and only *applies* remote ops to clips it does not own. Ownership is enforced by
  the relay too, so an echo cannot be accepted even if a client misbehaves.
* On the receiving side, the script marks `applying=True` around Live API writes and,
  after the notes listener fires, compares the clip's content with the expected
  content. Equal → ignore. Different and not applying → `conflict`.
* Duplicate `op_id`s are acknowledged but not re-applied or re-broadcast.

## Conflict handling

Live cannot make a clip read-only. When a user edits a clip they do not own:
1. The script emits `conflict`; the companion shows a warning in Live and in its
   console, and **pauses** applying remote ops to that clip (keeping only the
   latest authoritative state, so the queue stays bounded).
2. The user resolves with `revert <clip>` (companion re-applies the authoritative
   snapshot, discarding the local edit *by explicit choice*) or `claim <clip>`
   (relay grants ownership only if the owner is offline or released it; the local
   content then becomes authoritative via a `snapshot` op).

## Reconnection

Companion reconnects with exponential backoff (1 s → 30 s), sends `hello` with
`last_seq`, and applies the returned ops or the full snapshot before resuming
writes. Owned clips re-send a `snapshot` op after every reconnect, which also
heals any `stale base_rev` rejection. While disconnected, the latest local
state per owned clip is kept (not a growing queue). If the relay answers `hello`
with a permanent auth error (`unknown session`, `unknown participant`, `bad secret`)
the companion stops reconnecting and tells the user to create/join again;
`participant already connected` and transport failures keep retrying.

If ownership of a clip moved away while this participant was offline and it has
unsent local edits, the companion does **not** overwrite them: the clip enters the
CONFLICT state and the user chooses `revert` or `claim`.

The relay bounds `welcome` to one line: when snapshot + missed ops would exceed the
line limit it sends `ops: null, resync: true`; if the snapshot alone is too large it
replies `error "session too large"` instead of closing silently. Invite codes are 12
base32 characters, are retired once the guest has joined, and failed `join`/`hello`
attempts are throttled per source address.

## Project change safety

The companion records the Live Set path at first bind. If `script_hello` later
reports a different `set_path`, the companion stops syncing and asks the user to
run `rebind` explicitly. Old session state is never written into a new project.
