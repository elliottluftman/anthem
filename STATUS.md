# Anthem — project status (2026-10-21)

Native Ableton ↔ Ableton MIDI clip collaboration, proof of concept. Two producers open
their own Live, join one session, edit notes in Live's normal piano roll, and see each
other's notes appear in their own native clips. No Max for Live, no export/bounce, no
separate editor.

This file is the single place to read first. It says what exists, how far each part has
been verified, and what is still untested. The detailed documents it points to live in
`docs/`.

## 1. What has been built

| Component | Path | What it does |
|---|---|---|
| Remote Script | `remote_script/Anthem/` | Runs inside Live (Python 3.11 embedded). Finds the two template clips, watches Live's `notes` listener on the clip you own, diffs by Live `note_id`, sends deltas; applies the other person's edits with `add_new_notes` / `apply_note_modifications` / `remove_notes_by_id`. Never blocks: one non-blocking loopback socket polled from a 50 ms `Live.Base.Timer`, no threads. |
| Companion | `companion/anthem.py`, `companion/core.py` | Local CLI process. `install`, `create`, `join`, `resume`, `status`. Keeps the relay link (reconnect/backoff), the per-clip replica, ownership, conflicts, diagnostics (`~/.anthem/anthem.log`). `core.py` is a pure state machine (no I/O) so it is fully unit-tested. |
| Relay | `relay/relay.py` | asyncio server: create/join/resume, participant secrets (hashed), ownership enforcement, relay-assigned sequence/revision numbers, bounded op log, optional JSON persistence, optional TLS, per-IP limits, invite-code and create-token throttling. |
| Core | `anthem_core/protocol.py`, `anthem_core/session.py` | Wire protocol (NDJSON, strict validation, size caps) and session/ownership/ordering logic shared by relay and tests. |
| Tools | `tools/live_verify.py`, `tools/demo_one_live.py` | Drivers that exercise the Remote Script inside a real Live, and a real-Live-plus-simulated-guest demo through the real relay/companions. |
| Tests | `tests/` | 156 pytest tests: protocol, session, relay over real sockets (incl. TLS), companion state machine, Remote Script against mocked Live objects, headless surface, two-simulated-clients end-to-end. |
| Docs | `docs/` | `SETUP.md` (participant instructions), `TEST_CHECKLIST.md` (two-computer acceptance test), `PROTOCOL.md`, `CAPABILITY_MATRIX.md` (what was verified in Live and how), `LIMITATIONS.md`, `HANDOFF.md` (architecture, risks, next steps). |

Scope of the prototype (on purpose): **MIDI notes only**, in **two fixed Session clips** —
`Anthem Drums` slot 1 (owned by the host) and `Anthem Bass` slot 1 (owned by the guest).
Pitch, position, length, velocity, mute, probability, velocity deviation, release velocity.
Nothing else syncs (instruments, devices, audio, automation, tempo, clip settings, other
tracks). One owner per clip; editing the other person's clip raises a conflict with
`revert`/`claim`, never a merge. Full list: `docs/LIMITATIONS.md`.

## 2. Verification — what is proven, at which level

| Level | Status | Evidence |
|---|---|---|
| Implemented | ✅ | this repository |
| Tested automatically | ✅ 156/156 | `ANTHEM_HOME=/tmp/anthem-test python3 -m pytest tests -q` |
| Tested against mocked Ableton objects | ✅ | `tests/mock_live.py`, `tests/test_remote_script.py` (clearly mocked, not Live) |
| Two simulated clients end to end | ✅ | `tests/test_e2e_sim.py`: real relay + two real companions + the real Remote Script code on mocked sets. Draw/move/resize/velocity/delete both ways, 40-note burst, conflict + revert, ownership handover via `claim`, duplicate identity rejected, clip deleted/recreated, relay restart with persistence, different-set guard. |
| Verified inside **one** actual Ableton (Live 12.4.1 Suite, macOS 26) | ✅ | `tools/live_verify.py` 11/11: template creation, apply add/move/resize/velocity/mute/pitch/delete, idempotent snapshots, no echo, listener detection 25–43 ms. A human's piano-roll edits (draw, move, transpose, resize, Cmd+D, delete) were detected with stable note ids (`tools/live_verify_phaseB.txt`). `tools/demo_one_live.py`: real Live host + simulated guest through the real relay — guest's bass line appeared in Live, the human's drum edits reached the guest (`tools/demo_one_live.txt`, `tools/demo_one_live_final.txt`). |
| Verified between **two** actual Ableton installations | ❌ **not yet** | This is the next thing to do. Until it is done, do not describe the prototype as working end to end. |

