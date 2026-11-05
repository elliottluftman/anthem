# Compatibility and limitations (prototype, 2026-10-16)

This is an engineering proof of concept. Everything below distinguishes what the
prototype does today from what the intended product will do.

## Compatibility

| Item | Status |
|---|---|
| Ableton Live 12.4 (Suite) on macOS 26 | Remote Script loads; API behaviour verified in-process (see CAPABILITY_MATRIX.md). Live auto-updated from 12.3.8 to 12.4 during development; both loaded the script. |
| Live 12 Standard / Intro | Expected to work — no Max for Live, only the Remote Script API that every edition exposes. **Not yet verified on a Standard install.** |
| Live 11 | Untested. The note API used (`note_id`, `get_all_notes_extended`, `apply_note_modifications`, `add_new_notes`, `remove_notes_by_id`) exists since Live 11.0, so it is plausible but unverified. |
| Windows | Companion/relay are pure-stdlib Python and path handling is cross-platform; the Remote Script is identical. **Not yet run on Windows.** |
| Python for companion/relay | 3.9+ (stdlib only). Live's embedded Python 3.11 runs the Remote Script; no install needed there. |
| Max for Live | Not required, not used. |

## What is synchronized

Only **MIDI notes** (pitch, start, duration, velocity, mute, probability, velocity
deviation, release velocity) of **fixed Session View clips**: `Anthem Drums` slot 1 and
`Anthem Bass` slot 1. Nothing else. In particular NOT synchronized (and never claimed to be):

* clip name/colour/length/loop settings, clip creation/deletion themselves
  (a deleted clip is reported as *lost*, not re-created on the other side)
* tracks (creation, renaming, order), devices, presets, mixer, automation,
  arrangement clips, audio, samples, warping, comping, MPE/expression beyond the
  per-note fields above, tempo/time signature, playback/transport.

  Third-party plug-ins: their *internal state* is not synchronized (research and the
  save-and-ship design in `PLUGIN_RESEARCH.md`; blocked on reloading plug-in presets through
  the API). Their *sound* can be shared as a **render** — see below.

Unsupported data is **preserved**: edits are id-based (`apply_note_modifications`),
so note expression attached to a note survives remote modifications; the receiver
never clears and recreates a whole clip unless the content actually differs.

## Renders (plug-in on one side only)

* Explicit: the owner types `render <clip>`; nothing is rendered automatically. A render is
  one loop of the clip's track output (Post FX), recorded in real time through Live's session
  recording — the owner's transport runs for one loop and the loop is audible on their machine.
  While it records, every other armed track is un-armed and re-armed afterwards.
* The receiver gets a listen-only audio track `"<track> (render)"` with a looping clip; its
  content is replaced by the next render; edits to it are never sent anywhere; nothing in it is
  merged back. The receiver still cannot edit the plug-in — that is the point.
* Live must record WAV or AIFF (FLAC is not read). Shipped as 16- or 24-bit PCM WAV, max
  32 MiB per render; 128 MiB of renders per session are kept in relay memory, oldest complete
  files evicted first; a relay restart loses the files and the owner's companion re-uploads
  from its local copy (`~/.anthem/renders/<session>/`).
* Tempo and clip length are the owner's: the receiver hears the loop at the owner's tempo,
  warped by Live's default clip warping. Sends/returns of the track and master effects are not
  part of the render. Return/master plug-ins cannot be rendered this way.
* Renders are verified inside one real Live (capture and placement) and end to end between two
  simulated clients; not yet between two real Lives.

## Workflow restrictions

* Fixed template: both sides run `setup` to create the two tracks/clips in a fresh
  disposable set, then add matching stock instruments and tempo by hand. Existing
  projects are not supported (no project transfer, no reconstruction of arbitrary
  subsets).
* Ownership: host edits Drums, guest edits Bass. Ownership can move only via
  `claim` while the owner is offline (or after `release`). Simultaneous editing of
  one clip is out of scope.
* Two participants only. One session per companion process.
* After an ownership handover (`claim` while the previous owner was offline), the previous
  owner's clip is compared with the session when they return; if it differs they get a
  conflict and choose `revert` (take the new owner's version) or `claim` it back. The same
  comparison (pitch, start, duration, velocity, mute, probability, velocity deviation,
  release velocity) runs at every bind and whenever a lost clip reappears, so a clip edited
  while the companion was down is not overwritten without an explicit `revert`.
* Live cannot make a clip read-only, so editing the other person's clip is detected
  and raised as a **conflict**: a warning appears in Live's status bar and the
  companion; the companion stops writing remote updates into that clip; the user
  chooses `revert <clip>` (discard own edit, explicitly) or `claim <clip>`.
  Nothing is discarded silently.

## Undo / redo (expected behaviour — not yet observed; the mock only verifies that each op is wrapped in begin/end_undo_step)

Remote edits are applied through Live's API and therefore **enter the receiver's
undo history** (each remote op is wrapped in one undo step). If the receiver presses
Undo, the remote edit disappears locally, the script sees content that differs from
the owner's → conflict → `revert` restores it. The owner's undo is just a local edit
and propagates normally.

## Playback / timing

Each machine plays locally with its own instruments and transport. No shared clock,
no sample-accurate playback, no audio. Tempo is matched manually.

## Reconnection and persistence

* Companion ↔ relay: automatic reconnect with backoff; on reconnect the companion
  sends `hello` with its last sequence number and receives either the missed ops
  or a full snapshot; owned clips re-send a snapshot so the relay is authoritative.
* While offline, synchronization pauses; latest state only is kept (no growing queue).
* Size caps: a clip above 2000 notes (or whose notes do not fit one 256 KiB message) is
  reported as lost ("too large") by its owner and resumes when it shrinks; the relay rejects
  ops beyond 2000 notes per clip or 1500 notes across the whole session (so a session can
  always be re-joined). Plenty for a 4-bar drum/bass sketch; not for dense multi-bar clips.
* Relay restart: sessions, ownership and notes survive if `--state` is used; the
  op log does not, so clients resync from the snapshot. Without `--state`, sessions
  are gone and both participants must create/join again (the companion stops retrying
  and says so). Sessions idle for 24 h are dropped by the relay.
* Companion restart: `anthem.py resume`.
* Opening a different Live Set re-instantiates the Remote Script; the companion
  notices the new set path and refuses to write until `rebind` is typed. This needs the
  original set to have been saved once (an unsaved set has no path; the companion warns).

## Security (test level)

Session secret per participant, hashed at the relay; ownership enforced by the
relay; strict message validation and size limits; rate limiting, per-address
connection/session caps and failed-auth throttling; optional `--create-token`;
loopback token between companion and Remote Script; optional TLS.
There are no accounts, no billing, no admin. Plaintext relay is for LAN tests only.

## Known risks / open questions

* The Remote Script API is not officially documented by Ableton for project
  manipulation; a Live update could change behaviour. (12.3.8 → 12.4 did not.)
* Live note ids are per clip and per machine; the receiver's id map lives in memory —
  a companion/Live restart on the receiver side rebuilds it from a snapshot by content
  matching (identical overlapping notes are matched arbitrarily, which is harmless
  for content but can swap ids).
* Detection of native edits relies on the `notes` listener; see CAPABILITY_MATRIX.md
  for what has been verified with a human editing in Live vs. API-originated edits.
