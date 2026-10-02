"""--ssh with a stand-in for ssh: the browser's requests must go through the tunnel by name."""
import http.server
import os
import signal
import threading
import time
import unittest

from browzer.tunnel import Tunnel, TunnelError
from tests import test_pty
from tests.test_pty import BAR, HAVE_BROWSER, Session, frame_sizes

FAKE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "fake_ssh.py")


def setUpModule():
    test_pty.setUpModule()


def tearDownModule():
    test_pty.tearDownModule()


class FarSide(http.server.BaseHTTPRequestHandler):
    def do_GET(self):
        body = b"<title>through the tunnel</title>far"
        self.send_response(200)
        self.send_header("Content-Type", "text/html")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args):
        pass


class TunnelTest(unittest.TestCase):
    def test_target_and_profile_name(self):
        t = Tunnel("-p 2200 dev@build box".replace(" box", ""))
        self.assertEqual((t.label, t.slug, t.args), ("dev@build", "dev-build", ["-p", "2200", "dev@build"]))
        with self.assertRaises(TunnelError):
            Tunnel("  ")

    def test_a_failing_ssh_is_quoted(self):
        os.environ["FAKE_SSH_FAIL"] = "1"
        self.addCleanup(os.environ.pop, "FAKE_SSH_FAIL")
        with self.assertRaises(TunnelError) as caught:
            Tunnel("testbox", FAKE).start(5)
        self.assertIn("Permission denied (publickey)", str(caught.exception))

    def test_a_missing_ssh_is_said(self):
        with self.assertRaises(TunnelError):
            Tunnel("testbox", "/no/such/ssh").start(1)


@unittest.skipUnless(HAVE_BROWSER, "no Chromium on PATH")
class SshTest(unittest.TestCase):
    def setUp(self):
        self.far = http.server.ThreadingHTTPServer(("127.0.0.1", 0), FarSide)
        threading.Thread(target=self.far.serve_forever, daemon=True).start()
        self.addCleanup(self.far.server_close)
        self.addCleanup(self.far.shutdown)
        self.log = os.path.join(test_pty.SITE, f"ssh-{self.id()}.log")
        self.env = {"BROWZER_SSH": FAKE, "FAKE_SSH_LOG": self.log, "FAKE_SSH_FAR_PORT": str(self.far.server_address[1])}

    def logged(self):
        with open(self.log) as fh:
            return fh.read().splitlines()

    def test_localhost_is_the_far_sides_and_names_cross_unresolved(self):
        s = Session("--transfer", "inline", "--ssh", "dev@testbox", url="http://localhost:4321/", env=self.env)
        self.addCleanup(s.close)
        s.until(BAR + b" [dev@testbox]   through the tunnel", "the far side's page, the host in the bar")
        self.assertIn("request name localhost 4321", self.logged())
        self.assertIn("-N -D 127.0.0.1:", self.logged()[0])
        self.assertTrue(self.logged()[0].endswith("dev@testbox"))

        s.then(b"\x1b[108;5u" + b"http://some.host.invalid/x\r", BAR + b" [dev@testbox]   through the tunnel", "another host")
        self.assertIn("request name some.host.invalid 80", self.logged(), "the name went to the far side to resolve")

    def test_a_dead_tunnel_comes_back(self):
        s = Session("--transfer", "inline", "--ssh", "testbox", url="http://localhost:4321/", env=self.env)
        self.addCleanup(s.close)
        s.until(b"through the tunnel", "the page")
        first = int(self.logged()[0].split()[1])
        s.out = b""
        os.kill(first, signal.SIGKILL)
        s.until(b"the ssh tunnel to testbox closed; reconnecting", "the notice", timeout=10)
        end = time.monotonic() + 5
        while len([line for line in self.logged() if line.startswith("pid ")]) < 2 and time.monotonic() < end:
            time.sleep(0.05)
        pids = [int(line.split()[1]) for line in self.logged() if line.startswith("pid ")]
        self.assertEqual(len(pids), 2, "ssh was started again")
        self.addCleanup(lambda: os.kill(pids[1], signal.SIGKILL) if test_pty.kitty.alive(pids[1]) else None)
        time.sleep(0.3)   # the new proxy binds its port
        s.then(b"\x1b[114;5u", lambda o: frame_sizes(o) or b"through the tunnel" in o, "a reload through the new tunnel")
        self.assertGreaterEqual(len([line for line in self.logged() if line.startswith("request name localhost")]), 2)

    def test_a_killed_browzer_takes_its_ssh_along(self):
        s = Session("--transfer", "inline", "--ssh", "testbox", url="http://localhost:4321/", env=self.env)
        self.addCleanup(s.close)
        s.until(b"through the tunnel", "the page")
        ssh = int(self.logged()[0].split()[1])
        s.proc.kill()
        end = time.monotonic() + 5
        while test_pty.kitty.alive(ssh) and time.monotonic() < end:
            time.sleep(0.05)
        self.assertFalse(test_pty.kitty.alive(ssh))

    def test_a_failing_ssh_ends_browzer_with_its_words(self):
        s = Session("--ssh", "testbox", env={**self.env, "FAKE_SSH_FAIL": "1"})
        self.addCleanup(s.close)
        self.assertEqual(s.proc.wait(10), 1)
        self.assertIn(b"ssh to testbox failed: testbox: Permission denied (publickey).", s.proc.stderr.read())


if __name__ == "__main__":
    unittest.main()
