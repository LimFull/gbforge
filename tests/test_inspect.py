"""python3 -m unittest discover -s tests"""
import json
import os
import subprocess
import sys
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "tools"))
from gb2gba import RomError, inspect  # noqa: E402

TINY = os.path.join(ROOT, "tests/fixtures/tiny/tiny.gb")
CLI = os.path.join(ROOT, "tools/gb2gba.py")


def read(path):
    with open(path, "rb") as f:
        return f.read()


def run_cli(path):
    return subprocess.run([sys.executable, CLI, "inspect", path, "--json"], capture_output=True, text=True)


class InspectTest(unittest.TestCase):
    def test_golden_and_deterministic(self):
        golden = read(os.path.join(ROOT, "tests/golden/tiny.inspect.json"))
        a, b = run_cli(TINY), run_cli(TINY)
        self.assertEqual(a.returncode, 0)
        self.assertEqual(a.stdout, b.stdout)
        self.assertEqual(json.loads(a.stdout), json.loads(golden))

    def test_header_checksum_mismatch_is_error(self):
        rom = bytearray(read(TINY))
        rom[0x14D] ^= 0xFF
        with self.assertRaisesRegex(RomError, "header checksum"):
            inspect(bytes(rom))

    def test_missing_banks_is_error(self):
        rom = read(TINY)
        with self.assertRaisesRegex(RomError, "missing banks"):
            inspect(rom[:0x4000])

    def test_global_checksum_mismatch_is_warning(self):
        rom = bytearray(read(TINY))
        rom[0x7FFF] ^= 0xFF
        self.assertIn("global checksum", inspect(bytes(rom))["warnings"][0])

    def test_invalid_rom_exit_code_2(self):
        tmp = os.path.join(ROOT, "build", "short.gb")
        os.makedirs(os.path.dirname(tmp), exist_ok=True)
        with open(tmp, "wb") as f:
            f.write(b"\0" * 100)
        r = run_cli(tmp)
        self.assertEqual(r.returncode, 2)
        self.assertIn("too small", json.loads(r.stdout)["errors"][0])

    def test_unsupported_cartridge_exit_code_4(self):
        rom = bytearray(read(TINY))
        rom[0x147] = 0x13                                  # MBC3+RAM+BATTERY
        rom[0x14D] = (rom[0x14D] - 0x13) & 0xFF            # header checksum: (x - b - 1) per byte
        tmp = os.path.join(ROOT, "build", "mbc3.gb")
        with open(tmp, "wb") as f:
            f.write(rom)
        r = subprocess.run([sys.executable, CLI, "port", tmp, "--output", os.path.join(ROOT, "build", "mbc3.gba")],
                           capture_output=True, text=True)
        self.assertEqual(r.returncode, 4)
        self.assertIn("MBC3+RAM+BATTERY cartridge", r.stderr)


if __name__ == "__main__":
    unittest.main()
