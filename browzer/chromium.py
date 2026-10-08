"""Finding and starting the system's Chromium, without a window, with the debugging pipe."""
import fcntl
import os
import shutil
import signal
import sys
import time

from .cdp import Pipe

CANDIDATES = ("chromium", "chromium-browser", "google-chrome-stable", "google-chrome", "chrome")
SCREENS = ((1920, 1080), (2560, 1440), (3840, 2160))
# a person with a mouse is at the terminal. Without a display the browser reports no pointer at
# all, and pages lay out for touch (blink's HoverType: hover = 2, PointerType: fine = 4); with
# the debugging pipe it reports navigator.webdriver, and sites refuse it as a robot
PERSON = ("--blink-settings=primaryHoverType=2,availableHoverTypes=2,primaryPointerType=4,availablePointerTypes=4",
          "--disable-blink-features=AutomationControlled")


class NotFound(Exception):
    pass


def find(explicit=None):
    """The browser binary: the one asked for, $BROWZER_CHROMIUM, or the first candidate on PATH."""
    for name in (explicit, os.environ.get("BROWZER_CHROMIUM")):
        if name:
            path = shutil.which(name)
            if not path:
                raise NotFound(f"no browser at '{name}'")
            return path
    for name in CANDIDATES:
        path = shutil.which(name)
        if path:
            return path
    raise NotFound("no Chromium found on PATH (tried " + ", ".join(CANDIDATES) + "); use --chromium PATH")


class Process:
    """The browser process, started with posix_spawn (no fork of this interpreter)."""

    def __init__(self, pid):
        self.pid, self.returncode = pid, None

    def poll(self):
        if self.returncode is None:
            pid, status = os.waitpid(self.pid, os.WNOHANG)
            if pid:
                self.returncode = os.waitstatus_to_exitcode(status)
        return self.returncode

    def wait(self, timeout):
        """The exit code, or None when it still runs after `timeout` seconds."""
        end = time.monotonic() + timeout
        while self.poll() is None and time.monotonic() < end:
            time.sleep(0.02)
        return self.returncode

    def kill(self):
        if self.poll() is None:
            try:
                os.killpg(self.pid, signal.SIGKILL)   # its own session: the group is the browser's
            except ProcessLookupError:
                pass
            self.wait(2)


def _high(fd):
    """The same pipe end on a descriptor clear of 3 and 4, which the browser gets."""
    new = fcntl.fcntl(fd, fcntl.F_DUPFD_CLOEXEC, 10)
    os.close(fd)
    return new


def windowless(width, height):
    """The flags for a browser nobody sees, on a screen its width x height page fits on.

    Where Chromium has a display backend that draws nowhere (Linux), it runs as the regular
    browser on that: headless mode proper calls itself HeadlessChrome, has no GPU and a
    800 x 600 screen, and sites take it for a robot. Its dialogs have no window to be in:
    browzer shows the print preview as a tab and answers for the file chooser."""
    screen = next((s for s in SCREENS if s[0] >= width and s[1] >= height), (width, height))
    if sys.platform.startswith("linux"):
        # WebGL without a GPU is the software renderer, as headless mode has it: the regular
        # browser has to be told
        return ["--ozone-platform=headless", "--ozone-override-screen-size=%d,%d" % screen,
                "--enable-unsafe-swiftshader", "--hide-crash-restore-bubble", "--disable-search-engine-choice-screen"]
    return ["--headless=new", "--screen-info={%dx%d}" % screen]


def launch(binary, profile, width, height, log_path=None, extra=()):
    """Starts the browser in its own session (the terminal's signals do not reach it); it
    exits by itself when our end of the pipe closes. Returns (Process, Pipe)."""
    to_browser_r, to_browser_w = map(_high, os.pipe())
    from_browser_r, from_browser_w = map(_high, os.pipe())
    argv = [binary, *windowless(width, height), *PERSON, "--remote-debugging-pipe", f"--user-data-dir={profile}",
            "--no-first-run", "--no-default-browser-check", f"--window-size={width},{height}",
            *extra, "about:blank"]
    actions = [
        (os.POSIX_SPAWN_OPEN, 0, os.devnull, os.O_RDONLY, 0),
        (os.POSIX_SPAWN_OPEN, 1, os.devnull, os.O_WRONLY, 0),
        (os.POSIX_SPAWN_OPEN, 2, log_path or os.devnull, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600),
        (os.POSIX_SPAWN_DUP2, to_browser_r, 3),   # every other descriptor of ours is close-on-exec
        (os.POSIX_SPAWN_DUP2, from_browser_w, 4),
    ]
    try:
        pid = os.posix_spawn(binary, argv, os.environ, file_actions=actions, setsid=True)
    finally:
        os.close(to_browser_r)
        os.close(from_browser_w)
    return Process(pid), Pipe(to_browser_w, from_browser_r)
