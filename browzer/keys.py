"""Terminal input: bytes from the tty into key, mouse, paste and focus events, and key
events into what the browser's Input.dispatchKeyEvent takes.

Keyboard: the kitty keyboard protocol, flags 1 + 4 (ctrl / alt chords and Esc arrive as
`CSI code[:shifted[:base]] ; mods u`, text as text), with the legacy encodings as a fallback.
Mouse: SGR reports with pixel coordinates (modes 1006 + 1016).
"""
import codecs
import re
from dataclasses import dataclass

SHIFT, ALT, CTRL, SUPER = 1, 2, 4, 8


@dataclass(frozen=True)
class Key:
    name: str      # one character, or a DOM key name: Enter, ArrowLeft, F5 ...
    mods: int = 0


@dataclass(frozen=True)
class Mouse:
    kind: str      # press, release, move, wheel
    x: int         # pixels from the terminal's top left
    y: int
    button: int = -1   # 0 left, 1 middle, 2 right; -1 none
    mods: int = 0
    dx: int = 0    # wheel notches: right and down are positive
    dy: int = 0


@dataclass(frozen=True)
class Paste:
    text: str


@dataclass(frozen=True)
class Focus:
    gained: bool


_CSI = re.compile(rb"\x1b\[([<>=?]?)([0-9;:]*)([\x40-\x7e])")
_CSI_PARTIAL = re.compile(rb"\x1b\[[<>=?]?[0-9;:]*\Z")
_PASTE_END = b"\x1b[201~"
_LETTER = {"A": "ArrowUp", "B": "ArrowDown", "C": "ArrowRight", "D": "ArrowLeft", "H": "Home", "F": "End",
           "P": "F1", "Q": "F2", "R": "F3", "S": "F4"}
_TILDE = {2: "Insert", 3: "Delete", 5: "PageUp", 6: "PageDown", 7: "Home", 8: "End", 11: "F1", 12: "F2",
          13: "F3", 14: "F4", 15: "F5", 17: "F6", 18: "F7", 19: "F8", 20: "F9", 21: "F10", 23: "F11", 24: "F12"}
_CODEPOINT = {27: "Escape", 13: "Enter", 9: "Tab", 127: "Backspace", 8: "Backspace"}
_CONTROL = {"\r": "Enter", "\n": "Enter", "\t": "Tab", "\x7f": "Backspace", "\x08": "Backspace"}


def _mods(field):
    """The modifier bits of a CSI parameter `mods[:event]` (the protocol sends them plus one)."""
    head = field.split(":")[0]
    return max(0, int(head) - 1) if head.isdigit() else 0


