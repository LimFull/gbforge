#!/usr/bin/env python3
"""Collect CDL coverage for exactly what a verify scenario does on the original ROM.

usage: scripts/scenario_cdl.py ROM SCENARIO.json OUT.cdl [--merge OLD.cdl] [--extra FRAMES] [--reuse]
Plays the scenario on the original through emucap to record the absolute frame of each input,
then replays that schedule headless in Mesen with the Code/Data Logger (scripts/mesen_cdl.py).
"""
import argparse
import json
import os
import subprocess
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import emucap_run  # noqa: E402


def main():
    p = argparse.ArgumentParser()
    p.add_argument("rom")
    p.add_argument("scenario")
    p.add_argument("out")
    p.add_argument("--merge")
    p.add_argument("--extra", type=int, default=300, help="frames to keep running after the last step")
    p.add_argument("--reuse", action="store_true", help="reuse OUT.run.json from a previous emucap pass")
    a = p.parse_args()
    scenario = json.load(open(a.scenario))
    run_json = a.out + ".run.json"  # the emucap pass is minutes long; --reuse skips it
    if a.reuse and os.path.exists(run_json):
        r = json.load(open(run_json))
    else:
        with tempfile.TemporaryDirectory() as tmp:
            r = emucap_run.run(os.path.abspath(a.rom), scenario, tmp)
        with open(run_json, "w") as f:
            json.dump(r, f)
    # Mesen restarts its frame count on reset and a headless script cannot survive emu.reset(), so each
    # reset starts a new headless run that resumes from the cart RAM the previous one left behind.
    segments = [[]]
    for i in r.get("inputs", []):
        if "reset" in i:
            segments[-1] = (segments[-1], i["reset"])
            segments.append([])
        else:
            segments[-1].append(i)
    segments[-1] = (segments[-1], r["final_frame"] + a.extra)
    clear = [s["write"] for s in scenario["steps"] if "write" in s and s["write"]["memory_type"] == "gbCartRam"]
    with tempfile.TemporaryDirectory() as tmp:
        for n, (inputs, frames) in enumerate(segments):
            cmd = [sys.executable, os.path.join(HERE, "mesen_cdl.py"), a.rom, a.out, "--frames", str(frames),
                   "--inputs", ";".join(f"{i['frame']}:{','.join(i['buttons'])}:{i['press']}" for i in inputs),
                   "--cart-out", os.path.join(tmp, f"cart{n}")]
            if n:
                cmd += ["--cart-in", os.path.join(tmp, f"cart{n - 1}")]
            elif clear:
                cmd += ["--clear-cart", str(clear[0].get("length", 0))]
            merge = a.out if n else a.merge
            if merge:
                cmd += ["--merge", merge]
            subprocess.run(cmd, check=True)


if __name__ == "__main__":
    main()
