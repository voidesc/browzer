#!/usr/bin/env python3
"""Stands in for `ssh -N -D 127.0.0.1:PORT HOST` in the tests: a SOCKS5 proxy whose "far side"
is one local server ($FAKE_SSH_FAR_PORT), whatever host and port are asked for. It writes
its pid and each request (address type, host, port) to $FAKE_SSH_LOG."""
import os
import socket
import struct
import sys
import threading

if os.environ.get("FAKE_SSH_FAIL"):
    print("testbox: Permission denied (publickey).", file=sys.stderr)
    sys.exit(255)
port = int(sys.argv[sys.argv.index("-D") + 1].rsplit(":", 1)[1])
log = open(os.environ["FAKE_SSH_LOG"], "a", buffering=1)
log.write(f"pid {os.getpid()} args {' '.join(sys.argv[1:])}\n")


def exactly(sock, n):
    data = b""
    while len(data) < n:
        chunk = sock.recv(n - len(data))
        if not chunk:
            raise EOFError
        data += chunk
    return data


def pump(a, b):
    try:
        while chunk := a.recv(65536):
            b.sendall(chunk)
    except OSError:
        pass
    finally:
        for s in (a, b):
            try:
                s.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass


def serve(client):
    try:
        _, count = exactly(client, 2)
        exactly(client, count)
        client.sendall(b"\x05\x00")
        _, _, _, kind = exactly(client, 4)
        if kind == 3:
            host = exactly(client, exactly(client, 1)[0]).decode()
            what = "name"
        else:
            host = socket.inet_ntop(socket.AF_INET if kind == 1 else socket.AF_INET6, exactly(client, 4 if kind == 1 else 16))
            what = "address"
        (asked,) = struct.unpack(">H", exactly(client, 2))
        log.write(f"request {what} {host} {asked}\n")
        far = socket.create_connection(("127.0.0.1", int(os.environ["FAKE_SSH_FAR_PORT"])))
        client.sendall(b"\x05\x00\x00\x01" + bytes(6))
        threading.Thread(target=pump, args=(far, client), daemon=True).start()
        pump(client, far)
    except (OSError, EOFError):
        client.close()


listener = socket.socket()
listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
listener.bind(("127.0.0.1", port))
listener.listen(16)
while True:
    conn, _ = listener.accept()
    threading.Thread(target=serve, args=(conn,), daemon=True).start()