class Parser:
    """Feed it what the tty delivered; it returns the events that are complete."""

    def __init__(self):
        self._buf = b""
        self._text = codecs.getincrementaldecoder("utf-8")("replace")
        self._pasting = False

    @property
    def pending(self):
        """True while bytes wait for the rest of their sequence (a lone ESC, say)."""
        return bool(self._buf)

    def feed(self, data):
        self._buf += data
        return self._drain(final=False)

    def flush(self):
        """What is left is all there will be: a lone ESC is the Escape key."""
        return self._drain(final=True)

    def _drain(self, final):
        out, buf = [], self._buf
        while buf:
            if self._pasting:
                end = buf.find(_PASTE_END)
                if end < 0:
                    break
                out.append(Paste(buf[:end].decode("utf-8", "replace")))
                buf, self._pasting = buf[end + len(_PASTE_END):], False
            elif buf[0] != 0x1b:
                end = buf.find(b"\x1b")
                run, buf = (buf, b"") if end < 0 else (buf[:end], buf[end:])
                out.extend(self._chars(self._text.decode(run)))
            elif buf.startswith(b"\x1b["):
                m = _CSI.match(buf)
                if m:
                    buf = buf[m.end():]
                    out.extend(self._csi(m[1].decode(), m[2].decode(), chr(m[3][0])))
                elif _CSI_PARTIAL.match(buf) and not final:
                    break
                else:
                    buf = buf[2:]
            elif buf.startswith(b"\x1bO") and len(buf) >= 3:
                name = _LETTER.get(chr(buf[2]))
                buf = buf[3:]
                if name:
                    out.append(Key(name))
            elif (len(buf) == 1 or buf == b"\x1bO") and not final:
                break
            elif len(buf) == 1 or buf[1] == 0x1b:
                out.append(Key("Escape"))
                buf = buf[1:]
            else:   # ESC then a character: alt + that character, the legacy way
                lead = buf[1]
                width = 1 if lead < 0x80 else 2 if lead < 0xe0 else 3 if lead < 0xf0 else 4
                if len(buf) < 1 + width and not final:
                    break
                one, buf = buf[1:1 + width], buf[1 + width:]
                out.extend(Key(k.name, k.mods | ALT) for k in self._chars(one.decode("utf-8", "replace")))
        self._buf = buf
        return out

    @staticmethod
    def _chars(text):
        for ch in text:
            if ch in _CONTROL:
                yield Key(_CONTROL[ch])
            elif ch == "\x00":
                yield Key(" ", CTRL)
            elif ch < " ":   # legacy ctrl + letter
                yield Key(chr(ord(ch) + 96), CTRL)
            else:
                yield Key(ch)

    def _csi(self, lead, params, final):
        fields = params.split(";") if params else []
        if lead == "<" and final in "Mm":
            if len(fields) == 3 and all(f.isdigit() for f in fields):
                yield self._mouse(int(fields[0]), int(fields[1]), int(fields[2]), final == "M")
        elif lead:
            return   # an answer to a query, not input
        elif final == "u":
            codes = fields[0].split(":") if fields else [""]   # code[:shifted[:base layout]]
            if not codes[0].isdigit():
                return
            if len(fields) > 1 and fields[1].endswith(":3"):
                return   # a key release
            mods = _mods(fields[1]) if len(fields) > 1 else 0
            code = int(codes[0])
            if mods & (CTRL | ALT | SUPER) and len(codes) > 2 and codes[2].isdigit():
                code = int(codes[2])   # a chord is the key, not the letter the layout puts on it
            name = _CODEPOINT.get(code) or (chr(code) if 32 <= code < 57344 or code > 63743 else None)
            if name:
                yield Key(name, mods)
        elif final == "~":
            num = int(fields[0]) if fields and fields[0].isdigit() else 0
            if num == 200:
                self._pasting = True
            elif num in _TILDE:
                yield Key(_TILDE[num], _mods(fields[1]) if len(fields) > 1 else 0)
        elif final in _LETTER and (not fields or fields[0] in ("", "1")):
            yield Key(_LETTER[final], _mods(fields[1]) if len(fields) > 1 else 0)
        elif final in "IO" and not fields:
            yield Focus(final == "I")

    @staticmethod
    def _mouse(code, x, y, down):
        mods = (SHIFT if code & 4 else 0) | (ALT if code & 8 else 0) | (CTRL if code & 16 else 0)
        base = code & ~(4 | 8 | 16 | 32)
        if base in (64, 65, 66, 67):
            dx, dy = {64: (0, -1), 65: (0, 1), 66: (-1, 0), 67: (1, 0)}[base]
            return Mouse("wheel", x, y, mods=mods, dx=dx, dy=dy)
        button = base if base in (0, 1, 2) else -1
        if code & 32:
            return Mouse("move", x, y, button, mods)
        return Mouse("press" if down else "release", x, y, button, mods)


# --- to the browser ------------------------------------------------------------------

_VK = {"Enter": 13, "Tab": 9, "Backspace": 8, "Escape": 27, "Delete": 46, "Insert": 45, "ArrowLeft": 37,
       "ArrowUp": 38, "ArrowRight": 39, "ArrowDown": 40, "PageUp": 33, "PageDown": 34, "End": 35, "Home": 36,
       **{f"F{n}": 111 + n for n in range(1, 13)}}
# what ctrl + a letter means to an editable field; headless Chromium wants it spelled out
_COMMANDS = {"a": "selectAll", "c": "copy", "v": "paste", "x": "cut", "z": "undo", "y": "redo"}


def cdp_mods(mods):
    """Our modifier bits as the DevTools protocol's (alt 1, ctrl 2, meta 4, shift 8)."""
    return (1 if mods & ALT else 0) | (2 if mods & CTRL else 0) | (4 if mods & SUPER else 0) | (8 if mods & SHIFT else 0)


def cdp_key_events(key):
    """The Input.dispatchKeyEvent parameter sets for one key: its press, then its release."""
    name, base = key.name, {"modifiers": cdp_mods(key.mods)}
    chord = bool(key.mods & (CTRL | ALT | SUPER))
    if len(name) > 1:
        base.update(key=name, code=name, windowsVirtualKeyCode=_VK.get(name, 0))
        text = "\r" if name == "Enter" and not chord else None
    else:
        low = name.lower()
        if low.isascii() and low.isalpha():
            code, vk = "Key" + low.upper(), ord(low.upper())
        elif name.isascii() and name.isdigit():
            code, vk = "Digit" + name, ord(name)
        elif name == " ":
            code, vk = "Space", 32
        else:
            code, vk = "", 0
        base.update(key=name, code=code, windowsVirtualKeyCode=vk)
        text = None if chord else name
        if key.mods & CTRL and low in _COMMANDS:
            command = "redo" if low == "z" and key.mods & SHIFT else _COMMANDS[low]
            base["commands"] = [command]
    down = {**base, "type": "keyDown", "text": text} if text else {**base, "type": "rawKeyDown"}
    up = {k: v for k, v in base.items() if k != "commands"}
    return [down, {**up, "type": "keyUp"}]
