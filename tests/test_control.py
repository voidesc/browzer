"""`browzer ctl` against a real browzer running in a pseudo-terminal."""
import io
import json
import os
import shutil
import socket
import stat
import tempfile
import threading
import unittest

from browzer import control, ctl, kitty, snapshot
from browzer.keys import ALT, CTRL, SHIFT, Key, parse_chord
from tests import test_pty
from tests.test_pty import HAVE_BROWSER, Session, frame_sizes


def setUpModule():
    test_pty.setUpModule()


def tearDownModule():
    test_pty.tearDownModule()


class ChordTest(unittest.TestCase):
    def test_names_and_modifiers(self):
        self.assertEqual(parse_chord("Enter"), Key("Enter"))
        self.assertEqual(parse_chord("ctrl+a"), Key("a", CTRL))
        self.assertEqual(parse_chord("Shift+Tab"), Key("Tab", SHIFT))
        self.assertEqual(parse_chord("ctrl+alt+down"), Key("ArrowDown", CTRL | ALT))
        self.assertEqual(parse_chord("space"), Key(" "))
        self.assertEqual(parse_chord("+"), Key("+"))
        self.assertEqual(parse_chord("ctrl++"), Key("+", CTRL))

    def test_nonsense_is_refused(self):
        for bad in ("hyper+a", "Entre", ""):
            with self.assertRaises(ValueError):
                parse_chord(bad)


class SnapshotTest(unittest.TestCase):
    def test_outline(self):
        def node(nid, role, name="", children=(), **more):
            return {"nodeId": nid, "role": {"value": role}, "name": {"value": name}, "childIds": list(children),
                    "backendDOMNodeId": int(nid) + 100, **more}
        nodes = [
            node("1", "RootWebArea", "a page", ["2", "5", "7", "8"]),
            node("2", "link", "new window", ["3"], parentId="1"),
            node("3", "StaticText", "new window", ["4"], parentId="2"),
            node("4", "InlineTextBox", "new window", parentId="3"),
            node("5", "generic", "", ["6"], parentId="1"),
            node("6", "textbox", "", parentId="5", value={"value": "typed"},
                 properties=[{"name": "focused", "value": {"value": True}}, {"name": "invalid", "value": {"value": "false"}}]),
            node("7", "StaticText", "plain words", parentId="1"),
            node("8", "button", "hidden", parentId="1", ignored=True),
        ]
        self.assertEqual(snapshot.render(nodes), "\n".join([
            'link "new window" [e102]',
            "textbox value='typed' focused [e106]",
            'text "plain words"',
        ]))

    def test_long_pages_are_cut(self):
        nodes = [{"nodeId": "0", "role": {"value": "RootWebArea"}, "childIds": [str(i) for i in range(1, 9)]}]
        nodes += [{"nodeId": str(i), "parentId": "0", "role": {"value": "button"}, "name": {"value": f"b{i}"}} for i in range(1, 9)]
        self.assertEqual(snapshot.render(nodes, max_lines=3).splitlines()[-1], "… 5 more lines")


