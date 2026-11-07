#!/usr/bin/env python3
"""gb2gba — GB/GBC → GBA native porter CLI.

  gb2gba inspect ROM [--json]
  gb2gba analyze ROM --out DIR [--no-scan] [--entry BB:AAAA]... [--trace FILE] [--cdl FILE]
  gb2gba port ROM --output OUT.gba [--native-policy strict] [--no-scan] [--entry BB:AAAA]... [--cdl FILE] [--colour gbc|raw]
  gb2gba verify ROM PORT.gba --scenario SCENARIO.json --out DIR [--reuse-original]   (needs emucap)

Exit codes: 0 ok, 2 invalid ROM, 3 unresolved paths (strict), 4 cartridge not supported by the runtime,
5 over budget, 6 external tool error, 8 port differs from the original.
"""
import argparse
import glob
import hashlib
import json
import os
import re
import shutil
import struct
import subprocess
import sys
import tempfile
import zlib
from concurrent.futures import ThreadPoolExecutor

FROZEN = getattr(sys, "frozen", False)  # release bundle (scripts/package.py): tools sit next to the executable
ROOT = os.path.dirname(sys.executable) if FROZEN else os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
EXE = ".exe" if os.name == "nt" else ""
CORE = os.environ.get("GBFORGE_CORE", os.path.join(ROOT, *([] if FROZEN else ["build"]), "core", "gbforge-core" + EXE))
TOOLCHAIN = os.environ.get("GBFORGE_TOOLCHAIN", os.path.join(ROOT, *([] if FROZEN else ["build"]), "toolchain"))
WORK = os.path.join(tempfile.gettempdir(), "gbforge") if FROZEN else os.path.join(ROOT, "build", "port")  # port's generated C, objects
GBA_ROM_LIMIT = 32 << 20

TOOL_VERSION = "0.1.0"

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


def arm_tool(name):
    """A binary of the Arm GNU Toolchain fetched by scripts/fetch_toolchain.py."""
    path = os.path.join(TOOLCHAIN, "arm", "bin", f"arm-none-eabi-{name}{EXE}")
    if not os.path.exists(path):
        raise SystemExit(f"{path} missing: run scripts/fetch_toolchain.py")
    return path


def build_gba(gen, out, base, title, cflags):
    """Compile runtime + generated code and link OUT.gba, with BASE.elf/.map/.sym. True on success."""
    rt, tonc = os.path.join(ROOT, "runtime-gba"), os.path.join(TOOLCHAIN, "tonc")
    gcc, objdir = arm_tool("gcc"), os.path.join(os.path.dirname(gen), "obj")
    shutil.rmtree(objdir, ignore_errors=True)
    os.makedirs(objdir)
    cc = [gcc, "-mthumb", "-mcpu=arm7tdmi", "-O2", "-g", "-Wall", "-Wno-unused-label", "-ffunction-sections",
          "-I", rt, "-I", os.path.join(tonc, "include"), *cflags, "-c"]
    srcs = [os.path.join(rt, "gb.c"), os.path.join(rt, "crt0.s"), *sorted(glob.glob(os.path.join(gen, "*.c"))),
            os.path.join(gen, "rom.s")]
    objs = [os.path.join(objdir, os.path.basename(s) + ".o") for s in srcs]
    # cwd=gen: rom.s .incbin's its neighbours by relative name
    with ThreadPoolExecutor(os.cpu_count()) as ex:
        if any(r.returncode for r in ex.map(lambda so: subprocess.run([*cc, so[0], "-o", so[1]], cwd=gen), zip(srcs, objs))):
            return False
    elf = base + ".elf"
    rsp = os.path.join(objdir, "objs.rsp")  # Windows: the object list can exceed the command-line limit
    with open(rsp, "w") as f:
        f.write("\n".join('"' + o.replace("\\", "/") + '"' for o in objs))
    link = [gcc, "-mthumb", "-mcpu=arm7tdmi", "-nostartfiles", "-T", os.path.join(rt, "gba.ld"), "@" + rsp,
            "-L", tonc, "-ltonc", "-Wl,--gc-sections,--no-warn-rwx-segments,-Map," + base + ".map", "-o", elf]
    if subprocess.run(link).returncode or subprocess.run([arm_tool("objcopy"), "-O", "binary", elf, out]).returncode:
        return False
    with open(out, "r+b") as f:  # header: title, then the complement check over 0xA0-0xBC
        f.seek(0xA0)
        f.write(title.encode().ljust(12, b"\0"))
        f.seek(0xA0)
        f.write(bytes([(-sum(f.read(0x1D)) - 0x19) & 0xFF]))
    with open(base + ".sym", "w") as f:
        return subprocess.run([arm_tool("nm"), elf], stdout=f).returncode == 0


