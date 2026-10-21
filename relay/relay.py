#!/usr/bin/env python3
"""Anthem relay: auth, ownership, ordering and a bounded op log for two-person sessions.

Run:  python3 relay/relay.py --host 0.0.0.0 --port 9801 [--cert c.pem --key k.pem]
                             [--state ~/.anthem/relay-state.json] [--create-token TOKEN]
                             [--log-level INFO]

All state/ownership/sequence logic lives in anthem_core.session.Session; all wire
handling goes through anthem_core.protocol. Nothing from the wire is ever executed.
Persistence (--state) stores sessions, participants (secret hashes only), ownership,
revisions and authoritative notes. The op log is NOT persisted: after a restart,
resuming clients receive ``resync=true`` and a full snapshot.
"""
import argparse
import asyncio
import collections
import hmac
import json
import logging
import os
import secrets
import signal
import ssl
import sys
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from anthem_core import protocol, session as sessmod  # noqa: E402
from anthem_core.session import Rejected, Session  # noqa: E402

log = logging.getLogger("anthem.relay")

AUTH_TIMEOUT = 10.0          # first message must authenticate within this
IDLE_TIMEOUT = 90.0          # no message at all -> close
RATE_WINDOW, RATE_MAX = 10.0, 200
MAX_STRIKES = 20             # malformed-but-authenticated messages before close
MAX_SESSIONS = 1000
MAX_CONNECTIONS = 200
MAX_CONNECTIONS_PER_IP = 10
SESSION_CREATE_WINDOW, SESSION_CREATE_MAX = 3600.0, 20   # sessions one IP may create per window
FAIL_WINDOW = 600.0          # sliding window for failed pre-auth attempts, per IP
FAIL_DELAY_AFTER, FAIL_DELAY = 5, 2.0    # >= this many failures: every attempt is answered late
FAIL_BLOCK_AFTER = 50        # >= this many failures: refused outright until they age out
SESSION_TTL = 24 * 3600      # idle (nobody online) sessions are dropped after this
SWEEP_INTERVAL = 60.0
PERSIST_DEBOUNCE = 1.0
MAX_WRITE_BUFFER = 8 * 1024 * 1024  # slow peer -> drop it instead of growing forever

CLIENT_TYPES = {"create", "join", "hello", "op", "claim", "release", "ping"}
AUTH_TYPES = {"create", "join", "hello"}
# Pre-auth failures that count toward the per-IP throttle: anything a guesser or a prober would
# trigger. Any protocol.ProtocolError before auth (bad JSON, bad version, oversized line) counts
# as well. Honest mistakes of an already-identified client (short secret, bad clips, relay full,
# participant already connected, ...) are not counted.
THROTTLED_REASONS = {
    "unknown code", "bad secret", "unknown participant", "unknown session",
    "create not allowed", "first message must be create, join or hello",
    "bad participant id", "malformed auth",
}


class AuthError(Exception):
    pass


class Conn(object):
    """One client connection. ``session``/``participant`` are set after auth."""

    def __init__(self, reader, writer):
        self.reader, self.writer = reader, writer
        self.session = None
        self.participant = None
        self.strikes = 0
        self.closed = False
        self.times = collections.deque()
        self.peer = writer.get_extra_info("peername")
        self.ip = self.peer[0] if isinstance(self.peer, tuple) else str(self.peer)

    def write(self, msg):
        """Encode and queue ``msg``. Returns False (and closes) when it could not be sent."""
        try:
            line = protocol.encode(msg)
        except protocol.ProtocolError as e:
            log.warning("%s: write failed: %s", self.peer, e)
            self.close()
            return False
        return self.write_line(line)

    def write_line(self, line):
        if self.closed:
            return False
        try:
            self.writer.write(line)
            if self.writer.transport.get_write_buffer_size() > MAX_WRITE_BUFFER:
                log.warning("%s: write buffer overflow, dropping peer", self.peer)
                self.close()
                return False
        except Exception as e:  # dead transport
            log.warning("%s: write failed: %s", self.peer, e)
            self.close()
            return False
        return True

    def close(self):
        if not self.closed:
            self.closed = True
            try:
                self.writer.close()
            except Exception:
                pass


