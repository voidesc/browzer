"""The browser: Chromium pages as tabs, the active one drawn below a one-row bar and fed by
the terminal's keys and mouse."""
import base64
import glob
import os
import re
import selectors
import shutil
import signal
import tempfile
import time

from . import chromium, kitty, term
from .bar import LineEdit, fit
from .cdp import Closed, ProtocolError
from .keys import ALT, CTRL, Focus, Key, Mouse, Parser, Paste, cdp_key_events, cdp_mods
from .url import SEARCH, normalize

BAR_ROWS = 1
WHEEL_STEP = 100          # CSS pixels a wheel notch scrolls
DOUBLE_CLICK = (0.4, 6)   # seconds, device pixels: a second press inside both is a double click
BUTTONS = ("left", "middle", "right")
BUTTON_BITS = (1, 4, 2)   # the protocol's `buttons` mask, by our button number
HINT = " ^L address · ^T tab · ^Q quit "

# what the pointer should look like over a point of the page: the element's own cursor, or
# for `auto` the text beam over text and the arrow elsewhere
POINTER_AT = """(() => { const e = document.elementFromPoint(%f, %f); if (!e) return 'default';
const c = getComputedStyle(e).cursor; if (c !== 'auto') return c;
const r = document.caretRangeFromPoint(%f, %f);
return r && r.startContainer.nodeType === 3 && e.contains(r.startContainer) ? 'text' : 'default' })()"""
# the selected text, a field's included, a password field's never
SELECTION = """(() => { const a = document.activeElement;
if (a && typeof a.selectionStart === 'number' && a.selectionEnd > a.selectionStart)
  return a.type === 'password' ? '' : a.value.substring(a.selectionStart, a.selectionEnd);
return String(getSelection()) })()"""


class Unsupported(Exception):
    pass


class Tab:
    def __init__(self, target, session):
        self.target, self.session = target, session
        self.url, self.title, self.loading = "", "", False


class Dialog:
    """A page's alert, confirm or prompt, waiting in the bar for its answer."""

    def __init__(self, tab, kind, message, default):
        self.tab, self.kind, self.message = tab, kind, " ".join(message.split())
        self.edit = LineEdit(default, fresh=True) if kind == "prompt" else None


