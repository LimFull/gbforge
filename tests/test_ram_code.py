"""RAM code traced back to ROM (gb2gba.ram_overlays), from a mesen_cdl.lua RAM execution log."""
import os
import sys
import tempfile
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "tools"))
from gb2gba import ram_overlays  # noqa: E402


class RamCodeTest(unittest.TestCase):
    def test_runs_found_in_rom(self):
        # ROM holds a routine at 0x150: LD A,B / OR C / RET Z / CALL $C010 / RET, copied to C000.
        # C010 is reached only by the call: a separate run, found at its own ROM offset.
        routine = bytes([0x78, 0xB1, 0xC8, 0xCD, 0x10, 0xC0, 0xC9])
        callee = bytes([0x3E, 0x01, 0xC9])
        rom = bytearray(0x8000)
        rom[0x150:0x157] = routine
        rom[0x160:0x163] = callee
        log = {0xC000: routine[0:4], 0xC001: routine[1:5], 0xC002: routine[2:6], 0xC003: routine[3:7],
               0xC006: routine[6:7] + b"\0\0\0", 0xC010: callee + b"\0", 0xC012: callee[2:] + b"\0\0\0",
               0xD000: bytes([0xAF, 0xC9, 0, 0])}   # not in ROM: generated at run time
        with tempfile.NamedTemporaryFile("w", suffix=".ram", delete=False) as f:
            f.write("".join(f"{pc:04x} {b[:4].hex()}\n" for pc, b in log.items()))
        try:
            found, missing, entries = ram_overlays(bytes(rom), f.name)
        finally:
            os.remove(f.name)
        self.assertEqual(found, [(0xC000, 0x150, 7), (0xC010, 0x160, 3)])
        self.assertEqual(missing, [0xD000])
        self.assertEqual(entries, [0xC000, 0xC001, 0xC002, 0xC003, 0xC006, 0xC010, 0xC012])


if __name__ == "__main__":
    unittest.main()
