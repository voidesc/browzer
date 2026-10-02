"""browzer end to end in a pseudo-terminal: the real program, the real browser, no window.

The pty stands in for the terminal: it reports a size in cells and pixels, the test types
and clicks by writing the bytes a terminal would send, and reads what browzer draws.
With 80 x 24 cells of 10 x 20 px the bar is the top 20 px, so a point of the page is 20 px
further down on the terminal.
"""
import base64
import fcntl
import os
import pty
import select
import shutil
import signal
import struct
import subprocess
import sys
import tempfile
import termios
import time
import unittest

from browzer import chromium, kitty, term
from tests.test_kitty import images

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
BOX = "position:fixed;left:20px;width:200px;height:30px"
PAGE = f"""<title>browzer test</title><body style="margin:0;height:5000px">
<button style="{BOX};top:20px" onclick="document.title='clicked'">press</button>
<input style="{BOX};top:100px" oninput="document.title='typed '+this.value">
<a style="{BOX};top:160px;display:block" target=_blank href="second.html">new window</a>
<button style="{BOX};top:220px" onclick="document.title='confirmed '+confirm('sure?')">ask</button>
<button style="{BOX};top:280px" onclick="document.title='prompted '+prompt('name?','bob')">prompt</button>
<script>addEventListener('scroll',()=>{{document.title='scrolled '+(scrollY>0)}})</script>"""
# where those are on the terminal
BUTTON, INPUT, LINK, ASK, PROMPT = (100, 55), (100, 135), (100, 195), (100, 255), (100, 315)

CTRL_Q, CTRL_L, CTRL_T, CTRL_W = b"\x1b[113;5u", b"\x1b[108;5u", b"\x1b[116;5u", b"\x1b[119;5u"
CTRL_A, CTRL_C, CTRL_PGUP, ALT_LEFT = b"\x1b[97;5u", b"\x1b[99;5u", b"\x1b[5;5~", b"\x1b[1;3D"
BAR = b"\x1b[0;7m"   # how the page's line in the bar starts

try:
    chromium.find()
    HAVE_BROWSER = True
except chromium.NotFound:
    HAVE_BROWSER = False

SITE = None


def setUpModule():
    global SITE
    SITE = tempfile.mkdtemp(prefix="browzer-test-site-")
    with open(os.path.join(SITE, "first.html"), "w") as fh:
        fh.write(PAGE)
    with open(os.path.join(SITE, "second.html"), "w") as fh:
        fh.write("<title>second page</title>second")


def tearDownModule():
    shutil.rmtree(SITE, ignore_errors=True)


def click(at):
    return b"\x1b[<0;%d;%dM\x1b[<0;%d;%dm" % (*at, *at)


class Session:
    def __init__(self, *args, cols=80, rows=24, xpix=800, ypix=480):
        self.master, self.slave = pty.openpty()
        self.winsize(cols, rows, xpix, ypix)
        env = {k: v for k, v in os.environ.items() if k not in ("SSH_CONNECTION", "SSH_TTY", "TMUX")}
        argv = [sys.executable, os.path.join(ROOT, "bin", "browzer"), "--temp-profile", *args, os.path.join(SITE, "first.html")]
        self.proc = subprocess.Popen(argv, stdin=self.slave, stdout=self.slave, stderr=subprocess.PIPE, env=env)
        self.out = b""

    def winsize(self, cols, rows, xpix, ypix):
        fcntl.ioctl(self.slave, termios.TIOCSWINSZ, struct.pack("HHHH", rows, cols, xpix, ypix))

    def type(self, data):
        os.write(self.master, data)

    def until(self, done, what, timeout=20):
        """Reads browzer's output until done(output so far) is true."""
        if isinstance(done, bytes):
            want, done = done, lambda o: want in o
        end = time.monotonic() + timeout
        while not done(self.out):
            left = end - time.monotonic()
            if left <= 0 or self.proc.poll() is not None:
                err = self.proc.stderr.read().decode() if self.proc.poll() is not None else ""
                bars = [b[:70] for b in self.out.split(b"\x1b[1;1H")[1:]][-4:]
                raise AssertionError(f"never saw {what}; exit={self.proc.poll()} stderr={err!r} last bars={bars!r}")
            if select.select([self.master], [], [], min(left, 0.2))[0]:
                self.out += os.read(self.master, 1 << 20)
        return self.out

    def then(self, data, done, what):
        """Types `data`, then waits for `done` in what browzer draws after it."""
        self.out = b""
        self.type(data)
        return self.until(done, what)

    def close(self):
        if self.proc.poll() is None:
            self.proc.kill()
        self.proc.wait()
        self.proc.stderr.close()
        os.close(self.master)
        os.close(self.slave)


def frame_sizes(out):
    return [kitty.png_size(base64.b64decode(payload)) for keys, payload in images(out) if b"t=t" not in keys]


