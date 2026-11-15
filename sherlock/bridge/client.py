#!/usr/bin/env python3
"""Runner-container stand-in for `apptainer` / `gcloud`: forwards to hostexec.py (see there)."""
import json
import os
import signal
import socket
import struct
import sys
import threading

sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
sock.connect(os.environ["BB3_HOSTEXEC_SOCK"])
lock = threading.Lock()


def send(kind, payload):
    with lock:
        sock.sendall(kind + struct.pack(">I", len(payload)) + payload)


def bye(signum, _frame):
    try:
        sock.close()  # the helper terminates the host process
    finally:
        os._exit(128 + signum)


for s in (signal.SIGTERM, signal.SIGINT, signal.SIGHUP):
    signal.signal(s, bye)

send(b"A", json.dumps({"argv": [os.path.basename(sys.argv[0]), *sys.argv[1:]], "cwd": os.getcwd()}).encode())


def stdin_pump():
    try:
        while chunk := os.read(0, 65536):
            send(b"I", chunk)
    except OSError:
        pass
    try:
        send(b"E", b"")
    except OSError:
        pass


threading.Thread(target=stdin_pump, daemon=True).start()


def recvn(n):
    buf = b""
    while len(buf) < n:
        chunk = sock.recv(n - len(buf))
        if not chunk:
            sys.exit(255)
        buf += chunk
    return buf


while True:
    head = recvn(5)
    kind, size = head[:1], struct.unpack(">I", head[1:])[0]
    data = recvn(size)
    if kind == b"O":
        os.write(1, data)
    elif kind == b"R":
        os.write(2, data)
    elif kind == b"X":
        os._exit(struct.unpack(">i", data)[0])
