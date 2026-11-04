#!/usr/bin/env python3
"""Record Mesen's Code/Data Logger for a GB/GBC ROM played headless with scripted input.

usage: scripts/mesen_cdl.py ROM OUT.cdl --frames N [--inputs "700:start:3;800:a:3"] [--merge OLD.cdl]
The CDL (1 flag byte per ROM byte) feeds `gb2gba analyze|port --cdl`. Needs MESEN_BIN (local.env);
Mesen runs from a private copy in build/mesen-cdl so the emucap package stays untouched.
"""
import argparse
import os
import shutil
import subprocess

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SETTINGS = '{"Debug": {"ScriptWindow": {"AllowIoOsAccess": true, "ScriptTimeout": 600}}, "Preferences": {"SingleInstance": false}}'


def main():
    p = argparse.ArgumentParser()
    p.add_argument("rom")
    p.add_argument("out")
    p.add_argument("--frames", type=int, required=True)
    p.add_argument("--inputs", default="")
    p.add_argument("--merge", help="OR the result into this existing CDL")
    a = p.parse_args()
    mesen = os.environ.get("MESEN_BIN")
    if not mesen:
        raise SystemExit("MESEN_BIN not set (see local.env.example)")
    home = os.path.join(ROOT, "build", "mesen-cdl")
    if not os.path.exists(os.path.join(home, "Mesen")):
        shutil.copytree(os.path.dirname(mesen), home, dirs_exist_ok=True)
    with open(os.path.join(home, "settings.json"), "w") as f:
        f.write(SETTINGS)
    out = os.path.abspath(a.out)
    env = dict(os.environ, CDL_OUT=out, CDL_FRAMES=str(a.frames), CDL_INPUTS=a.inputs)
    subprocess.run([os.path.join(home, "Mesen"), "--testRunner", os.path.abspath(a.rom),
                    os.path.join(ROOT, "scripts", "mesen_cdl.lua")], env=env, check=True,
                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    rom_size = os.path.getsize(a.rom)
    with open(out, "rb") as f:
        cdl = bytearray(f.read())
    if len(cdl) != rom_size:
        raise SystemExit(f"CDL has {len(cdl)} bytes, ROM {rom_size}")
    if a.merge and os.path.exists(a.merge):
        with open(a.merge, "rb") as f:
            old = f.read()
        cdl = bytearray(x | y for x, y in zip(cdl, old))
        with open(out, "wb") as f:
            f.write(cdl)
    print(f"{out}: {sum(1 for b in cdl if b & 1)} code bytes, {sum(1 for b in cdl if b & 0x0C)} entry points")


if __name__ == "__main__":
    main()
