# Working on Anthem

Ableton ↔ Ableton MIDI clip collaboration prototype. Start with `STATUS.md`, then
`docs/HANDOFF.md` and `docs/PROTOCOL.md`.

## Rules that matter here

- **Honesty about verification.** Four levels, keep them distinct everywhere (README, docs,
  commit messages): tested automatically → tested against mocked Live (`tests/mock_live.py`
  is NOT Ableton) → verified inside one real Live → verified between two real Lives (not yet
  done). Never mark something "works in Live" from documentation or mocks alone.
- **Live never blocks.** The Remote Script does no network I/O except a non-blocking loopback
  socket polled from a `Live.Base.Timer` tick; no threads inside Live; every tick/listener
  body is wrapped so an exception cannot stop the timer.
- **Never discard a user's notes silently.** Any write into a clip whose content differs from
  what was last known must go through the conflict path (`revert`/`claim`), except the
  explicit `force` on `revert`/`resync`.
- **Ownership is enforced by the relay**, and the script's `owned` flag must follow the
  companion's (`set_owned`). Only owners send `local_delta`/`local_snapshot` as authoritative.
- Keep all three processes stdlib-only Python. Keep `companion/core.py` pure (no I/O).
- Bound everything by bytes as well as by count (256 KiB per line, 2000 notes per op/clip,
  1500 per session).

## Commands

```
ANTHEM_HOME=/tmp/anthem-test python3 -m pytest tests -q        # 156 tests, ~20 s
python3 -m compileall -q remote_script companion relay anthem_core tools
python3 tools/live_verify.py [--reload] [--no-interactive]     # against a running Live with Anthem selected
python3 tools/demo_one_live.py --seconds 60                    # real Live host + simulated guest
```

`ANTHEM_HOME` keeps tests away from the real `~/.anthem` (identities, token, log).

## Live-side development loop (macOS)

- Install: `python3 companion/anthem.py install` copies `remote_script/Anthem` to
  `~/Music/Ableton/User Library/Remote Scripts/Anthem`. Live scans that folder at startup.
- Changes to `clipbinding.py` / `bridge.py` / `clipsync.py` can be hot-reloaded without
  restarting Live: `python3 tools/live_verify.py --reload --no-interactive` (sends
  `reload_code`). Changes to `anthem.py` need a Live restart (re-select the control surface
  or quit/relaunch).
- Script log lines appear in `~/Library/Preferences/Ableton/Live <version>/Log.txt` as
  `RemoteScriptMessage: (Anthem) ...`. Live auto-updates on launch; always read the newest
  version folder.
- Only ever touch tracks named `Anthem Drums` / `Anthem Bass` in a disposable set.

## Layout

```
remote_script/Anthem/   anthem.py (ControlSurface), clipbinding.py, bridge.py, clipsync.py
companion/              anthem.py (CLI + asyncio), core.py (pure state machine)
relay/relay.py          asyncio relay
anthem_core/            protocol.py, session.py
tests/                  pytest; live_stubs/ + mock_live.py are stubs/mocks, not Live
tools/                  live_verify.py, demo_one_live.py, evidence logs
docs/                   SETUP, TEST_CHECKLIST, PROTOCOL, CAPABILITY_MATRIX, LIMITATIONS, HANDOFF
probe/                  throwaway API-introspection script from day one (not part of the product)
```