class RelayServer(object):
    def __init__(self, host="127.0.0.1", port=0, ssl_context=None, state_path=None,
                 auth_timeout=AUTH_TIMEOUT, idle_timeout=IDLE_TIMEOUT, create_token=None):
        self.host, self.port, self.ssl_context = host, port, ssl_context
        self.state_path = os.path.expanduser(state_path) if state_path else None
        self.auth_timeout, self.idle_timeout = auth_timeout, idle_timeout
        self.create_token = create_token   # when set, `create` must carry a matching "token"
        self.sessions = {}      # session id -> Session
        self.codes = {}         # invite code -> session id (retired once the guest has joined)
        self.touched = {}       # session id -> wall time of last activity
        self.online = {}        # (session id, participant) -> Conn
        self.conns = set()
        self.ip_conns = collections.Counter()   # ip -> open connections
        self.created = {}       # ip -> deque of monotonic times of session creation
        self.failures = {}      # ip -> deque of monotonic times of failed pre-auth attempts
        self._server = None
        self._sweeper = None
        self._persist_handle = None
        self._dirty = False

    # --- lifecycle ------------------------------------------------------------
    async def start(self):
        self._load_state()
        self._server = await asyncio.start_server(
            self._handle, self.host, self.port, ssl=self.ssl_context, limit=protocol.MAX_LINE_BYTES)
        self.port = self._server.sockets[0].getsockname()[1]
        self._sweeper = asyncio.ensure_future(self._sweep_loop())
        return self.port

    async def stop(self):
        if self._sweeper:
            self._sweeper.cancel()
        if self._server:
            self._server.close()
        for c in list(self.conns):
            c.close()
        if self._server:
            try:
                await asyncio.wait_for(self._server.wait_closed(), 5)
            except (asyncio.TimeoutError, asyncio.CancelledError):
                pass
        self._persist_now()
        log.info("relay stopped")

    async def _sweep_loop(self):
        while True:
            await asyncio.sleep(SWEEP_INTERVAL)
            now = time.time()
            for sid in list(self.sessions):
                s = self.sessions[sid]
                if now - self.touched.get(sid, now) > SESSION_TTL and \
                        not any(p["online"] for p in s.participants.values()):
                    log.info("dropping idle session %s", sid)
                    self.codes.pop(s.code, None)
                    del self.sessions[sid]
                    self.touched.pop(sid, None)
                    self._schedule_persist()
            for table, window in ((self.failures, FAIL_WINDOW), (self.created, SESSION_CREATE_WINDOW)):
                for ip in list(table):
                    if not self._window(table, ip, window):
                        del table[ip]

    def _window(self, table, ip, window):
        """The deque of timestamps for ``ip`` in ``table``, pruned to the last ``window`` seconds."""
        d = table.get(ip)
        if d is None:
            d = table[ip] = collections.deque()
        now = time.monotonic()
        while d and d[0] < now - window:
            d.popleft()
        return d

    # --- persistence ------------------------------------------------------------
    def _load_state(self):
        if not self.state_path or not os.path.exists(self.state_path):
            return
        with open(self.state_path, "r", encoding="utf-8") as f:
            data = json.load(f)
        for d in data.get("sessions", []):
            try:
                s = Session.from_json(d)
            except (KeyError, TypeError, ValueError, Rejected) as e:
                log.warning("skipping corrupt session in state file: %s", e)
                continue
            self._register(s)
        log.info("loaded %d session(s) from %s (op log not persisted: resuming clients "
                 "get resync=true)", len(self.sessions), self.state_path)

    def _schedule_persist(self):
        self._dirty = True
        if not self.state_path or self._persist_handle is not None:
            return
        self._persist_handle = asyncio.get_running_loop().call_later(PERSIST_DEBOUNCE, self._persist_now)

    def _persist_now(self):
        """Write the state file if anything changed since the last successful write. Never raises."""
        if self._persist_handle is not None:
            self._persist_handle.cancel()
            self._persist_handle = None
        if not self.state_path or not self._dirty:
            return
        tmp = self.state_path + ".tmp"
        try:
            d = os.path.dirname(self.state_path)
            if d:
                os.makedirs(d, exist_ok=True)
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump({"sessions": [s.to_json() for s in self.sessions.values()]}, f)
            os.replace(tmp, self.state_path)
            self._dirty = False
            log.debug("state persisted (%d sessions)", len(self.sessions))
        except OSError as e:  # stays dirty: the next change retries
            log.error("persist failed: %s", e)

    # --- session registry ---------------------------------------------------------
    def _register(self, s):
        self.sessions[s.id] = s
        if len(s.participants) < sessmod.MAX_PARTICIPANTS:
            self.codes[s.code] = s.id
        self.touched[s.id] = time.time()

    def _online_conns(self, s):
        return [c for (sid, _), c in self.online.items() if sid == s.id and not c.closed]

    def _broadcast(self, s, msg, exclude=None):
        for c in self._online_conns(s):
            if c is not exclude:
                c.write(msg)

    def _broadcast_line(self, s, line, exclude=None):
        for c in self._online_conns(s):
            if c is not exclude:
                c.write_line(line)

    def _broadcast_ownership(self, s, clip):
        self._broadcast(s, {"type": "ownership", "clip": clip, "owner": s.owner_of(clip)})

    # --- connection handling ------------------------------------------------------
    async def _handle(self, reader, writer):
        conn = Conn(reader, writer)
        self.conns.add(conn)
        self.ip_conns[conn.ip] += 1
        try:
            if len(self.conns) > MAX_CONNECTIONS:
                await self._error_close(conn, "relay busy")
                return
            if self.ip_conns[conn.ip] > MAX_CONNECTIONS_PER_IP:
                await self._error_close(conn, "too many connections")
                return
            if len(self._window(self.failures, conn.ip, FAIL_WINDOW)) >= FAIL_BLOCK_AFTER:
                await self._error_close(conn, "too many failed attempts")
                return
            await self._run(conn)
        except (ConnectionError, asyncio.IncompleteReadError, asyncio.CancelledError):
            pass
        except Exception:
            log.exception("%s: unhandled error", conn.peer)
        finally:
            self.conns.discard(conn)
            self.ip_conns[conn.ip] -= 1
            if not self.ip_conns[conn.ip]:
                del self.ip_conns[conn.ip]
            conn.close()
            self._detach(conn)

    def _detach(self, conn):
        s, pid = conn.session, conn.participant
        if s is None or self.online.get((s.id, pid)) is not conn:
            return
        del self.online[(s.id, pid)]
        if s.id in self.sessions:
            s.set_online(pid, False)
            self.touched[s.id] = time.time()
            self._broadcast(s, {"type": "presence", "participants": s.presence()})
            log.info("session %s: %s offline", s.id, pid)

    async def _error_close(self, conn, reason):
        log.info("%s: error -> close: %s", conn.peer, reason)
        conn.write({"type": "error", "reason": reason})
        try:
            await asyncio.wait_for(conn.writer.drain(), 2)
        except Exception:
            pass
        conn.close()

    async def _readline(self, conn, timeout):
        """Returns a line, b'' on EOF; raises ProtocolError on an over-limit line."""
        try:
            return await asyncio.wait_for(conn.reader.readline(), timeout)
        except asyncio.TimeoutError:
            return b""
        except ValueError:  # StreamReader limit exceeded
            raise protocol.ProtocolError("line too large")

    async def _run(self, conn):
        try:
            line = await self._readline(conn, self.auth_timeout)
            if not line:
                return
            if len(self._window(self.failures, conn.ip, FAIL_WINDOW)) >= FAIL_DELAY_AFTER:
                await asyncio.sleep(FAIL_DELAY)   # tarpit: every pre-auth answer from this IP is late
            msg = protocol.decode(line, CLIENT_TYPES)
            if msg["type"] not in AUTH_TYPES:
                raise AuthError("first message must be create, join or hello")
            s, pid = self._authenticate(conn, msg)
        except (protocol.ProtocolError, AuthError, Rejected) as e:
            reason = str(getattr(e, "reason", e))
            if isinstance(e, protocol.ProtocolError) or reason in THROTTLED_REASONS:
                self._window(self.failures, conn.ip, FAIL_WINDOW).append(time.monotonic())
            await self._error_close(conn, reason)
            return
        while not conn.closed:
            try:
                line = await self._readline(conn, self.idle_timeout)
            except protocol.ProtocolError as e:
                await self._error_close(conn, str(e))
                return
            if not line:
                return
            now = time.monotonic()
            conn.times.append(now)
            while conn.times and conn.times[0] < now - RATE_WINDOW:
                conn.times.popleft()
            if len(conn.times) > RATE_MAX:
                await self._error_close(conn, "rate limit exceeded")
                return
            try:
                msg = protocol.decode(line, CLIENT_TYPES)
            except protocol.ProtocolError as e:
                await self._error_close(conn, str(e))
                return
            t = msg["type"]
            if t in AUTH_TYPES:
                await self._error_close(conn, "already authenticated")
                return
            self.touched[s.id] = time.time()
            if t == "ping":
                conn.write({"type": "pong"})
            elif t == "op":
                self._handle_op(conn, s, pid, msg)
            else:
                self._handle_ownership(conn, s, pid, t, msg)
            if conn.strikes >= MAX_STRIKES:
                await self._error_close(conn, "too many invalid messages")
                return
            await conn.writer.drain()

    # --- auth: create / join / hello --------------------------------------------
    def _authenticate(self, conn, msg):
        """Authenticate the first message. Raises AuthError/Rejected; never anything else."""
        try:
            return self._authenticate_checked(conn, msg)
        except (AuthError, Rejected):
            raise
        except (TypeError, ValueError) as e:
            # Defensive: a shape we did not anticipate must still be a counted, answered failure
            # rather than an unhandled exception that closes the socket silently.
            log.warning("%s: malformed auth message (%s: %s)", conn.peer, type(e).__name__, e)
            raise AuthError("malformed auth")

    def _authenticate_checked(self, conn, msg):
        t, pid, secret = msg["type"], msg.get("participant"), msg.get("secret")
        # Shape checks first, before any session is touched: wrong types must never reach
        # hmac.compare_digest / dict lookups (TypeError) and must count toward the throttle.
        if not protocol.valid_id(pid):
            raise AuthError("bad participant id")
        if not isinstance(secret, str):
            raise AuthError("malformed auth")
        for k in ("token", "code", "session"):
            if msg.get(k) is not None and not isinstance(msg[k], str):
                raise AuthError("malformed auth")
        last_seq = 0
        if t == "create":
            token = msg.get("token")
            if self.create_token is not None and not (
                    token is not None and _tokens_match(token, self.create_token)):
                raise AuthError("create not allowed")
            if len(self.sessions) >= MAX_SESSIONS:
                raise AuthError("relay full")
            created = self._window(self.created, conn.ip, SESSION_CREATE_WINDOW)
            if len(created) >= SESSION_CREATE_MAX:
                raise AuthError("too many sessions")
            clips = _validate_clips(msg.get("clips"))
            code = sessmod.new_code()
            while code in self.codes:
                code = sessmod.new_code()
            s = Session(secrets.token_urlsafe(12), code, clips)
            s.add_participant(pid, secret, "host")
            self._register(s)
            created.append(time.monotonic())
            log.info("session %s created by %s (%d clips)", s.id, pid, len(clips))
        elif t == "join":
            code = msg.get("code")
            sid = self.codes.get(code.strip().upper()) if isinstance(code, str) else None
            if sid is None:
                raise AuthError("unknown code")
            s = self.sessions[sid]
            s.add_participant(pid, secret, "guest")
            del self.codes[s.code]   # single use: the session is full now
            log.info("session %s: %s joined as guest", s.id, pid)
        else:  # hello
            s = self.sessions.get(msg.get("session")) if isinstance(msg.get("session"), str) else None
            if s is None:
                raise AuthError("unknown session")
            s.authenticate(pid, secret)
            if (s.id, pid) in self.online:
                raise AuthError("participant already connected")
            last_seq = msg.get("last_seq", 0)
            if not isinstance(last_seq, int) or last_seq < 0:
                raise AuthError("bad last_seq")
        role = s.participants[pid]["role"]
        # A client claiming to be ahead of us (e.g. relay restarted) must resync from the snapshot.
        ops = None if last_seq > s.seq else s.ops_since(last_seq)
        welcome = {
            "type": "welcome", "session": s.id, "code": s.code, "participant": pid, "role": role,
            "seq": int(s.seq), "snapshot": s.snapshot(), "ops": ops, "resync": ops is None,
        }
        try:
            line = protocol.encode(welcome)
        except protocol.ProtocolError:
            welcome.update(ops=None, resync=True)   # backlog does not fit one line: snapshot only
            try:
                line = protocol.encode(welcome)
            except protocol.ProtocolError:
                # Session.apply_op refuses any op whose welcome would not fit, so this is only
                # reachable for state that did not come through apply_op (e.g. an old state file).
                raise AuthError("session too large")
        if t != "hello":
            self._schedule_persist()
        if not conn.write_line(line):
            raise AuthError("welcome not delivered")
        conn.session, conn.participant = s, pid
        self.online[(s.id, pid)] = conn
        s.set_online(pid, True)
        self.touched[s.id] = time.time()
        self._broadcast(s, {"type": "presence", "participants": s.presence()})
        for clip in s.clips:
            self._broadcast_ownership(s, clip)
        log.info("session %s: %s online as %s (last_seq=%d, resync=%s)", s.id, pid, role,
                 last_seq, welcome["resync"])
        return s, pid

    # --- op / claim / release ---------------------------------------------------------
    def _handle_op(self, conn, s, pid, msg):
        op_id = msg.get("op_id") if protocol.valid_id(msg.get("op_id")) else None
        try:
            op = protocol.validate_op(msg)
        except protocol.ProtocolError as e:
            conn.strikes += 1
            conn.write({"type": "reject", "op_id": op_id, "reason": str(e)})
            return
        # The relayed form carries seq/rev/participant on top of the incoming line (and the
        # normalized payload may be longer than what was sent), so it can exceed MAX_LINE_BYTES
        # although the incoming line did not. Encode it BEFORE applying: if it does not fit, the
        # op is rejected and nothing changes, instead of being acked and then silently closing
        # the receiver's connection. These are the exact seq/rev the op gets if accepted
        # (non-duplicate), so the same line is what gets broadcast.
        p = op["payload"]
        relayed = {
            "type": "op", "seq": s.seq + 1, "rev": s.revs.get(op["clip"], 0) + 1, "participant": pid,
            "op_id": op_id, "clip": op["clip"], "kind": op["kind"], "payload": p,
        }
        try:
            line = protocol.encode(relayed)
        except protocol.ProtocolError:
            log.debug("session %s: op %s from %s rejected: relayed form too large", s.id, op_id, pid)
            conn.write({"type": "reject", "op_id": op_id, "reason": "clip too large"})
            return
        try:
            seq, rev, dup = s.apply_op(pid, op)
        except Rejected as e:
            log.debug("session %s: op %s from %s rejected: %s", s.id, op_id, pid, e.reason)
            conn.write({"type": "reject", "op_id": op_id, "reason": e.reason})
            return
        conn.write({"type": "op_ack", "op_id": op_id, "seq": seq, "rev": rev, "duplicate": dup})
        if dup:
            return
        n = len(p["notes"]) if op["kind"] == "snapshot" else \
            len(p["added"]) + len(p["modified"]) + len(p["removed"])
        log.debug("session %s: seq=%d %s/%s rev=%d by %s (%d notes)", s.id, seq, op["clip"],
                  op["kind"], rev, pid, n)
        if (seq, rev) == (relayed["seq"], relayed["rev"]):
            self._broadcast_line(s, line, exclude=conn)
        else:  # cannot happen for a freshly accepted op; kept so a future change stays correct
            self._broadcast(s, dict(relayed, seq=seq, rev=rev), exclude=conn)
        self._schedule_persist()

    def _handle_ownership(self, conn, s, pid, t, msg):
        clip = msg.get("clip")
        if not protocol.valid_id(clip):
            conn.strikes += 1
            conn.write({"type": "reject", "op_id": None, "reason": "bad clip id"})
            return
        try:
            (s.claim if t == "claim" else s.release)(pid, clip)
        except Rejected as e:
            conn.write({"type": "reject", "op_id": None, "reason": e.reason})
            return
        log.info("session %s: %s %s %s -> owner %s", s.id, pid, t, clip, s.owner_of(clip))
        self._broadcast_ownership(s, clip)
        self._schedule_persist()


