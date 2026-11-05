# Anthem — native Ableton ↔ Ableton MIDI clip collaboration (proof of concept)

Two producers open their own Ableton Live, join one session, and edit MIDI notes in
Live's normal piano roll; supported edits appear in the other person's native clip.
No export, no bounce, no separate editor. This repository is the engineering spike that
proves the Live integration and the synchronization path.

* `remote_script/Anthem/` — Ableton MIDI Remote Script (Python, runs inside Live, no Max for Live)
* `companion/` — local companion process (session create/join, relay link, diagnostics)
* `relay/` — relay server (auth, ownership, ordering; runs locally or on a host, optional TLS)
* `anthem_core/` — protocol + session logic shared by relay, companion and tests
* `tools/live_verify.py` — Milestone-1 driver that exercises the script inside a real Live
* `docs/` — PROTOCOL, SETUP, TEST_CHECKLIST, CAPABILITY_MATRIX, LIMITATIONS, HANDOFF, PLUGIN_RESEARCH (plug-in state sync / hear-only rendering: verified API facts + workarounds)

## Status — what is proven, honestly

| Level | Status |
|---|---|
| Implemented | Remote Script, companion, relay, protocol, fixed two-clip template, ownership, conflict detection, reconnect/resync, diagnostics; renders (2026-11-04): installed-plug-in exchange, `render <clip>` records one loop of a track through the API, ships it as WAV, the other side gets a listen-only `"<track> (render)"` audio track |
| Tested automatically | `ANTHEM_HOME=/tmp/anthem-test python3 -m pytest tests -q` (170 tests) — protocol validation, ownership, dedupe, ordering, feedback-loop suppression, reconnection, invalid/oversized messages, state preservation after rejects, relay over real sockets incl. TLS, per-IP limits and throttles, companion state machine, ownership handover, conflict/revert, token auth. Three adversarial multi-agent review rounds (65 confirmed findings in total) were run and fixed before this count. |
| Tested against mocked Ableton objects | Remote Script diff/apply/conflict/lifecycle logic (`tests/mock_live.py`, clearly labelled as mocked) |
| Tested with two simulated clients | `tests/test_e2e_sim.py`: real relay + two real companions + the real Remote Script code running headless on mocked sets — draw/move/resize/velocity/delete both ways, 40-note burst, conflict + revert, duplicate identity rejected, clip deleted/re-created, relay restart with persistence, different-set guard. First-edit latency ≈ 174 ms on loopback. |
| Verified inside one actual Ableton instance | Live 12.4.3 (renders: `render.py` captured 8 beats of Serum 2 and placed the WAV as a looping Session clip; `probe/plugin_probe/reports/…report11.txt`). Live 12.4.1 Suite / macOS 26: track+clip creation, add/move/resize/velocity/mute/pitch/delete application, idempotent snapshots, no echo, listener-based detection of a **human's** piano-roll edits (draw, move, transpose, resize, duplicate, delete) — see `docs/CAPABILITY_MATRIX.md`. Plus `tools/demo_one_live.py`: real Live as host + a simulated guest through the real relay and companions — the guest's bass line appeared in Live's `Anthem Bass` clip and the human's drum edits reached the guest (log: `tools/demo_one_live.txt`; final build re-run: `tools/demo_one_live_final.txt`, including the conflict → `revert` path when Live's clip was not empty). |
| Verified between two actual Ableton installations | **Yes, once** (2026-11-04, `docs/TEST_RESULTS_2026-11-04.md`): Live 12.4.5 ↔ Live 12.4.x over a phone hotspot; checklist steps 1–12 and 14–19 passed including plug-in renders; two defects fixed during the run; steps 13/20 not run. |

Scope of the first prototype: MIDI notes of two fixed Session clips (`Anthem Drums`
owned by the host, `Anthem Bass` owned by the guest), plus explicit audio renders of a clip's
track for a participant who lacks a plug-in. Everything else is explicitly out of scope —
`docs/LIMITATIONS.md`.

## Quick start (one machine, smoke test)

```
python3 -m pytest tests -q                      # all automated tests
python3 relay/relay.py --host 127.0.0.1 --port 9801 --state /tmp/anthem-relay.json
python3 companion/anthem.py install             # then select "Anthem" in Live ▸ Settings ▸ Link, Tempo & MIDI
python3 companion/anthem.py create --relay 127.0.0.1:9801     # host; prints invite code; type `setup`
python3 companion/anthem.py join CODE --relay 127.0.0.1:9801 --script-port 9810  # guest; on one machine use a different script port (a second Live would need its own machine)
```

Full participant instructions: `docs/SETUP.md`; guest onboarding in one page: `docs/FRIEND_SETUP.md`. Verification driver for the Live
integration alone: `python3 tools/live_verify.py` (needs Live running with Anthem selected).

## Design in one paragraph

Live never blocks: the Remote Script only polls a non-blocking loopback socket from a
50 ms Live timer; the companion does all real networking. Each clip has one owner; the
relay enforces it, which makes feedback loops structurally impossible. Edits are
incremental and id-based (Live's per-clip `note_id` + `apply_note_modifications`), so
note expression is preserved and unchanged snapshots cost zero writes. Ordering uses
relay sequence numbers, never wall clocks. Details: `docs/HANDOFF.md`, `docs/PROTOCOL.md`.
