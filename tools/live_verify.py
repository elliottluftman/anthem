#!/usr/bin/env python3
"""Milestone-1 verification driver: stands in for the companion and exercises the Anthem
Remote Script inside a REAL Ableton Live instance, logging every message with timings.

Usage:  python3 tools/live_verify.py [--port 9800] [--log tools/live_verify.jsonl]

Phase A (automatic): setup template tracks/clips, apply add/move/resize/velocity/pitch/
delete to the non-owned 'bass' clip, read back after each step, check idempotent
snapshots, and measure the notes-listener detection delay for API-originated edits.
Phase B (interactive): waits for YOU to edit the 'Anthem Drums' clip in Live's piano roll
and prints each detected native edit. Ctrl-C to stop.

Nothing here touches tracks other than 'Anthem Drums' / 'Anthem Bass'.
"""
import argparse
import json
import socket
import sys
import time

CLIPS = [{"clip": "drums", "track_name": "Anthem Drums", "slot": 0, "owned": True},
         {"clip": "bass", "track_name": "Anthem Bass", "slot": 0, "owned": False}]


def note(nid, pitch, start, dur, vel=100.0, mute=False):
    return {"id": nid, "pitch": pitch, "start": start, "dur": dur, "vel": vel, "mute": mute,
            "prob": 1.0, "vel_dev": 0.0, "rel_vel": 64.0}


