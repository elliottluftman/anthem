# Plug-in API probe (2026-11-04, Live 12.4.3)

Throwaway evidence for `docs/PLUGIN_RESEARCH.md`. Not part of the product.

How it was run (no Live restart): a hook appended to the *installed* `clipsync.py` imported a
`probe_plugin.py` module on `reload_code`; `reload_driver.py` listens on 127.0.0.1:9800, waits for the
script's `script_hello`, sends `{"type":"reload_code","v":1}` and prints what comes back. Each stage
overwrote `probe_plugin.py`; the stage code is summarised at the top of each report. The hook and
module were removed afterwards (installed copy verified identical to `remote_script/Anthem`).

Reports:

| File | Stage | What it shows |
|---|---|---|
| `AnthemProbe_plugin_report.txt` | 1, read-only | full attribute/docstring dump of `PluginDevice`, `Track` (frozen/arm/routing), `Song` (record/undo), `Application`, `Browser`, `BrowserItem`, `Device`, `Live.Licensing`; `gc` scan finding `PythonLicensing.licensing_bridge` with `save_current_set` |
| `AnthemProbe_plugin_report2.txt` | 2 | browser plug-in tree (AU/VST/VST3, Serum 2), `load_item(Serum 2 VST3)` 726 ms, parameters exposed = 1 (`Device On`), `get_parameter_names` = 2623 names, `presets` = `['Default']`, listeners attach, undo band grew on load |
| `AnthemProbe_plugin_report3.txt` | 3 | first attempt: routing list empty right after track creation (needs a tick), AU variant loads as `AuPluginDevice` |
| `AnthemProbe_plugin_report4.txt` | 4 | `is_editor_open` setter works with device selected; API-only recording of the plug-in track (`input_routing_type`, `arm`, `session_record`, `fire`) produced an `.aif`; `set_has_unsaved_changes` is a property |
| `AnthemProbe_plugin_report5.txt` | 5 | a `.adv` copied into the User Library is visible to `Browser` (name includes `.adv`) and `load_item` loads it in 161 ms |
| `AnthemProbe_plugin_report6.txt`, `…7.txt`, `…8.txt` | 6–8 | a hand-generated plug-in `.adv` (Serum 2 `PluginDevice` subtree from a saved set; `generated_plugin_adv_head.xml` shows the wrapper) is indexed and listed as loadable, but `load_item` returns in 1–16 ms with no device, no dialog, no log line; report7 loaded a stale index entry of the deleted first file |
| `AnthemProbe_plugin_report9.txt` | 9 | a `.als` copied into `User Library/Clips` appears as one loadable item with no track children; not loaded (would open the set) |
| `AnthemProbe_plugin_report10.txt` | 10 | `Track.create_audio_clip(path, 0.0)` places a file in the Arrangement; browser sample load does not create a Session clip; `fire(record_length=4.0)` records exactly 4.000 beats and stops by itself |
| `AnthemProbe_plugin_report11.txt` | 11 | the shipped `render.py` inside Live: Serum 2 on a disposable `Anthem Drums` track, `RenderCapture` 8 beats in 5.6 s (peak −7 dBFS), `companion.audio` → 24-bit WAV 1.15 MB, `apply_render` → looping 8-beat Session clip in `Anthem Drums (render)`, cleanup |

All writes were made to a disposable unsaved default set on tracks named `Anthem Plugin Probe` /
`Anthem Probe Render`, which were deleted at the end of each stage.