def runtime_gaps(header):
    """Cartridge features the GBA runtime does not implement yet (README: Compatibility)."""
    cart = header["cartridge_type"]
    if cart["mapper"] not in ("ROM", "MBC1", "MBC2", "MBC3", "MBC5"):
        return [f"{cart['name']} cartridge: the runtime has ROM-only and MBC1/2/3/5"]
    if header["ram_size"] > 32 << 10:
        return [f"{header['ram_size'] >> 10} KiB cart RAM: GBA SRAM holds 32 KiB"]
    return []


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


# SM83 instruction lengths: 3 for 16-bit operands, 2 for 8-bit operands and CB/STOP, else 1.
OP3 = {0x01, 0x11, 0x21, 0x31, 0x08, 0xC2, 0xC3, 0xC4, 0xCA, 0xCC, 0xCD, 0xD2, 0xD4, 0xDA, 0xDC, 0xEA, 0xFA}
OP2 = {0x06, 0x0E, 0x16, 0x1E, 0x26, 0x2E, 0x36, 0x3E, 0xC6, 0xCE, 0xD6, 0xDE, 0xE6, 0xEE, 0xF6, 0xFE,
       0x18, 0x20, 0x28, 0x30, 0x38, 0xE0, 0xF0, 0xE8, 0xF8, 0xCB, 0x10}


def ram_overlays(rom, ram_log):
    """Code the original executed from RAM (OUT.cdl.ram, see scripts/mesen_cdl.lua), traced back to
    the ROM bytes it was copied from: [(ram_addr, rom_offset, size)], RAM addresses not found, and
    the executed instructions inside found code (entry points: the analyzer does not follow calls
    into RAM). Executed instructions are joined into runs; each run's bytes are looked up in the ROM.
    ponytail: one copy per RAM address (the first seen); code patched after copying is not found."""
    seen = {}
    with open(ram_log) as f:
        for line in f.read().split():
            if len(line) == 4:
                pc = int(line, 16)
            else:
                seen.setdefault(pc, bytes.fromhex(line))
    runs, found, missing, entries = [], [], [], []
    for pc in sorted(seen):
        size = 3 if seen[pc][0] in OP3 else 2 if seen[pc][0] in OP2 else 1
        if runs and pc <= runs[-1][1]:
            runs[-1][1] = max(runs[-1][1], pc + size)
        else:
            runs.append([pc, pc + size])
    for start, end in runs:
        code = bytearray(end - start)
        for pc in range(start, end):
            if pc in seen:
                code[pc - start:pc - start + 4] = seen[pc][:end - pc]
        at = rom.find(bytes(code))
        if at < 0:
            missing.append(start)
            continue
        found.append((start, at, end - start))
        entries += [pc for pc in seen if start <= pc < end]
    return found, missing, sorted(entries)


