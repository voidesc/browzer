"""The one-row bar: a line editor for the address and for prompts, and fitting text to the row."""
from .keys import ALT, CTRL, SUPER


class LineEdit:
    """One line of text with a cursor. `fresh` means the text is as if selected: the first
    thing typed replaces it, the first movement keeps it."""

    def __init__(self, text="", fresh=False):
        self.text, self.cursor, self.fresh = text, len(text), fresh and bool(text)

    def insert(self, text):
        text = " ".join(text.splitlines()) if "\n" in text or "\r" in text else text
        if self.fresh:
            self.text, self.cursor, self.fresh = "", 0, False
        self.text = self.text[:self.cursor] + text + self.text[self.cursor:]
        self.cursor += len(text)

    def _word_start(self):
        at = self.cursor
        while at and not self.text[at - 1].isalnum():
            at -= 1
        while at and self.text[at - 1].isalnum():
            at -= 1
        return at

    def key(self, key):
        """Applies a key; returns "submit", "cancel" or None."""
        name, mods = key.name, key.mods
        ctrl, chord = bool(mods & CTRL), bool(mods & (CTRL | ALT | SUPER))
        if name == "Enter":
            return "submit"
        if name == "Escape":
            return "cancel"
        fresh, self.fresh = self.fresh, False
        if name == "ArrowLeft":
            self.cursor = self._word_start() if ctrl else max(0, self.cursor - 1)
        elif name == "ArrowRight":
            self.cursor = min(len(self.text), self.cursor + 1)
        elif name == "Home" or (ctrl and name == "a"):
            self.cursor = 0
        elif name == "End" or (ctrl and name == "e"):
            self.cursor = len(self.text)
        elif ctrl and name == "u" or (fresh and name in ("Backspace", "Delete")):
            self.text, self.cursor = "", 0
        elif name == "Backspace" and chord or (ctrl and name == "w"):
            at = self._word_start()
            self.text, self.cursor = self.text[:at] + self.text[self.cursor:], at
        elif name == "Backspace":
            if self.cursor:
                self.text, self.cursor = self.text[:self.cursor - 1] + self.text[self.cursor:], self.cursor - 1
        elif name == "Delete":
            self.text = self.text[:self.cursor] + self.text[self.cursor + 1:]
        elif len(name) == 1 and not chord:
            self.fresh = fresh
            self.insert(name)
        return None

    def window(self, width):
        """(the stretch of text that fits `width` cells with the cursor in it, the cursor's
        cell in that stretch)."""
        if width <= 0:
            return "", 0
        start = max(0, self.cursor - width + 1)
        return self.text[start:start + width], self.cursor - start


def fit(text, width):
    """`text` cut to `width` cells with an ellipsis, or padded to it."""
    if width <= 0:
        return ""
    if len(text) > width:
        return text[:width - 1] + "…"
    return text.ljust(width)
