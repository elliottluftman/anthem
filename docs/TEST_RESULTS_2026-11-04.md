# Two-computer test — 2026-11-04 (first run between two real Ableton Lives)

**Host:** Elliott, Live 12.4.5 Suite, macOS 26, Serum 2 installed (33 plug-ins listed), relay on the same Mac.
**Guest:** Konrad, Live 12.0.2 Suite at first → updated to a 12.4.x build during the test (exact build not
recorded), macOS 14.1 Apple Silicon, 1084 plug-ins listed, no Serum 2. Both companions driven by a Claude
on each machine; humans made the edits in Live.
**Network:** the room Wi-Fi isolated the two Macs from each other (same router, no ping either way);
two USB-C cables gave no Thunderbolt link ("No device connected"); a phone hotspot worked
(172.20.10.x). Relay plaintext on the hotspot, `--state` enabled. Session `Jxx1jie_KhpP3SFG`.

## Checklist results (`docs/TEST_CHECKLIST.md`)

| Step | Result | Notes |
|---|---|---|
| Preparation / session / setup | pass | guest needed to select Anthem in Live and save a set first; host set stayed unsaved (guard inactive, warning printed) |
| 1 draw note (host→guest) | pass | 16→17 notes on the guest; detect→companion 197 ms, relay ack 1 ms (host, localhost) |
| 2 move note | pass | arrived as several `modified` updates (rev 8→14), same note moved, no duplicate |
| 3 velocity | pass | |
| 4 delete | pass | |
| 5 guest draws/moves/deletes bass | pass | arrived "instantly" per the host; **one CONFLICT on the host's bass clip** when the first guest notes arrived (23:30:56, "local content differs from owner's"); `revert bass` fixed it; root cause open |
| 6 no feedback loop | pass | `pending_ops` 0 throughout, no repeats, 171 ops total |
| 7 duplicate several notes | pass | |
| 8 undo/redo | pass | host undo/redo propagated; guest editing the host's drums clip → CONFLICT printed on the guest, `revert drums` restored (as designed). While the guest's drums were in CONFLICT, host edits stopped appearing on the guest until the revert — correct, but surprising to the users |
| 9 guest companion quit + resume | pass | missed ops applied on reconnect |
| 10 relay restart | pass | both reconnected in ~2 s from `--state`; render re-uploaded by the host |
| 11 duplicate identity | pass | "participant already connected" |
| 12 guest deletes/re-creates drums clip | pass | clip lost / found, notes re-applied |
| 13 different set | **not run** | |
| 14 playback both sides | pass | no dropouts reported; host applied.ms median 1.5 ms (n=29) |
| 15 PLUG-IN hints | pass after fix | guest's 1084 plug-ins exceeded the 512-name cap → relay rejected his list; cap raised to 4096 (8558b20) |
| 16 `render drums` (host) | pass | 16 beats recorded in 9.1–9.3 s, 2.25 MB WAV, 15 chunks |
| 17 guest receives render | pass | guest needed a Live update first (12.0.2 could not place the file); after it: downloaded, placed in 18 ms, audible |
| 18 re-render replaces | pass | Saturator added after Serum; render 2228440c replaced the clip in place |
| 19 relay restart re-upload | pass after fix | a restarted host companion had forgotten its render; now re-uploads from `~/.anthem/renders` (914c916) |
| 20 FLAC | not run | |

Latencies (host side, hotspot): detect→companion median 182 ms / p95 197 ms (n=13); op_ack 1 ms (relay on the
same machine); guest op_ack median 82 ms / p95 138 ms; applied 1.5 ms median / 3.3 ms p95.

## Findings to act on

1. **Guest hears two versions.** The guest put his own instrument on `Anthem Drums`, so he heard his
   instrument *and* the render. Wanted: render track created next to the MIDI track, same colour, MIDI
   track auto-muted while a render exists ("pairing").
2. **"Change the sound on the MIDI clip and hear it there"** is only possible for devices both sides
   have. Next feature: stock-device sync (auto-load the same stock instrument/preset on the peer, live
   parameter sync via `DeviceParameter` listeners, same owner rule as notes). Plug-ins a side lacks stay
   render-only; make renders automatic (re-render after edits when the owner stops playing).
3. **Bass CONFLICT on the host** at first guest notes: investigate with a verbose companion log
   (`--verbose`) next time; the script's consistency check fired once while applying the guest's first
   deltas.
4. Guest CONFLICT pausing remote updates is correct but needs a louder, persistent hint in Live's status bar.
5. Live 12.0.x cannot place renders (`ClipSlot.create_audio_clip` missing); require Live 12.1+ for the
   receiving side or fall back to the Arrangement/manual drag. Document the minimum version.
6. Network: document "same Wi-Fi may still isolate clients; use a phone hotspot or Tailscale" in SETUP
   (the cable route needs a real Thunderbolt cable).
7. Host set unsaved during the whole test → different-set guard inactive; step 13 skipped.
