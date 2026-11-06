#!/usr/bin/env python3
"""Port test ROMs and verify each against its original: CDL of the scenario run, port, verify.

usage: scripts/test_roms.py [--scenario S.json] [--out DIR] [--fresh] ROM...
Prints one line per ROM: "ok", or the step that failed with its last output line. The original's
CDL and screenshots are kept between runs (the original is deterministic), so a rerun only ports
and plays the port again; --fresh redoes them. Needs local.env (emucap). Mooneye ROMs:
scripts/fetch_test_roms.sh.
"""
import argparse
import os
import subprocess
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def main():
    p = argparse.ArgumentParser()
    p.add_argument("roms", nargs="+")
    p.add_argument("--scenario", default=os.path.join(ROOT, "tests/scenarios/test-rom.json"))
    p.add_argument("--out", default=os.path.join(ROOT, "build/test-roms/results"))
    p.add_argument("--fresh", action="store_true", help="record the original again")
    a = p.parse_args()
    os.makedirs(a.out, exist_ok=True)
    failed = 0
    for rom in map(os.path.abspath, a.roms):
        name = os.path.basename(os.path.dirname(rom)) + "-" + os.path.splitext(os.path.basename(rom))[0]
        w = os.path.join(a.out, name)
        keep = not a.fresh and os.path.exists(os.path.join(w, "original", "result.json"))
        steps = [("cdl", ["scripts/scenario_cdl.py", rom, a.scenario, w + ".cdl"])] if not keep else []
        steps += [("port", ["tools/gb2gba.py", "port", rom, "--output", w + ".gba", "--no-scan", "--cdl", w + ".cdl",
                            "--allow-unresolved"]),
                  ("verify", ["tools/gb2gba.py", "verify", rom, w + ".gba", "--scenario", a.scenario, "--out", w]
                   + (["--reuse-original"] if keep else []))]
        for label, cmd in steps:
            r = subprocess.run([sys.executable, *cmd], cwd=ROOT, capture_output=True, text=True)
            if r.returncode:
                last = ((r.stdout + r.stderr).strip().splitlines() or [""])[-1][:120]
                print(f"{name:32} {label} failed ({r.returncode}): {last}", flush=True)
                failed += 1
                break
        else:
            print(f"{name:32} ok", flush=True)
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
