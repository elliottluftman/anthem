#!/usr/bin/env python3
"""Anthem companion CLI: asyncio glue around companion.core.Companion.

  anthem.py install [--dest DIR]
  anthem.py create --relay HOST:PORT [--tls] [--insecure-tls] [--name NAME] [--script-port 9800] [--create-token T]
  anthem.py join CODE --relay HOST:PORT [--tls] [--insecure-tls] [--script-port 9800]
  anthem.py resume [SESSION_ID] [--relay HOST:PORT] [--tls] [--script-port 9800]
  anthem.py status
"""
import argparse
import asyncio
import errno
import hmac
import json
import logging
import logging.handlers
import os
import secrets
import shutil
import ssl
import sys
import threading
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))

from anthem_core import protocol  # noqa: E402
from companion import core  # noqa: E402

ANTHEM_DIR = Path(os.environ.get("ANTHEM_HOME", str(Path.home() / ".anthem")))
SESSIONS_DIR = ANTHEM_DIR / "sessions"
LOG_PATH = ANTHEM_DIR / "anthem.log"
DEFAULT_SCRIPT_PORT = 9800
BACKOFF_MIN, BACKOFF_MAX = 1.0, 30.0
SCRIPT_HELLO_TIMEOUT = 10.0     # a script connection must authenticate within this time

# Relay `error` reasons after which reconnecting can never succeed (user must create/join again).
PERMANENT_ERRORS = ("unknown session", "unknown participant", "bad secret", "session too large",
                    "create not allowed", "unknown code", "session full")


def permanent_error_advice(reason):
    """Instruction for a permanent relay error, or None when retrying makes sense."""
    r = str(reason or "")
    if r == "unknown code":
        return "invite code not recognised - check the code (it is retired once the guest joined) and join again"
    if r == "create not allowed":
        return "this relay requires a create token (anthem.py create --create-token TOKEN)"
    if r == "session too large":
        return "the session no longer fits one relay message; create a new session"
    if r in ("session full",) or (r.startswith("role ") and r.endswith("unavailable")):
        return "the session already has both participants; create or join another session"
    if r in PERMANENT_ERRORS:
        return "this identity cannot resume the session; run 'anthem.py create' or 'anthem.py join CODE' again"
    return None

log = logging.getLogger("anthem")
log.addHandler(logging.NullHandler())   # quiet unless setup_logging() attaches the file handler


def _private_dir(path):
    """Create `path` 0700 (or tighten an existing one)."""
    try:
        path.mkdir(parents=True, mode=0o700)
    except FileExistsError:
        try:
            os.chmod(str(path), 0o700)
        except OSError:
            pass


