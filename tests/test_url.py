import os
import tempfile
import unittest

from browzer.url import normalize


class NormalizeTest(unittest.TestCase):
    def test_urls_pass(self):
        for url in ("https://example.com/a?b", "about:blank", "data:text/html,x", "file:///tmp/x"):
            self.assertEqual(normalize(url), url)

    def test_hosts_get_a_scheme(self):
        self.assertEqual(normalize("example.com/path"), "https://example.com/path")
        self.assertEqual(normalize("localhost:3000"), "http://localhost:3000")
        self.assertEqual(normalize("127.0.0.1:8080/x"), "http://127.0.0.1:8080/x")
        self.assertEqual(normalize("devbox:3000"), "http://devbox:3000")
        self.assertEqual(normalize("192.168.0.4"), "http://192.168.0.4")

    def test_words_are_searched(self):
        self.assertEqual(normalize("kitty graphics protocol"), "https://duckduckgo.com/?q=kitty+graphics+protocol")
        self.assertEqual(normalize("python", "https://s.example/?q={}", bare_file=False), "https://s.example/?q=python")
        self.assertEqual(normalize("what is 2+2?"), "https://duckduckgo.com/?q=what+is+2%2B2%3F")

    def test_files(self):
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "a page.html")
            open(path, "w").close()
            self.assertEqual(normalize(path), "file://" + d + "/a%20page.html")
            old = os.getcwd()
            os.chdir(d)
            try:
                open("notes", "w").close()
                self.assertTrue(normalize("notes").startswith("file://"), "a bare name is a file on the command line")
                self.assertTrue(normalize("notes", bare_file=False).startswith("https://duckduckgo.com"),
                                "and words in the address bar")
                self.assertTrue(normalize("./notes", bare_file=False).startswith("file://"))
            finally:
                os.chdir(old)

    def test_empty(self):
        self.assertEqual(normalize("  "), "about:blank")


if __name__ == "__main__":
    unittest.main()
