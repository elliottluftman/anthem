# Plug-ins in a shared Ableton session — research and workarounds

Date: 2026-11-04. Live 12.4.3 Suite, macOS, Serum 2 (AU + VST3) installed on the test machine.
Evidence: `probe/plugin_probe/` (in-Live probe reports), Live binary string dump, web research in §5.
Verification labels follow `CLAUDE.md`: **verified in Live** = observed inside the real Live 12.4.3
on this machine; **binary** = name/docstring found in the Live 12.4.1 executable, not exercised;
**web** = documented or reported elsewhere; **untested** = reasoned, not tried.

**Update, same day:** the hear-only render path (§3) is now implemented as the `render <clip>`
feature (see `docs/PROTOCOL.md` → Renders, `remote_script/Anthem/render.py`, `companion/audio.py`),
verified inside Live 12.4.3 with Serum 2 (`probe/plugin_probe/reports/AnthemProbe_plugin_report11.txt`)
and end to end with two simulated clients (`tests/test_render_e2e.py`). The §2 plug-in *state* path
remains research only (blocked, see §2b).

Two questions were asked:

1. Both producers have the same plug-in (e.g. Serum 2). Do the patches match, and can changes made
   *inside the plug-in window* be synced while working?
2. Only one producer has the plug-in. Can the other one still *hear* it (and simply not edit it)?

Short answers: (1) at join time yes, exactly; while working, knobs Live can see sync live, the
plug-in's internal state can only move on a *save-and-ship* cycle. Extracting the state from the saved
set is verified; a candidate API for triggering the save is found but not called; **re-loading the
shipped state through the API is the open problem**: a hand-made plug-in preset file is accepted by
the browser but loads nothing, so the receiver step needs a Live-written plug-in preset as reference
(one click by a tester) before it can be called workable. (2) Yes. Two render paths exist: Live's
Freeze (manual click, detectable) and API-only real-time capture (fully verified in Live).

## 1. What the Live API exposes for plug-ins (verified in Live 12.4.3)