def _write_secret(path, text):
    """Create `path` with mode 0600 from the first byte (exclusive temp file, then atomic rename)."""
    tmp = path.with_suffix(".tmp")
    try:
        os.unlink(str(tmp))
    except FileNotFoundError:
        pass
    fd = os.open(str(tmp), os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(text)
        os.replace(str(tmp), str(path))
    except BaseException:
        try:
            os.unlink(str(tmp))
        except OSError:
            pass
        raise


def setup_logging(verbose=False):
    _private_dir(ANTHEM_DIR)
    h = logging.handlers.RotatingFileHandler(str(LOG_PATH), maxBytes=1_000_000, backupCount=3)
    h.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s"))
    log.addHandler(h)
    log.setLevel(logging.DEBUG if verbose else logging.INFO)


# ----------------------------------------------------------------- identities
def identity_path(session):
    if not protocol.valid_id(session):     # the name becomes a file name: never trust it blindly
        raise ValueError("invalid session id %r" % (session,))
    return SESSIONS_DIR / ("%s.json" % session)


def save_identity(data):
    p = identity_path(data["session"])     # raises ValueError for a hostile session id
    _private_dir(ANTHEM_DIR)
    _private_dir(SESSIONS_DIR)
    _write_secret(p, json.dumps(data, indent=1, sort_keys=True))


def token_path(port=DEFAULT_SCRIPT_PORT):
    """Loopback token shared with the Remote Script (which reads ~/.anthem/script.token for the
    default port). A companion on a non-default --script-port gets its own file so two
    companions on one machine (tests, demos) do not overwrite each other's token."""
    return ANTHEM_DIR / ("script.token" if port == DEFAULT_SCRIPT_PORT else "script-%d.token" % port)


def write_token(port=DEFAULT_SCRIPT_PORT):
    """Generate the loopback token and write it 0600. Returns the token (raises OSError)."""
    token = secrets.token_urlsafe(24)
    _private_dir(ANTHEM_DIR)
    _write_secret(token_path(port), token + "\n")
    return token


def load_identities():
    out = []
    if SESSIONS_DIR.is_dir():
        for p in SESSIONS_DIR.glob("*.json"):
            try:
                with open(str(p), encoding="utf-8") as f:
                    d = json.load(f)
                d["_mtime"] = p.stat().st_mtime
                out.append(d)
            except (OSError, ValueError) as e:
                log.warning("unreadable identity file %s: %s", p, e)
    out.sort(key=lambda d: d["_mtime"], reverse=True)
    return out


def _summarize(msg):
    """Loggable one-liner for a message: never notes, never secrets."""
    t = msg.get("type")
    if t in ("create", "join", "hello"):
        return "%s participant=%s" % (t, msg.get("participant"))
    if t == "op":
        p = msg.get("payload") or {}
        n = len(p.get("notes", [])) if msg.get("kind") == "snapshot" else sum(
            len(p.get(k, [])) for k in ("added", "modified", "removed"))
        return "op %s clip=%s kind=%s base_rev=%s seq=%s n=%d" % (
            msg.get("op_id"), msg.get("clip"), msg.get("kind"), msg.get("base_rev"), msg.get("seq"), n)
    if t in ("apply_snapshot", "apply_delta", "local_snapshot", "local_delta"):
        n = len(msg.get("notes", [])) + sum(len(msg.get(k, [])) for k in ("added", "modified", "removed"))
        return "%s clip=%s op_id=%s n=%d" % (t, msg.get("clip"), msg.get("op_id"), n)
    if t == "welcome":
        ops = msg.get("ops")
        return "welcome session=%s seq=%s ops=%s resync=%s" % (
            msg.get("session"), msg.get("seq"), None if ops is None else len(ops), msg.get("resync"))
    return " ".join("%s=%s" % (k, v) for k, v in msg.items()
                    if k not in ("v", "notes", "added", "modified", "secret", "token"))


# ------------------------------------------------------------------ transport
class Link(object):
    """One NDJSON connection; reads lines, decodes with the given type set."""

    def __init__(self, name, reader, writer, types):
        self.name, self.reader, self.writer, self.types = name, reader, writer, types
        self.closed = False

    def send(self, msg):
        if self.closed or self.writer.is_closing():
            return False
        try:
            self.writer.write(protocol.encode(msg))
            log.debug("%s <- %s", self.name, _summarize(msg))
            return True
        except (protocol.ProtocolError, OSError) as e:
            log.warning("%s: cannot send %s: %s", self.name, msg.get("type"), e)
            return False

    async def messages(self):
        while True:
            try:
                line = await self.reader.readline()
            except (asyncio.IncompleteReadError, ConnectionError, OSError, asyncio.LimitOverrunError,
                    ValueError) as e:      # ValueError: line over the StreamReader limit
                log.info("%s: read error %s", self.name, e)
                return
            if not line:
                return
            try:
                msg = protocol.decode(line.rstrip(b"\r\n"), self.types)
            except protocol.ProtocolError as e:
                log.warning("%s: dropped malformed message: %s", self.name, e)
                continue
            log.debug("%s -> %s", self.name, _summarize(msg))
            yield msg

    async def close(self):
        self.closed = True
        try:
            self.writer.close()
            await self.writer.wait_closed()
        except (OSError, ConnectionError):
            pass


class SessionRunner(object):
    """Drives one Companion: relay client with backoff, loopback server for the Remote Script,
    stdin commands, periodic ticks, debounced identity persistence."""

    def __init__(self, comp, relay_addr, script_port=DEFAULT_SCRIPT_PORT, stdin=None,
                 tls=False, insecure=False, persist=None, out=None):
        self.comp = comp
        self.relay_host, self.relay_port = relay_addr
        self.script_port = script_port
        self.stdin = stdin
        self.tls, self.insecure = tls, insecure
        self.persist = persist
        self.out = out or (lambda s: print(s, flush=True))
        self.relay = None
        self.script = None
        self.server = None
        self.token = None       # loopback token the script must present (None = file unwritable)
        self.ready = asyncio.Event()
        self.stopped = asyncio.Event()
        self._persist_pending = None
        self._persist_last = 0.0
        self._tasks = []

    # --- actions ---
    def _handle(self, fn, msg):
        """Run one handler and dispatch its actions; a handler bug must never kill a loop."""
        try:
            self.dispatch(fn(msg))
        except Exception:
            log.exception("handler failed on %s", _summarize(msg))

    def dispatch(self, actions):
        for act in actions:
            kind = act[0]
            if kind == "relay":
                if self.relay is None:
                    log.info("relay offline; dropped %s", act[1].get("type"))
                elif not self.relay.send(act[1]):
                    self.out("ERROR: could not send %s to the relay (message too large or link closing); "
                             "see %s" % (act[1].get("type"), LOG_PATH))
            elif kind == "relay_drop":
                self.out("relay: %s - closing the link to reconnect" % act[1])
                log.warning("relay link dead (%s); closing", act[1])
                if self.relay is not None:
                    asyncio.ensure_future(self.relay.close())    # relay_loop sees EOF and reconnects
            elif kind == "script":
                if self.script is None or not self.script.send(act[1]):
                    log.info("script offline; dropped %s", act[1].get("type"))
            elif kind == "print":
                self.out(act[1])
            elif kind == "persist":
                self._schedule_persist()
            elif kind == "quit":
                self.stopped.set()

    def _schedule_persist(self):
        if self.persist is None:
            return
        now = time.monotonic()
        if now - self._persist_last >= 1.0:
            self._do_persist()
        elif self._persist_pending is None:
            loop = asyncio.get_event_loop()
            self._persist_pending = loop.call_later(1.0 - (now - self._persist_last), self._do_persist)

    def _do_persist(self):
        self._persist_pending = None
        self._persist_last = time.monotonic()
        try:
            self.persist(self.comp.identity())
        except (OSError, ValueError) as e:
            log.error("cannot persist identity: %s", e)

    # --- relay ---
    def _ssl(self):
        if not self.tls:
            return None
        ctx = ssl.create_default_context()
        if self.insecure:
            ctx.check_hostname = False
            ctx.verify_mode = ssl.CERT_NONE
        return ctx

    async def relay_loop(self):
        backoff = BACKOFF_MIN
        fatal = None
        while not self.stopped.is_set():
            self.out("relay: connecting to %s:%d%s" % (self.relay_host, self.relay_port, " (tls)" if self.tls else ""))
            try:
                reader, writer = await asyncio.wait_for(
                    asyncio.open_connection(self.relay_host, self.relay_port, ssl=self._ssl(),
                                            limit=protocol.MAX_LINE_BYTES + 1024), timeout=15)
            except (OSError, ssl.SSLError, asyncio.TimeoutError) as e:
                self.out("relay: connect failed (%s); retry in %.0fs" % (e, backoff))
                try:
                    await asyncio.wait_for(self.stopped.wait(), timeout=backoff)
                except asyncio.TimeoutError:
                    pass
                backoff = min(BACKOFF_MAX, backoff * 2)
                continue
            self.relay = Link("relay", reader, writer, protocol.RELAY_TYPES)
            self.out("relay: connected")
            log.info("relay connected")
            self.dispatch(self.comp.on_relay_connected())
            stream = self.relay.messages()
            while True:
                try:    # second line of defence behind Companion's relay_drop: no line at all for 3 pings
                    msg = await asyncio.wait_for(stream.__anext__(), 3 * core.PING_INTERVAL)
                except StopAsyncIteration:
                    break
                except asyncio.TimeoutError:
                    self.out("relay: no data for %.0fs - closing the link to reconnect" % (3 * core.PING_INTERVAL))
                    log.warning("relay read timed out; closing")
                    break
                self._handle(self.comp.on_relay, msg)
                if msg.get("type") == "welcome":
                    backoff = BACKOFF_MIN      # a real session: reset the backoff
                if msg.get("type") == "error":
                    fatal = permanent_error_advice(msg.get("reason"))
                    if fatal:
                        self.out("relay: giving up (%s): %s" % (msg.get("reason"), fatal))
                        log.error("permanent relay error %r; stopping", msg.get("reason"))
                    break
            await stream.aclose()
            await self.relay.close()
            self.relay = None
            log.info("relay disconnected")
            self.dispatch(self.comp.on_relay_disconnected())
            if fatal:
                self.stopped.set()     # after the link is closed: run()'s shutdown must not race us
            if self.stopped.is_set():
                break
            self.out("relay: reconnecting in %.0fs" % backoff)
            try:
                await asyncio.wait_for(self.stopped.wait(), timeout=backoff)
            except asyncio.TimeoutError:
                pass
            backoff = min(BACKOFF_MAX, backoff * 2)

    # --- script ---
    def _script_authenticated(self, first):
        if first is None or first.get("type") != "script_hello":
            return False
        if self.token is None:
            return True        # token file could not be written: local dev fallback
        tok = first.get("token")
        if not isinstance(tok, str):
            return False
        # compare bytes: compare_digest raises on non-ASCII str
        return hmac.compare_digest(tok.encode("utf-8"), self.token.encode("utf-8"))

    async def on_script_conn(self, reader, writer):
        peer = writer.get_extra_info("peername")
        link = Link("script", reader, writer, protocol.LOCAL_TYPES)
        # First message must be an authenticated script_hello; until then nothing is processed
        # and the current (authenticated) Live link is left alone.
        stream = link.messages()
        try:
            first = await asyncio.wait_for(stream.__anext__(), SCRIPT_HELLO_TIMEOUT)
        except (StopAsyncIteration, asyncio.TimeoutError):
            first = None
        if not self._script_authenticated(first):
            log.warning("unauthenticated script connection from %s", peer)
            await stream.aclose()
            await link.close()
            return
        old = self.script
        if old is not None:
            log.info("script: new connection %s replaces the old one", peer)
            self.dispatch(self.comp.on_script_disconnected())
            await old.close()
        self.script = link
        self.out("script: Live connected")
        log.info("script connected from %s", peer)
        self.dispatch(self.comp.on_script_connected())
        self._handle(self.comp.on_script, first)
        async for msg in stream:
            self._handle(self.comp.on_script, msg)
        await link.close()
        if self.script is link:
            self.script = None
            log.info("script disconnected")
            self.dispatch(self.comp.on_script_disconnected())

    # --- stdin / ticks ---
    async def stdin_loop(self):
        if self.stdin is None:
            q = asyncio.Queue()
            loop = asyncio.get_event_loop()

            def pump():
                for line in sys.stdin:
                    loop.call_soon_threadsafe(q.put_nowait, line)
                loop.call_soon_threadsafe(q.put_nowait, None)

            threading.Thread(target=pump, daemon=True).start()
            self.stdin = q
        while not self.stopped.is_set():
            line = await self.stdin.get()
            if line is None:
                return
            try:
                self.dispatch(self.comp.on_command(line))
            except Exception:  # never let a command kill the loop
                log.exception("command failed: %r", line)

    async def tick_loop(self):
        while not self.stopped.is_set():
            await asyncio.sleep(1.0)
            self.dispatch(self.comp.on_tick(time.monotonic()))

    # --- lifecycle ---
    async def run(self):
        """Returns False when the companion could not start (script port in use)."""
        try:
            self.server = await asyncio.start_server(self.on_script_conn, "127.0.0.1", self.script_port,
                                                     limit=protocol.MAX_LINE_BYTES + 1024)
        except OSError as e:
            if e.errno != errno.EADDRINUSE:
                raise
            self.out("port %d already in use — run the second companion with --script-port %d"
                     % (self.script_port, self.script_port + 10))
            self.stopped.set()
            return False
        try:
            self.token = write_token(self.script_port)
            log.info("script token written to %s", token_path(self.script_port))
        except OSError as e:
            self.token = None
            self.out("WARNING: cannot write %s (%s); accepting Remote Script connections without a token"
                     % (token_path(self.script_port), e))
        self.out("script: listening on 127.0.0.1:%d for the Live Remote Script" % self.script_port)
        self.ready.set()
        self._tasks = [asyncio.ensure_future(c) for c in (self.relay_loop(), self.stdin_loop(), self.tick_loop())]
        try:
            await self.stopped.wait()
        finally:
            for t in self._tasks:
                t.cancel()
            await asyncio.gather(*self._tasks, return_exceptions=True)
            if self.relay is not None:
                await self.relay.close()
            if self.script is not None:
                await self.script.close()
            self.server.close()
            await self.server.wait_closed()
            if self._persist_pending is not None:
                self._persist_pending.cancel()
                self._do_persist()
        return True

    def stop(self):
        self.stopped.set()


async def run_session(comp, relay_addr, script_port=DEFAULT_SCRIPT_PORT, stdin=None, **kw):
    """Run the session loop until `quit`/stop. `stdin` may be an asyncio.Queue of command lines
    (None = real stdin). Pass `runner=[]` in kw to receive the SessionRunner (for tests)."""
    holder = kw.pop("runner", None)
    r = SessionRunner(comp, relay_addr, script_port, stdin, **kw)
    if holder is not None:
        holder.append(r)
    return await r.run()


# ------------------------------------------------------------------- commands
def parse_relay(s):
    if not s or ":" not in s:
        raise SystemExit("--relay must be HOST:PORT")
    host, port = s.rsplit(":", 1)
    return host.strip("[]"), int(port)


def cmd_install(args):
    src = HERE.parent / "remote_script" / "Anthem"
    if not (src / "__init__.py").is_file():
        raise SystemExit("remote_script/Anthem/__init__.py not found - the Remote Script is not built yet "
                         "(it is being written separately). Nothing installed.")
    if args.dest:
        dest = Path(args.dest)
    elif sys.platform == "darwin":
        dest = Path.home() / "Music" / "Ableton" / "User Library" / "Remote Scripts" / "Anthem"
    elif sys.platform.startswith("win"):
        dest = Path(os.environ.get("USERPROFILE", str(Path.home()))) / "Documents" / "Ableton" / "User Library" / "Remote Scripts" / "Anthem"
    else:
        raise SystemExit("unsupported platform %s: pass --dest explicitly" % sys.platform)
    if dest.exists():
        # Never delete an arbitrary folder: only an empty one or a previous Anthem install.
        if not dest.is_dir():
            raise SystemExit("%s exists and is not a directory. Nothing installed." % dest)
        previous = (dest / "__init__.py").is_file() and (dest / "clipbinding.py").is_file()
        if any(dest.iterdir()) and not previous:
            raise SystemExit("%s is not empty and does not look like an Anthem install - refusing to "
                             "overwrite it. --dest must be the Anthem folder itself, e.g. --dest %s. "
                             "Nothing installed." % (dest, dest / "Anthem"))
        if previous:
            shutil.rmtree(str(dest))
    dest.parent.mkdir(parents=True, exist_ok=True)
    shutil.copytree(str(src), str(dest), dirs_exist_ok=True,
                    ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
    print("Installed Remote Script to %s" % dest)
    print("One manual step in Live: Settings > Link, Tempo & MIDI > Control Surface: Anthem "
          "(Input: None, Output: None), then restart Live or re-select the script.")


def cmd_status(args):
    ids = load_identities()
    if not ids:
        print("no saved sessions (%s)" % SESSIONS_DIR)
        return
    print("%-20s %-9s %-6s %-22s %-8s %s" % ("session", "code", "role", "relay", "last_seq", "set"))
    for d in ids:
        print("%-20s %-9s %-6s %-22s %-8s %s" % (d.get("session"), d.get("code"), d.get("role"),
                                                 "%s%s" % (d.get("relay"), " tls" if d.get("tls") else ""),
                                                 d.get("last_seq", 0), d.get("set_path") or "-"))


def _run(comp, relay, args, extra):
    if args.insecure_tls:
        print("WARNING: --insecure-tls disables certificate verification; only for self-signed test relays.")

    def persist(ident):
        if not ident.get("session"):
            return
        ident.update(extra)
        save_identity(ident)

    setup_logging(getattr(args, "verbose", False))
    log.info("session start participant=%s relay=%s:%d", comp.participant, relay[0], relay[1])
    ok = True
    try:
        ok = asyncio.run(run_session(comp, relay, args.script_port, tls=args.tls or args.insecure_tls,
                                     insecure=args.insecure_tls, persist=persist))
    except KeyboardInterrupt:
        pass
    print("bye")
    if ok is False:
        sys.exit(1)


def cmd_create(args):
    relay = parse_relay(args.relay)
    comp = core.Companion(core.new_participant_id(), secrets.token_urlsafe(24), create_clips=core.TEMPLATE_CLIPS,
                          create_token=args.create_token or os.environ.get("ANTHEM_CREATE_TOKEN") or None)
    print("creating a new session as host (participant %s)" % comp.participant)
    _run(comp, relay, args, {"relay": "%s:%d" % relay, "tls": bool(args.tls or args.insecure_tls), "name": args.name})


def cmd_join(args):
    relay = parse_relay(args.relay)
    comp = core.Companion(core.new_participant_id(), secrets.token_urlsafe(24), join_code=args.code.strip().upper())
    print("joining session %s as guest (participant %s)" % (args.code, comp.participant))
    _run(comp, relay, args, {"relay": "%s:%d" % relay, "tls": bool(args.tls or args.insecure_tls)})


def cmd_resume(args):
    ids = load_identities()
    if args.session:
        match = [d for d in ids if d.get("session") == args.session]
        if not match:
            raise SystemExit("no saved identity for session %s (see: anthem.py status)" % args.session)
        d = match[0]
    elif ids:
        d = ids[0]
    else:
        raise SystemExit("no saved sessions to resume")
    relay = parse_relay(args.relay or d.get("relay"))
    args.tls = args.tls or bool(d.get("tls"))
    comp = core.Companion(d["participant"], d["secret"], session=d["session"], code=d.get("code"),
                          role=d.get("role"), set_path=d.get("set_path"), last_seq=d.get("last_seq", 0))
    print("resuming session %s as %s (last_seq=%d)" % (d["session"], d.get("role"), comp.last_seq))
    _run(comp, relay, args, {"relay": "%s:%d" % relay, "tls": bool(args.tls), "name": d.get("name")})


def main(argv=None):
    ap = argparse.ArgumentParser(prog="anthem.py", description="Anthem companion")
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("install", help="install the Live Remote Script")
    p.add_argument("--dest", help="the Anthem folder itself, e.g. '<User Library>/Remote Scripts/Anthem' "
                                  "(default: Live's User Library); an existing non-Anthem folder is never overwritten")
    p.set_defaults(fn=cmd_install)

    def net(p):
        p.add_argument("--relay", help="HOST:PORT")
        p.add_argument("--tls", action="store_true")
        p.add_argument("--insecure-tls", action="store_true", help="TLS without certificate verification (test only)")
        p.add_argument("--script-port", type=int, default=DEFAULT_SCRIPT_PORT)
        p.add_argument("--verbose", action="store_true", help="debug-level log")

    p = sub.add_parser("create", help="create a session (you are the host)")
    net(p)
    p.add_argument("--name", default=None)
    p.add_argument("--create-token", default=None,
                   help="token required by relays started with --create-token (or env ANTHEM_CREATE_TOKEN)")
    p.set_defaults(fn=cmd_create)
    p = sub.add_parser("join", help="join with an invite code (you are the guest)")
    p.add_argument("code")
    net(p)
    p.set_defaults(fn=cmd_join)
    p = sub.add_parser("resume", help="resume a saved session")
    p.add_argument("session", nargs="?")
    net(p)
    p.set_defaults(fn=cmd_resume)
    p = sub.add_parser("status", help="list saved sessions")
    p.set_defaults(fn=cmd_status)

    args = ap.parse_args(argv)
    if args.cmd in ("create", "join") and not args.relay:
        ap.error("--relay HOST:PORT is required")
    args.fn(args)


if __name__ == "__main__":
    main()
