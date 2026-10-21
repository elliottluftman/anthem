# Developer handoff — Anthem (Ableton ↔ Ableton MIDI clip collaboration PoC)

Date: 2026-10-16. Status by verification level is in the README; this document
explains how it is built, why, and what is still open.

## 1. Architecture

```
 Machine A                                   Machine B
 ┌──────────────────────────┐                ┌──────────────────────────┐
 │ Ableton Live             │                │ Ableton Live             │
 │  Remote Script "Anthem"  │                │  Remote Script "Anthem"  │
 │  (Live main thread,      │                │                          │
 │   Live.Base.Timer 50 ms) │                │                          │
 └──────┬───────────────────┘                └──────┬───────────────────┘
        │ loopback TCP 127.0.0.1:9800, NDJSON         │
 ┌──────┴───────────────────┐                ┌──────┴───────────────────┐
 │ companion/anthem.py      │                │ companion/anthem.py      │
 │  asyncio; identity file; │                │                          │
 │  replica; diagnostics    │                │                          │
 └──────┬───────────────────┘                └──────┬───────────────────┘
        │ TCP (+TLS), NDJSON                         │
        └───────────────► relay/relay.py ◄────────────┘
                 auth · ownership · seq/rev · bounded log · optional JSON state
```

Three design rules drive everything:

1. **Live never blocks.** The Remote Script does no network I/O beyond a non-blocking
   loopback socket polled from a 50 ms Live timer (`remote_script/Anthem/bridge.py`).
   There are no threads inside Live. Everything that can wait (internet, retries,
   reconnects) lives in the companion process.
2. **Ownership instead of merging.** Each clip has exactly one owner; only the owner's
   edits are accepted by the relay. This removes concurrent-edit conflicts from the
   prototype and makes feedback loops structurally impossible (a participant never
   sends for clips it does not own and never applies to clips it owns).
3. **Incremental, id-based edits.** Live 11+/12 exposes stable per-clip `note_id`s.
   The owner diffs the clip by id (`clipsync.diff`) and sends add/modify/remove sets;
   the receiver applies them with `add_new_notes` / `apply_note_modifications` /
   `remove_notes_by_id` through an owner-id → local-id map. Snapshots exist for
   join/reconnect/repair and are applied by content matching, so an unchanged
   snapshot costs zero writes.

### Components

| Path | Role | Lines |
|---|---|---|
| `remote_script/Anthem/anthem.py` | ControlSurface subclass: timer, message dispatch, bind/setup/reload | ~200 |
| `remote_script/Anthem/clipbinding.py` | one collaboration clip ↔ one Live clip; owner diffing, receiver apply, conflict detection | ~330 |
| `remote_script/Anthem/bridge.py` | non-blocking NDJSON loopback client with reconnect, bounded outbox | ~170 |
| `remote_script/Anthem/clipsync.py` | pure diff/compare helpers + id map | ~90 |
| `anthem_core/protocol.py` | message types, strict validation, size limits | ~180 |
| `anthem_core/session.py` | relay-side session: participants, roles, ownership, seq/rev, bounded log, persistence | ~210 |
| `relay/relay.py` | asyncio server around `Session` (TLS, limits, rate limit, persistence) | ~460 |
| `companion/core.py` | pure companion state machine (testable without sockets) | — |
| `companion/anthem.py` | CLI + asyncio glue (install/create/join/resume/status) | — |
| `tools/live_verify.py` | Milestone-1 driver run against real Live | ~250 |
| `tests/` | pytest: core, relay, remote script (mock Live), headless surface, companion | — |

## 2. Protocol

See `docs/PROTOCOL.md`. Key properties: protocol version on every message; relay-assigned
`seq` (session) and `rev` (clip) for ordering — no wall clocks; per-op `op_id` for
idempotency; `base_rev` on deltas so a stale owner is told to resend a snapshot;
strict validation with size caps (256 KiB/line, 2000 notes per op and per clip, 1500 per session so the welcome always fits one line); roles (`host`/`guest`)
resolved to participant ids at join; secrets hashed at the relay.

## 3. Object mapping and how it evolves

Today the mapping is a fixed template: collaboration clip id → (`track_name`, Session
slot index). Both sides run `setup`, which creates missing tracks/clips via the API.
Live object identity is re-resolved on `tracks`, `name` and `has_clip` listener events,
so track reordering is harmless and rename/deletion are reported as `clip_lost`.

