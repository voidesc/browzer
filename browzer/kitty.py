"""The kitty graphics protocol: putting one PNG frame on the screen.

A frame is drawn at the cursor, scaled into a box of cells, without moving the cursor (C=1)
and without an answer from the terminal (q=2: an answer would arrive as input).
"""
import base64
import glob
import os

CHUNK = 4096   # the protocol's limit for one escape's payload
SPOOL_MARK = "tty-graphics-protocol"   # the terminal only deletes a t=t file with this in its path


def _keys(image_id, cols, rows):
    return b"a=T,f=100,i=%d,c=%d,r=%d,q=2,C=1" % (image_id, cols, rows)


def show_inline(png_b64, image_id, cols, rows):
    """The frame sent through the terminal's own stream, base64 as the browser delivered it.
    Works anywhere, a remote terminal included."""
    data = png_b64 if isinstance(png_b64, bytes) else png_b64.encode()
    if len(data) <= CHUNK:
        return b"\x1b_G%s;%s\x1b\\" % (_keys(image_id, cols, rows), data)
    parts = [b"\x1b_G%s,m=1;%s\x1b\\" % (_keys(image_id, cols, rows), data[:CHUNK])]
    for at in range(CHUNK, len(data), CHUNK):
        more = 1 if at + CHUNK < len(data) else 0
        parts.append(b"\x1b_Gm=%d;%s\x1b\\" % (more, data[at:at + CHUNK]))
    return b"".join(parts)


def spool_dir():
    return "/dev/shm" if os.path.isdir("/dev/shm") else (os.environ.get("TMPDIR") or "/tmp")


def spool_path(serial):
    return os.path.join(spool_dir(), f"browzer-{os.getpid()}-{SPOOL_MARK}-{serial}.png")


def show_file(png, path, image_id, cols, rows):
    """The frame written to a file in shared memory that the terminal reads and deletes
    (t=t): only the path crosses the terminal's stream. Needs a terminal on this machine."""
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        os.write(fd, png)
    finally:
        os.close(fd)
    return b"\x1b_G%s,t=t;%s\x1b\\" % (_keys(image_id, cols, rows), base64.b64encode(path.encode()))


def delete(image_id):
    """Removes an image and frees its data."""
    return b"\x1b_Ga=d,d=I,i=%d,q=2\x1b\\" % image_id


def alive(pid):
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        pass
    return True


def sweep():
    """Deletes the spool files the terminal never picked up: this process's, and those of
    browzers that are gone (a killed one cannot sweep)."""
    for path in glob.glob(os.path.join(spool_dir(), f"browzer-*-{SPOOL_MARK}-*.png")):
        pid = os.path.basename(path).split("-")[1]
        if pid.isdigit() and (int(pid) == os.getpid() or not alive(int(pid))):
            try:
                os.unlink(path)
            except OSError:
                pass


def png_size(png):
    """(width, height) from a PNG's header."""
    return int.from_bytes(png[16:20], "big"), int.from_bytes(png[20:24], "big")
