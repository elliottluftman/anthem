# First-session testing checklist (two computers)

Record for each run: Live versions, OS versions, relay location (LAN/internet, TLS?),
and for each step: pass/fail, observed delay, anything surprising.
`status` in the companion prints median/p95 latencies; `~/.anthem/anthem.log` has details.

## Preparation (both)
- [ ] `python3 companion/anthem.py install`, restart Live, select **Anthem** control surface (Input/Output None).
- [ ] Live status bar shows `Anthem: waiting for companion`.
- [ ] File ▸ New Live Set (disposable), save it once (Cmd/Ctrl+S), set the agreed tempo.

## Session
- [ ] Host: `anthem.py create --relay HOST:PORT` → invite code shown. Type `setup`.
- [ ] Guest: `anthem.py join CODE --relay HOST:PORT`. Type `setup`.
- [ ] Both: "Supported parts" table shows drums (host-owned) and bass (guest-owned), both found.
- [ ] Both: add a stock instrument on each track (same ones).

## Acceptance steps (A = host on Drums, B = guest on Bass)
1. [ ] A draws a note in *Anthem Drums*. B sees an editable native note. Delay: ___ ms.
2. [ ] A moves it, then resizes it. B sees both. Delay: ___ / ___ ms.
3. [ ] A changes its velocity. B's note shows the new velocity (check in B's velocity lane).
4. [ ] A deletes it. B sees it disappear.
5. [ ] B draws/moves/deletes notes in *Anthem Bass* while A keeps editing Drums. Both converge.
6. [ ] Watch `status` on both: no op repeats, `pending` stays 0, no conflict warnings. (No feedback loop.)
7. [ ] A selects several notes, copies and pastes them (Cmd/Ctrl+C/V, duplicate Cmd/Ctrl+D). B receives all. Note count matches.
8. [ ] A presses Undo then Redo. Record exactly what B sees. B presses Undo on *Drums* (a clip B does not own): expect a conflict warning on B; `revert drums` on B restores. Record behaviour.
9. [ ] Disconnect B's network (or kill B's companion). A keeps editing. Reconnect / `anthem.py resume`: B shows `resync` or the missed ops and the clips converge. Record how long.
10. [ ] Kill and restart the relay (with `--state`). Both companions reconnect; `status` shows resync; a new edit propagates.
11. [ ] On a third terminal on B's machine, `anthem.py resume --script-port 9811` for B's identity while B is connected → rejected ("participant already connected").
12. [ ] B deletes the *Anthem Drums* clip in Session View. B's companion prints `clip_lost`; A's edits are not lost at A. B re-creates the clip (double-click the slot) → `clip_found`, snapshot re-applied.
13. [ ] A opens a different Live Set. A's companion refuses to bind ("different set"); nothing is written into the new set. A reopens the test set (or types `rebind` deliberately).
14. [ ] Both play back locally; material is audible on both machines through local instruments.

## Measurements
- Detection delay (owner): companion `status` shows "detect→companion lag" (time from the Live notes listener firing to the companion receiving the delta, median/p95). For the raw listener delay use `tools/live_verify.py` phase B.
- Transport delay: op_ack round trip (companion `status`, median / p95).
- Apply delay (receiver): `applied.ms` (companion `status`).
- CPU: Live's CPU meter with the session idle vs. with the integration active.
- Audio: play a 4-bar loop on both for 2 minutes while editing; note any dropouts.

## Known expectations
- Remote edits enter the receiver's Undo history (one step per op).
- Editing the other participant's clip triggers a conflict, not a merge.
- Nothing except MIDI notes of the two template clips is synchronized.