def analysis_args(args, rom):
    out = ["--no-scan"] if args.no_scan else []
    for e in (args.entry or []) + far_call_entries(rom):
        out += ["--entry", e]
    if args.trace:
        out += ["--trace", args.trace]
    if args.cdl:
        out += ["--cdl", args.cdl]
        if os.path.exists(args.cdl + ".ram"):
            found, _, entries = ram_overlays(rom, args.cdl + ".ram")
            for ram, at, size in found:
                bank, off = at // 0x4000, at % 0x4000 + (0x4000 if at >= 0x4000 else 0)
                out += ["--overlay", f"{ram:04X}:{bank:02X}:{off:04X}:{size:X}"]
            for pc in entries:
                out += ["--entry", f"00:{pc:04X}"]
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
            "pace": {"cyc": f"0x{syms['gb'] + 28:08X}", "ppu_acc": f"0x{syms['ppu_acc']:08X}",
                     "ppu_on_at": f"0x{syms['ppu_on_at']:08X}", "lcdc": f"0x{syms['io'] + 0x40:08X}",
                     "pace_next": f"0x{syms['gb_pace_next']:08X}"},  # when the GB frame count ticks
            "inject": f"0x{syms['gb_inject']:08X}",         # struct GbInject (runtime-gba/gb.c)
            "keys": f"0x{syms['gb_keys']:08X}",             # u16 KEYINPUT override, 0xFFFF = none
            "script": f"0x{syms['gb_script']:08X}",         # {u32 GB frame, u32 keys}[32]: gb_keys by frame
            "colour": f"0x{syms['gb_colour']:08X}",         # u16[0x8000] CGB rgb555 -> GBA palette value
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
    work = os.path.join(WORK, stem)
    aux = os.path.join(work, stem) if FROZEN else os.path.splitext(out)[0]  # .elf/.map/.sym/.map.json (verify, scripts)
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

    gaps = runtime_gaps(report["rom_header"])
    if gaps:
        print("unsupported: " + "; ".join(gaps), file=sys.stderr)
        return finish(4, errors=gaps)
    if report["rom_header"]["cartridge_type"]["timer"] and report["rom_header"]["ram_size"] >= 32 << 10:
        report.setdefault("warnings", []).append("MBC3 clock is not saved: 32 KiB cart RAM fills GBA SRAM")
    report["far_call_entries"] = len(far_call_entries(rom))
    found, missing, _ = ram_overlays(rom, args.cdl + ".ram") if args.cdl and os.path.exists(args.cdl + ".ram") else ([], [], [])
    report["ram_code"] = {"compiled": [f"{r:04X} <- {a:06X} ({n} bytes)" for r, a, n in found],
                          "not_in_rom": [f"{r:04X}" for r in missing]}
    os.makedirs(gen, exist_ok=True)
    with open(os.path.join(gen, "overlays.c"), "w") as f:  # runtime-gba: RAM code compiled from ROM
        f.write('#include "gb.h"\nconst GbOverlay gb_overlays[] = {\n'
                + "".join(f"    {{0x{r:04X}, 0x{a:06X}, {n}}},\n" for r, a, n in found)
                + f"    {{0, 0, 0}}}};\nconst unsigned gb_overlay_count = {len(found)};\n")
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
    report["colour"] = args.colour
    table = colour_table() if args.colour == "gbc" else range(0x8000)
    with open(os.path.join(gen, "colour.bin"), "wb") as f:
        f.write(struct.pack("<32768H", *table))
    with open(os.path.join(gen, "rom.s"), "w") as f:
        f.write('.section .rodata\n.global gb_rom\n.balign 4\ngb_rom:\n.incbin "rom.gb"\n'
                '.global gb_colour\n.balign 2\ngb_colour:\n.incbin "colour.bin"\n')
    title = re.sub(r"[^A-Z0-9]", "", report["rom_header"]["title"].upper())[:12] or "GBFORGE"
    if args.profile:
        report.setdefault("warnings", []).append("profiling build (timer IRQ sampler)")
    for stale in (out, aux + ".map.json"):
        if os.path.exists(stale):
            os.remove(stale)  # never leave a previous build behind a failed one
    if not build_gba(gen, out, aux, title, ["-DGB_PROFILE"] if args.profile else []):
        return finish(6, errors=["GBA build failed"])
    size = os.path.getsize(out)
    report["build_sizes"] = {"gba_bytes": size}
    if size > GBA_ROM_LIMIT:
        return finish(5, errors=[f"{size} bytes exceeds the 32 MiB GBA ROM limit"])
    write_map(aux + ".sym", aux + ".map.json", report["rom_header"])
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


# Mesen's LCD models, rgb555 -> displayed RGB: GbDefaultVideoFilter (GbcAdjustColors) and
# GbaDefaultVideoFilter (GbaAdjustColors, the Ares formula), both on by default. The GBA LCD
# shows the same rgb555 much darker and more saturated than the GBC LCD.
def gbc_lcd(v):
    r, g, b = v & 31, v >> 5 & 31, v >> 10 & 31
    return (min(240, (r * 26 + g * 4 + b * 2) >> 2), min(240, (g * 24 + b * 8) >> 2),
            min(240, (r * 6 + g * 4 + b * 22) >> 2))


def gba_lcd(v):
    lr, lg, lb = (((v >> s & 31) / 31.0) ** 4.0 for s in (0, 5, 10))
    out = lambda m: int(min((m / 255) ** (1 / 2.2) * 0xFFFF / 280, 255))  # noqa: E731
    return out(0 * lb + 50 * lg + 240 * lr), out(30 * lb + 230 * lg + 10 * lr), out(220 * lb + 10 * lg + 50 * lr)


MIX_INV = ((50300, -11000, 1500), (-700, 52800, -7200), (-11400, 100, 54700))  # gba_lcd's mixing, inverted (/12037000)


