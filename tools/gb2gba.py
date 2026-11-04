#!/usr/bin/env python3
"""gb2gba — GB/GBC → GBA native porter CLI.

  gb2gba inspect ROM [--json]

Exit codes: 0 ok, 2 invalid or unsupported ROM (see README).
"""
import argparse
import hashlib
import json
import sys

TOOL_VERSION = "0.0.1"

# Pan Docs "The Cartridge Header" — 0147 cartridge type
CART_TYPES = {
    0x00: "ROM", 0x01: "MBC1", 0x02: "MBC1+RAM", 0x03: "MBC1+RAM+BATTERY",
    0x05: "MBC2", 0x06: "MBC2+BATTERY", 0x08: "ROM+RAM", 0x09: "ROM+RAM+BATTERY",
    0x0B: "MMM01", 0x0C: "MMM01+RAM", 0x0D: "MMM01+RAM+BATTERY",
    0x0F: "MBC3+TIMER+BATTERY", 0x10: "MBC3+TIMER+RAM+BATTERY",
    0x11: "MBC3", 0x12: "MBC3+RAM", 0x13: "MBC3+RAM+BATTERY",
    0x19: "MBC5", 0x1A: "MBC5+RAM", 0x1B: "MBC5+RAM+BATTERY",
    0x1C: "MBC5+RUMBLE", 0x1D: "MBC5+RUMBLE+RAM", 0x1E: "MBC5+RUMBLE+RAM+BATTERY",
    0x20: "MBC6", 0x22: "MBC7+SENSOR+RUMBLE+RAM+BATTERY",
    0xFC: "POCKET CAMERA", 0xFD: "BANDAI TAMA5", 0xFE: "HuC3", 0xFF: "HuC1+RAM+BATTERY",
}
RAM_SIZES = {0x00: 0, 0x02: 8 << 10, 0x03: 32 << 10, 0x04: 128 << 10, 0x05: 64 << 10}
# SHA-256 of the 48-byte boot logo at 0104-0133 (hash instead of embedding the bitmap)
LOGO_SHA256 = "daf4cabdc852baa0291849203f0b41fd0b4ecd58e0d7aff4a509f5de4d7f9a2e"


class RomError(Exception):
    pass


def inspect(rom):
    """Parse the GB cartridge header. Raises RomError for unusable ROMs."""
    if len(rom) < 0x150:
        raise RomError(f"file too small for a GB header ({len(rom)} bytes)")
    errors, warnings = [], []

    cgb = rom[0x143]
    cgb_mode = {0x80: "cgb_compatible", 0xC0: "cgb_only"}.get(cgb, "dmg")
    title_end = 0x13F if cgb_mode != "dmg" else 0x144
    title = rom[0x134:title_end].split(b"\0")[0].decode("ascii", "replace")

    cart_code = rom[0x147]
    if cart_code not in CART_TYPES:
        raise RomError(f"unknown cartridge type 0x{cart_code:02X}")
    cart_name = CART_TYPES[cart_code]
    parts = cart_name.split("+")

    rom_code = rom[0x148]
    if rom_code > 8:
        raise RomError(f"unknown ROM size code 0x{rom_code:02X}")
    rom_size = (32 << 10) << rom_code
    ram_code = rom[0x149]
    if ram_code not in RAM_SIZES:
        raise RomError(f"unknown RAM size code 0x{ram_code:02X}")

    if len(rom) < rom_size:
        raise RomError(f"file is {len(rom)} bytes but header declares {rom_size} (missing banks)")
    if len(rom) > rom_size:
        warnings.append(f"file is {len(rom)} bytes, larger than header ROM size {rom_size}; extra bytes ignored")

    hc = 0
    for b in rom[0x134:0x14D]:
        hc = (hc - b - 1) & 0xFF
    if hc != rom[0x14D]:
        errors.append(f"header checksum mismatch: header 0x{rom[0x14D]:02X}, computed 0x{hc:02X}")
    gc_header = (rom[0x14E] << 8) | rom[0x14F]
    gc = (sum(rom[:rom_size]) - rom[0x14E] - rom[0x14F]) & 0xFFFF
    if gc != gc_header:
        warnings.append(f"global checksum mismatch: header 0x{gc_header:04X}, computed 0x{gc:04X} (not checked by hardware)")
    logo_ok = hashlib.sha256(rom[0x104:0x134]).hexdigest() == LOGO_SHA256
    if not logo_ok:
        warnings.append("boot logo does not match; real hardware would refuse to boot")

    old_lic = rom[0x14B]
    header = {
        "title": title,
        "manufacturer_code": rom[0x13F:0x143].decode("ascii", "replace") if cgb_mode != "dmg" else None,
        "cgb_flag": f"0x{cgb:02X}",
        "cgb_mode": cgb_mode,
        "sgb_support": rom[0x146] == 0x03,
        "licensee": rom[0x144:0x146].decode("ascii", "replace") if old_lic == 0x33 else f"0x{old_lic:02X}",
        "cartridge_type": {
            "code": f"0x{cart_code:02X}", "name": cart_name, "mapper": parts[0],
            "ram": "RAM" in parts, "battery": "BATTERY" in parts,
            "timer": "TIMER" in parts, "rumble": "RUMBLE" in parts,
        },
        "rom_size": rom_size,
        "rom_banks": rom_size // 0x4000,
        "ram_size": RAM_SIZES[ram_code],
        "destination": "japan" if rom[0x14A] == 0 else "overseas",
        "version": rom[0x14C],
        "header_checksum": f"0x{rom[0x14D]:02X}",
        "global_checksum": f"0x{gc_header:04X}",
        "logo_valid": logo_ok,
    }
    if errors:
        raise RomError("; ".join(errors))
    return {
        "tool_version": TOOL_VERSION,
        "rom_size_bytes": len(rom),
        "rom_sha256": hashlib.sha256(rom).hexdigest(),
        "rom_sha1": hashlib.sha1(rom).hexdigest(),
        "rom_header": header,
        "warnings": warnings,
        "errors": [],
    }


def cmd_inspect(args):
    rom = open(args.rom, "rb").read()
    try:
        report = inspect(rom)
        code = 0
    except RomError as e:
        report = {"tool_version": TOOL_VERSION, "rom_sha256": hashlib.sha256(rom).hexdigest(),
                  "rom_header": None, "warnings": [], "errors": [str(e)]}
        code = 2
    if args.json:
        print(json.dumps(report, indent=2, sort_keys=True))
    elif code:
        print(f"error: {report['errors'][0]}", file=sys.stderr)
    else:
        h = report["rom_header"]
        c = h["cartridge_type"]
        print(f"{h['title']}  [{h['cgb_mode']}{', SGB' if h['sgb_support'] else ''}]")
        print(f"  cartridge  {c['name']} ({c['code']})")
        print(f"  ROM        {h['rom_size'] >> 10} KiB / {h['rom_banks']} banks")
        print(f"  RAM        {h['ram_size'] >> 10} KiB")
        print(f"  region     {h['destination']}, version {h['version']}, licensee {h['licensee']}")
        print(f"  sha256     {report['rom_sha256']}")
        for w in report["warnings"]:
            print(f"  warning: {w}")
    return code


def main(argv=None):
    p = argparse.ArgumentParser(prog="gb2gba")
    sub = p.add_subparsers(dest="cmd", required=True)
    i = sub.add_parser("inspect", help="parse ROM header, hashes, mapper")
    i.add_argument("rom")
    i.add_argument("--json", action="store_true")
    i.set_defaults(func=cmd_inspect)
    args = p.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
