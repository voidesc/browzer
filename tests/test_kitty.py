import base64
import os
import re
import unittest

from browzer import kitty

ESCAPE = re.compile(rb"\x1b_G([^;\x1b]*)(?:;([^\x1b]*))?\x1b\\")


def images(stream):
    """The images a terminal stream transmits: (keys of the first escape, payload joined)."""
    out, open_ = [], None
    for keys, payload in ESCAPE.findall(stream):
        if b"a=T" in keys:
            open_ = [keys, b""]
        if open_ is None:
            continue
        open_[1] += payload
        if b"m=1" not in keys:
            out.append(tuple(open_))
            open_ = None
    return out


class KittyTest(unittest.TestCase):
    def test_small_frame_is_one_escape(self):
        self.assertEqual(kitty.show_inline("QUJD", 7, 80, 23), b"\x1b_Ga=T,f=100,i=7,c=80,r=23,q=2,C=1;QUJD\x1b\\")

    def test_large_frame_is_chunked_and_whole(self):
        data = base64.b64encode(os.urandom(10_000))
        stream = kitty.show_inline(data, 1, 10, 5)
        escapes = ESCAPE.findall(stream)
        self.assertEqual(len(escapes), -(-len(data) // kitty.CHUNK))
        self.assertTrue(all(len(payload) <= kitty.CHUNK for _, payload in escapes))
        self.assertEqual([b"m=1" in keys for keys, _ in escapes], [True] * (len(escapes) - 1) + [False])
        self.assertEqual(images(stream), [(b"a=T,f=100,i=1,c=10,r=5,q=2,C=1,m=1", data)])

    def test_frame_exactly_two_chunks(self):
        data = b"A" * (2 * kitty.CHUNK)
        self.assertEqual(len(ESCAPE.findall(kitty.show_inline(data, 1, 1, 1))), 2)

    def test_file_frame_names_a_deletable_path(self):
        path = kitty.spool_path(1)
        try:
            stream = kitty.show_file(b"\x89PNG", path, 3, 80, 23)
            keys, payload = ESCAPE.findall(stream)[0]
            self.assertIn(b"t=t", keys)
            self.assertEqual(base64.b64decode(payload).decode(), path)
            self.assertIn(kitty.SPOOL_MARK, path)
            with open(path, "rb") as fh:
                self.assertEqual(fh.read(), b"\x89PNG")
            self.assertEqual(os.stat(path).st_mode & 0o777, 0o600)
        finally:
            kitty.sweep()
        self.assertFalse(os.path.exists(path))

    def test_sweep_takes_a_dead_browzers_files_and_leaves_a_live_ones(self):
        dead = os.path.join(kitty.spool_dir(), f"browzer-4194303-{kitty.SPOOL_MARK}-1.png")   # the highest pid there can be: not running
        live = os.path.join(kitty.spool_dir(), f"browzer-{os.getppid()}-{kitty.SPOOL_MARK}-1.png")
        for path in (dead, live):
            open(path, "wb").close()
        try:
            kitty.sweep()
            self.assertFalse(os.path.exists(dead))
            self.assertTrue(os.path.exists(live))
        finally:
            for path in (dead, live):
                if os.path.exists(path):
                    os.unlink(path)

    def test_png_size(self):
        png = b"\x89PNG\r\n\x1a\n\0\0\0\rIHDR" + (800).to_bytes(4, "big") + (460).to_bytes(4, "big")
        self.assertEqual(kitty.png_size(png), (800, 460))


if __name__ == "__main__":
    unittest.main()
