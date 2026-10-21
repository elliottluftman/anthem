"""Non-blocking NDJSON TCP client used by the Remote Script to talk to the companion.

Everything here runs on Live's main thread from a Live.Base.Timer tick; no threads,
no blocking calls. The companion (a separate process) does all real network I/O.
"""
import errno
import json
import socket
import time

from .clipsync import OVERSIZED

MAX_LINE_BYTES = 256 * 1024
MAX_OUTBOX = 256            # messages; beyond this the oldest are dropped and a resync is flagged
RECONNECT_SECONDS = 2.0


class Bridge(object):
    def __init__(self, host="127.0.0.1", port=9800, log=None):
        self.addr = (host, port)
        self.log = log or (lambda *a: None)
        self.sock = None
        self.connected = False
        self._connecting_since = 0.0
        self._next_attempt = 0.0
        self._inbuf = b""
        self._outbox = []       # list of bytes lines
        self._partial = b""     # unsent remainder of the line currently being written
        self.dropped = False    # set when outbox overflowed; owner should resend a snapshot
        self.just_connected = False
        self.just_disconnected = False

    # --- public ---------------------------------------------------------------
    def send(self, msg):
        if not self.connected:
            return False
        msg = dict(msg)
        msg.setdefault("v", 1)
        line = json.dumps(msg, separators=(",", ":")).encode("utf-8") + b"\n"
        if len(line) > MAX_LINE_BYTES:
            self.log("bridge: refusing to send oversized message (%d bytes)" % len(line))
            return OVERSIZED   # distinct from False (= not connected): the caller must not advance state
        if len(self._outbox) >= MAX_OUTBOX:
            del self._outbox[0]
            self.dropped = True
        self._outbox.append(line)
        return True

    def poll(self):
        """Advance the connection state machine. Returns a list of decoded inbound messages."""
        self.just_connected = self.just_disconnected = False
        now = time.time()
        if self.sock is None:
            if now >= self._next_attempt:
                self._start_connect(now)
            return []
        if not self.connected:
            self._finish_connect(now)
            return []
        self._flush()
        if not self.connected:      # _flush may have dropped the socket (RST/EPIPE)
            return []
        return self._read()

    def close(self):
        self._drop("closed")
        self._next_attempt = float("inf")

    # --- internals ------------------------------------------------------------
    def _start_connect(self, now):
        try:
            s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            s.setblocking(False)
            s.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
            err = s.connect_ex(self.addr)
            if err not in (0, errno.EINPROGRESS, errno.EWOULDBLOCK, errno.EALREADY):
                s.close()
                self._next_attempt = now + RECONNECT_SECONDS
                return
            self.sock = s
            self._connecting_since = now
        except OSError as e:
            self.log("bridge: connect error %s" % e)
            self._next_attempt = now + RECONNECT_SECONDS

    def _finish_connect(self, now):
        err = self.sock.getsockopt(socket.SOL_SOCKET, socket.SO_ERROR)
        if err == 0:
            # SO_ERROR is 0 both while in progress and after success; probe with a zero-byte send.
            try:
                self.sock.send(b"")
            except (BlockingIOError, InterruptedError):
                pass
            except OSError as e:
                if e.errno in (errno.ENOTCONN, errno.EINPROGRESS, errno.EAGAIN):
                    if now - self._connecting_since > 5.0:
                        self._drop("connect timeout")
                    return
                self._drop("connect failed: %s" % e)
                return
            self.connected = True
            self.just_connected = True
            self._inbuf = b""
            self._outbox = []
            self._partial = b""
            self.dropped = False
            self.log("bridge: connected to companion %s:%d" % self.addr)
        elif err in (errno.EINPROGRESS, errno.EALREADY):
            if now - self._connecting_since > 5.0:
                self._drop("connect timeout")
        else:
            self._drop("connect refused (%s)" % errno.errorcode.get(err, err))

    def _flush(self):
        try:
            while self._partial or self._outbox:
                if not self._partial:
                    self._partial = self._outbox.pop(0)
                n = self.sock.send(self._partial)
                self._partial = self._partial[n:]
                if self._partial:
                    return  # kernel buffer full; try next tick
        except (BlockingIOError, InterruptedError):
            return
        except OSError as e:
            self._drop("send failed: %s" % e)

    def _read(self):
        msgs = []
        try:
            while True:
                chunk = self.sock.recv(65536)
                if not chunk:
                    self._drop("companion closed connection")
                    return msgs
                self._inbuf += chunk
                if len(self._inbuf) > MAX_LINE_BYTES and b"\n" not in self._inbuf:
                    self._drop("oversized line from companion")
                    return msgs
                if len(chunk) < 65536:
                    break
        except (BlockingIOError, InterruptedError):
            pass
        except OSError as e:
            self._drop("recv failed: %s" % e)
            return msgs
        while b"\n" in self._inbuf:
            line, self._inbuf = self._inbuf.split(b"\n", 1)
            if not line.strip():
                continue
            try:
                msg = json.loads(line.decode("utf-8"))
                if isinstance(msg, dict) and msg.get("v") == 1 and isinstance(msg.get("type"), str):
                    msgs.append(msg)
                else:
                    self.log("bridge: ignoring malformed message")
            except ValueError:
                self.log("bridge: ignoring undecodable line")
        return msgs

    def _drop(self, reason):
        if self.sock is not None:
            try:
                self.sock.close()
            except OSError:
                pass
        was = self.connected
        self.sock = None
        self.connected = False
        self._inbuf = b""
        self._outbox = []
        self._partial = b""
        self._next_attempt = time.time() + RECONNECT_SECONDS
        if was:
            self.just_disconnected = True
            self.log("bridge: disconnected (%s)" % reason)
