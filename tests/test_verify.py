"""PNG decoding (all 5 filter types) and letterbox picture comparison used by `gb2gba verify`."""
import os
import struct
import sys
import tempfile
import unittest
import zlib

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "tools"))
from gb2gba import read_png, same_picture  # noqa: E402


def paeth(a, b, c):
    p = a + b - c
    pa, pb, pc = abs(p - a), abs(p - b), abs(p - c)
    return a if pa <= pb and pa <= pc else b if pb <= pc else c


def write_png(path, rows):
    """Encode RGB rows, cycling filter types 0-4 so the decoder sees each one."""
    h, w = len(rows), len(rows[0])
    raw, prev = b"", bytes(w * 3)
    for y, row in enumerate(rows):
        line, ftype, out = bytes(v for px in row for v in px), y % 5, bytearray()
        for i, v in enumerate(line):
            a = line[i - 3] if i >= 3 else 0
            b, c = prev[i], prev[i - 3] if i >= 3 else 0
            out.append((v - [0, a, b, (a + b) // 2, paeth(a, b, c)][ftype]) & 0xFF)
        raw += bytes([ftype]) + out
        prev = line

    def chunk(kind, body):
        return struct.pack(">I", len(body)) + kind + body + struct.pack(">I", zlib.crc32(kind + body))
    with open(path, "wb") as f:
        f.write(b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", w, h, 8, 2, 0, 0, 0))
                + chunk(b"IDAT", zlib.compress(raw)) + chunk(b"IEND", b""))


class VerifyTest(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        shades = [(255, 255, 255), (170, 170, 170), (85, 85, 85), (0, 0, 0)]
        self.gb = [[shades[(x * 7 + y * 3) % 4] for x in range(160)] for y in range(144)]
        gba_pal = {s: (s[0] // 2, s[1] // 3, 200) for s in shades}   # different colours, same structure
        self.gba = [[(0, 0, 0)] * 240 for _ in range(160)]
        for y in range(144):
            for x in range(160):
                self.gba[y + 8][x + 40] = gba_pal[self.gb[y][x]]

    def tearDown(self):
        self.dir.cleanup()

    def path(self, name):
        return os.path.join(self.dir.name, name)

    def test_decode_roundtrip(self):
        write_png(self.path("gb.png"), self.gb)
        w, h, rows = read_png(self.path("gb.png"))
        self.assertEqual((w, h), (160, 144))
        self.assertEqual(rows, self.gb)

    def test_letterbox_match_and_mismatch(self):
        write_png(self.path("gb.png"), self.gb)
        write_png(self.path("gba.png"), self.gba)
        self.assertTrue(same_picture(self.path("gb.png"), self.path("gba.png"))[0])
        self.gba[8 + 50][40 + 70] = self.gba[8][40] if self.gba[8][40] != self.gba[58][110] else (1, 2, 3)
        write_png(self.path("gba.png"), self.gba)
        ok, why = same_picture(self.path("gb.png"), self.path("gba.png"))
        self.assertFalse(ok)
        self.assertIn("(70,50)", why)


if __name__ == "__main__":
    unittest.main()
