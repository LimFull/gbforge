# gbforge

> A tool that statically analyzes and recompiles the GB CPU code in `.gb` / `.gbc` ROMs into a `.gba` that **runs natively on real GBA hardware**.

⚠️ **Status: Phase 4 (compatibility breadth) in progress.**
- Converts DMG and CGB games on ROM-only, MBC1, MBC2, MBC3 and MBC5 cartridges.
- Verified against the original on Dragon Quest I&II, Pokémon Yellow (title to save) and public test ROMs; see [Tested games](#tested-games).
- Commercial games still build as research builds (`--allow-unresolved`): code paths the analysis could not resolve stop the game with a trap screen instead of running.

---

## Quick start

**Download** the zip for your OS from [Releases](https://github.com/LimFull/gbforge/releases) (Windows x64, macOS Apple Silicon, Linux x64), unzip it and run `gbforge` in a terminal. Nothing else to install: the bundle carries its own GBA compiler.

```bash
gbforge inspect game.gbc                     # mapper, CGB flag, sizes
gbforge port game.gbc --output game.gba
```

On Windows run `gbforge.exe`; the bundle is not code-signed, so if SmartScreen stops it choose **More info → Run anyway**. The macOS bundle is signed and notarized. Downloading it in Terminal skips the Gatekeeper prompt:

```bash
curl -LO https://github.com/LimFull/gbforge/releases/download/v0.1.0/gbforge-0.1.0-macos-arm64.zip
unzip gbforge-0.1.0-macos-arm64.zip && cd gbforge-0.1.0-macos-arm64
```

After a browser download, macOS asks whether to open `gbforge` the first time it runs; choose **Open** (or run `xattr -dr com.apple.quarantine gbforge-0.1.0-macos-arm64` beforehand).

**From source** you need Python 3.12+, CMake, Ninja and a C++ compiler. `scripts/fetch_toolchain.py` downloads the pinned [Arm GNU Toolchain](https://developer.arm.com/downloads/-/arm-gnu-toolchain-downloads) (about 150 MB on macOS and Linux, 300 MB on Windows) and builds libtonc with it, both checked by SHA-256.

Recording a play-through and verifying need a little more:
- [emucap](https://github.com/mcpads/emucap) with its Mesen2 host (`adapters/mesen2/build.sh`)
- a GBA BIOS you own
- LimFull/emucap `d141eda` or later, if a GBA session runs longer than ~7,600 frames

```bash
git clone https://github.com/LimFull/gbforge.git && cd gbforge
scripts/fetch_toolchain.py                # GBA compiler + libtonc into build/toolchain
cmake -G Ninja -B build/core . && ninja -C build/core gbforge-core   # analyzer + code generator; fetches gb-recompiled (pinned)
cp local.env.example local.env            # paths to emucap, Mesen and the GBA BIOS (recording and verifying only)
scripts/setup-check.sh                    # reports what is missing

tools/gb2gba.py port tests/fixtures/tiny/tiny.gb --output out/tiny.gba   # the bundled test ROM
```

Run the result in mGBA:

| OS | Command |
|---|---|
| macOS | `open -a mGBA out/tiny.gba` (or `/Applications/mGBA.app/Contents/MacOS/mGBA out/tiny.gba`) |
| Linux | `mgba-qt out/tiny.gba` (Debian/Ubuntu package `mgba-qt`), or `flatpak run io.mgba.mGBA out/tiny.gba` |
| Windows | `"C:\Program Files\mGBA\mGBA.exe" out\tiny.gba`, or drag the `.gba` onto the mGBA window |

On real hardware, copy the `.gba` to a flash cart (EZ-Flash, Everdrive GBA, etc.). Games keep their save in the cart's SRAM: the ROM carries the `SRAM_V113` marker, so carts and emulators pick 32 KiB SRAM.

Then convert your own ROM by following [Converting a game](#converting-a-game). With a release bundle, run `gbforge` where the steps say `tools/gb2gba.py`; recording (steps 3–4) and verifying (step 7) need a source checkout and emucap. Static analysis alone rarely finds all of a commercial game's code, so record a play-through of the parts you want to run first.

## Converting a game

Put ROMs in `roms/`; git ignores that folder. Before running commands that drive an emulator (steps 3, 4 and 7), load the paths into the shell with `set -a; . ./local.env; set +a`.

1. **Inspect** the header: mapper, CGB flag, sizes. `port` stops with exit code 4 on a cartridge the runtime does not support.
   ```bash
   tools/gb2gba.py inspect roms/game.gbc
   ```
2. **Write a scenario**: the inputs to play and what to observe. It is a JSON list of steps:
   ```json
   {"steps": [
     {"step": 600},
     {"shot": "title"},
     {"tap": ["start"], "press": 2, "after": 120},
     {"read": "wram", "memory_type": "gameboyMemory", "address": 49152, "length": 16}
   ]}
   ```
   - `step` advances frames. `tap` holds buttons for `press` frames, then waits `after` frames. `shot` saves a screenshot, and `read` saves memory for comparison.
   - Other steps:
     - `write`: e.g. clear cart RAM so the game sees no save
     - `until`: wait for a memory value
     - `reset`
     - `inject`: checkpoints, see step 3
   - The full list is in `scripts/emucap_run.py`.
3. **Find a longer route** by playing the original in one live session.
   ```bash
   scripts/emucap_explore.py serve roms/game.gbc PREFIX.json build/route &
   scripts/emucap_explore.py send build/route a:2:60 right:8:30 wait:120 shot:here
   scripts/emucap_explore.py send build/route quit
   ```
   - `PREFIX.json` is a scenario the session plays before taking commands. Use `{"steps": []}` to start from power-on.
   - Commands:
     - `button:press:after` (buttons joined with `+`)
     - `wait:N`
     - `shot:NAME`, written to `build/route/NAME.png`
     - any scenario step as JSON
     - `quit`
   - Every command is appended to `build/route/steps.json`, which is itself a scenario.
   - **Checkpoints** (optional): `{"inject": {"at": PCs, "every": 180}}`. The port's timing is close to the original but not cycle-exact. In long routes, especially in games that run an RNG while idle, it drifts off by a frame or more. A checkpoint copies the original's whole state into the port and restarts it, every `every` frames.
     - The original stops *after* executing the instruction at `at`. Pick one whose next PC is a compiled entry outside interrupt handlers, in code the game runs every frame: a `CALL`, which lands on the callee, or a `RET`, which lands on a return site.
     - Used so far: `02E5` in Dragon Quest I&II, a `CALL` in its main loop. Pokémon Yellow uses `[381E, 1E6E]`: the `CALL Joypad` that starts `JoypadLowSensitivity`, and `DelayFrame`'s `RET`.
4. **Record coverage**: replay the scenario on the original with Mesen's Code/Data Logger. This writes which ROM bytes ran as code, and which instructions ran from RAM.
   ```bash
   scripts/scenario_cdl.py roms/game.gbc route.json build/game.cdl [--merge build/game.cdl]
   ```
5. **Port**.
   ```bash
   tools/gb2gba.py port roms/game.gbc --output out/game.gba --cdl build/game.cdl --allow-unresolved
   ```
   - Writes `out/game.gba`, `game.report.json` (analysis and unresolved paths) and `game.map.json` (addresses for the verifier).
   - `--colour gbc`, the default, adjusts CGB palettes so the darker GBA screen shows what the GBC screen showed. `--colour raw` keeps the values unchanged.
6. **Run** it in mGBA or on a flash cart. If the port reaches code it does not have, it shows a trap screen with the reason and `bank:address`. Extend the route to cover that part and repeat steps 4–5.
7. **Verify** against the original. This plays the same scenario on both and compares screenshots and memory reads.
   ```bash
   tools/gb2gba.py verify roms/game.gbc out/game.gba --scenario route.json --out build/verify/game [--reuse-original]
   ```
   Results go to `build/verify/game/{original,port}/*.png` and `verify.json`. `--reuse-original` skips replaying the original, which is deterministic.

## Compatibility

What the runtime covers today. A game that relies on something in the right-hand column stops with a trap screen or renders wrong.

| Area | Supported | Not yet |
|---|---|---|
| Cartridge | ROM only, MBC1, MBC2, MBC3 with its clock, MBC5; cart RAM up to 32 KiB, kept in GBA SRAM | MBC1 mode-1 banking at 0000-3FFF (≥ 1 MiB carts, multicarts); MBC6/7, HuC, camera; more than 32 KiB of cart RAM |
| Model | DMG, CGB (palettes, VRAM/WRAM banks, HDMA) | CGB double speed (runs at normal speed) |
| Video | BG, window, 8×8/8×16 sprites, palettes; mid-frame changes of LCDC, scroll and window position; sprites behind BG; DMG sprite order; 10 sprites per line | CGB tiles drawn over sprites (BG attribute bit 7); palette changes within a frame |
| Sound | All four channels, on the GBA's own GB sound hardware | — |
| CPU / timing | All documented SM83 opcodes, interrupts, timer, DIV, STAT/LYC, HALT, OAM DMA | Cycle-exact timing (close, but not instruction-exact) |
| Code in RAM | Code the game copies from ROM into RAM, found in the recorded run | Code generated or patched in RAM |
| Code coverage | Static analysis plus the code a recorded run executed (`--cdl`) | Paths neither found nor played: the strict build refuses them, `--allow-unresolved` turns them into traps |

### MBC1 / MBC3 checklist

MBC1 and MBC3 are implemented. Phase 4 still needs a commercial game on each, verified against the original.

**MBC1**
- [x] ROM bank: 5-bit low register (00 → 01) plus the 2-bit upper register, masked to the ROM size
- [x] RAM enable; RAM banks 0-3 in mode 1 (32 KiB)
- [x] mooneye `emulator-only/mbc1`: 10 of 13 match the original
- [ ] Mode 1 showing bank 20/40/60 at 0000-3FFF (≥ 1 MiB carts): `rom_8Mb`, `rom_16Mb`
- [ ] MBC1M multicart wiring: `multicart_rom_8Mb`
- [ ] A commercial MBC1 game verified against the original

**MBC3**
- [x] 7-bit ROM bank (00 → 01), RAM enable, RAM banks 0-3
- [x] Clock registers 08-0C (seconds, minutes, hours, day low, day high with halt and carry), latch on 00 → 01
- [x] Clock counts real time while the game runs, and is saved after cart RAM when it fits in GBA SRAM
- [ ] Time that passes while the console is off (needs a real-time clock on the GBA cart)
- [ ] Clock saved on 32 KiB RAM carts (cart RAM fills GBA SRAM; `port` warns)
- [ ] MBC30 (8 RAM banks)
- [ ] Any MBC3 test run against the original (mooneye has no MBC3 tests; the code has only been reviewed)
- [ ] A commercial MBC3 game verified against the original (Pokémon Red/Blue; Gold/Silver/Crystal for the clock)

## Tested games

Each one was ported, played with a scenario in `tests/scenarios/` and compared against the original. ROMs are not in this repository, except the redistributable test ROMs; SHA-256 prefixes identify the dumps used.

| Game | Cartridge | SHA-256 | Scenario | Result vs the original |
|---|---|---|---|---|
| tiny (own homebrew) | DMG, ROM only | `133d82836e17e149` | `tiny-diff.json` | ✅ All checks match; strict build |
| Dragon Quest I&II (Japan) | CGB, MBC5, 8 KiB RAM | `7cae9c90cf50e5b0` | `dq12-p3.json`: title → field → battle → save → continue | ✅ 24/25; the save matches. One mid-fade screen is a frame off |
| Pokémon Yellow (USA, Europe) | CGB-compatible, MBC5, 32 KiB RAM | `8cbaa499397e4f1a` | `pokeyellow-start.json`: title → new game → names → room → stairs → save | ✅ 29/40; walking, map changes, menus and the 32 KiB save match. 11 screens caught mid-text or mid-fade are a frame off |
| dmg-acid2 | DMG, ROM only | `464e14b7d42e7fee` | `dmg-acid2.json` | ✅ Matches |
| cgb-acid2 | CGB, ROM only | `197fb0bcec544f04` | `cgb-acid2.json` | ⚠️ 106 px off, in the eyes (CGB tiles over sprites) |
| mooneye `emulator-only/mbc1` (13) | MBC1 | mts-20260714 | `test-rom.json` | ⚠️ 10/13; mode-1 banking and multicart wiring not implemented |
| mooneye `emulator-only/mbc2` (7) | MBC2 | mts-20260714 | `test-rom.json` | ✅ 7/7 |
| mooneye `emulator-only/mbc5` (8) | MBC5, up to 8 MiB | mts-20260714 | `test-rom.json` | ✅ 8/8 |

DQ and Yellow are research builds (`--allow-unresolved`). No MBC1 or MBC3 commercial game has been tested yet. Details: [reports/phase3.md](reports/phase3.md), [reports/phase4.md](reports/phase4.md).

## Verification

How `gb2gba verify` compares a port with its original under emucap (Mesen2 for both GB/GBC and GBA):

- **Same start:** original runs clear GB RAM at power-on (Mesen randomizes it), as the port starts, so a scenario replays identically.
- **Same input moments:** the port is always paused exactly at a GB frame boundary, and it applies scheduled inputs itself, so inputs land at the same game moment however fast the port runs.
- **Screens:**
  - CGB games are compared down to colour values. Each emulator's LCD colour model is undone, so a palette-only difference fails.
  - Pixels that Mesen blended from two frames, as in the middle of a fade, count as differences.
  - DMG games are compared up to a one-to-one colour mapping.
  - `shot_tolerance` in a scenario allows a few pixels for blinking cursors.
- **Speed:** the emulator runs unthrottled, since scenarios count frames, not wall time. The Dragon Quest P3 comparison takes about 5.5 minutes. [reports/phase4.md §7.1](reports/phase4.md) has the details.

## Patching a port

Translations and other binary patches can be made against a port's `.gba`:

- The same gbforge version, ROM, CDL and options give the same `.gba` bytes on Windows, macOS and Linux; CI checks this on every push. `port` prints the SHA-256 (also `gba_sha256` in the report) for players to check before patching. Ship the CDL and the exact `port` command with the patch.
- A source checkout writes the symbol table next to the `.gba` (`game.sym`, `nm` format), so hooks can look up runtime symbols instead of hard-coding addresses.
- The runtime leaves these GBA resources alone:

  | Resource | Free for patches |
  |---|---|
  | EWRAM | Above `__end__` (in `game.sym`); the runtime has no heap |
  | OAM | Entries 40–127 (GB sprites use 0–39) |
  | OBJ tiles | 512 and up (`0x06014000`–) |
  | BG tiles | Char block 2 (`0x06008000`–`0x0600BFFF`), except on the trap screen |
  | Screen blocks | 31, except on the trap screen |
  | Palettes | BG 8–14, OBJ 8–15 |

- Release notes say when a release changes any of the above, or the names and layout of runtime symbols that hooks use.

## Exit codes

| Exit code | Meaning |
|---:|---|
| `0` | Passed |
| `2` | Invalid or unsupported ROM |
| `3` | Unresolved paths (the strict build refuses them) |
| `4` | Cartridge type not supported by the runtime |
| `5` | Code size / ROM / RAM budget exceeded |
| `6` | External tool error (GBA compiler, emucap, etc.) |
| `8` | Port differs from the original during verification |

## Roadmap

| Phase | Scope | Gate |
|---|---|---|
| **0** | Environment check — devkitARM, mGBA, `hello.gba`, `gb-recompiled` spike | `hello.gba` displays and takes input in mGBA |
| **1** | ROM Inspect + analysis — header/SHA-256/banks, CFG/IR, `inspect`/`analyze` JSON | Identical JSON on repeated runs, clear exit codes |
| **2** | Minimal strict native — registers/flags/memory/branches, starting with basic opcodes | Homebrew ROM runs as ARM code; missing paths error out |
| **3** | GBC compatibility — CGB, MBC5, PPU/DMA/timer/IRQ, saves | A commercial GBC game (DQ1&2) runs title → field → battle → save |
| **4** | Compatibility breadth — MBC1/2/3 (+ clock), MBC5 RAM banking, audio output, mid-frame register changes, BG/OBJ priority, 10 sprites per line | Public test ROMs (dmg-acid2, cgb-acid2) match the original; a second commercial game on MBC1 or MBC3 runs title → gameplay |
| **5** | Strict native — resolve dynamic dispatch (jump tables, RST tables, per-line handlers) statically or by annotation, so ports build without `--allow-unresolved`; run on real hardware | The validation games build strict and run on a flash cart |

## Development

```bash
python3 -m unittest discover -s tests     # host tests: inspect, far calls, RAM code, screen compare, colour table
set -a; . ./local.env; set +a
tools/gb2gba.py verify tests/fixtures/tiny/tiny.gb out/tiny.gba --scenario tests/scenarios/tiny-diff.json --out build/verify/tiny

scripts/fetch_test_roms.sh                # mooneye-test-suite into build/test-roms (pinned by SHA-256)
scripts/test_roms.py tests/fixtures/acid2/*.gb* build/test-roms/mts-*/emulator-only/mbc*/*.gb   # record, port, verify each

make -C tests/fixtures/tiny               # rebuild the test ROM (needs RGBDS)
```

Releases: pushing a `vX.Y.Z` tag (matching `TOOL_VERSION` in `tools/gb2gba.py`) makes CI build the Windows and Linux bundles into a draft release. The macOS bundle is built, signed and notarized on a Mac (`scripts/package.py --sign`, `NOTARY_PROFILE` in `local.env`), then added with `gh release upload vX.Y.Z dist/gbforge-*-macos-arm64.zip` before the draft is published.

`scripts/test_roms.py` keeps each original's recording, so a rerun only ports and plays the port again (`--fresh` redoes it).

## How it works

**Principles**
1. **Truly native** — Not a Goomba-style bundle of an emulator plus a ROM. GB code is translated ahead of time to C → ARM/Thumb, and there is no GB interpreter to fall back on. A runtime that reproduces the *semantics* of GB hardware is still required.
2. **Correctness first, then performance** — Each feature is checked against the original before it is optimized.
3. **Generic only** — No game-specific code or data: ROM analysis, recompilation and the GBA runtime work the same for every game. Game-specific work (wider screens, new art, translations) belongs in separate projects built on gbforge's output.

**Why a simple conversion doesn't work**

| | GB/GBC | GBA |
|---|---|---|
| CPU | SM83 (8-bit) | ARM7TDMI ~16.78 MHz, ARM/Thumb |
| Screen | 160×144 | 240×160 |
| RAM | Varies by model/banking | EWRAM 256 KiB + IWRAM 32 KiB, VRAM 96 KiB |
| ROM | MBC cartridge banking | Up to 32 MiB linear memory map |

The GBA's GB compatibility mode is a separate hardware mode. gbforge produces **a new ARM executable that runs in GBA mode**.

**Pipeline**

```text
.gb/.gbc (supplied locally by the user)
   │
   ├─ ROM Inspect ────── header, MBC, CGB flag, checksums, SHA-256
   ├─ Static Analyzer ── CFG, bank tracking, dynamic jumps, hardware register access
   │                     + coverage of a recorded play-through (Mesen CDL), code copied to RAM
   ├─ IR / Metadata ──── functions/edges keyed by (rom_bank, gb_pc), unresolved marked
   │
   ├─ Native Codegen (IR → C → arm-none-eabi-gcc)
   │
   ├─ GBA Runtime ────── GB CPU semantics / MBC / Timer / IRQ / PPU / APU / Input / Save
   │
   └─ out/game.gba ───── verify against the original (emucap) → mGBA → hardware (EZ-Flash, etc.)
```

- `gbforge-core` links gb-recompiled's analyzer and turns each analyzed GB function into a C function. `runtime-gba/gb.h` is the contract between generated code and runtime.
- `runtime-gba/gb.c` provides the GB memory map, cartridge, IO, interrupts, sound and display.
- GB VRAM writes go straight into GBA BG/OBJ hardware. Display registers are replayed line by line through HBlank DMA.
- The GB ROM is read in place from the GBA cartridge.

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
| [Arm GNU Toolchain](https://developer.arm.com/downloads/-/arm-gnu-toolchain-downloads) · [libtonc](https://github.com/gbadev-org/libtonc) | GBA cross-compilation (GCC, newlib) · GBA hardware library |

## Copyright / ROM policy

- This repository **does not contain ROMs, commercial game assets, or code generated from commercial ROMs.** Scenarios for commercial games hold only inputs, labels and addresses.
- It is a tool for converting ROMs the user legally owns, locally.
- Tests use only test ROMs licensed for redistribution: our own homebrew and the MIT-licensed acid2 ROMs (`tests/fixtures/acid2/LICENSE`).