Measured on one machine: apply inside Live 0.2–7 ms per op (median ~1.3 ms), listener
fires 25–43 ms after an edit, full path edit → other side ≈ 174 ms on loopback (a WAN adds
its round trip). Relay ack median 1 ms on loopback.

Hardening: three adversarial multi-agent review rounds were run (each finding independently
reproduced by two reviewers before being accepted) — 28 + 21 + 16 confirmed defects, all
fixed with regression tests. See `docs/HANDOFF.md` §6b.

## 3. What still needs testing

1. **Two real Live installations, two machines** — run `docs/TEST_CHECKLIST.md` end to
   end and record Live versions, OS versions, latencies from `status`, and anything odd.
   This is the one result that matters most.
2. **Velocity and note-expression edits made by a human** (only API-applied so far; the
   tester had not found the velocity lane).
3. **Undo/redo behaviour across machines** (expected: remote edits enter the receiver's undo
   history; receiver's Undo on the other person's clip → conflict → `revert`). Not observed yet.
4. **Live 12 Standard** (expected to work — no Max for Live used), **Windows** (paths are
   handled, never run), **Live 11** (untested; the note API exists since 11.0).
5. **Audio dropouts / CPU under load** with a busy set while syncing (casual observation only).
6. **Internet relay with TLS** on a real host (TLS is tested with a self-signed cert in the
   suite; no hosted relay exists yet).
7. **Reconnect over a real network change** (Wi‑Fi switch, laptop sleep) — dead-link detection
   is implemented (90 s) and unit-tested, not field-tested.

## 4. How to run it

Quick smoke on one machine (relay + host; a second Live needs a second machine):

```
ANTHEM_HOME=/tmp/anthem-test python3 -m pytest tests -q          # all tests
python3 relay/relay.py --host 0.0.0.0 --port 9801 --state ~/.anthem/relay-state.json
python3 companion/anthem.py install        # then Live ▸ Settings ▸ Link, Tempo & MIDI ▸ Control Surface: Anthem
python3 companion/anthem.py create --relay RELAY_IP:9801          # host; type `setup`
python3 companion/anthem.py join CODE --relay RELAY_IP:9801       # guest (other machine); type `setup`
```

Verify the Live integration alone (Live running with Anthem selected):
`python3 tools/live_verify.py` — phase A is automatic, phase B asks you to edit notes.

Participant instructions: `docs/SETUP.md`. Companion commands while running:
`status | setup | revert <clip> | claim <clip> | release <clip> | resync | rebind | quit`.

## 5. Where to continue (for the next developer / AI assistant)

Read in this order: `STATUS.md` (this) → `docs/HANDOFF.md` → `docs/PROTOCOL.md` →
`docs/LIMITATIONS.md` → `docs/CAPABILITY_MATRIX.md`. `CLAUDE.md` has the working
conventions (how to test, how to verify in Live, what not to claim).

Suggested next steps, in order:
1. Run the two-machine test; fold results into `docs/CAPABILITY_MATRIX.md`.
2. Replace the fixed template with a "share this clip" picker: store a collaboration id on
   the clip (clip-name suffix or similar) instead of binding by track name + slot.
3. Minimal companion UI (tray/menu-bar app) replacing the terminal; invite deep links.
4. Playback/tempo sync as its own design (clock, scheduling, latency) — out of scope today.
5. Hosted relay (TLS, create token) for testers who are not on one LAN.

Known open risks: Ableton does not document the Remote Script API for project manipulation
(three Live versions in one day behaved identically, but a future update could change it);
Live merges overlapping same-pitch notes (detected and healed by a snapshot, not silently
ignored); unsaved sets have no file path, so save the test set once.
