import unittest

from browzer.keys import ALT, CTRL, SHIFT, Focus, Key, Mouse, Parser, Paste, cdp_key_events, cdp_mods


def feed(*chunks):
    p, out = Parser(), []
    for chunk in chunks:
        out += p.feed(chunk)
    return out + p.flush()


class ParserTest(unittest.TestCase):
    def test_text_and_legacy_controls(self):
        self.assertEqual(feed(b"a\xc3\xa9\r\t\x7f"), [Key("a"), Key("é"), Key("Enter"), Key("Tab"), Key("Backspace")])
        self.assertEqual(feed(b"\x11"), [Key("q", CTRL)])

    def test_utf8_split_across_reads(self):
        self.assertEqual(feed(b"\xe2\x82", b"\xac"), [Key("€")])

    def test_kitty_chords(self):
        self.assertEqual(feed(b"\x1b[113;5u"), [Key("q", CTRL)])
        self.assertEqual(feed(b"\x1b[27u"), [Key("Escape")])
        self.assertEqual(feed(b"\x1b[97;6u"), [Key("a", CTRL | SHIFT)])
        self.assertEqual(feed(b"\x1b[97;5:3u"), [], "a release is not a key")

    def test_a_chord_is_the_key_in_the_base_layout(self):
        self.assertEqual(feed(b"\x1b[1081::113;5u"), [Key("q", CTRL)], "ctrl+q on a Russian layout")
        self.assertEqual(feed(b"\x1b[1081::113u"), [Key("й")], "without a modifier it is the letter")

    def test_named_keys(self):
        self.assertEqual(feed(b"\x1b[A\x1b[1;3D\x1b[5~\x1b[3;5~\x1bOP\x1b[15~"),
                         [Key("ArrowUp"), Key("ArrowLeft", ALT), Key("PageUp"), Key("Delete", CTRL), Key("F1"), Key("F5")])

    def test_sequence_split_across_reads(self):
        self.assertEqual(feed(b"\x1b", b"[1;3", b"C"), [Key("ArrowRight", ALT)])

    def test_lone_escape_needs_a_flush(self):
        p = Parser()
        self.assertEqual(p.feed(b"\x1b"), [])
        self.assertTrue(p.pending)
        self.assertEqual(p.flush(), [Key("Escape")])
        self.assertFalse(p.pending)

    def test_legacy_alt(self):
        self.assertEqual(feed(b"\x1bx"), [Key("x", ALT)])

    def test_mouse(self):
        self.assertEqual(feed(b"\x1b[<0;120;45M\x1b[<0;120;45m"),
                         [Mouse("press", 120, 45, 0), Mouse("release", 120, 45, 0)])
        self.assertEqual(feed(b"\x1b[<35;7;9M"), [Mouse("move", 7, 9)])
        self.assertEqual(feed(b"\x1b[<32;7;9M"), [Mouse("move", 7, 9, 0)], "a drag keeps its button")
        self.assertEqual(feed(b"\x1b[<65;5;6M\x1b[<80;5;6M"),
                         [Mouse("wheel", 5, 6, dy=1), Mouse("wheel", 5, 6, mods=CTRL, dy=-1)])

    def test_paste_and_focus(self):
        self.assertEqual(feed(b"\x1b[200~two\nlines \x1b[A", b"\x1b[201~x"), [Paste("two\nlines \x1b[A"), Key("x")])
        self.assertEqual(feed(b"\x1b[I\x1b[O"), [Focus(True), Focus(False)])

    def test_query_answers_are_not_input(self):
        self.assertEqual(feed(b"\x1b[?1u\x1b[?62;c"), [])


class CdpKeysTest(unittest.TestCase):
    def test_a_character_types_itself(self):
        down, up = cdp_key_events(Key("a"))
        self.assertEqual((down["type"], down["text"], down["code"], down["windowsVirtualKeyCode"]), ("keyDown", "a", "KeyA", 65))
        self.assertEqual(up["type"], "keyUp")

    def test_enter_carries_a_carriage_return(self):
        self.assertEqual(cdp_key_events(Key("Enter"))[0]["text"], "\r")

    def test_a_chord_types_nothing_and_names_its_command(self):
        down, up = cdp_key_events(Key("a", CTRL))
        self.assertEqual((down["type"], down["modifiers"], down["commands"]), ("rawKeyDown", 2, ["selectAll"]))
        self.assertNotIn("text", down)
        self.assertNotIn("commands", up)
        self.assertEqual(cdp_key_events(Key("z", CTRL | SHIFT))[0]["commands"], ["redo"])

    def test_named_key(self):
        down, _ = cdp_key_events(Key("PageDown"))
        self.assertEqual((down["type"], down["key"], down["windowsVirtualKeyCode"]), ("rawKeyDown", "PageDown", 34))

    def test_mods(self):
        self.assertEqual(cdp_mods(SHIFT | ALT | CTRL), 8 | 1 | 2)


if __name__ == "__main__":
    unittest.main()