@unittest.skipUnless(HAVE_BROWSER, "no Chromium on PATH")
class BrowserTest(unittest.TestCase):
    def open(self, *args, **size):
        s = Session("--transfer", "inline", *args, **size)
        self.addCleanup(s.close)
        s.until(lambda o: b"browzer test" in o and frame_sizes(o), "the page's first frame")
        return s

    def test_draws_takes_input_resizes_and_quits(self):
        s = self.open()
        out = s.out
        self.assertTrue(out.startswith(term.ENTER), "the terminal modes come first")
        self.assertTrue(b"^Q quit" in out, "the bar says how to leave")
        # one bar row of 20 px, so the page gets 80 x 23 cells = 800 x 460 px
        self.assertEqual(set(frame_sizes(out)), {(800, 460)})
        self.assertTrue(b"c=80,r=23" in images(out)[0][0])

        s.then(b"\x1b[<65;400;300M", b"scrolled true", "the page scrolling")   # a wheel notch down
        s.then(click(BUTTON), b"clicked", "the click reaching the button")
        s.then(click(INPUT) + b"hi", b"typed hi", "typing into the field")

        s.out = b""
        s.winsize(100, 30, 1000, 600)
        s.proc.send_signal(signal.SIGWINCH)
        s.until(lambda o: (1000, 580) in frame_sizes(o), "a frame at the new size")

        s.then(CTRL_Q, lambda o: o.endswith(term.LEAVE), "the terminal restored")
        self.assertEqual(s.proc.wait(10), 0)

    def test_address_bar_and_history(self):
        s = self.open()
        s.then(CTRL_L, b" address \xe2\x80\xba ", "the address editor")
        s.then(os.path.join(SITE, "second.html").encode() + b"\r", BAR + b"   second page", "the typed address loading")
        s.then(ALT_LEFT, BAR + b"   browzer test", "alt+left going back")
        s.then(b"\x1b[<0;300;10M\x1b[<0;300;10m", b" address \xe2\x80\xba ", "a click on the bar editing the address")
        s.then(b"\x1b[27u", BAR, "escape leaving the address as it was")
        self.assertTrue(b"browzer test" in s.out)

    def test_tabs(self):
        s = self.open()
        s.then(click(LINK), BAR + b" 2/2   second page", "a new-window link opening as a second tab")
        s.then(CTRL_PGUP, BAR + b" 1/2   browzer test", "ctrl+pageup showing the first tab")
        s.then(b"\x1b[50;3u", BAR + b" 2/2   second page", "alt+2 showing the second")
        s.then(CTRL_W, BAR + b"   browzer test", "ctrl+w closing it")
        s.then(CTRL_T, b" address \xe2\x80\xba ", "ctrl+t asking where to go")
        s.then(os.path.join(SITE, "second.html").encode() + b"\r", BAR + b" 2/2   second page", "the new tab loading")
        s.type(CTRL_W + CTRL_W)
        self.assertEqual(s.proc.wait(10), 0, "closing the last tab ends browzer")

    def test_dialogs(self):
        s = self.open()
        s.then(click(ASK), b" confirm: sure?", "the confirm in the bar")
        s.then(b"n", b"confirmed false", "n answering no")
        s.then(click(ASK), b" confirm: sure?", "the confirm again")
        s.then(b"\r", b"confirmed true", "enter answering yes")
        s.then(click(PROMPT), b" name? \xe2\x80\xba ", "the prompt in the bar")
        self.assertTrue(b"bob" in s.out, "with its default")
        s.then(b"zed\r", b"prompted zed", "typing replacing the default")
        s.then(click(PROMPT), b" name? \xe2\x80\xba ", "the prompt again")
        s.then(b"\x1b[27u", b"prompted null", "escape dismissing it")

    def test_copy_and_pointer(self):
        s = self.open()
        s.then(click(INPUT) + b"hello", b"typed hello", "typing")
        s.then(CTRL_A + CTRL_C, b"\x1b]52;c;" + base64.b64encode(b"hello") + b"\x1b\\", "the selection on the clipboard")
        s.then(b"\x1b[<35;%d;%dM" % LINK, b"\x1b]22;pointer\x1b\\", "the hand over a link")
        s.then(b"\x1b[<35;600;400M", b"\x1b]22;default\x1b\\", "the arrow over nothing")

    def test_file_transfer_spools_and_sweeps(self):
        s = Session("--transfer", "file")
        self.addCleanup(s.close)
        out = s.until(lambda o: any(b"t=t" in keys for keys, _ in images(o)), "a frame sent as a file")
        keys, payload = next(i for i in images(out) if b"t=t" in i[0])
        path = base64.b64decode(payload).decode()
        with open(path, "rb") as fh:   # no terminal here to delete it, so it is still there
            self.assertEqual(kitty.png_size(fh.read()), (800, 460))
        s.type(CTRL_Q)
        self.assertEqual(s.proc.wait(10), 0)
        self.assertFalse(os.path.exists(path), "leftover frames are swept on exit")

    def test_sigterm_restores_the_terminal(self):
        s = self.open()
        s.proc.terminate()
        s.until(lambda o: o.endswith(term.LEAVE), "the terminal restored")
        self.assertEqual(s.proc.wait(10), 0)


class StartupTest(unittest.TestCase):
    def test_refuses_a_terminal_without_pixels(self):
        if not HAVE_BROWSER:
            self.skipTest("no Chromium on PATH")
        s = Session(xpix=0, ypix=0)
        self.addCleanup(s.close)
        self.assertEqual(s.proc.wait(10), 1)
        self.assertIn(b"does not report its size in pixels", s.proc.stderr.read())

    def test_refuses_a_pipe(self):
        run = subprocess.run([sys.executable, os.path.join(ROOT, "bin", "browzer")], stdin=subprocess.DEVNULL,
                             capture_output=True)
        self.assertEqual(run.returncode, 2)

    def test_missing_browser_is_said(self):
        self.assertIsNone(shutil.which("no-such-browser"))
        with self.assertRaises(chromium.NotFound):
            chromium.find("no-such-browser")


if __name__ == "__main__":
    unittest.main()