| Fact | Status | Evidence |
|---|---|---|
| `Browser.load_item(item)` loads a plug-in onto the selected track. Serum 2 VST3 appeared as `PluginDevice` in 726 ms; the AU variant as `AuPluginDevice`. | verified in Live | report2, report3 |
| The browser tree lists installed plug-ins by format: `query:Plugins#VST3:Xfer%20Records:Serum%202`, `…#AUv2:…`. | verified in Live | report2 |
| A freshly loaded plug-in exposes **one** `DeviceParameter` (`Device On`). Plug-in parameters appear in `device.parameters` only after the user "configures" them in Live's device panel (GUI only; no API to configure). | verified in Live | report2 |
| `PluginDevice.get_parameter_names(0, -1)` returns **all** plug-in parameter names (Serum 2: 2623) — names only, no values. | verified in Live | report2 |
| `PluginDevice.presets` / `selected_preset_index` exist with listeners, but Serum 2 exposes no program list (`['Default']`, index 0). Useful only for plug-ins that publish programs. | verified in Live | report2 |
| `PluginDevice.is_editor_open` is read/write with a listener. Setting it `True` opens the plug-in window when the device is selected; `False` closes it. | verified in Live | report4 |
| Listeners `parameters`, `presets`, `selected_preset_index`, `is_editor_open` attach and detach cleanly. | verified in Live | report2 |
| `Track.is_frozen` / `can_be_frozen` with listener exist. **No** freeze/flatten/export method anywhere in the API. | verified in Live (attributes); binary (no method) | report1, strings |
| A `.adv` preset file copied into the User Library shows up in `Browser.user_library` (indexer scan logged ~20 s later; item name includes the extension) and `load_item` loads it in 161 ms. `Track.delete_device(i)` removes a device. | verified in Live | report5, report3, `Indexer.txt` |
| A **hand-generated** `.adv` wrapping the `PluginDevice` subtree of Serum 2 (copied byte-for-byte from one of the user's saved sets, header from the same set, with and without an `Id` attribute) is indexed and listed as loadable, but `load_item` returns in 1–16 ms with **no device, no dialog, no log line**. The same mechanism loads a Live-written stock-device `.adv` fine. | verified in Live (negative) | report6–8, `generated_plugin_adv_head.xml` |
| A `.als` placed in the User Library appears as a single loadable item without track children; loading it was not attempted (in the GUI that opens the set and raises the Save-changes dialog). | verified in Live (listing only) | report9 |
| API-only recording of one track's output into another: `audio.input_routing_type = <that track>` (channels `Pre FX` / `Post FX` / `Post Mixer`), `audio.arm = True`, `song.session_record = True`, `slot.fire()`; 2.5 s later an `.aif` existed under `Live Recordings/<Temp Project>/Samples/Recorded/`. Routing lists are empty for one tick after `create_audio_track`. | verified in Live | report4 |
| No `Song.save`. But `Live.Licensing.PythonLicensingBridge` (held by a `PythonLicensing` object reachable through `gc.get_objects()`) has `save_current_set()` — docstring "Saves the current Live session." — and a `set_has_unsaved_changes` property. Live uses it itself when a trial expires. | found in Live via gc, **not called** | report1, report4, strings |
| Live writes its undo history to disk continuously (`~/Library/Preferences/Ableton/Live <ver>/Undo/0.band`, crash recovery); it grew 850 602 → 875 043 bytes when Serum 2 was loaded. Proprietary binary. | verified in Live (growth); format unknown | report2 |
| Frozen audio is written under `<Project>/Samples/Processed/Freeze/`. | binary | strings |
| `Application.open_dialog_count`, `current_dialog_message`, `press_current_dialog_button` exist (Live's own dialogs, not OS save panels). | verified in Live (attributes) | report1 |

## 2. Question 1 — same plug-in on both sides

### 2a. Join time
The `.als` carries the plug-in's complete internal state (an opaque chunk) and Live restores it when the
set opens. Reconstructing the project on the friend's machine from your set therefore opens Serum 2 with
the identical patch. Requirements: same plug-in **and format** (Live does not substitute AU for VST3),
compatible plug-in version, and any external content the plug-in references installed on both sides.
Status: standard Live behaviour; the project-transfer step itself is not built yet.

### 2b. While working — three layers

1. **Knobs Live can see** (`Device On`, configured parameters, rack macros, their automation): plain
   `DeviceParameter` values with listeners → same observe/diff/apply path as notes, real time. *Not yet
   implemented in Anthem; the API is the one the prototype already uses for notes.* Caveat: a plug-in
   exposes zero configured parameters until the user configures them in the GUI.
2. **Changes inside the plug-in window** (new wavetable, LFO routing, loading a preset in Serum's own
   browser): not observable through the API. The state only exists inside the plug-in until Live
   serialises it — on Save, and in the undo history on disk.
3. **Workaround: save-and-ship.** Owner side: trigger a save → the companion reads the `.als`
   (gzip XML) → extracts the `PluginDevice` subtree for that device → ships it. Receiver side: wrap it
   as an `.adv` into `User Library/Presets/…/Anthem/` → wait for the browser index (~20 s observed) →
   select the track/device → `load_item(.adv)` → `delete_device` on the old one.
   Receiver steps verified with a stock-device `.adv` (index ~20 s, `load_item` 161 ms,
   `delete_device`). **Blocked for plug-ins as of today:** the generated Serum 2 `.adv` is listed as
   loadable but `load_item` silently does nothing (report6–8). Unknown whether Live-written plug-in
   presets have a different wrapper, whether the indexer stores extra metadata at scan time, or
   whether plug-in presets are simply not loadable through the API (ClyphX's authors note that
   loading *plug-ins* by the browser route "doesn't seem to work", §5). Next test: save a Serum 2
   preset from the device title bar in Live, diff its XML against `generated_plugin_adv_head.xml`,
   and load **that** file through `load_item`.
   Fallbacks if the `.adv` route stays closed: (i) ship the state as the plug-in's native preset
   (`.vstpreset` = VST3 `Comp`/`Cont` chunks, which are exactly the `ProcessorState`/`ControllerState`
   hex; `.aupreset` = the AU `Buffer` plist) into the plug-in's own preset folder, and the receiver
   loads it in the plug-in's browser — semi-manual but format-documented; (ii) for plug-ins that
   publish programs, set `selected_preset_index`; (iii) full project re-transfer (open the shipped
   `.als`) as the always-works but disruptive path. Cost of any reload: a short audio gap and a
   reset of the configured-parameter list.

### 2c. Triggers for "save-and-ship"
- `is_editor_open` listener: the plug-in window closing is a strong "the patch may have changed"
  signal (verified in Live that the property tracks the window).
- `Undo/0.band` growth while a plug-in window is open: cheap change detector (verified growth; no
  parsing).
- A timer (e.g. every 60 s while the editor is open).

### 2d. Triggering the save itself — options, in order of preference
1. **User presses Cmd+S** (works today; the companion watches the `.als` mtime).
2. **`save_current_set()` on the licensing bridge** (found, not called). Expected: saves silently when
   the set has a path, opens the OS save panel otherwise. Needs one test on a *saved* disposable set.
   Risk: internal API, could vanish in any Live update; must be guarded with a feature check and a
   fallback to option 1.
3. Accessibility automation of File ▸ Save: not available on this Mac (`osascript` has no assistive
   access), and brittle.

## 3. Question 2 — only one side has the plug-in: hear, don't edit

Yes. The sound crosses as rendered audio; the owner's rendering updates the listener's copy. Two
render paths:

### 3a. Freeze path (recommended first)
Owner right-clicks the track ▸ Freeze. Live renders offline (faster than real time, includes the
track's device chain). The script sees `Track.is_frozen` flip (listener verified to exist) and the
companion finds the new file in `Samples/Processed/Freeze/` (folder name verified in the binary) and
uploads it. Receiver: an audio track named e.g. `Serum 2 (Elliott) 🔒` plays that render; our
ownership model makes it read-only (edits revert with a visible message). Re-freeze → re-upload →
swap. Variant: the receiver may still edit the **MIDI notes** of that track (plain MIDI sync);
they hear the previous render until the owner re-freezes.
Limits: one click per update on the owner's side (no freeze API); return/master tracks cannot be
frozen; render files are large (transfer time).

### 3b. API-only capture path (verified in Live)
The companion can render without any click: create a hidden audio track, set its input to the
plug-in track (`Pre FX`/`Post FX`/`Post Mixer`), arm it, enable session record, fire a slot, stop
after the clip/section length, upload the `.aif`. This ran end to end in Live 12.4.3 and produced a
real file. Limits: it records in **real time** and runs the transport (the owner hears playback);
session record also records into any other armed track, so the owner's armed tracks must be handled;
recordings land in the project's `Samples/Recorded/` (or a temp project for unsaved sets).
Best use: "capture while you play" — record only while the owner is already playing — or an explicit
"render for Jake" command.

### 3c. What does not work
Streaming the plug-in's live audio to the other machine in real time is a different product (audio
streaming, clock sync); not needed for collaboration and out of scope.

## 4. Suggested next steps (in order)
1. One test of `save_current_set()` on a saved disposable set (one click from the user to open it).
2. Plug-in preset reload (the blocker): in Live, with Serum 2 on a track, click the device's Save
   Preset button; gunzip the resulting `.adv`, compare with `probe/plugin_probe/generated_plugin_adv_head.xml`,
   then `load_item` the Live-written file through the API. If that loads, fix the generator; if it
   does not, the `.adv` route is closed and the `.vstpreset`/`.aupreset` fallback becomes the plan.
   Then confirm fidelity by saving again and diffing the `ProcessorState` hex.
3. Freeze detection: freeze a track by hand, confirm `is_frozen` listener fires and the file appears.
4. Then design the "device" object type for the protocol (device identity, state blob, render blob).

## 5. Web research (what others have documented or tried)

Sources: Live Object Model runtime dumps for Live 11.0.0 (nsuspray/Live_API_Doc) and Live 12
(midiremotescripts.structure-void.com), Ableton's own decompiled Live 12 Remote Scripts, the Live 12
manual, ClyphX and AbletonOSC sources, Ableton forum threads. help.ableton.com articles could not be
fetched directly (403); facts from them rest on search snippets and are marked *(snippet)*.

**Plug-in parameters.** The manual documents the behaviour seen in the probe: plug-ins with up to 64
parameters get all of them in Live's panel; above that the panel is empty until the user configures
parameters, and configured parameters are per instance and saved with the Set. `Options.txt`
`-_PluginAutoPopulateThreshold=N` raises the threshold (max 128, reported unreliable at 128). The
`.als` reserves exactly 128 `PluginFloatParameter` slots per plug-in device (verified in the user's
sets). `PluginDevice.selected_preset_index` is settable (Ableton's Push 2 script sets it);
`get_parameter_names` is used by Native Instruments' scripts. No LOM member reads or writes the
opaque state in Live 11 or 12; Max for Live exposes the same two extras only.

**Saving / loading.** No save, export, render or freeze method exists on `Song`, `Track` or
`Application` in Live 11 or 12 (full method lists checked). Live 12 added `Song.file_path`,
`Track.insert_device(name)`, `duplicate_device`, `create_audio_clip(path, position)` (Arrangement
only), `Song.undo()/redo() -> str`. Ableton's Push 2 loads presets via `hotswap_target` +
`load_item`, or sets `selected_preset_index` for plug-in preset items. ClyphX loads *native* devices
by walking the browser and calling `load_item`, with the source comment "using a similar method for
loading plugins doesn't seem to work!". AbletonOSC exposes no browser/load/preset/freeze/save.
`Song.set_data/get_data` and `Track.set_data/get_data` persist arbitrary script data in the Set.

**File formats (verified on the user's own sets, Live 12.3.6 writer).** `PluginDevice/PluginDesc/
Vst3PluginInfo/{Name,Uid,Preset/Vst3Preset/{ProcessorState,ControllerState}}` with hex text
(`ProcessorState` decodes to `XferJson…{"component":"processor",…}` for Serum 2);
`AuPluginDevice/PluginDesc/AuPluginInfo/Preset/AuPreset/Buffer` holding a hex Apple plist (40 KB for
Serum 2). Live-written `.adv` for a stock device = `<Ableton …>` + the device subtree without `Id`
(verified); for plug-ins no Live-written sample was available. Community generators exist for
`.adg/.adv/.als` (`ableton-device-creator`, `@pohy/ableton`, `alpax`); one forum report has Live
rejecting a re-encoded set, so byte-faithful serialisation matters.

**Does Live write plug-in state while editing?** Yes, in the crash-recovery history, not in a set
file: `~/Library/Preferences/Ableton/Live <ver>/{CrashRecoveryInfo.cfg, BaseFiles/<set>.als,
Undo/0.band}`; `0.band` is proprietary binary that grows as you work and contains `Vst3PluginInfo`
strings. The project `Backup/` folder is written only on user Save (from the second save on). There is
no auto-save of the `.als`; third-party "LiveSaver" exists for that reason.

**Freeze / render.** Freeze files: `<Project>/Samples/Processed/Freeze/`, 32-bit, two loop cycles per
Session clip; unsaved sets use the Temporary Folder; Group/Return/Main tracks cannot be frozen; External
Instrument tracks freeze in real time. Live 12.2 renamed "Freeze and Flatten" to **"Bounce Track in
Place"** (`Samples/Processed/Bounce`) *(snippet)*. Shortcut Cmd+Alt+Shift+F *(snippet)*. GUI automation
(SoundFlow, System Events) works only with Accessibility permission and must match the toggling menu
label `/[Un]?Freeze Track[s]?/`; version-dependent menu text is the main fragility. Freeze and VST/AU
support are in Intro, Standard and Suite; Max for Live is Suite-only.

**Other collaboration tools.** None found that auto-renders stems for a peer missing a plug-in:
Splice Studio (shut down) only detected missing plug-ins; Satellite Sessions (Mixed In Key) captures
audio/MIDI in real time via its own plug-ins and requires everyone to own the plug-ins; Audiomovers
Listento / Waves Stream / LANDR Sessions stream audio only; VST Connect gives the remote performer no
access to the studio's plug-ins; browser DAWs sidestep the problem. Live's "Collect All and Save"
copies samples, not plug-ins.

Key links: https://github.com/nsuspray/Live_API_Doc · https://midiremotescripts.structure-void.com/reference/live12/Live.PluginDevice.runtime/ ·
https://midiremotescripts.structure-void.com/guides/runtime-changes-11-to-12/ · https://www.ableton.com/en/live-manual/12/using-plug-ins/ ·
https://www.ableton.com/en/live-manual/12/computer-audio-resources-and-strategies/ · https://forum.ableton.com/viewtopic.php?p=1837238 ·
https://github.com/ldrolez/clyphx-live10 · https://github.com/ideoforms/AbletonOSC · https://github.com/waltzforvenus/ableton-mcp/pull/22 ·
https://mixedinkey.com/satellite/collaboration-with-ableton-live/ · https://forum.soundflow.org/-6503 · https://pypi.org/project/ableton-device-creator/
