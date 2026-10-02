"""The terminal: its size in cells and pixels, and the modes browzer runs it in."""
import fcntl
import os
import struct
import termios
import tty
from typing import NamedTuple

SYNC_BEGIN = b"\x1b[?2026h"   # synchronized update: the terminal paints once, at SYNC_END
SYNC_END = b"\x1b[?2026l"
CLEAR = b"\x1b[2J"

# alternate screen, no cursor, mouse (any motion, SGR, in pixels), kitty keyboard protocol
# flags 1 + 4 (ctrl/alt chords and Esc arrive unambiguous, with the key's letter in the base
# layout, so ctrl+q is ctrl+q in any layout), bracketed paste, focus events
ENTER = b"\x1b[?1049h\x1b[?25l\x1b[?1003h\x1b[?1006h\x1b[?1016h\x1b[>5u\x1b[?2004h\x1b[?1004h"
LEAVE = b"\x1b[?1004l\x1b[?2004l\x1b[<u\x1b[?1016l\x1b[?1006l\x1b[?1003l\x1b_Ga=d,d=A,q=2\x1b\\\x1b[?25h\x1b[?1049l"


class Size(NamedTuple):
    cols: int
    rows: int
    xpix: int
    ypix: int

    @property
    def cell_w(self):
        return self.xpix // self.cols if self.cols else 0

    @property
    def cell_h(self):
        return self.ypix // self.rows if self.rows else 0


def size(fd):
    rows, cols, xpix, ypix = struct.unpack("HHHH", fcntl.ioctl(fd, termios.TIOCGWINSZ, b"\0" * 8))
    return Size(cols, rows, xpix, ypix)


def move(row, col):
    return b"\x1b[%d;%dH" % (row, col)


def write_all(fd, data):
    view = memoryview(data)
    while view:
        view = view[os.write(fd, view):]


class Screen:
    """Raw mode and browzer's terminal modes for the length of a `with` block."""

    def __init__(self, in_fd, out_fd):
        self.in_fd, self.out_fd, self._saved = in_fd, out_fd, None

    def __enter__(self):
        self._saved = termios.tcgetattr(self.in_fd)
        tty.setraw(self.in_fd)
        write_all(self.out_fd, ENTER + CLEAR)
        return self

    def __exit__(self, *exc):
        try:
            write_all(self.out_fd, LEAVE)
        finally:
            termios.tcsetattr(self.in_fd, termios.TCSADRAIN, self._saved)