def _tokens_match(a, b):
    """Constant-time equality of two str tokens. Compared as UTF-8 bytes: str/str compare_digest
    raises TypeError on non-ASCII input, which would otherwise escape as an unhandled error."""
    return hmac.compare_digest(a.encode("utf-8"), b.encode("utf-8"))


def _validate_clips(clips):
    if not isinstance(clips, list) or not 1 <= len(clips) <= protocol.MAX_TRACKS:
        raise AuthError("clips must be a list of 1..%d entries" % protocol.MAX_TRACKS)
    out, seen = [], set()
    for c in clips:
        if not isinstance(c, dict) or not protocol.valid_id(c.get("clip")) or c["clip"] in seen:
            raise AuthError("bad or duplicate clip id")
        name = c.get("name", c["clip"])
        if c.get("owner") not in sessmod.ROLES:
            raise AuthError("clip owner must be 'host' or 'guest'")
        if not isinstance(c.get("track_name"), str) or len(c["track_name"]) > 64:
            raise AuthError("clip track_name must be a string of <= 64 chars")
        if not isinstance(name, str) or len(name) > 64:
            raise AuthError("clip name must be a string of <= 64 chars")
        slot = c.get("slot", 0)
        if not isinstance(slot, int) or isinstance(slot, bool) or not 0 <= slot <= 63:
            raise AuthError("clip slot must be an int 0..63")
        seen.add(c["clip"])
        out.append({"clip": c["clip"], "name": name, "track_name": c["track_name"],
                    "slot": slot, "owner": c["owner"]})
    return out


