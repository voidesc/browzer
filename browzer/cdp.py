"""The Chrome DevTools Protocol over --remote-debugging-pipe.

The browser reads requests on its fd 3 and writes answers and events on its fd 4, one JSON
message each, ended by a NUL. No TCP port is opened, so nothing else on the machine can
attach to the browser.
"""
import json
import os
import select
import time


class Closed(Exception):
    """The browser closed its end of the pipe."""


class ProtocolError(Exception):
    """The browser answered a call with an error."""


class Pipe:
    def __init__(self, write_fd, read_fd):
        self.write_fd, self.read_fd = write_fd, read_fd
        self._buf = bytearray()
        self._scanned = 0     # bytes of _buf already known to hold no NUL
        self._next_id = 0
        self._backlog = []    # messages read while call() waited for its own answer

    def send(self, method, params=None, session=None):
        """Queues one request; returns its id. The answer arrives as a message with that id."""
        self._next_id += 1
        msg = {"id": self._next_id, "method": method, "params": params or {}}
        if session:
            msg["sessionId"] = session
        data = json.dumps(msg, separators=(",", ":")).encode() + b"\0"
        while data:
            data = data[os.write(self.write_fd, data):]
        return self._next_id

    def _parse(self):
        out = []
        while True:
            end = self._buf.find(b"\0", self._scanned)
            if end < 0:
                self._scanned = len(self._buf)
                return out
            out.append(json.loads(bytes(self._buf[:end])))
            del self._buf[:end + 1]
            self._scanned = 0

    def read(self):
        """One read from the pipe (call when it is readable); the complete messages so far,
        the ones call() set aside first."""
        chunk = os.read(self.read_fd, 1 << 22)
        if not chunk:
            raise Closed
        self._buf += chunk
        out, self._backlog = self._backlog + self._parse(), []
        return out

    def take(self):
        """The messages call() set aside, without reading."""
        out, self._backlog = self._backlog, []
        return out

    def call(self, method, params=None, session=None, timeout=10.0):
        """A request and its answer, blocking; everything else that arrives meanwhile is
        kept for read() / take()."""
        want = self.send(method, params, session)
        end = time.monotonic() + timeout
        while True:
            left = end - time.monotonic()
            if left <= 0 or not select.select([self.read_fd], [], [], left)[0]:
                raise TimeoutError(f"{method}: no answer in {timeout:g}s")
            chunk = os.read(self.read_fd, 1 << 22)
            if not chunk:
                raise Closed
            self._buf += chunk
            for msg in self._parse():
                if msg.get("id") != want:
                    self._backlog.append(msg)
                elif "error" in msg:
                    raise ProtocolError(f"{method}: {msg['error'].get('message', msg['error'])}")
                else:
                    return msg.get("result", {})

    def close(self):
        for fd in (self.write_fd, self.read_fd):
            try:
                os.close(fd)
            except OSError:
                pass
