# Anthem prototype — participant setup

Prototype status: engineering test, not a product. Read `docs/LIMITATIONS.md` first.

## Requirements (both participants)

* Ableton Live **12.4.x** (verified on 12.4.1 Suite, macOS 26; the script also loaded on 12.3.8 and 12.4.0). Live 12 Standard should work:
  nothing here uses Max for Live. Live 11 is untested.
* Python 3.9+ on the machine (macOS: `python3` ships with Xcode CLT or Homebrew; Windows:
  python.org installer, tick "Add to PATH"). No third-party packages.
* This repository copied to the machine (zip or `git clone`).
* Network access to the relay (`HOST:PORT`, see "Relay" below).

## 1. Install the Remote Script (once)

```
cd Anthem
python3 companion/anthem.py install
```

This copies `remote_script/Anthem/` to Live's user Remote Scripts folder:

* macOS: `~/Music/Ableton/User Library/Remote Scripts/Anthem`
* Windows: `%USERPROFILE%\Documents\Ableton\User Library\Remote Scripts\Anthem`

Then **restart Live** and select it once:
Live ▸ Settings (Preferences) ▸ *Link, Tempo & MIDI* ▸ Control Surface slot: **Anthem**,
Input: *None*, Output: *None*. Live remembers this.
Live's status bar shows `Anthem: waiting for companion (127.0.0.1:9800)`.

## 2. Relay (one machine, or a server)

```
python3 relay/relay.py --host 0.0.0.0 --port 9801 --state ~/.anthem/relay-state.json
```

* Same LAN / localhost test: plaintext is acceptable.
* Over the internet: run it behind TLS — `--cert fullchain.pem --key privkey.pem`
  (e.g. Let's Encrypt on a small VPS) and have both companions pass `--tls`.
  Do not expose a plaintext relay to the internet; the relay authenticates
  participants but a plaintext link leaks the session secret.
* Relay restart: sessions/notes/ownership are restored from `--state`; the op log is not,
  so clients resume with a full snapshot (`resync`). Document this to testers.

## 3. Host: create the session

1. Live ▸ File ▸ New Live Set (use a **disposable** set) and **save it once** (Cmd/Ctrl+S) —
   the different-project guard compares set paths and is inactive for unsaved sets.
2. `python3 companion/anthem.py create --relay HOST:9801 [--tls]`
3. The companion prints the **invite code**. Keep the companion running.
4. In the companion type `setup` — it creates the two template MIDI tracks
   `Anthem Drums` and `Anthem Bass`, each with an empty 4-bar (16-beat) clip in Session slot 1,
   and prints the "Supported parts" table.
5. Drop a stock instrument on each track (e.g. Drum Rack / Operator). Tell your
   collaborator which ones, and the tempo.

## 4. Collaborator: join

1. Live ▸ File ▸ New Live Set (disposable) and save it once.
2. `python3 companion/anthem.py join CODE --relay HOST:9801 [--tls]`
3. Type `setup` in the companion; add the same instruments; set the same tempo.
4. The host's `Anthem Drums` notes appear in your `Anthem Drums` clip; your
   `Anthem Bass` notes appear on the host's machine.

Ownership in the template: host edits **Drums**, guest edits **Bass**. Editing the
other clip raises a visible conflict (see LIMITATIONS.md → Conflicts).

## Environment variables and extra flags

`ANTHEM_HOME` (default `~/.anthem`) — where identities, the token and the log live;
`ANTHEM_CREATE_TOKEN` or `--create-token` — relay create token; `--insecure-tls` — skip
certificate verification for a self-signed test relay (prints a warning); `--verbose` —
debug logging; `--script-port` — loopback port (only for running several companions on
one machine; the Remote Script always uses 9800).

## Companion commands while running

`status` · `setup` · `revert <clip>` · `claim <clip>` · `release <clip>` · `resync` · `rebind` · `quit`

Diagnostics: `~/.anthem/anthem.log` (no secrets). Session identity: `~/.anthem/sessions/`.

## Security notes for testers

* The companion writes a random token to `~/.anthem/script.token` (0600) at start; the
  Remote Script sends it in `script_hello`, and the companion ignores any other local
  process that connects to port 9800. Limits: if the token file cannot be written the
  companion prints a warning and accepts the script without a token; the Remote Script
  always reads `~/.anthem/script.token` (regardless of `ANTHEM_HOME` or `--script-port`,
  which are for tests/demos); and the token authenticates the script to the companion
  only — the script itself trusts whatever listens on 127.0.0.1:9800, so on a shared
  machine another local user could drive Live while your companion is not running.
  Keep `~/.anthem` private (0700).
* Invite codes are 12 characters and stop working once the guest has joined. The relay
  throttles failed join/resume attempts per address.
* A relay on the internet should run with `--create-token SECRET` so only your testers
  can create sessions (`anthem.py create ... --create-token SECRET`), and with TLS.
* The relay drops sessions nobody has used for 24 h (`SESSION_TTL`); create/join again.

## Reconnecting later

`python3 companion/anthem.py resume` (most recent session) — then open the *same*
Live Set you used before; a different set is refused until you type `rebind`.