Next step for arbitrary projects: store a collaboration id on the Live object itself.
Candidates: encode it in the clip name suffix (visible, survives save/load, user can
break it) or in a hidden clip annotation; `Clip`/`Track` have no custom-metadata API.
Project reconstruction (devices, presets, samples) is a separate problem from edit sync.

## 4. Conflicts, ownership, recovery

* Relay enforces ownership; unauthorized ops are rejected explicitly (`reject`), the
  clip state is untouched.
* Local edits on a non-owned clip: the script's post-listener content check raises
  `conflict`; the companion pauses writes to that clip and offers `revert` (explicit
  discard) or `claim` (allowed only when the owner is offline or released). Nothing is
  silently discarded, but there is also no merge.
* Reconnect: `hello(last_seq)` → missed ops or full snapshot; owned clips re-snapshot.
* Different set opened: Live re-instantiates the script → `script_hello` with a new
  `set_path` → companion refuses to bind until `rebind`.

## 5. Performance (measured, one machine, Live 12.4.1)

| Stage | Measured |
|---|---|
| Native-edit detection (listener fire after edit) | 25–43 ms for API edits; human edits reported 150–200 ms after gesture end including the 100 ms debounce and 50 ms tick |
| Apply inside Live | 0.2–7 ms per op (`demo_one_live.py` run: n=8, median 1.3 ms, p95 3.0 ms) |
| Relay op_ack round trip (loopback relay) | median 1 ms, p95 2 ms (n=5) — WAN adds the network RTT |
| Loopback round trip script↔driver | ~100 ms (dominated by the 50 ms tick each way) |
| Full path edit → other side (relay + 2 companions + headless script, loopback) | ≈ 174 ms for the first edit (`tests/test_e2e_sim.py`); WAN adds the network RTT — companion `status` reports op_ack median/p95 |

Bursts: owner debounce coalesces a gesture into one delta; receiver applies a delta as
three Live calls at most. Outbox in Live is capped at 256 messages (overflow → snapshot).
Companion keeps latest-state only while offline.

## 6. Security posture (test level)

Per-participant random secret (hashed server-side), relay-side ownership checks,
strict schema validation, line/notes caps (2000 notes per op and per clip), 200 msg/10 s
rate limit, 90 s idle timeout, max 200 connections / 1000 sessions plus per-address
connection and session-creation caps, failed-auth throttling, 12-character invite codes
retired after join, optional `--create-token`, TLS optional via `--cert/--key`.
Companion ↔ Remote Script loopback is protected by a per-run token in
`~/.anthem/script.token`. No code or file paths are accepted from the wire. Secrets are
stored 0600 in `~/.anthem` and are never logged.

## 6b. Review history

Three multi-agent adversarial review rounds were run on 2026-10-16 (each finding verified by
two independent refuters before being accepted): 28, 21 and 16 confirmed findings, all fixed
with regression tests. Recurring themes worth keeping in mind when extending the code: keep the
script's `owned` flag and the companion's in lock-step (`set_owned`), never write into a clip
whose content differs from what was last known without an explicit user decision, and bound
every message/log/queue by bytes as well as by count.

## 7. Unresolved risks / open questions

1. **Two-machine verification has not been run yet.** Everything between two Lives is
   proven only with real sockets + a headless copy of the Remote Script.
2. Undocumented API: Ableton does not document the Remote Script Python API for project
   manipulation. Three consecutive Live versions behaved identically today, but a
   future change could break `apply_note_modifications` semantics or the listener.
3. Live's overlap rule (same-pitch overlapping notes are merged) can make a receiver
   diverge in theory; the script detects and reports it and the companion re-snapshots.
4. Remote edits enter the receiver's undo history; the receiver's Undo therefore creates a
   conflict rather than an undo of "their own" action. Acceptable for the PoC, not for a
   product — would need an undo-aware strategy (e.g. re-apply on `can_undo` changes).
5. Unsaved sets report an empty `file_path`; the companion treats that as "unknown project"
   (warns, and requires `rebind` if an empty path shows up again), but it cannot tell two
   unsaved sets apart. Saving the disposable set once makes the guard precise.
6. Windows and Live Standard/11 are untested.
7. Max for Live is not needed; the Extensions SDK was not investigated (run-once model).

## 8. Suggested next steps

1. Run `docs/TEST_CHECKLIST.md` on two machines; record latencies.
2. Add velocity/expression human tests.
3. Replace the fixed template with ids stored on the clip and a "share this clip" picker.
4. Measure audio-thread impact under load (Live CPU meter, dropouts) with a busy set.
5. Companion UI (tray app) replacing the CLI; deep-link invite handling.