@unittest.skipUnless(HAVE_BROWSER, "no Chromium on PATH")
class ControlTest(unittest.TestCase):
    def setUp(self):
        self.runtime = tempfile.mkdtemp(prefix="browzer-test-run-")
        self.addCleanup(shutil.rmtree, self.runtime, ignore_errors=True)
        old = os.environ.get("XDG_RUNTIME_DIR")
        os.environ["XDG_RUNTIME_DIR"] = self.runtime
        self.addCleanup(lambda: os.environ.__setitem__("XDG_RUNTIME_DIR", old) if old else os.environ.pop("XDG_RUNTIME_DIR"))
        self.s = Session("--transfer", "inline", env={"XDG_RUNTIME_DIR": self.runtime})
        self.addCleanup(self.s.close)
        self.s.until(lambda o: b"browzer test" in o and frame_sizes(o), "the page's first frame")
        # browzer keeps drawing while ctl waits for its answer: something has to read the terminal
        threading.Thread(target=self.drain, daemon=True).start()
        self.first, self.second = (os.path.join(test_pty.SITE, name) for name in ("first.html", "second.html"))

    def drain(self):
        try:
            while os.read(self.s.master, 1 << 20):
                pass
        except OSError:
            pass

    def ctl(self, *argv, status=0):
        out, err = io.StringIO(), io.StringIO()
        got = ctl.main(list(argv), out=out, err=err)
        self.assertEqual(got, status, f"browzer ctl {' '.join(argv)}: {err.getvalue() or out.getvalue()}")
        return (out.getvalue() if status == 0 else err.getvalue()).rstrip("\n")

    def test_socket_is_private(self):
        path = control.socket_path(self.s.proc.pid)
        self.assertEqual(stat.S_IMODE(os.stat(path).st_mode), 0o600)
        self.assertEqual(stat.S_IMODE(os.stat(os.path.dirname(path)).st_mode), 0o700)

    def test_ls_open_tabs_and_history(self):
        self.assertEqual(self.ctl("ls"), f"browzer {self.s.proc.pid}\n  * 1  browzer test  file://{self.first}")
        self.assertEqual(self.ctl("open", self.second), f"second page\nfile://{self.second}")
        self.assertEqual(self.ctl("text"), "second")
        self.assertEqual(self.ctl("back"), f"browzer test\nfile://{self.first}")
        self.assertEqual(self.ctl("forward"), f"second page\nfile://{self.second}")
        self.assertIn("there is no page that way", self.ctl("forward", status=1))
        self.ctl("back")
        self.assertEqual(self.ctl("open", "--new-tab", self.second), f"second page\nfile://{self.second}")
        self.assertEqual(self.ctl("ls").splitlines()[1:],
                         [f"    1  browzer test  file://{self.first}", f"  * 2  second page  file://{self.second}"])
        self.assertEqual(self.ctl("tab", "1").splitlines()[0], f"  * 1  browzer test  file://{self.first}")
        self.assertEqual(self.ctl("--tab", "2", "close"), f"  * 1  browzer test  file://{self.first}")
        self.assertIn("there is no tab 5", self.ctl("--tab", "5", "text", status=1))

    def test_snapshot_click_fill_type_press_eval(self):
        outline = self.ctl("snapshot")
        refs = {}
        for line in outline.splitlines():
            if "[e" in line:
                refs[line.strip().rsplit(" [", 1)[0]] = line.rsplit("[", 1)[1].rstrip("]")
        self.assertIn('button "press"', refs, outline)
        self.assertIn('link "new window"', refs, outline)
        self.ctl("click", refs['button "press"'])
        self.assertEqual(self.ctl("eval", "document.title"), '"clicked"')
        field = next(ref for what, ref in refs.items() if what.startswith("textbox"))
        self.ctl("fill", field, "hello there")
        self.assertEqual(self.ctl("eval", "document.title"), '"typed hello there"')
        self.ctl("press", "ctrl+a")
        self.ctl("type", "x")
        self.assertEqual(self.ctl("eval", "document.title"), '"typed x"')
        self.ctl("fill", field, "")
        self.assertEqual(self.ctl("eval", "document.querySelector('input').value"), '""')
        self.ctl("click", "100", "35")   # the button again, by its place on the page
        self.assertEqual(self.ctl("eval", "document.title"), '"clicked"')
        self.assertEqual(self.ctl("eval", "({a: [1, 2]})"), '{"a": [1, 2]}')
        self.assertEqual(self.ctl("eval", "undefined"), "undefined")
        self.assertEqual(self.ctl("eval", "new Promise(r => setTimeout(() => r(7), 20))"), "7")
        self.assertIn("ReferenceError", self.ctl("eval", "nope()", status=1))
        self.assertIn("not a ref", self.ctl("click", "button", status=1))
        self.assertIn("snapshot", self.ctl("click", "e99999999", status=1))
        self.assertIn("not a key", self.ctl("press", "Entre", status=1))

    def test_a_page_sees_a_regular_browser(self):
        seen = json.loads(self.ctl("eval", """({agent: navigator.userAgent, robot: navigator.webdriver,
            mouse: matchMedia('(hover: hover) and (pointer: fine)').matches,
            screen: [screen.width, screen.height], hints: navigator.userAgentData.brands.length,
            webgl: !!document.createElement('canvas').getContext('webgl')})"""))
        self.assertNotIn("Headless", seen["agent"])
        self.assertIs(seen["robot"], False)
        self.assertTrue(seen["mouse"], "the terminal's mouse is a mouse to the page")
        self.assertGreaterEqual(seen["screen"], [800, 460], "the page fits on its screen")
        self.assertTrue(seen["hints"], "the browser still says which browser it is")
        self.assertTrue(seen["webgl"], "with a GPU or without")

    def test_screenshot(self):
        path = os.path.join(self.runtime, "shot.png")
        self.assertEqual(self.ctl("screenshot", path), path)
        with open(path, "rb") as fh:
            self.assertEqual(kitty.png_size(fh.read()), (800, 460))

    def test_failures_are_said(self):
        with socket.socket() as s:   # a port nothing listens on: bound, never listening
            s.bind(("127.0.0.1", 0))
            self.assertIn("ERR_CONNECTION_REFUSED", self.ctl("open", f"http://127.0.0.1:{s.getsockname()[1]}/", status=1))
        self.assertIn("is not running", self.ctl("--instance", "1", "text", status=3))

    def test_quit(self):
        self.ctl("quit")
        self.assertEqual(self.s.proc.wait(10), 0)
        self.assertFalse(os.path.exists(control.socket_path(self.s.proc.pid)))
        self.assertEqual(self.ctl("ls"), "no browzer is running")


class NoControlTest(unittest.TestCase):
    @unittest.skipUnless(HAVE_BROWSER, "no Chromium on PATH")
    def test_no_control_means_no_socket(self):
        runtime = tempfile.mkdtemp(prefix="browzer-test-run-")
        self.addCleanup(shutil.rmtree, runtime, ignore_errors=True)
        s = Session("--transfer", "inline", "--no-control", env={"XDG_RUNTIME_DIR": runtime})
        self.addCleanup(s.close)
        s.until(lambda o: b"browzer test" in o, "the page")
        self.assertEqual(os.listdir(runtime), [])


if __name__ == "__main__":
    unittest.main()
