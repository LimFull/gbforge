# gbforge

> A tool that statically analyzes and recompiles the GB CPU code in `.gb` / `.gbc` ROMs into a `.gba` that **runs natively on real GBA hardware**.

⚠️ **Status: Phase 3 in progress.** Our homebrew test ROM runs on the GBA as strict native code and matches the original. Dragon Quest I&II boots and its title → menus → name entry match the original under emucap. It runs as a research build: the strict gate is bypassed and unresolved paths trap instead of using an interpreter. Field, battle and save come next. See [reports/phase3.md](reports/phase3.md).

The first commercial validation target is **Dragon Warrior I & II / Dragon Quest I & II (GB/GBC)**. Per-game plugins will later widen the view to the GBA's native 240×160 and improve tiles, palettes, and UI.

---

## Principles

1. **Truly native** — Not a Goomba-style bundle of an emulator plus a ROM. GB code is translated ahead of time to C → ARM/Thumb, and `strict` mode never silently falls back to a GB interpreter. A compatibility runtime that reproduces the *semantics* of GB hardware is still required.
2. **Correctness → measure performance → expand the screen** — Start with the minimum needed to run a public test ROM on the GBA.
3. **Separate the generic engine from game-specific mods** — ROM analysis, recompilation, and the GBA runtime are shared code; game-specific addresses, camera, UI, and art live in plugins.

## Why a simple conversion doesn't work

| | GB/GBC | GBA |
|---|---|---|
| CPU | SM83 (8-bit) | ARM7TDMI ~16.78 MHz, ARM/Thumb |
| Screen | 160×144 | 240×160 |
| RAM | Varies by model/banking | EWRAM 256 KiB + IWRAM 32 KiB, VRAM 96 KiB |
| ROM | MBC cartridge banking | Up to 32 MiB linear memory map |

The GBA's GB compatibility mode is a separate hardware mode. This project produces **a new ARM executable that runs in GBA mode**.

## Architecture

```text
.gb/.gbc (supplied locally by the user)
   │
   ├─ ROM Inspect ────── header, MBC, CGB flag, checksums, SHA-256
   ├─ Static Analyzer ── CFG, bank tracking, dynamic jumps, hardware register access
   ├─ IR / Metadata ──── functions/edges keyed by (rom_bank, gb_pc), unresolved marked
   │
   ├─ Native Codegen (IR → C → devkitARM)
   ├─ Asset / Map Analyzer (tiles, palettes, text, maps)
   │
   ├─ GBA Runtime ────── GB CPU semantics / MBC / Timer / IRQ / PPU / APU / Input / Save
   ├─ Game Plugin ────── applied only to ROMs with a matching SHA-256 (fail closed)
   │
   └─ output/game.gba ── verify in mGBA → verify on hardware (EZ-Flash, etc.)
```

## Development

