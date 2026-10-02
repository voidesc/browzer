"""The control socket: another program (an agent's shell, a script) drives a running browzer.

One unix socket per browzer, in a directory only its user can enter. A client connects,
sends one JSON line `{"cmd": ..., ...}` and reads one JSON line back: `{"ok": true, ...}`
or `{"ok": false, "error": ...}`. Whoever can connect can read and steer every page, the
logged-in ones included: the same trust as the user's own shell. `--no-control` runs
browzer without it.
"""
import json
import os
import socket
import tempfile
import time

from . import snapshot
from .keys import CTRL, Key, cdp_key_events, parse_chord
from .kitty import alive

TEXT_LIMIT = 200_000
DEFAULT_TIMEOUT, MAX_TIMEOUT = 30.0, 120.0


def runtime_dir():
    """The directory of the sockets: ours alone, or an error."""
    base = os.environ.get("XDG_RUNTIME_DIR")
    path = os.path.join(base, "browzer") if base else os.path.join(tempfile.gettempdir(), f"browzer-{os.getuid()}")
    os.makedirs(path, mode=0o700, exist_ok=True)
    st = os.stat(path)
    if st.st_uid != os.getuid() or st.st_mode & 0o077:
        raise PermissionError(f"{path} must belong to you alone (mode 700)")
    return path


def socket_path(pid):
    return os.path.join(runtime_dir(), f"{pid}.sock")


def instances():
    """(pid, socket path) of every running browzer, the newest first; the socket a dead one
    left behind is removed."""
    found = []
    directory = runtime_dir()
    for name in os.listdir(directory):
        pid, _, ext = name.partition(".")
        if ext != "sock" or not pid.isdigit():
            continue
        path = os.path.join(directory, name)
        if alive(int(pid)):
            found.append((os.stat(path).st_mtime, int(pid), path))
        else:
            try:
                os.unlink(path)
            except OSError:
                pass
    return [(pid, path) for _, pid, path in sorted(found, reverse=True)]


class Request:
    """One client waiting for its one answer."""

    def __init__(self, conn, deadline):
        self.conn, self.deadline, self.on_timeout = conn, deadline, None

    @property
    def done(self):
        return self.conn is None

    def reply(self, **result):
        self._send({"ok": True, **result})

    def fail(self, error):
        self._send({"ok": False, "error": str(error)})

    def _send(self, answer):
        conn, self.conn = self.conn, None
        if conn is None:
            return
        try:
            conn.settimeout(5)
            conn.sendall(json.dumps(answer).encode() + b"\n")
        except OSError:
            pass
        finally:
            conn.close()