class Driver(object):
    def __init__(self, port, logpath, reload=False):
        self.reload = reload
        self.srv = socket.socket()
        self.srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self.srv.bind(("127.0.0.1", port))
        self.srv.listen(1)
        self.log = open(logpath, "a")
        self.conn = None
        self.buf = b""
        self.results = []

    def record(self, direction, msg):
        self.log.write(json.dumps({"t": time.time(), "dir": direction, "msg": msg}) + "\n")
        self.log.flush()

    def wait_script(self):
        print("waiting for the Anthem Remote Script to connect (is Live running with Anthem selected?)")
        self.conn, _ = self.srv.accept()
        self.conn.settimeout(0.2)
        hello = self.wait_for("script_hello", 10)
        print("script connected:", hello)
        return hello

    def send(self, msg):
        msg.setdefault("v", 1)
        self.record("->script", msg)
        self.conn.sendall(json.dumps(msg).encode() + b"\n")

    def recv(self, timeout):
        end = time.time() + timeout
        while time.time() < end:
            if b"\n" in self.buf:
                line, self.buf = self.buf.split(b"\n", 1)
                m = json.loads(line)
                self.record("<-script", m)
                return m
            try:
                d = self.conn.recv(65536)
                if not d:
                    raise SystemExit("script closed the connection")
                self.buf += d
            except socket.timeout:
                pass
        return None

    def wait_for(self, typ, timeout, **match):
        end = time.time() + timeout
        while time.time() < end:
            m = self.recv(end - time.time())
            if m is None:
                break
            if m["type"] == typ and all(m.get(k) == v for k, v in match.items()):
                return m
            if m["type"] in ("conflict", "clip_lost", "log"):
                print("   (async)", m)
        raise SystemExit("timeout waiting for %s %s" % (typ, match))

    def readback(self, clip):
        self.send({"type": "request_snapshot", "clip": clip})
        return self.wait_for("local_snapshot", 5, clip=clip)["notes"]

    def check(self, name, ok, detail=""):
        self.results.append((name, ok, detail))
        print("%s %s %s" % ("PASS" if ok else "FAIL", name, detail))

    def apply(self, clip, op_id, **kw):
        t0 = time.time()
        msg = {"type": "apply_delta" if "notes" not in kw else "apply_snapshot", "clip": clip, "op_id": op_id}
        msg.update(kw)
        self.send(msg)
        a = self.wait_for("applied", 5, op_id=op_id)
        return a, (time.time() - t0) * 1000.0

    @staticmethod
    def content(notes):
        return sorted((n["pitch"], round(n["start"], 4), round(n["dur"], 4), n["vel"], n["mute"]) for n in notes)

    def phase_a(self):
        if self.reload:
            self.send({"type": "reload_code"})
            self.wait_for("inventory", 10)
        self.send({"type": "setup_template", "clips": CLIPS})
        inv = self.wait_for("inventory", 10)
        found = {c["clip"]: c["found"] for c in inv["clips"]}
        self.check("discover/create template tracks+clips", all(found.values()), str(inv["clips"]))
        snap = self.wait_for("local_snapshot", 5, clip="drums")
        drums_before = len(snap["notes"])
        self.check("read existing notes of owned clip", True, "%d notes" % drums_before)

        # Start from an empty bass clip (a previous run may have left notes; Live merges
        # overlapping same-pitch notes, which would make the arithmetic below meaningless).
        a, _ = self.apply("bass", "clear", notes=[])
        base = self.content(self.readback("bass"))
        self.check("clear non-owned clip via empty snapshot", a["ok"] and base == [], str(base))
        notes = [note(1, 36, 0.0, 0.5), note(2, 43, 1.0, 0.25, 80.0), note(3, 48, 2.0, 1.0)]
        a, rt = self.apply("bass", "add1", added=notes, modified=[], removed=[])
        got = self.content(self.readback("bass"))
        self.check("apply add 3 notes", a["ok"] and got == sorted(base + self.content(notes)),
                   "apply=%.1fms roundtrip=%.1fms" % (a["ms"], rt))
        # modify: move, resize, velocity, mute, pitch (each on a different note + one with all)
        mods = [note(1, 38, 0.5, 0.75, 64.0, True), note(2, 43, 1.0, 0.25, 80.0), note(3, 60, 2.5, 1.0)]
        a, rt = self.apply("bass", "mod1", added=[], modified=mods, removed=[])
        got = self.content(self.readback("bass"))
        self.check("apply move+resize+velocity+mute+pitch via apply_note_modifications (pitch falls back if needed)",
                   a["ok"] and got == sorted(base + self.content(mods)), "apply=%.1fms got=%s" % (a["ms"], got))
        # remove
        a, rt = self.apply("bass", "rm1", added=[], modified=[], removed=[2])
        got = self.content(self.readback("bass"))
        self.check("apply delete note", a["ok"] and got == sorted(base + self.content([mods[0], mods[2]])), "apply=%.1fms" % a["ms"])
        # snapshot idempotency
        snapshot = [mods[0], mods[2]]
        a1, _ = self.apply("bass", "snap1", notes=snapshot)
        a2, _ = self.apply("bass", "snap2", notes=snapshot)
        got = self.content(self.readback("bass"))
        self.check("snapshot apply + unchanged snapshot is a no-op", a1["ok"] and a2["ok"] and got == self.content(snapshot),
                   "ms1=%.1f ms2=%.1f" % (a1["ms"], a2["ms"]))
        # no conflict should have been raised by our own writes (listener fires asynchronously)
        m = self.recv(1.5)
        self.check("no feedback/conflict after own writes", m is None or m["type"] not in ("conflict", "local_delta"), str(m))

        # Detection path on the OWNED clip using an API-originated edit
        t0 = time.time()
        a, _ = self.apply("drums", "det1", added=[note(1, 36, 0.0, 0.25), note(2, 38, 1.0, 0.25)], modified=[], removed=[])
        d = self.wait_for("local_delta", 5, clip="drums")
        delay = (d["t_detect"] - t0) * 1000.0
        self.check("notes listener detects edit on owned clip (API-originated)", len(d["added"]) == 2,
                   "detect delay=%.0fms, reported %.0fms after apply" % (delay, (time.time() - t0) * 1000))
        self.apply("drums", "det2", added=[], modified=[], removed=[1, 2])  # remote ids == local ids here (same machine)
        d = self.wait_for("local_delta", 5, clip="drums")
        self.check("listener detects deletion", len(d["removed"]) == 2, str(d["removed"]))
        self.check("clean up: drums clip back to its initial %d notes" % drums_before,
                   len(self.readback("drums")) == drums_before, "")

    def phase_b(self):
        print("\n=== Phase B: now edit the 'Anthem Drums' clip in Live's piano roll. ===")
        print("Draw a note; move it; resize it; change its velocity; copy/paste a few; delete.")
        print("Each detected native edit prints below. Ctrl-C when done.\n")
        try:
            while True:
                m = self.recv(1.0)
                if m is None:
                    continue
                if m["type"] == "local_delta":
                    lag = (time.time() - m["t_detect"]) * 1000.0
                    print("[%s] NATIVE EDIT on %s: +%d ~%d -%d  (debounce+transport %.0f ms) %s" % (
                        time.strftime("%H:%M:%S"), m["clip"], len(m["added"]), len(m["modified"]), len(m["removed"]), lag,
                        [(n["pitch"], n["start"], n["dur"], n["vel"]) for n in m["added"] + m["modified"]]))
                else:
                    print("[%s] %s" % (time.strftime("%H:%M:%S"), m))
        except KeyboardInterrupt:
            pass

    def summary(self):
        print("\n=== Phase A summary ===")
        for name, ok, detail in self.results:
            print("  [%s] %s  %s" % ("x" if ok else " ", name, detail))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=9800)
    ap.add_argument("--log", default="tools/live_verify.jsonl")
    ap.add_argument("--no-interactive", action="store_true")
    ap.add_argument("--reload", action="store_true", help="dev: hot-reload the script's modules first")
    args = ap.parse_args()
    d = Driver(args.port, args.log, reload=args.reload)
    d.wait_script()
    d.phase_a()
    d.summary()
    if not args.no_interactive:
        d.phase_b()


if __name__ == "__main__":
    main()
