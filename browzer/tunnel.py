"""--ssh HOST: the browser runs here, its network is HOST's.

`ssh -N -D` gives a SOCKS proxy on this machine whose far end is HOST; the browser sends
every request through it, names included, so `http://localhost:3000` is HOST's port 3000
and HOST's own DNS answers. Only the network crosses the wire: frames and input stay local.
The proxy listens on this machine's loopback, so other local programs could use it too
while browzer runs.
"""
import os
import re
import shlex
import signal
import socket
import subprocess
import tempfile
import time


class TunnelError(Exception):
    pass


def _die_with_parent():
    """In the child, before it becomes ssh: have the kernel end it when browzer goes, however
    browzer goes (Linux; elsewhere a killed browzer leaves its ssh behind)."""
    try:
        import ctypes
        ctypes.CDLL(None).prctl(1, signal.SIGTERM)   # PR_SET_PDEATHSIG
    except (OSError, AttributeError):
        pass


class Tunnel:
    def __init__(self, target, ssh="ssh"):
        self.target, self.ssh = target, ssh
        self.args = shlex.split(target)
        if not self.args:
            raise TunnelError("--ssh needs a host (user@host, or ssh options and a host)")
        self.label = next((a for a in reversed(self.args) if not a.startswith("-")), target)
        self.port, self.proc, self.errors = 0, None, None

    @property
    def slug(self):
        """The host as a file name, for its own browser profile."""
        return re.sub(r"[^A-Za-z0-9._-]+", "-", self.label).strip("-") or "host"

    def chromium_args(self):
        proxy = f"127.0.0.1:{self.port}"
        return [f"--proxy-server=socks5://{proxy}",
                "--proxy-bypass-list=<-loopback>",                             # localhost is the far side's too
                "--host-resolver-rules=MAP * ~NOTFOUND , EXCLUDE 127.0.0.1"]   # no name is looked up here

    def _spawn(self):
        self.errors = tempfile.TemporaryFile()
        self.proc = subprocess.Popen(
            [self.ssh, "-N", "-D", f"127.0.0.1:{self.port}", "-o", "ExitOnForwardFailure=yes", "-o", "BatchMode=yes",
             "-o", "ServerAliveInterval=15", "-o", "ServerAliveCountMax=3", *self.args],
            stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=self.errors, start_new_session=True,
            preexec_fn=_die_with_parent)

    def _listening(self):
        with socket.socket() as s:
            s.settimeout(0.2)
            return s.connect_ex(("127.0.0.1", self.port)) == 0

    def start(self, timeout=20.0):
        """Opens the tunnel and waits until its proxy answers."""
        with socket.socket() as s:   # a free port; ssh takes it over a moment later
            s.bind(("127.0.0.1", 0))
            self.port = s.getsockname()[1]
        try:
            self._spawn()
        except OSError as e:
            self.stop()
            raise TunnelError(f"cannot run {self.ssh}: {e}")
        end = time.monotonic() + timeout
        while time.monotonic() < end:
            if self.proc.poll() is not None:
                self.errors.seek(0)
                said = self.errors.read().decode("utf-8", "replace").strip().splitlines()
                self.stop()
                raise TunnelError(f"ssh to {self.label} failed: {said[-1] if said else f'exit {self.proc.returncode}'}")
            if self._listening():
                return
            time.sleep(0.05)
        self.stop()
        raise TunnelError(f"ssh to {self.label} did not open its tunnel in {timeout:g}s")

    def revive(self):
        """Starts ssh again on the same port when it has died (a suspend, a network change).
        True when it had to."""
        if self.proc is None or self.proc.poll() is None:
            return False
        if self.errors:
            self.errors.close()
        try:
            self._spawn()
        except OSError:
            pass
        return True

    def stop(self):
        if self.proc and self.proc.poll() is None:
            self.proc.terminate()
            try:
                self.proc.wait(2)
            except subprocess.TimeoutExpired:
                self.proc.kill()
        if self.errors:
            self.errors.close()
            self.errors = None
