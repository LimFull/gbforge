#!/usr/bin/env python3
"""gb2gba — GB/GBC → GBA native porter CLI.

  gb2gba inspect ROM [--json]
  gb2gba analyze ROM --out DIR [--no-scan] [--entry BB:AAAA]... [--trace FILE] [--cdl FILE]
  gb2gba port ROM --output OUT.gba [--native-policy strict] [--no-scan] [--entry BB:AAAA]... [--cdl FILE]
  gb2gba verify ROM PORT.gba --scenario SCENARIO.json --out DIR [--reuse-original]   (needs emucap)

Exit codes: 0 ok, 2 invalid ROM, 3 unresolved paths (strict), 5 over budget, 6 external tool error,
8 port differs from the original.
"""
import argparse
import hashlib
import json
import os
import re
import shutil
import struct
import subprocess
import sys
import zlib

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CORE = os.environ.get("GBFORGE_CORE", os.path.join(ROOT, "build", "core", "gbforge-core"))
GBA_ROM_LIMIT = 32 << 20

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


def core(args):
    """Run gbforge-core (C++ analyzer/codegen on gb-recompiled's gbrecomp_core)."""
    if not os.path.exists(CORE):
        raise SystemExit("gbforge-core not built: cmake -G Ninja -B build/core . && ninja -C build/core")
    r = subprocess.run([CORE, *args], stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    if r.returncode not in (0, 3):
        sys.stderr.write(r.stderr[-2000:])
    return r.returncode


def far_call_entries(rom):
    """Targets of constant far calls `LD HL,nn / LD B,bank / RST n` (either load order) through an RST
    handler that writes B to the MBC ROM bank register and then jumps to HL (DQ1&2's RST $10). Static
    evidence for entries that a CDL only has when the original happened to run them."""
    def far_rst(v):
        i = rom.find(b"\x78\xea", v, v + 8)  # LD A,B ; LD (nn),A
        if i < 0 or not 0x20 <= rom[i + 3] < 0x40:
            return False
        nxt = rom[i + 4]
        return nxt == 0xE9 or (nxt & 0xC7 == 0xC7 and rom[nxt & 0x38] == 0xE9)  # JP HL, or RST to JP HL
    rsts = [0xC7 | v for v in range(0, 0x40, 8) if far_rst(v)]
    banks, found = len(rom) // 0x4000, set()
    for rst in rsts:
        for m in re.finditer(rb"\x21(..)\x06(.)" + bytes([rst]) + rb"|\x06(.)\x21(..)" + bytes([rst]), rom, re.S):
            addr = int.from_bytes(m.group(1) or m.group(4), "little")
            bank = (m.group(2) or m.group(3))[0]
            if 0x4000 <= addr < 0x8000 and 0 < bank < banks:
                found.add(f"{bank:02X}:{addr:04X}")
    return sorted(found)


def analysis_args(args, rom):
    out = ["--no-scan"] if args.no_scan else []
    for e in (args.entry or []) + far_call_entries(rom):
        out += ["--entry", e]
    if args.trace:
        out += ["--trace", args.trace]
    if args.cdl:
        out += ["--cdl", args.cdl]
    return out


def inspect_or_exit(path):
    with open(path, "rb") as f:
        rom = f.read()
    try:
        return rom, inspect(rom)
    except RomError as e:
        print(f"error: {e}", file=sys.stderr)
        sys.exit(2)


def cmd_analyze(args):
    rom, report = inspect_or_exit(args.rom)
    os.makedirs(args.out, exist_ok=True)
    out = os.path.join(args.out, "analysis.json")
    if core(["analyze", args.rom, out, *analysis_args(args, rom)]) != 0:
        return 2
    with open(out) as f:
        summary = json.load(f)["summary"]
    report.update({"analysis": summary, "unresolved_edges": summary["unresolved_edges"]})
    with open(os.path.join(args.out, "summary.json"), "w") as f:
        json.dump(report, f, indent=2, sort_keys=True)
    print(json.dumps(summary, indent=2))
    return 0


def read_symbols(path):
    syms = {}
    with open(path) as f:
        for line in f:
            parts = line.split()
            if len(parts) == 3:
                syms[parts[2]] = int(parts[0], 16)
    return syms


def write_map(sym_path, map_path, header):
    syms = read_symbols(sym_path)
    functions = {f"{m.group(1)}:{m.group(2)}": f"0x{a:08X}"
                 for name, a in sorted(syms.items()) if (m := re.fullmatch(r"f_([0-9A-F]{2})_([0-9A-F]{4})", name))}
    doc = {
        "schema": "gbforge.map", "schema_version": 1,
        "memory": {
            "cpu": f"0x{syms['gb']:08X}",
            "trap": f"0x{syms['gb_trap_info']:08X}",
            "frames": f"0x{syms['gb_frames']:08X}",          # u32 GBA VBlanks, then u32 lag frames
            "lag_frames": f"0x{syms['gb_lag_frames']:08X}",
            "gb_frames": f"0x{syms['gb_paced']:08X}",       # u32 GB frames elapsed in GB time
            "pace": {"cyc": f"0x{syms['gb'] + 28:08X}", "frame_start": f"0x{syms['frame_start']:08X}",
                     "pace_f": f"0x{syms['gb_pace_f']:08X}"},  # where the GB frame count ticks
            **({"profile": {"pcs": f"0x{syms['gb_prof_pcs']:08X}", "count": f"0x{syms['gb_prof_count']:08X}", "size": 4096}}
               if "gb_prof_pcs" in syms else {}),
            # GB-visible windows. D000-DFFF matches only while SVBK selects bank 0/1 (CGB).
            "wram": {"gb": "0xC000", "gba": f"0x{syms['gb_wram']:08X}", "size": 0x2000},
            "io": {"gb": "0xFF00", "gba": f"0x{syms['io']:08X}", "size": 0x80},  # stored values; LY/STAT/P1/DIV are computed on read
            "hram": {"gb": "0xFF80", "gba": f"0x{syms['gb_hram']:08X}", "size": 0x80},  # FFFF = IE
        },
        "functions": functions,
    }
    with open(map_path, "w") as f:
        json.dump(doc, f, indent=2)


def cmd_port(args):
    rom, report = inspect_or_exit(args.rom)
    out = os.path.abspath(args.output)
    stem = os.path.splitext(os.path.basename(out))[0]
    work = os.path.join(ROOT, "build", "port", stem)
    gen = os.path.join(work, "gen")
    shutil.rmtree(gen, ignore_errors=True)
    os.makedirs(os.path.dirname(out), exist_ok=True)
    report["native_policy"] = args.native_policy
    report_path = os.path.splitext(out)[0] + ".report.json"

    def finish(code, **extra):
        report.update(extra)
        with open(report_path, "w") as f:
            json.dump(report, f, indent=2, sort_keys=True)
        return code

    report["far_call_entries"] = len(far_call_entries(rom))
    if core(["codegen", args.rom, gen, *analysis_args(args, rom)]) != 0:
        return finish(3, errors=["codegen failed (see stderr)"])
    with open(os.path.join(gen, "codegen.json")) as f:
        cg = json.load(f)
    report["codegen"] = {k: cg[k] for k in ("functions", "entries", "stats")}
    unresolved = cg["unresolved"]
    report["unresolved_edges"] = cg["analysis_unresolved_edges"] + len(unresolved)
    report["unresolved"] = sorted(set(unresolved))
    if args.native_policy == "strict" and report["unresolved_edges"]:
        if not args.allow_unresolved:
            print(f"blocked: {report['unresolved_edges']} unresolved edges (strict); see {report_path}", file=sys.stderr)
            return finish(3, errors=["unresolved_edges"])
        # research build: still no interpreter — unresolved targets trap at run time
        report["gate"] = "bypassed"
        report.setdefault("warnings", []).append("strict gate bypassed (--allow-unresolved): not a native result")
        print(f"warning: {report['unresolved_edges']} unresolved edges, gate bypassed", file=sys.stderr)

    shutil.copyfile(args.rom, os.path.join(gen, "rom.gb"))
    with open(os.path.join(gen, "rom.s"), "w") as f:
        f.write('.section .rodata\n.global gb_rom\n.balign 4\ngb_rom:\n.incbin "rom.gb"\n')
    title = re.sub(r"[^A-Z0-9]", "", report["rom_header"]["title"].upper())[:12] or "GBFORGE"
    make = [os.path.join(ROOT, "scripts", "dkarm.sh"), "make", "-s", f"-j{os.cpu_count() or 4}",
            "-C", os.path.join(ROOT, "runtime-gba"), f"GEN={gen}", f"OUT={out}", f"TITLE={title}"]
    if args.profile:
        make.append("EXTRA_CFLAGS=-DGB_PROFILE")
        report.setdefault("warnings", []).append("profiling build (timer IRQ sampler)")
    for stale in (out, os.path.splitext(out)[0] + ".map.json"):
        if os.path.exists(stale):
            os.remove(stale)  # never leave a previous build behind a failed one
    if subprocess.run(make).returncode != 0:
        return finish(6, errors=["GBA build failed"])
    size = os.path.getsize(out)
    report["build_sizes"] = {"gba_bytes": size}
    if size > GBA_ROM_LIMIT:
        return finish(5, errors=[f"{size} bytes exceeds the 32 MiB GBA ROM limit"])
    write_map(os.path.splitext(out)[0] + ".sym", os.path.splitext(out)[0] + ".map.json", report["rom_header"])
    print(f"{out}: {size} bytes, {cg['functions']} GB functions, "
          + ("strict gate BYPASSED" if report.get("gate") == "bypassed" else "strict native"))
    return finish(0)


def read_png(path):
    """Minimal PNG decoder (8-bit RGB/RGBA, non-interlaced) → (width, height, rows of pixel tuples)."""
    with open(path, "rb") as f:
        data = f.read()
    pos, idat = 8, b""
    while pos < len(data):
        n, kind = struct.unpack(">I4s", data[pos:pos + 8])
        body = data[pos + 8:pos + 8 + n]
        if kind == b"IHDR":
            w, h, depth, ctype, _, _, interlace = struct.unpack(">IIBBBBB", body)
            if depth != 8 or ctype not in (2, 6) or interlace:
                raise ValueError(f"unsupported PNG {path}")
            bpp = 3 if ctype == 2 else 4
        elif kind == b"IDAT":
            idat += body
        pos += 12 + n
    raw, stride, prev, rows = zlib.decompress(idat), w * bpp, bytearray(w * bpp), []
    for y in range(h):
        ftype, line = raw[y * (stride + 1)], bytearray(raw[y * (stride + 1) + 1:(y + 1) * (stride + 1)])
        for i in range(stride):
            a = line[i - bpp] if i >= bpp else 0
            b, c = prev[i], prev[i - bpp] if i >= bpp else 0
            pred = {0: 0, 1: a, 2: b, 3: (a + b) // 2,
                    4: min((a, b, c), key=lambda v: (abs(a + b - c - v), (a, b, c).index(v)))}[ftype]
            line[i] = (line[i] + pred) & 0xFF
        rows.append([tuple(line[x * bpp:x * bpp + 3]) for x in range(w)])
        prev = line
    return w, h, rows


def same_picture(gb_png, gba_png, tolerance=0):
    """GB 160x144 vs the port's letterboxed 240x160 at (40,8), equal up to a colour mapping
    (the two emulators apply different LCD colour correction). Pixels disagreeing with the
    majority mapping are counted; up to `tolerance` of them (blink phases) still pass."""
    gw, gh, g = read_png(gb_png)
    aw, ah, a = read_png(gba_png)
    if (gw, gh) != (160, 144) or (aw, ah) != (240, 160):
        return False, f"unexpected sizes {gw}x{gh} / {aw}x{ah}"
    votes = {}
    for y in range(144):
        for x in range(160):
            key = (g[y][x], a[y + 8][x + 40])
            votes[key] = votes.get(key, 0) + 1
    fwd, back = {}, {}
    for (p, q), _ in sorted(votes.items(), key=lambda kv: -kv[1]):   # strongest pairs first, one-to-one
        if p not in fwd and q not in back:
            fwd[p], back[q] = q, p
    bad = [(x, y) for y in range(144) for x in range(160) if fwd.get(g[y][x]) != a[y + 8][x + 40]]
    if not bad:
        return True, f"{len(fwd)} colours"
    where = f"{len(bad)} px differ, first at {bad[0]}"
    return len(bad) <= tolerance, where


def cmd_verify(args):
    sys.path.insert(0, os.path.join(ROOT, "scripts"))
    import emucap_run
    with open(args.scenario) as f:
        scenario = json.load(f)
    try:
        saved = os.path.join(args.out, "original", "result.json")
        if args.reuse_original and os.path.exists(saved):  # the original is deterministic: iterate on the port only
            with open(saved) as f:
                orig = json.load(f)
        else:
            orig = emucap_run.run(os.path.abspath(args.rom), scenario, os.path.join(args.out, "original"))
        port = emucap_run.run(os.path.abspath(args.port), scenario, os.path.join(args.out, "port"),
                              **emucap_run.port_options(args.port))  # GB-time frames, GBA addresses
    except Exception as e:  # emucap/emulator failures are external-tool errors, not mismatches
        print(f"error: {e}", file=sys.stderr)
        return 6
    checks = []
    for label, want in orig["reads"].items():
        got = port["reads"].get(label)
        checks.append({"check": f"read:{label}", "ok": got == want, "original": want, "port": got})
    tolerance = scenario.get("shot_tolerance", 0)
    for label in orig["shots"]:
        ok, why = same_picture(os.path.join(args.out, "original", label + ".png"),
                               os.path.join(args.out, "port", label + ".png"), tolerance)
        checks.append({"check": f"shot:{label}", "ok": ok, "detail": why})
    ok = all(c["ok"] for c in checks)
    with open(os.path.join(args.out, "verify.json"), "w") as f:
        json.dump({"tool_version": TOOL_VERSION, "rom_sha256": orig["rom_sha256"], "port_sha256": port["rom_sha256"],
                   "scenario": os.path.basename(args.scenario), "status": "pass" if ok else "mismatch",
                   "checks": checks}, f, indent=2)
    for c in checks:
        print(f"{'ok  ' if c['ok'] else 'FAIL'} {c['check']}  {c.get('detail') or c['port']}")
    return 0 if ok else 8


def main(argv=None):
    p = argparse.ArgumentParser(prog="gb2gba")
    sub = p.add_subparsers(dest="cmd", required=True)
    i = sub.add_parser("inspect", help="parse ROM header, hashes, mapper")
    i.add_argument("rom")
    i.add_argument("--json", action="store_true")
    i.set_defaults(func=cmd_inspect)
    v = sub.add_parser("verify", help="run a scenario on the original and the port via emucap, compare")
    v.add_argument("rom")
    v.add_argument("port")
    v.add_argument("--scenario", required=True)
    v.add_argument("--out", required=True)
    v.add_argument("--reuse-original", action="store_true", help="reuse OUT/original/result.json from an earlier run")
    v.set_defaults(func=cmd_verify)
    for name, func in (("analyze", cmd_analyze), ("port", cmd_port)):
        c = sub.add_parser(name)
        c.add_argument("rom")
        c.add_argument("--no-scan", action="store_true", help="disable aggressive scan for unreferenced code")
        c.add_argument("--entry", action="append", help="extra entry point BB:AAAA (repeatable)")
        c.add_argument("--trace", help="gb-recompiled trace file of executed bank:addr entries")
        c.add_argument("--cdl", help="Mesen CDL of the original run (scripts/mesen_cdl.py): entries + data ranges")
        c.set_defaults(func=func)
        if name == "analyze":
            c.add_argument("--out", required=True)
        else:
            c.add_argument("--output", required=True)
            c.add_argument("--native-policy", choices=["strict"], default="strict",
                           help="strict: no interpreter, block on unresolved edges (hybrid is not implemented)")
            c.add_argument("--profile", action="store_true", help="link the sampling profiler (scripts/port_profile.py)")
            c.add_argument("--allow-unresolved", action="store_true",
                           help="research build: build despite unresolved edges (they trap at run time)")
    args = p.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
