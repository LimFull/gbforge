"""Constant far-call entry detection (`gb2gba.far_call_entries`)."""
import os
import sys
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "tools"))
from gb2gba import far_call_entries  # noqa: E402


class FarCallTest(unittest.TestCase):
    def rom(self, handler):
        rom = bytearray(4 * 0x4000)
        rom[0x10:0x10 + len(handler)] = handler
        rom[0x30] = 0xE9                                   # RST $30: JP HL
        rom[0x200:0x206] = b"\x21\x76\x41\x06\x03\xd7"     # LD HL,$4176 ; LD B,3 ; RST $10
        rom[0x300:0x306] = b"\x06\x02\x21\x00\x50\xd7"     # LD B,2 ; LD HL,$5000 ; RST $10
        rom[0x400:0x406] = b"\x21\x00\x20\x06\x02\xd7"     # target outside ROMX: ignored
        return bytes(rom)

    def test_dq_style_rst10(self):
        # LD A,($4000) ; PUSH AF ; LD A,B ; LD ($2100),A ; RST $30 ; POP AF ; LD ($2100),A ; RET
        rom = self.rom(b"\xfa\x00\x40\xf5\x78\xea\x00\x21\xf7\xf1\xea\x00\x21\xc9")
        self.assertEqual(far_call_entries(rom), ["02:5000", "03:4176"])

    def test_rst_without_bank_switch(self):
        rom = self.rom(b"\x78\xea\x00\xc0\xe9")              # writes B to WRAM, not the MBC
        self.assertEqual(far_call_entries(rom), [])


if __name__ == "__main__":
    unittest.main()