def colour_table():
    """rgb555 -> the rgb555 that looks on the GBA LCD as the original looks on the GBC LCD (nearest RGB).
    First guess by inverting gba_lcd, then hill-climb; within 1/255 of the exhaustive optimum."""
    shown = [gba_lcd(v) for v in range(0x8000)]
    table = []
    for v in range(0x8000):
        t = gbc_lcd(v)
        mixed = [(c * 280 / 0xFFFF) ** 2.2 * 255 for c in t]
        lin = [max(0.0, min(1.0, sum(k * m for k, m in zip(row, mixed)) / 12037000)) for row in MIX_INV]
        cur = sum(round(31 * x ** 0.25) << s for x, s in zip(lin, (0, 5, 10)))
        err = lambda w: sum((p - q) ** 2 for p, q in zip(shown[w], t))  # noqa: E731
        while True:
            r, g, b = cur & 31, cur >> 5 & 31, cur >> 10
            near = [r2 | g2 << 5 | b2 << 10 for r2 in (r - 1, r, r + 1) for g2 in (g - 1, g, g + 1)
                    for b2 in (b - 1, b, b + 1) if 0 <= r2 < 32 and 0 <= g2 < 32 and 0 <= b2 < 32]
            best = min(near, key=lambda w: (err(w), w != cur))
            if best == cur:
                break
            cur = best
        table.append(cur)
    return table


def port_colours(rom_path, port_path):
    """Original screen colour -> the port screen colours it may show as, or None (DMG: Mesen
    draws its own DMG palette, compare up to a colour mapping instead)."""
    with open(rom_path, "rb") as f:
        if not f.read(0x144)[0x143] & 0x80:
            return None
    with open(os.path.splitext(port_path)[0] + ".map.json") as f:
        addr = int(json.load(f)["memory"]["colour"], 16) - 0x08000000
    with open(port_path, "rb") as f:
        table = struct.unpack("<32768H", f.read()[addr:addr + 0x10000])
    expect = {}
    for v in range(0x8000):
        expect.setdefault(gbc_lcd(v), set()).add(gba_lcd(table[v]))
    return expect


def same_picture(gb_png, gba_png, tolerance=0, expect=None):
    """GB 160x144 vs the port's letterboxed 240x160 at (40,8). With `expect` (port_colours) each
    port pixel must show the original pixel's rgb555 through the port's colour table; without it
    the pictures need only be equal up to a one-to-one colour mapping. Disagreeing pixels are
    counted; up to `tolerance` of them (blink phases) still pass.
    ponytail: Mesen's frame blending (BlendFrames, on by default) averages two frames, so a fade
    caught mid-way fails the exact check even in phase; map blended pixels if a scenario needs it."""
    gw, gh, g = read_png(gb_png)
    aw, ah, a = read_png(gba_png)
    if (gw, gh) != (160, 144) or (aw, ah) != (240, 160):
        return False, f"unexpected sizes {gw}x{gh} / {aw}x{ah}"
    if expect is not None:
        bad = [(x, y) for y in range(144) for x in range(160) if a[y + 8][x + 40] not in expect.get(g[y][x], ())]
        return len(bad) <= tolerance, f"{len(bad)} px differ, first at {bad[0]}" if bad else "exact colours"
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
    if FROZEN:
        raise SystemExit("verify needs a source checkout and emucap (README: Quick start)")
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
        inject = [os.path.join(args.out, "original", f) for f in orig.get("inject", [])]  # captured states
        port = emucap_run.run(os.path.abspath(args.port), scenario, os.path.join(args.out, "port"),
                              inject_states=inject, **emucap_run.port_options(args.port))
    except Exception as e:  # emucap/emulator failures are external-tool errors, not mismatches
        print(f"error: {e}", file=sys.stderr)
        return 6
    checks = []
    for label, want in orig["reads"].items():
        got = port["reads"].get(label)
        checks.append({"check": f"read:{label}", "ok": got == want, "original": want, "port": got})
    tolerance = scenario.get("shot_tolerance", 0)
    expect = port_colours(args.rom, args.port)
    for label in orig["shots"]:
        ok, why = same_picture(os.path.join(args.out, "original", label + ".png"),
                               os.path.join(args.out, "port", label + ".png"), tolerance, expect)
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
            c.add_argument("--colour", choices=["gbc", "raw"], default="gbc",
                           help="gbc: CGB palettes adjusted to look on the GBA LCD as on the GBC LCD; raw: unchanged")
            c.add_argument("--allow-unresolved", action="store_true",
                           help="research build: build despite unresolved edges (they trap at run time)")
    args = p.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