class Control:
    def __init__(self, app):
        self.app = app
        instances()   # sweeps what killed browzers left
        self.path = socket_path(os.getpid())
        if os.path.lexists(self.path):
            os.unlink(self.path)
        self.sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        mask = os.umask(0o177)
        try:
            self.sock.bind(self.path)
        finally:
            os.umask(mask)
        self.sock.listen(8)
        self.sock.setblocking(False)
        self.pending = []   # requests not answered yet
        self.loading = []   # (tab, request): answered when that tab's page has loaded

    def fileno(self):
        return self.sock.fileno()

    def close(self):
        for req in self.pending:
            req.fail("browzer is closing")
        self.sock.close()
        try:
            os.unlink(self.path)
        except OSError:
            pass

    # --- requests -----------------------------------------------------------------

    def accept(self):
        try:
            conn, _ = self.sock.accept()
        except BlockingIOError:
            return
        conn.settimeout(0.5)   # a client sends its line at once; one that does not is dropped
        line = b""
        try:
            while not line.endswith(b"\n") and len(line) < (1 << 20):
                chunk = conn.recv(65536)
                if not chunk:
                    break
                line += chunk
            msg = json.loads(line)
            cmd = msg["cmd"]
        except (OSError, ValueError, KeyError, TypeError):
            Request(conn, 0).fail("bad request: one JSON line with a cmd")
            return
        try:
            timeout = min(MAX_TIMEOUT, max(0.1, float(msg.get("timeout", DEFAULT_TIMEOUT))))
        except (TypeError, ValueError):
            timeout = DEFAULT_TIMEOUT
        req = Request(conn, time.monotonic() + timeout)
        handler = getattr(self, "cmd_" + str(cmd).replace("-", "_"), None)
        if not handler:
            req.fail(f"unknown command '{cmd}'")
            return
        self.pending.append(req)
        self.app.after("control", timeout)
        try:
            handler(req, msg)
        except (KeyError, ValueError, TypeError, IndexError) as e:
            req.fail(f"bad {cmd} request: {type(e).__name__}: {e}")

    def expire(self):
        """Answers the requests whose time is up; called by the app's timer."""
        now = time.monotonic()
        for req in self.pending:
            if not req.done and now >= req.deadline:
                (req.on_timeout or (lambda: req.fail("timed out")))()
        self.pending = [r for r in self.pending if not r.done]
        self.loading = [(t, r) for t, r in self.loading if not r.done]
        if self.pending:
            self.app.after("control", max(0.0, min(r.deadline for r in self.pending) - now))

    def loaded(self, tab):
        """The app saw `tab` finish loading."""
        waiting, self.loading = [r for t, r in self.loading if t is tab], [(t, r) for t, r in self.loading if t is not tab]
        for req in waiting:
            self.describe(tab, req)

    # --- helpers ------------------------------------------------------------------

    def tabs(self):
        app = self.app
        return [{"index": i + 1, "title": t.title, "url": t.url, "active": t is app.tab, "loading": t.loading}
                for i, t in enumerate(app.tabs)]

    def tab_of(self, msg):
        """The tab a request is about: the one it names (shown first), else the one on screen."""
        if msg.get("tab") is None:
            return self.app.tab
        index = int(msg["tab"])
        if not 1 <= index <= len(self.app.tabs):
            raise ValueError(f"there is no tab {index}")
        self.app.show(self.app.tabs[index - 1])
        return self.app.tab

    def describe(self, tab, req, **more):
        """Answers with what the tab now shows."""
        def titled(result):
            title = result.get("result", {}).get("value")
            if isinstance(title, str):
                tab.title = title
            index = self.app.tabs.index(tab) + 1 if tab in self.app.tabs else 0
            req.reply(tab=index, title=tab.title, url=tab.url, **more)
        self.app.send("Runtime.evaluate", {"expression": "document.title", "returnByValue": True},
                      then=titled, fail=req.fail, tab=tab)

    def when_loaded(self, tab, req):
        self.loading.append((tab, req))
        req.on_timeout = lambda: req.reply(tab=self.app.tabs.index(tab) + 1 if tab in self.app.tabs else 0,
                                           title=tab.title, url=tab.url, loading=True)

    def evaluate(self, tab, req, expression, then):
        def got(result):
            if "exceptionDetails" in result:
                details = result["exceptionDetails"]
                req.fail(details.get("exception", {}).get("description") or details.get("text", "the script failed"))
            else:
                then(result.get("result", {}))
        self.app.send("Runtime.evaluate", {"expression": expression, "returnByValue": True, "awaitPromise": True,
                                           "userGesture": True}, then=got, fail=req.fail, tab=tab)

    @staticmethod
    def node(ref):
        """A snapshot's ref (`e123`) as the backend node id it stands for."""
        ref = str(ref).strip().lstrip("@[").rstrip("]")
        if not ref.startswith("e") or not ref[1:].isdigit():
            raise ValueError(f"'{ref}' is not a ref from snapshot (they look like e123)")
        return int(ref[1:])

    @staticmethod
    def stale(req):
        """What to do when the browser does not know a ref: say so, and what helps."""
        return lambda why: req.fail(f"{why}: the page changed since that snapshot, take a new one")

    def click_at(self, tab, req, x, y):
        base = {"x": x, "y": y, "button": "left", "clickCount": 1}
        self.app.send("Input.dispatchMouseEvent", {"type": "mouseMoved", "x": x, "y": y}, tab=tab)
        self.app.send("Input.dispatchMouseEvent", {**base, "type": "mousePressed", "buttons": 1}, tab=tab, fail=req.fail)
        self.app.send("Input.dispatchMouseEvent", {**base, "type": "mouseReleased", "buttons": 0}, tab=tab,
                      fail=req.fail, then=lambda r: req.reply(x=round(x), y=round(y)))

    # --- commands -----------------------------------------------------------------

    def cmd_ls(self, req, msg):
        req.reply(pid=os.getpid(), tabs=self.tabs())

    def cmd_open(self, req, msg):
        url = str(msg["url"])

        def navigate(tab):
            def went(result):
                if result.get("errorText"):
                    req.fail(f"{url}: {result['errorText']}")
                elif result.get("loaderId"):
                    self.when_loaded(tab, req)
                else:   # the same document: nothing loads
                    self.describe(tab, req)
            self.app.send("Page.navigate", {"url": url}, then=went, fail=req.fail, tab=tab)

        if msg.get("new_tab"):
            def made(result):
                tab = self.app.adopt(result["targetId"])
                self.app.show(tab)
                navigate(tab)
            self.app.send("Target.createTarget", {"url": "about:blank"}, then=made, fail=req.fail, browser=True)
        else:
            navigate(self.tab_of(msg))

    def cmd_reload(self, req, msg):
        tab = self.tab_of(msg)
        self.when_loaded(tab, req)
        self.app.send("Page.reload", fail=req.fail, tab=tab)

    def history(self, req, msg, step):
        tab = self.tab_of(msg)

        def got(history):
            at = history["currentIndex"] + step
            if not 0 <= at < len(history["entries"]):
                req.fail("there is no page that way")
                return
            self.when_loaded(tab, req)
            self.app.send("Page.navigateToHistoryEntry", {"entryId": history["entries"][at]["id"]}, fail=req.fail, tab=tab)
        self.app.send("Page.getNavigationHistory", then=got, fail=req.fail, tab=tab)

    def cmd_back(self, req, msg):
        self.history(req, msg, -1)

    def cmd_forward(self, req, msg):
        self.history(req, msg, 1)

    def cmd_tab(self, req, msg):
        self.tab_of({"tab": msg["tab"]})
        req.reply(tabs=self.tabs())

    def cmd_close(self, req, msg):
        tab = self.tab_of(msg)
        self.app.close_tab(tab)
        req.reply(tabs=self.tabs())

    def cmd_text(self, req, msg):
        tab = self.tab_of(msg)
        self.evaluate(tab, req, "document.body ? document.body.innerText : ''",
                      lambda r: req.reply(text=str(r.get("value", ""))[:TEXT_LIMIT], title=tab.title, url=tab.url))

    def cmd_eval(self, req, msg):
        self.evaluate(self.tab_of(msg), req, str(msg["expression"]),
                      lambda r: req.reply(value=r.get("value"), type=r.get("type"), description=r.get("description")))

    def cmd_snapshot(self, req, msg):
        tab = self.tab_of(msg)
        self.app.send("Accessibility.getFullAXTree", fail=req.fail, tab=tab,
                      then=lambda r: req.reply(text=snapshot.render(r.get("nodes", [])), title=tab.title, url=tab.url))

    def cmd_click(self, req, msg):
        tab = self.tab_of(msg)
        if "ref" not in msg:
            self.click_at(tab, req, float(msg["x"]), float(msg["y"]))
            return
        node = {"backendNodeId": self.node(msg["ref"])}

        def quads(result):
            quad = (result.get("quads") or [None])[0]
            if not quad:
                req.fail("that element is not on the page any more, or not visible; take a new snapshot")
            else:
                self.click_at(tab, req, sum(quad[0::2]) / 4, sum(quad[1::2]) / 4)
        self.app.send("DOM.scrollIntoViewIfNeeded", node, fail=self.stale(req), tab=tab,
                      then=lambda r: self.app.send("DOM.getContentQuads", node, then=quads, fail=self.stale(req), tab=tab))

    def cmd_fill(self, req, msg):
        tab, text = self.tab_of(msg), str(msg["text"])

        def focused(result):
            for params in cdp_key_events(Key("a", CTRL)):   # what is there goes
                self.app.send("Input.dispatchKeyEvent", params, tab=tab)
            if text:
                self.app.send("Input.insertText", {"text": text}, then=lambda r: req.reply(), fail=req.fail, tab=tab)
            else:
                down, up = cdp_key_events(Key("Delete"))
                self.app.send("Input.dispatchKeyEvent", down, tab=tab)
                self.app.send("Input.dispatchKeyEvent", up, then=lambda r: req.reply(), fail=req.fail, tab=tab)
        self.app.send("DOM.focus", {"backendNodeId": self.node(msg["ref"])}, then=focused, fail=self.stale(req), tab=tab)

    def cmd_type(self, req, msg):
        self.app.send("Input.insertText", {"text": str(msg["text"])}, then=lambda r: req.reply(), fail=req.fail,
                      tab=self.tab_of(msg))

    def cmd_press(self, req, msg):
        tab = self.tab_of(msg)
        down, up = cdp_key_events(parse_chord(str(msg["key"])))
        self.app.send("Input.dispatchKeyEvent", down, fail=req.fail, tab=tab)
        self.app.send("Input.dispatchKeyEvent", up, then=lambda r: req.reply(), fail=req.fail, tab=tab)

    def cmd_screenshot(self, req, msg):
        self.app.send("Page.captureScreenshot", {"format": "png"}, fail=req.fail, tab=self.tab_of(msg),
                      then=lambda r: req.reply(png=r.get("data", "")))

    def cmd_quit(self, req, msg):
        req.reply()
        self.app.done = True
