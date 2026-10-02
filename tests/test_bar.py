import unittest

from browzer.bar import LineEdit, fit
from browzer.keys import ALT, CTRL, Key


def typed(edit, *keys):
    return [edit.key(k if isinstance(k, Key) else Key(k)) for k in keys][-1]


class LineEditTest(unittest.TestCase):
    def test_typing_and_moving(self):
        e = LineEdit()
        typed(e, "a", "c", "ArrowLeft", "b")
        self.assertEqual((e.text, e.cursor), ("abc", 2))
        typed(e, "Home", "Delete", "End", "Backspace")
        self.assertEqual((e.text, e.cursor), ("b", 1))

    def test_fresh_text_is_replaced_by_typing_and_kept_by_moving(self):
        e = LineEdit("https://old.example", fresh=True)
        typed(e, "n")
        self.assertEqual(e.text, "n")
        e = LineEdit("https://old.example", fresh=True)
        typed(e, "End", "/", "x")
        self.assertEqual(e.text, "https://old.example/x")
        e = LineEdit("gone", fresh=True)
        typed(e, "Backspace")
        self.assertEqual(e.text, "")

    def test_word_and_line_kills(self):
        e = LineEdit("one two.three")
        typed(e, Key("w", CTRL))
        self.assertEqual(e.text, "one two.")
        typed(e, Key("Backspace", ALT))
        self.assertEqual(e.text, "one ")
        typed(e, Key("u", CTRL))
        self.assertEqual((e.text, e.cursor), ("", 0))

    def test_enter_and_escape(self):
        e = LineEdit("x")
        self.assertEqual(typed(e, "Enter"), "submit")
        self.assertEqual(typed(e, "Escape"), "cancel")
        self.assertIsNone(typed(e, Key("l", CTRL)), "a chord types nothing")
        self.assertEqual(e.text, "x")

    def test_paste_is_one_line(self):
        e = LineEdit()
        e.insert("two\nlines")
        self.assertEqual(e.text, "two lines")

    def test_window_keeps_the_cursor_in_view(self):
        e = LineEdit("0123456789")
        self.assertEqual(e.window(5), ("6789", 4))
        typed(e, "Home")
        self.assertEqual(e.window(5), ("01234", 0))

    def test_fit(self):
        self.assertEqual(fit("abcdef", 4), "abc…")
        self.assertEqual(fit("ab", 4), "ab  ")
        self.assertEqual(fit("ab", 0), "")


if __name__ == "__main__":
    unittest.main()