def make_ssl_context(cert, key):
    ctx = ssl.create_default_context(ssl.Purpose.CLIENT_AUTH)  # PROTOCOL_TLS_SERVER
    ctx.minimum_version = ssl.TLSVersion.TLSv1_2
    ctx.load_cert_chain(cert, key)
    return ctx


async def serve(host, port, ssl_context=None, state_path=None, create_token=None):
    """Start a relay and return the RelayServer (``.port`` is the bound port)."""
    srv = RelayServer(host, port, ssl_context, state_path, create_token=create_token)
    await srv.start()
    return srv


async def _main(args):
    ctx = make_ssl_context(args.cert, args.key) if args.cert else None
    srv = await serve(args.host, args.port, ctx, args.state, args.create_token)
    tls = "TLS ON" if ctx else "TLS OFF - plaintext is for LAN/localhost testing only!"
    state = ("state=%s (op log not persisted: clients resync after restart)" % srv.state_path
             if srv.state_path else "no persistence (--state not given)")
    create = ("create requires --create-token" if srv.create_token
              else "anyone may create sessions (restrict with --create-token)")
    print("Anthem relay listening on %s:%d | %s | %s | %s | idle sessions dropped after %dh "
          "(SESSION_TTL)" % (args.host, srv.port, tls, state, create, SESSION_TTL // 3600), flush=True)
    stop = asyncio.Event()
    loop = asyncio.get_event_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, stop.set)
        except (NotImplementedError, RuntimeError):
            pass
    await stop.wait()
    print("shutting down", flush=True)
    await srv.stop()


def main(argv=None):
    ap = argparse.ArgumentParser(description="Anthem relay server")
    ap.add_argument("--host", default="0.0.0.0")
    ap.add_argument("--port", type=int, default=9801)
    ap.add_argument("--cert")
    ap.add_argument("--key")
    ap.add_argument("--state", help="JSON state file, e.g. ~/.anthem/relay-state.json")
    ap.add_argument("--create-token", help="if set, `create` must carry a matching \"token\"")
    ap.add_argument("--log-level", default="INFO")
    args = ap.parse_args(argv)
    if bool(args.cert) != bool(args.key):
        ap.error("--cert and --key must be given together")
    logging.basicConfig(level=getattr(logging, args.log_level.upper(), logging.INFO),
                        format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    try:
        asyncio.run(_main(args))
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
