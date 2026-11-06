# gbforge

> A tool that statically analyzes and recompiles the GB CPU code in `.gb` / `.gbc` ROMs into a `.gba` that **runs natively on real GBA hardware**.

⚠️ **Status: Phase 3 (GBC compatibility) in progress.** The engine is generic: nothing in the analyzer, code generator or runtime is keyed to a particular game. Its hardware coverage so far is what the two validation targets need (see [Compatibility](#compatibility)):

- **Homebrew test ROM** (DMG, in this repo): runs as a strict native build and matches the original.
- **Dragon Quest I&II** (GBC, first commercial target): plays title → field → battle → save → continue on the GBA. Compared against the original from identical checkpoints, 22 of 25 checks match, with screens compared down to colour values. The remaining 3 come from a one-frame offset after LCD-off scene loads. It is still a research build: unresolved paths trap instead of using an interpreter.

Details: [reports/phase3.md](reports/phase3.md).

---

## Principles

1. **Truly native** — Not a Goomba-style bundle of an emulator plus a ROM. GB code is translated ahead of time to C → ARM/Thumb, and `strict` mode never silently falls back to a GB interpreter. A compatibility runtime that reproduces the *semantics* of GB hardware is still required.
2. **Correctness → measure performance → expand the screen** — Start with the minimum needed to run a public test ROM on the GBA.
3. **Generic only** — No game-specific code or data: ROM analysis, recompilation and the GBA runtime work the same for every game. Game-specific work (wider screens, new art, translations) belongs in separate projects built on gbforge's output.

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
   │                     + coverage of a recorded play-through (Mesen CDL)
   ├─ IR / Metadata ──── functions/edges keyed by (rom_bank, gb_pc), unresolved marked
   │
   ├─ Native Codegen (IR → C → devkitARM)
   │
   ├─ GBA Runtime ────── GB CPU semantics / MBC / Timer / IRQ / PPU / APU / Input / Save
   │
   └─ out/game.gba ───── verify against the original (emucap) → mGBA → hardware (EZ-Flash, etc.)
```

## Compatibility

What the runtime covers today. A game that uses something in the right-hand column will stop (trap) or render wrong until it is added.

| Area | Supported | Not yet |
|---|---|---|
| Cartridge | ROM only; MBC5 with one 8 KiB battery-backed RAM bank | MBC1/2/3 (incl. the MBC3 clock), MBC5 RAM banking. `port` refuses these with exit code 4 |
| Model | DMG, CGB (palettes, VRAM/WRAM banks, HDMA) | CGB double speed (runs at normal speed) |
| Video | BG, window, 8×8/8×16 sprites, DMG/CGB palettes, CGB colours adjusted for the GBA LCD (`--colour`) | BG priority bit, sprites behind BG, 10-sprites-per-line limit, mid-frame register changes (split status bars, wave effects) |
| Sound | Register read-back and channel status (sound drivers keep running) | Audio output (all ports are silent) |
| CPU / timing | All documented SM83 opcodes, interrupts, timer, DIV, STAT/LYC, HALT, OAM DMA | Cycle-exact interrupt entry. Timing is close to the original but not instruction-exact |
| Code in RAM | The standard HRAM OAM-DMA routine and one I/O-wait idiom, replaced by native hooks | Any other routine executed from RAM traps |
| Code coverage | Static analysis plus the code a recorded play-through executed (`--cdl`) | Paths neither found statically nor played are unresolved: the strict gate blocks the build, and `--allow-unresolved` builds them as traps |

Public test ROMs: `tests/fixtures/acid2` holds dmg-acid2 and cgb-acid2 (MIT). Both build and run, but their pictures do not match the original yet; they exercise the video gaps above. Run the conversion steps below on them, using `tests/scenarios/{dmg,cgb}-acid2.json` as the route.

For a new game, check the cartridge type first (`gb2gba inspect`; `port` stops with exit code 4 on an unsupported one) and expect to record a play-through that covers the parts of the game you want to run.

## Converting a game

ROMs go in `roms/` (gitignored). Commands that drive emulators need `set -a; . ./local.env; set +a` in the shell first.

1. **Inspect** the header: mapper, CGB flag, sizes.
   ```bash
   tools/gb2gba.py inspect roms/game.gbc
   ```
2. **Write a scenario**: the inputs to play and what to observe. It is a JSON list of steps:
   ```json
   {"steps": [
     {"step": 600},
     {"shot": "title"},
     {"tap": ["start"], "press": 2, "after": 120},
     {"read": "wram", "memory_type": "gameboyMemory", "address": 49152, "length": 16},
     {"reset": true}
   ]}
   ```
   Other steps: `until` (wait for a memory value), `write` (e.g. clear cart RAM), and `inject` (state checkpoints, see [Verification](#verification)). The full list is in `scripts/emucap_run.py`.
3. **Find a longer route** by playing the original in one live session. Every step sent is appended to `build/route/steps.json`, which is itself a scenario.
   ```bash
   scripts/emucap_explore.py serve roms/game.gbc PREFIX.json build/route &
   scripts/emucap_explore.py send build/route right:12:20 a:3:100 shot:here   # button:press:after
   scripts/emucap_explore.py send build/route quit
   ```
4. **Record coverage**: replay the scenario on the original with Mesen's Code/Data Logger.
   ```bash
   scripts/scenario_cdl.py roms/game.gbc route.json build/game.cdl [--merge build/game.cdl]
   ```
5. **Port**.
   ```bash
   tools/gb2gba.py port roms/game.gbc --output out/game.gba --cdl build/game.cdl [--allow-unresolved]
   ```
   Writes `out/game.gba`, `game.report.json` (analysis, unresolved edges) and `game.map.json` (addresses for the verifier). `--colour raw` keeps CGB palette values unchanged. The default `gbc` adjusts them so the darker GBA LCD shows what the GBC LCD showed.
6. **Run** it in mGBA or on a flash cart. If the port reaches code it does not have, it shows a trap screen with the reason and `bank:address`. Extend the route to cover it and repeat steps 4–5.
7. **Verify** against the original. This plays the same scenario on both and compares memory reads and screenshots.
   ```bash
   tools/gb2gba.py verify roms/game.gbc out/game.gba --scenario route.json --out build/verify/game [--reuse-original]
   ```

## Development

Requirements: git, CMake, Ninja, Python 3, devkitARM (native, or the `devkitpro/devkitarm` Docker image via `scripts/dkarm.sh`), [mGBA](https://mgba.io/), and [emucap](https://github.com/mcpads/emucap) with its Mesen2 host (`adapters/mesen2/build.sh`; GBA sessions longer than ~7,600 frames need the host fix in LimFull/emucap `d141eda`, and GBA needs a BIOS you own). [RGBDS](https://rgbds.gbdev.io/) is only needed to rebuild the test ROM.

```bash
cp local.env.example local.env            # set emucap / Mesen / BIOS paths
scripts/setup-check.sh                    # report tool versions, exit 1 if something is missing
cmake -G Ninja -B build/core . && ninja -C build/core gbforge-core   # analyzer + codegen (C++)
python3 -m unittest discover -s tests     # host tests (inspect golden, far calls, verify compare, colour table)

# end to end on the homebrew test ROM
tools/gb2gba.py analyze tests/fixtures/tiny/tiny.gb --out build/tiny
tools/gb2gba.py port tests/fixtures/tiny/tiny.gb --output out/tiny.gba
set -a; . ./local.env; set +a
tools/gb2gba.py verify tests/fixtures/tiny/tiny.gb out/tiny.gba --scenario tests/scenarios/tiny-diff.json --out build/verify/tiny

make -C tests/fixtures/tiny               # rebuild the test ROM (RGBDS)
scripts/dkarm.sh make -C examples/hello-gba
scripts/emucap_run.py examples/hello-gba/hello.gba tests/scenarios/hello-gba-smoke.json build/smoke/hello
scripts/mgba_peek.py examples/hello-gba/hello.gba 3 3000144:6   # mGBA cross-check via its GDB stub
```

How a port works: `gbforge-core` links gb-recompiled's analyzer and turns each analyzed GB function into a C function (`runtime-gba/gb.h` is the contract). `runtime-gba/gb.c` provides the GB memory map, MBC, IO and interrupts. It mirrors GB VRAM writes straight into GBA BG hardware and reads the GB ROM in place from the cartridge.

`emucap_run.py` drives emucap's Control MCP over stdio (`scripts/mcp_client.py`), so it also works outside an agent session.

## Verification

- **Determinism**: Mesen fills GB RAM with random values at power-on. Original runs clear WRAM, HRAM and wave RAM before the first instruction, as the port starts, so a scenario replays identically.
- **Checkpoints**: `{"inject": {"at": PC, "every": N}}`. On the original, the run stops at PC and saves the whole GB state. PC must be a compiled entry outside interrupt handlers, such as the main loop's idle wait. On the port, the next GB frame tick loads that state into `gb_inject` and restarts native code at PC. With `every`, this repeats before the first input after every N frames. Port timing is close to the original but not cycle-exact, and games that run an RNG in their idle loop (DQ1&2 does) drift apart within hundreds of frames, so checkpoints restart the comparison from identical state.
- **Screens**: Mesen applies different LCD colour models to GBC and GBA output. For CGB games, `verify` inverts both models and requires every port pixel to be the original pixel's colour value through the port's colour table, so palette-only differences fail. Pixels averaged by Mesen's frame blending (mid-fade) cannot be inverted and count as differences. DMG games are compared up to a one-to-one colour mapping. `shot_tolerance` in a scenario allows a few pixels for blink phases.

## Exit codes

| Exit code | Meaning |
|---:|---|
| `0` | Passed |
| `2` | Invalid or unsupported ROM |
| `3` | Unresolved instruction/bank/dynamic path (blocked in strict mode) |
| `4` | GB hardware feature not supported by the runtime (checked for the cartridge type; other features trap at run time) |
| `5` | Code size / ROM / RAM budget exceeded |
| `6` | External tool error (devkitARM, emucap, etc.) |
| `8` | State/screen mismatch against the original during verification |

`--native-policy strict` includes no interpreter. `hybrid` is a separate research build and does not count toward native results.

## Roadmap

| Phase | Scope | Gate |
|---|---|---|
| **0** | Environment check — devkitARM, mGBA, `hello.gba`, `gb-recompiled` spike | `hello.gba` displays and takes input in mGBA |
| **1** | ROM Inspect + analysis — header/SHA-256/banks, CFG/IR, `inspect`/`analyze` JSON | Identical JSON on repeated runs, clear exit codes |
| **2** | Minimal strict native — registers/flags/memory/branches, starting with basic opcodes | Homebrew ROM runs as ARM code; missing paths error out |
| **3** | GBC compatibility — CGB, MBC5, PPU/DMA/timer/IRQ, saves | A commercial GBC game (DQ1&2) runs title → field → battle → save |
| **4** | Compatibility breadth — MBC1/2/3 (+ clock), MBC5 RAM banking, audio output, mid-frame register changes, BG/OBJ priority, 10 sprites per line | Public test ROMs (dmg-acid2, cgb-acid2) match the original; a second commercial game on MBC1 or MBC3 runs title → gameplay |
| **5** | Strict native — resolve dynamic dispatch (jump tables, RST tables, per-line handlers) statically or by annotation, so ports build without `--allow-unresolved`; run on real hardware | The validation games build strict and run on a flash cart |

Priority order: `GBA hello world → ROM inspect → static codegen running on GBA → GB runtime → GBC compatibility → compatibility breadth → strict native`

## References

| Name | Use |
|---|---|
| [GB Recompiled](https://github.com/arcanite24/gb-recompiled) | SM83 analysis and static C generation (MIT; its SDL2 runtime can't be used on the GBA as-is) |
| [Dragon Warrior I & II Recomp](https://github.com/omegakatana92/Dragon-Warrior-I-II-Recomp) | Recompilation of DQ1&2, the first commercial validation target (not a GBA port) |
| [libtonc-template](https://github.com/gbadev-org/libtonc-template) | GBA C project skeleton |
| [Tonc](https://gbadev.net/tonc/) · [gbadoc](https://gbadev.net/gbadoc/) | GBA hardware documentation |
| [Pan Docs](https://gbdev.io/pandocs/) | Reference for GB hardware accuracy |
| [mgbdis](https://github.com/mattcurrie/mgbdis) | GB ROM disassembler |
| [mGBA](https://mgba.io/) · [SameBoy](https://github.com/LIJI32/SameBoy) | GBA run verification · GB reference comparison |
| [emucap](https://github.com/mcpads/emucap) | Agent/MCP control of Mesen2 for both GB/GBC and GBA — original ↔ port differential checks (GPL; used only as an external process) |
| [devkitPro / devkitARM](https://devkitpro.org/wiki/Getting_Started) | GBA cross-compilation |

## Copyright / ROM policy

- This repository **does not contain ROMs, commercial game assets, or code generated from commercial ROMs.** Scenarios for commercial games hold only inputs, labels and addresses.
- It is a tool for converting ROMs the user legally owns, locally.
- Tests use only test ROMs licensed for redistribution: our own homebrew and the MIT-licensed acid2 ROMs (`tests/fixtures/acid2/LICENSE`).
