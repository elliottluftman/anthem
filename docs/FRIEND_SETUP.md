# For the collaborator (and their AI assistant): get ready for the two-computer test

You are the **guest**. Your friend is the **host** and already has everything running. Goal of this
page: in about 15 minutes, have Live, the Remote Script and the companion ready so that the two of
you can run `docs/TEST_CHECKLIST.md` together. Facts about what is and is not verified are in
`STATUS.md`; do not skip them.

## What this is, in two sentences

Two Ableton Lives share the MIDI notes of two fixed clips through a small relay: the first clip of a
track named `Anthem Drums` (host edits it) and of a track named `Anthem Bass` (you edit it). If one of
you uses a plug-in the other does not own, the owner types `render drums`/`render bass` and the other
side gets a listen-only audio track with one recorded loop of that sound.

## Requirements

* Ableton Live 12 (tested on 12.4.x Suite, macOS; Standard should work, nothing uses Max for Live;
  Live 11 and Windows are untested — if you are on either, say so before starting and report what happens).
* Python 3.9+ (`python3 --version`). No packages needed.
* Live's recording file type set to WAV or AIFF (Settings ▸ Record Warp Launch ▸ File Type). Not FLAC.
* Network path to the relay (see below).

## 1. Get the code

```
git clone https://github.com/elliottluftman/anthem.git
cd anthem
python3 -m pip install --user pytest        # only for the self-check below
ANTHEM_HOME=/tmp/anthem-test python3 -m pytest tests -q     # expect: 170 passed
```

## 2. Install the Remote Script, then restart Live

```
python3 companion/anthem.py install
```

Quit Live completely and start it again. Then once: Live ▸ Settings (Preferences) ▸ **Link, Tempo &
MIDI** ▸ in a free *Control Surface* slot choose **Anthem**, Input *None*, Output *None*. Live's status
bar (bottom) should say `Anthem: waiting for companion (127.0.0.1:9800)`. If "Anthem" is not in the
list, Live did not see the folder: check it exists at
`~/Music/Ableton/User Library/Remote Scripts/Anthem` (macOS) or
`%USERPROFILE%\Documents\Ableton\User Library\Remote Scripts\Anthem` (Windows) and restart Live again.

## 3. A disposable Live Set

File ▸ New Live Set, then **save it once** (Cmd/Ctrl+S) anywhere. Set the tempo you both agreed on
(the host decides). Keep this set open during the whole test; do not use a real project.

## 4. Relay: how the two machines reach each other

The relay is one small Python process. Pick one:

* **Same Wi-Fi / LAN:** the host runs `python3 relay/relay.py --host 0.0.0.0 --port 9801 --state ~/.anthem/relay-state.json`
  and tells you their LAN IP. You use `--relay THAT_IP:9801`.
* **Different places (recommended for a first test): Tailscale** on both machines (free; makes a private
  encrypted network between you). Host runs the same relay command and gives you their Tailscale IP
  (100.x.y.z). You use `--relay 100.x.y.z:9801`. No TLS needed inside Tailscale.
* **A server on the internet:** only with TLS and a create token (`relay.py --cert … --key … --create-token …`,
  companions add `--tls`). Never a plaintext relay on the open internet.

Check reachability before the session: `nc -vz RELAY_IP 9801` (macOS/Linux) must say succeeded.

## 5. Join the session

The host runs `python3 companion/anthem.py create --relay RELAY_IP:9801`, which prints a 12-character
invite code, and sends it to you. You run:

```
python3 companion/anthem.py join CODE --relay RELAY_IP:9801
```

Expected within a few seconds: `relay: welcome …`, `script: Live connected` (if not, Live is not running
with Anthem selected), then type

```
setup
```

which creates `Anthem Drums` and `Anthem Bass` (empty 16-beat clips in Session slot 1) in your set and
prints the "Supported parts" table with both clips `found`. Put an instrument on each track (the host
tells you which; stock instruments are fine). You own **Bass**: draw notes in its first clip; the host's
drum notes appear in your `Anthem Drums` clip. Editing the drums clip yourself raises a conflict
(`revert drums` fixes it) — that is expected.

Companion commands: `status` · `render bass` · `revert <clip>` · `claim <clip>` · `release <clip>` ·
`resync` · `rebind` · `quit`. Log: `~/.anthem/anthem.log` (no secrets). Reconnect later with
`python3 companion/anthem.py resume` and the **same** Live Set.

## 6. Then run the checklist together

`docs/TEST_CHECKLIST.md`, steps 1–20, both of you watching. Write down for every step: pass/fail,
the delay you saw, anything odd, your Live version and OS. Latency numbers come from `status`.

## If something fails

* Companion says `script: Live connected` never appears → Anthem not selected as control surface, or Live
  not restarted after install. `~/Library/Preferences/Ableton/Live <version>/Log.txt` (newest version
  folder) contains lines with `(Anthem)`.
* `port 9800 already in use` → another companion is running; quit it.
* `relay: connect failed` → firewall or wrong IP; test with `nc -vz`.
* `participant already connected` → an older companion of yours is still attached; quit it, `resume`.
* Render errors naming the file type → Live is recording FLAC; switch to WAV/AIFF.

Everything you observe is useful, including "it worked". This is the first time two real Lives run it.