Requirements: git, CMake, Ninja, Python 3, [RGBDS](https://rgbds.gbdev.io/), devkitARM (native, or the `devkitpro/devkitarm` Docker image via `scripts/dkarm.sh`), [mGBA](https://mgba.io/), and [emucap](https://github.com/mcpads/emucap) with its Mesen2 host (`adapters/mesen2/build.sh`; GBA sessions longer than ~7,600 frames need the host fix in LimFull/emucap `d141eda`, and GBA needs a BIOS you own).

```bash
cp local.env.example local.env            # set emucap / Mesen / BIOS paths
scripts/setup-check.sh                    # report tool versions, exit 1 if something is missing

python3 -m unittest discover -s tests     # host tests (inspect golden, checksum errors, verify compare)
tools/gb2gba.py inspect rom.gbc [--json]  # header, mapper, sizes, SHA-256

cmake -G Ninja -B build/core . && ninja -C build/core gbforge-core   # analyzer + codegen (C++)
tools/gb2gba.py analyze tests/fixtures/tiny/tiny.gb --out build/tiny
tools/gb2gba.py port tests/fixtures/tiny/tiny.gb --output out/tiny.gba

make -C tests/fixtures/tiny               # rebuild our own test ROM (RGBDS)
scripts/dkarm.sh make -C examples/hello-gba

set -a; . ./local.env; set +a
scripts/emucap_run.py tests/fixtures/tiny/tiny.gb tests/scenarios/tiny-smoke.json build/smoke/tiny
scripts/emucap_run.py examples/hello-gba/hello.gba tests/scenarios/hello-gba-smoke.json build/smoke/hello
scripts/mgba_peek.py examples/hello-gba/hello.gba 3 3000144:6   # mGBA cross-check via its GDB stub
tools/gb2gba.py verify tests/fixtures/tiny/tiny.gb out/tiny.gba --scenario tests/scenarios/tiny-diff.json --out build/verify/tiny

# find an input route on the original in one live session; build/route/steps.json becomes a scenario
scripts/emucap_explore.py serve roms/game.gbc PREFIX.json build/route &
scripts/emucap_explore.py send build/route right:12:20 a:3:100 shot:here
scripts/scenario_cdl.py roms/game.gbc SCENARIO.json build/game.cdl --merge build/game.cdl   # coverage for `port --cdl`
```

How a port works: `gbforge-core` links gb-recompiled's analyzer and turns each analyzed GB function into a C function (`runtime-gba/gb.h` is the contract). `runtime-gba/gb.c` provides the GB memory map, MBC, IO and interrupts. It mirrors GB VRAM writes straight into GBA BG hardware and reads the GB ROM in place from the cartridge.

`emucap_run.py` drives emucap's Control MCP over stdio (`scripts/mcp_client.py`), so it also works outside an agent session.

## Target CLI (`--plugin`, `--renderer enhanced`, `--viewport`, `--graphics` not implemented yet)

```bash
gb2gba inspect roms/game.gbc --json > reports/game.inspect.json
gb2gba analyze roms/game.gbc --out reports/game

gb2gba port roms/homebrew.gb \
  --target gba --native-policy strict \
  --renderer compat --output out/homebrew.gba

gb2gba port roms/dragon-warrior-i-ii.gbc \
  --target gba --native-policy strict \
  --plugin dragon-warrior-i-ii \
  --renderer enhanced --viewport 240x160 \
  --graphics custom --output out/dq12-enhanced.gba

gb2gba verify roms/homebrew.gb out/homebrew.gba \
  --scenario tests/scenarios/homebrew-idle.json --out reports/verification
```

| Exit code | Meaning |
|---:|---|
| `0` | Passed |
| `2` | Invalid or unsupported ROM |
| `3` | Unresolved instruction/bank/dynamic path (blocked in strict mode) |
| `4` | GB hardware feature not supported by the runtime |
| `5` | Code size / ROM / RAM budget exceeded |
| `6` | External tool error (devkitARM, mGBA, etc.) |
| `7` | Plugin ROM fingerprint mismatch |
| `8` | State/screen mismatch against the original during verification |

`--native-policy strict` includes no interpreter. `hybrid` is a separate research build and does not count toward native results.

## Roadmap

| Phase | Scope | Gate |
|---|---|---|
| **0** | Environment check — devkitARM, mGBA, `hello.gba`, `gb-recompiled` spike | `hello.gba` displays and takes input in mGBA |
| **1** | ROM Inspect + analysis — header/SHA-256/banks, CFG/IR, `inspect`/`analyze` JSON | Identical JSON on repeated runs, clear exit codes |
| **2** | Minimal strict native — registers/flags/memory/branches, starting with basic opcodes | Homebrew ROM runs as ARM code; missing paths error out |
| **3** | GBC/RPG compatibility — CGB, MBC5, PPU/DMA/timer/IRQ, saves | DQ title → field → battle → save (compat) |
| **4** | Expanded screen — 240×160 camera, map streaming, BG/DMA optimization | Wider world renders correctly on 3+ field maps |
| **5** | Graphics upgrade — palette/tile/sprite replacement, UI relayout, 60 Hz | Reproducible build using only self-made art |

Priority order: `GBA hello world → ROM inspect → static codegen running on GBA → GB runtime → DQ compat → 240×160 expansion → graphics upgrade`

## Screen expansion (Dragon Quest plugin)

The original screen is **not scaled up**; more tiles are drawn from the same world space.

- Keep 8×8 tiles: 20×18 → 30×20 tiles (~+67%)
- World-coordinate camera → GBA BG scroll registers
- Off-screen tiles are decoded from the original map data and streamed via a ring buffer / VBlank DMA
- Dialogue, battle, and menus get per-scene viewport profiles; unsupported scenes fall back to compat
- Graphics upgrades start with tile *color and detail*. Converting to 4bpp alone doesn't improve the art — **new art is required**.

## References

| Name | Use |
|---|---|
| [GB Recompiled](https://github.com/arcanite24/gb-recompiled) | SM83 analysis and static C generation (MIT; its SDL2 runtime can't be used on the GBA as-is) |
| [Dragon Warrior I & II Recomp](https://github.com/omegakatana92/Dragon-Warrior-I-II-Recomp) | Recompilation reference for the same game (not a GBA port) |
| [libtonc-template](https://github.com/gbadev-org/libtonc-template) | GBA C project skeleton |
| [Tonc](https://gbadev.net/tonc/) · [gbadoc](https://gbadev.net/gbadoc/) | GBA hardware documentation |
| [Pan Docs](https://gbdev.io/pandocs/) | Reference for GB hardware accuracy |
| [mgbdis](https://github.com/mattcurrie/mgbdis) | GB ROM disassembler |
| [mGBA](https://mgba.io/) · [SameBoy](https://github.com/LIJI32/SameBoy) | GBA run verification · GB reference comparison |
| [emucap](https://github.com/mcpads/emucap) | Agent/MCP control of Mesen2 for both GB/GBC and GBA — original ↔ port differential checks (GPL; used only as an external process) |
| [devkitPro / devkitARM](https://devkitpro.org/wiki/Getting_Started) | GBA cross-compilation |

## Copyright / ROM policy

- This repository **does not contain ROMs, commercial game assets, or code generated from commercial ROMs.**
- It is a tool for converting ROMs the user legally owns, locally.
- Tests use only homebrew ROMs licensed for redistribution.
- Game plugins apply only to ROMs with an exact SHA-256 match and never patch other revisions with unverified addresses.