class App:
    def __init__(self, url, *, binary, profile, log_path, scale=1.0, transfer="auto", stats=False,
                 search=SEARCH, in_fd=0, out_fd=1):
        self.first_url, self.binary, self.profile, self.log_path = url, binary, profile, log_path
        self.scale, self.stats, self.search, self.in_fd, self.out_fd = scale, stats, search, in_fd, out_fd
        if transfer == "auto":   # a file in shared memory only reaches a terminal on this machine
            remote = any(os.environ.get(v) for v in ("SSH_CONNECTION", "SSH_TTY", "TMUX"))
            transfer = "inline" if remote else "file"
        self.transfer = transfer
        self.temp_profile = None
        self.proc = self.cdp = None
        self.tabs, self.tab = [], None         # all of them, and the one on screen
        self.mode = None                       # None, a LineEdit (the address), or a Dialog
        self.size = None
        self.parser = Parser()
        self.done = False
        self.error = None
        self.notice = ""
        self.image_id = self.shown_id = 0
        self.serial = 0
        self.replies = {}                      # request id -> what to do with its result
        self.timers = {}                       # name -> when it is due
        self.held = 0                          # mouse buttons down, as the protocol's mask
        self.last_press = (0.0, 0, 0, -1, 0)   # time, x, y, button, click count
        self.move = self.wheel = None          # the latest of each, sent once per batch of input
        self.hover = None                      # where the pointer rests, in CSS pixels
        self.pointer = "default"
        self.frames = []                       # arrival times within the last second
        self.frame_bytes = self.draw_ms = 0
        self.bar_drawn = None
        self.sized_at = 0.0                    # when the page's box last changed

    # --- geometry -----------------------------------------------------------------

    def view(self):
        """(columns, rows, pixel width, pixel height) of the page's box."""
        s = self.size
        rows = max(1, s.rows - BAR_ROWS)
        return s.cols, rows, s.cols * s.cell_w, rows * s.cell_h

    def css(self, px):
        return max(1, round(px / self.scale))

    # --- the browser --------------------------------------------------------------

    def send(self, method, params=None, then=None, tab=None, browser=False):
        """A request to the active tab (or `tab`, or the browser itself); `then` gets its result."""
        tab = tab or self.tab
        rid = self.cdp.send(method, params, None if browser or not tab else tab.session)
        if then:
            self.replies[rid] = then

    def start(self):
        self.size = term.size(self.out_fd)
        if not self.size.xpix or not self.size.ypix:
            raise Unsupported("this terminal does not report its size in pixels; browzer needs a terminal "
                              "with the kitty graphics protocol (kitty, ghostty)")
        kitty.sweep()
        if self.profile is None:
            for stale in glob.glob(os.path.join(tempfile.gettempdir(), "browzer-profile-*-*")):
                pid = os.path.basename(stale).split("-")[2]
                if pid.isdigit() and not kitty.alive(int(pid)):
                    shutil.rmtree(stale, ignore_errors=True)
            self.profile = self.temp_profile = tempfile.mkdtemp(prefix=f"browzer-profile-{os.getpid()}-")
        os.makedirs(self.profile, exist_ok=True)
        _, _, w, h = self.view()
        self.proc, self.cdp = chromium.launch(self.binary, self.profile, self.css(w), self.css(h), self.log_path)
        try:
            self.cdp.call("Target.setDiscoverTargets", {"discover": True})
            page = next(t for t in self.cdp.call("Target.getTargets")["targetInfos"] if t["type"] == "page")
        except (Closed, StopIteration, TimeoutError) as e:
            raise Unsupported(
                f"{self.binary} did not start a page ({type(e).__name__}). Another browzer may be using the "
                f"profile {self.profile}: try --temp-profile." + (f" Its messages: {self.log_path}" if self.log_path else ""))
        # downloads need a place and a question first; until then the bar says one was refused
        self.cdp.send("Browser.setDownloadBehavior", {"behavior": "deny", "eventsEnabled": True})
        self.show(self.adopt(page["targetId"]))
        self.send("Page.navigate", {"url": self.first_url})

    def stop(self):
        if self.cdp:
            try:
                self.cdp.send("Browser.close")
            except OSError:
                pass
            if self.proc.wait(3) is None:
                self.proc.kill()
            self.cdp.close()
        kitty.sweep()
        if self.temp_profile:
            shutil.rmtree(self.temp_profile, ignore_errors=True)

    # --- tabs ---------------------------------------------------------------------

    def adopt(self, target):
        """Attaches to a page and makes it a tab (not yet the one on screen)."""
        session = self.cdp.call("Target.attachToTarget", {"targetId": target, "flatten": True})["sessionId"]
        tab = Tab(target, session)
        self.tabs.append(tab)
        self.send("Page.enable", tab=tab)
        self.send("Emulation.setFocusEmulationEnabled", {"enabled": True}, tab=tab)
        return tab

    def show(self, tab):
        """Puts a tab on screen: the one before stops sending frames, this one starts."""
        if self.tab is tab:
            return
        if self.tab in self.tabs:
            self.send("Page.stopScreencast")
        self.tab, self.hover, self.held = tab, None, 0
        self.send("Page.bringToFront")
        self.apply_size()
        self.after("title", 0)

    def apply_size(self):
        _, _, w, h = self.view()
        self.sized_at = time.monotonic()
        self.send("Emulation.setDeviceMetricsOverride",
                  {"width": self.css(w), "height": self.css(h), "deviceScaleFactor": self.scale, "mobile": False})
        self.send("Page.stopScreencast")
        self.send("Page.startScreencast", {"format": "png", "everyNthFrame": 1, "maxWidth": w, "maxHeight": h})

    def new_tab(self):
        def made(result):
            self.show(self.adopt(result["targetId"]))
            self.mode = LineEdit()
        self.send("Target.createTarget", {"url": "about:blank"}, then=made, browser=True)

    def close_tab(self, tab):
        self.send("Target.closeTarget", {"targetId": tab.target}, browser=True)
        self.drop(tab)

    def drop(self, tab):
        """A tab is gone: its neighbour takes the screen; the last one gone ends browzer."""
        if tab not in self.tabs:
            return
        at = self.tabs.index(tab)
        self.tabs.remove(tab)
        if isinstance(self.mode, Dialog) and self.mode.tab is tab:
            self.mode = None
        if not self.tabs:
            self.done = True
        elif self.tab is tab:
            self.show(self.tabs[min(at, len(self.tabs) - 1)])

    def step_tab(self, step):
        if len(self.tabs) > 1:
            self.show(self.tabs[(self.tabs.index(self.tab) + step) % len(self.tabs)])

    # --- the loop -----------------------------------------------------------------

    def after(self, name, delay):
        """Asks for timer `name` to fire in `delay` seconds, or sooner if it is already set."""
        due = time.monotonic() + delay
        self.timers[name] = min(self.timers.get(name, due), due)

    def fire(self, name):
        if name == "title" and self.tab:
            tab = self.tab
            self.send("Runtime.evaluate", {"expression": "document.title", "returnByValue": True},
                      then=lambda r: self.on_title(tab, r))
        elif name == "pointer" and self.hover:
            x, y = self.hover
            self.send("Runtime.evaluate", {"expression": POINTER_AT % (x, y, x, y), "returnByValue": True},
                      then=self.on_pointer)

    def run(self):
        """Runs until ctrl+q, the last tab closing, the terminal going away, or the browser
        dying. Exit status."""
        try:
            self.start()
        except BaseException:
            self.stop()
            raise
        wake_r, wake_w = os.pipe()
        os.set_blocking(wake_w, False)
        signal.set_wakeup_fd(wake_w)
        old = {sig: signal.signal(sig, self.on_signal) for sig in (signal.SIGWINCH, signal.SIGTERM, signal.SIGHUP)}
        sel = selectors.DefaultSelector()
        for fd, what in ((self.in_fd, "tty"), (self.cdp.read_fd, "cdp"), (wake_r, "wake")):
            sel.register(fd, selectors.EVENT_READ, what)
        try:
            with term.Screen(self.in_fd, self.out_fd):
                self.draw_bar()
                while not self.done:
                    self.handle(self.cdp.take())
                    now = time.monotonic()
                    wait = 0.05 if self.parser.pending else 0.5
                    if self.timers:
                        wait = min(wait, max(0.0, min(self.timers.values()) - now))
                    ready = sel.select(wait)
                    for key, _ in ready:
                        if key.data == "tty":
                            data = os.read(self.in_fd, 65536)
                            if not data:
                                self.done = True
                            self.input(self.parser.feed(data))
                        elif key.data == "cdp":
                            self.handle(self.cdp.read())
                        else:
                            os.read(wake_r, 4096)
                            self.resize()
                    if not ready and self.parser.pending:
                        self.input(self.parser.flush())
                    now = time.monotonic()
                    for name in [n for n, due in self.timers.items() if due <= now]:
                        del self.timers[name]
                        self.fire(name)
                    self.draw_bar()
        except Closed:
            self.error = "the browser went away" + (f" (its messages: {self.log_path})" if self.log_path else "")
        finally:
            signal.set_wakeup_fd(-1)
            for sig, handler in old.items():
                signal.signal(sig, handler)
            self.stop()
        return 1 if self.error else 0

    def on_signal(self, sig, frame):
        if sig != signal.SIGWINCH:
            self.done = True

    def resize(self):
        new = term.size(self.out_fd)
        if new == self.size or not new.xpix or not new.rows:
            return
        self.size, self.shown_id, self.bar_drawn = new, 0, None
        term.write_all(self.out_fd, term.CLEAR)   # takes the old frame with it
        self.apply_size()

    # --- from the browser ---------------------------------------------------------

    def handle(self, messages):
        frame = None
        for msg in messages:
            if "id" in msg:
                then = self.replies.pop(msg["id"], None)
                if then and "result" in msg:
                    then(msg["result"])
                continue
            method, p = msg.get("method"), msg.get("params", {})
            if method == "Target.targetCreated":
                self.on_created(p["targetInfo"])
            elif method == "Target.targetInfoChanged":
                tab = self.by("target", p["targetInfo"].get("targetId"))
                if tab:
                    tab.url = p["targetInfo"].get("url", tab.url)
            elif method == "Target.targetDestroyed":
                self.drop(self.by("target", p.get("targetId")))
            elif method == "Browser.downloadWillBegin":
                self.notice = f"download refused: {p.get('suggestedFilename', '')} (browzer does not download yet)"
            tab = self.by("session", msg.get("sessionId"))
            if not tab:
                continue
            if method == "Page.screencastFrame":
                self.send("Page.screencastFrameAck", {"sessionId": p["sessionId"]}, tab=tab)
                if tab is self.tab:
                    frame = p["data"]
            elif method == "Page.frameStartedLoading" and p.get("frameId") == tab.target:
                tab.loading = True
            elif method == "Page.frameStoppedLoading" and p.get("frameId") == tab.target:
                tab.loading = False
                self.after("title", 0)
            elif method == "Page.frameNavigated" and "parentId" not in p["frame"]:
                tab.url, tab.title = p["frame"].get("url", tab.url), ""
            elif method == "Page.navigatedWithinDocument" and p.get("frameId") == tab.target:
                tab.url = p.get("url", tab.url)
            elif method == "Page.domContentEventFired":
                self.after("title", 0)
            elif method == "Page.javascriptDialogOpening":
                self.on_dialog(tab, p)
            elif method == "Page.javascriptDialogClosed":
                if isinstance(self.mode, Dialog) and self.mode.tab is tab:
                    self.mode = None
            elif method == "Inspector.targetCrashed":
                self.notice = "the page crashed; ctrl+r reloads it"
        if frame is not None:
            self.draw(frame)
            self.after("title", 0.25)

    def by(self, what, value):
        return next((t for t in self.tabs if getattr(t, what) == value), None) if value else None

    def on_created(self, info):
        """A page that one of our tabs opened (a link to a new window, window.open) is a new tab."""
        if info.get("type") == "page" and self.by("target", info.get("openerId")) and not self.by("target", info["targetId"]):
            try:
                self.show(self.adopt(info["targetId"]))
            except (ProtocolError, TimeoutError):
                pass   # gone again before we could attach

    def on_title(self, tab, result):
        """The browser announces no title changes here, so the page is asked: when it has
        loaded, and shortly after it repaints or takes input."""
        value = result.get("result", {}).get("value")
        if isinstance(value, str):
            tab.title = value

    def on_pointer(self, result):
        shape = str(result.get("result", {}).get("value", "default"))
        self.set_pointer(shape if re.fullmatch(r"[a-z-]+", shape) else "default")

    def set_pointer(self, shape):
        if shape != self.pointer:
            self.pointer = shape
            term.write_all(self.out_fd, b"\x1b]22;%s\x1b\\" % shape.encode())

    def on_dialog(self, tab, p):
        kind = p.get("type", "alert")
        if kind == "beforeunload":   # "leave this page?": yes, the key that led here was the answer
            self.send("Page.handleJavaScriptDialog", {"accept": True}, tab=tab)
            return
        self.show(tab)
        self.mode = Dialog(tab, kind, p.get("message", ""), p.get("defaultPrompt", ""))

    def answer(self, accept):
        dialog, self.mode = self.mode, None
        params = {"accept": accept}
        if dialog.edit and accept:
            params["promptText"] = dialog.edit.text
        self.send("Page.handleJavaScriptDialog", params, tab=dialog.tab)

    # --- drawing ------------------------------------------------------------------

    def draw(self, png_b64):
        began = time.monotonic()
        cols, rows, w, h = self.view()
        if began - self.sized_at < 1.0:
            # a frame rendered before the browser took the new size would be stretched into the box
            head = base64.b64decode(png_b64[:32])
            if kitty.png_size(head) != (w, h):
                return
        self.image_id = self.image_id % 4_000_000 + 1
        out = [term.SYNC_BEGIN, term.move(BAR_ROWS + 1, 1)]
        if self.transfer == "file":
            self.serial += 1
            out.append(kitty.show_file(base64.b64decode(png_b64), kitty.spool_path(self.serial), self.image_id, cols, rows))
        else:
            out.append(kitty.show_inline(png_b64, self.image_id, cols, rows))
        if self.shown_id:
            out.append(kitty.delete(self.shown_id))
        out.append(term.SYNC_END)
        term.write_all(self.out_fd, b"".join(out))
        self.shown_id = self.image_id
        now = time.monotonic()
        self.frames = [t for t in self.frames if now - t < 1.0] + [now]
        self.frame_bytes, self.draw_ms = len(png_b64) * 3 // 4, (now - began) * 1000

    def bar(self):
        """The bar's row as terminal bytes: the page's line, the address being typed, or a
        dialog's question."""
        cols = self.size.cols
        if isinstance(self.mode, LineEdit):
            return b"\x1b[0m" + self.editing(" address › ", self.mode, cols)
        if isinstance(self.mode, Dialog):
            d = self.mode
            if d.edit:
                lead = fit(f" {d.message} › ", min(len(d.message) + 4, cols * 2 // 3))
                return b"\x1b[0;1m" + self.editing(lead, d.edit, cols)
            keys = " [enter] " if d.kind == "alert" else " [y / n] "
            return b"\x1b[0;1m" + (fit(f" {d.kind}: {d.message}", cols - len(keys)) + keys)[:cols].encode() + b"\x1b[0m"
        tab = self.tab
        count = f" {self.tabs.index(tab) + 1}/{len(self.tabs)}" if len(self.tabs) > 1 else ""
        left = f"{count} {'◌' if tab.loading else ' '} {tab.title or tab.url}"
        if tab.title and tab.url:
            left += f"  ·  {tab.url}"
        if self.notice:
            left = f" {self.notice}"
        right = HINT
        if self.stats:
            now = time.monotonic()
            fps = sum(1 for t in self.frames if now - t < 1.0)
            right = f" {fps:2d} fps · {self.frame_bytes // 1024} KB · {self.draw_ms:.1f} ms · {self.transfer} ·{right}"
        right = right if len(right) < cols else ""
        return b"\x1b[0;7m" + (fit(left, cols - len(right)) + right).encode() + b"\x1b[0m"

    @staticmethod
    def editing(lead, edit, cols):
        """A line editor after its label, the cursor drawn as a reversed cell (the terminal's
        own cursor stays hidden: frames move it)."""
        shown, at = edit.window(max(1, cols - len(lead) - 1))
        shown = shown.ljust(at + 1)
        if edit.fresh:   # as if selected: typing replaces it
            body = b"\x1b[7m" + shown.rstrip().encode() + b"\x1b[27m"
        else:
            body = shown[:at].encode() + b"\x1b[7m" + shown[at].encode() + b"\x1b[27m" + shown[at + 1:].encode()
        return lead.encode() + body + b"\x1b[0m\x1b[K"

    def draw_bar(self):
        if not self.tab:
            return
        row = self.bar()
        if row != self.bar_drawn:
            self.bar_drawn = row
            term.write_all(self.out_fd, term.move(1, 1) + row)

    # --- from the terminal --------------------------------------------------------

    def input(self, events):
        for ev in events:
            if isinstance(ev, Mouse):
                self.on_mouse(ev)
                continue
            self.flush_pointer()
            if isinstance(ev, Key):
                self.on_key(ev)
            elif isinstance(ev, Paste):
                edit = self.mode if isinstance(self.mode, LineEdit) else getattr(self.mode, "edit", None)
                if edit:
                    edit.insert(ev.text)
                elif not self.mode:
                    self.send("Input.insertText", {"text": ev.text})
            elif isinstance(ev, Focus):
                pass
        self.flush_pointer()
        if events:
            self.after("title", 0.25)

    def on_key(self, key):
        self.notice = ""
        name, mods = key.name, key.mods
        if mods == CTRL and name == "q":
            self.done = True
        elif isinstance(self.mode, Dialog):
            self.dialog_key(key)
        elif isinstance(self.mode, LineEdit):
            did = self.mode.key(key)
            if did:
                text, self.mode = self.mode.text, None
                if did == "submit" and text.strip():
                    self.send("Page.navigate", {"url": normalize(text, self.search, bare_file=False)})
        elif mods == CTRL and name == "l":
            self.mode = LineEdit(self.tab.url if self.tab.url != "about:blank" else "", fresh=True)
        elif mods == CTRL and name == "t":
            self.new_tab()
        elif mods == CTRL and name == "w":
            self.close_tab(self.tab)
        elif mods == CTRL and name in ("PageUp", "PageDown"):
            self.step_tab(-1 if name == "PageUp" else 1)
        elif mods == ALT and name.isdigit() and name != "0":
            if int(name) <= len(self.tabs):
                self.show(self.tabs[int(name) - 1])
        elif mods == CTRL and name == "r" or (not mods and name == "F5"):
            self.send("Page.reload")
        elif mods == ALT and name in ("ArrowLeft", "ArrowRight"):
            step = -1 if name == "ArrowLeft" else 1
            self.send("Page.getNavigationHistory", then=lambda r: self.go(r, step))
        else:
            if mods & CTRL and not mods & ALT and name.lower() in ("c", "x") or (mods == CTRL and name == "Insert"):
                self.send("Runtime.evaluate", {"expression": SELECTION, "returnByValue": True}, then=self.copy)
            for params in cdp_key_events(key):
                self.send("Input.dispatchKeyEvent", params)

    def dialog_key(self, key):
        d = self.mode
        if d.edit:
            did = d.edit.key(key)
            if did:
                self.answer(did == "submit")
        elif key.name in ("Enter", "y", "Y") or (d.kind == "alert" and key.name in ("Escape", " ")):
            self.answer(True)
        elif key.name in ("Escape", "n", "N"):
            self.answer(False)

    def copy(self, result):
        """The page's selection onto the terminal's clipboard (OSC 52: it reaches the machine
        the terminal is on, across ssh too)."""
        text = result.get("result", {}).get("value")
        if isinstance(text, str) and text:
            term.write_all(self.out_fd, b"\x1b]52;c;" + base64.b64encode(text.encode()) + b"\x1b\\")
            self.notice = f"copied {len(text)} character{'s' if len(text) != 1 else ''}"

    def go(self, history, step):
        at = history["currentIndex"] + step
        if 0 <= at < len(history["entries"]):
            self.send("Page.navigateToHistoryEntry", {"entryId": history["entries"][at]["id"]})

    def point(self, ev):
        """An event's position in the page's CSS pixels; y is negative on the bar."""
        return ev.x / self.scale, (ev.y - BAR_ROWS * self.size.cell_h) / self.scale

    def on_mouse(self, ev):
        x, y = self.point(ev)
        if isinstance(self.mode, Dialog):
            return   # the page waits for the dialog's answer
        if y < 0:
            self.hover = None
            self.set_pointer("text")
            if ev.kind == "press" and ev.button == 0 and not self.mode:
                self.flush_pointer()
                self.mode = LineEdit(self.tab.url if self.tab.url != "about:blank" else "", fresh=True)
            if ev.kind != "release":
                return
        if ev.kind == "wheel":
            dx, dy = (self.wheel[2] + ev.dx, self.wheel[3] + ev.dy) if self.wheel else (ev.dx, ev.dy)
            self.wheel = (x, y, dx, dy, ev.mods)
        elif ev.kind == "move":
            self.move = (x, y, ev.mods)
        elif ev.button >= 0:
            self.flush_pointer()
            bit = BUTTON_BITS[ev.button]
            if ev.kind == "press":
                if isinstance(self.mode, LineEdit):
                    self.mode = None   # a click on the page leaves the address as it was
                when, px, py, button, count = self.last_press
                near = abs(ev.x - px) <= DOUBLE_CLICK[1] and abs(ev.y - py) <= DOUBLE_CLICK[1]
                again = button == ev.button and near and time.monotonic() - when < DOUBLE_CLICK[0]
                count = count % 3 + 1 if again else 1
                self.last_press = (time.monotonic(), ev.x, ev.y, ev.button, count)
                self.held |= bit
            else:
                if not self.held & bit:
                    return   # its press was on the bar
                count = self.last_press[4]
                self.held &= ~bit
            self.send("Input.dispatchMouseEvent", {
                "type": "mousePressed" if ev.kind == "press" else "mouseReleased", "x": x, "y": max(0.0, y),
                "button": BUTTONS[ev.button], "buttons": self.held, "clickCount": count, "modifiers": cdp_mods(ev.mods)})

    def flush_pointer(self):
        if self.move:
            x, y, mods = self.move
            self.move = None
            self.send("Input.dispatchMouseEvent",
                      {"type": "mouseMoved", "x": x, "y": y, "buttons": self.held, "modifiers": cdp_mods(mods)})
            self.hover = (x, y)
            self.after("pointer", 0.08)
        if self.wheel:
            x, y, dx, dy, mods = self.wheel
            self.wheel = None
            self.send("Input.dispatchMouseEvent", {"type": "mouseWheel", "x": x, "y": y, "deltaX": dx * WHEEL_STEP,
                                                   "deltaY": dy * WHEEL_STEP, "modifiers": cdp_mods(mods)})
            self.after("pointer", 0.08)
