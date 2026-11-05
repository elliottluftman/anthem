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

## Renders (a plug-in only one side has)
15. [ ] A puts a third-party instrument B does not own (or any plug-in) on *Anthem Drums*. Within a few seconds both companions print a `PLUG-IN:` line naming it (A: "the other participant does not have …"; B: "… not installed here"). Record the delay.
16. [ ] A types `render drums`. A's Live plays the drums clip once while an `Anthem Render Bus` track records it, then the bus disappears and the transport stops (if it was stopped before). A prints "render of drums shared (N KiB)". Record capture time (`ms`) and upload time.
17. [ ] B prints "render of drums … downloading" then "placed in Live". B has a new audio track `Anthem Drums (render)` with a looping clip in slot 1; launching the scene plays A's sound on B. Record download time and whether the loop length matches the clip (default 16 beats).
18. [ ] A changes the sound and types `render drums` again. B's render clip is replaced (same track, same slot). B's own edits to that render clip are not sent anywhere (the track is a listen-only copy).
19. [ ] Restart the relay (`--state`). A prints "re-uploading"; B keeps its copy; a third participant resuming would get the file again. Record.
20. [ ] Live preferences on A set to record FLAC → `render` reports an error naming the file type; set WAV or AIFF and retry.

## Measurements
- Detection delay (owner): companion `status` shows "detect→companion lag" (time from the Live notes listener firing to the companion receiving the delta, median/p95). For the raw listener delay use `tools/live_verify.py` phase B.
- Transport delay: op_ack round trip (companion `status`, median / p95).
- Apply delay (receiver): `applied.ms` (companion `status`).
- CPU: Live's CPU meter with the session idle vs. with the integration active.
- Audio: play a 4-bar loop on both for 2 minutes while editing; note any dropouts.

## Known expectations
- Remote edits enter the receiver's Undo history (one step per op).
- Editing the other participant's clip triggers a conflict, not a merge.
- Nothing except MIDI notes of the two template clips and explicit renders is synchronized.
- A render is a recording: it runs in real time and the owner hears the loop once; it is not updated until `render` is typed again.
