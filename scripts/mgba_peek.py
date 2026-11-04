#!/usr/bin/env python3
"""Cross-check a .gba in mGBA: run it for N seconds via mGBA's GDB stub, then read memory.

usage: scripts/mgba_peek.py ROM.gba SECONDS ADDR:LEN [ADDR:LEN ...]   (hex addresses)
Env: MGBA (default /Applications/mGBA.app/Contents/MacOS/mGBA)
"""
import os
import socket
import subprocess
import sys
import time

MGBA = os.environ.get("MGBA", "/Applications/mGBA.app/Contents/MacOS/mGBA")


def packet(sock, data):
    sock.sendall(b"$%s#%02x" % (data, sum(data) & 0xFF))
    return reply(sock)


def reply(sock):
    buf = b""
    while b"#" not in buf or len(buf) < buf.index(b"#") + 3:
        buf += sock.recv(4096)
    sock.sendall(b"+")
    body = buf[buf.index(b"$") + 1:buf.index(b"#")]
    return body


def main(rom, seconds, ranges):
    proc = subprocess.Popen([MGBA, "-g", rom], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        for _ in range(50):
            try:
                sock = socket.create_connection(("127.0.0.1", 2345), timeout=10)
                break
            except OSError:
                time.sleep(0.2)
        else:
            raise SystemExit("mGBA GDB stub did not come up")
        packet(sock, b"?")
        sock.sendall(b"$c#63")          # continue; reply arrives when we interrupt
        time.sleep(float(seconds))
        sock.sendall(b"\x03")
        reply(sock)
        for r in ranges:
            addr, length = (int(x, 16) for x in r.split(":"))
            print(f"{addr:#010x}: {packet(sock, b'm%x,%x' % (addr, length)).decode()}")
        sock.sendall(b"$k#6b")
    finally:
        proc.terminate()
        proc.wait(timeout=10)


if __name__ == "__main__":
    if len(sys.argv) < 4:
        raise SystemExit(__doc__)
    main(sys.argv[1], sys.argv[2], sys.argv[3:])
