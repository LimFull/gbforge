#!/usr/bin/env python3
"""Collect CDL coverage for exactly what a verify scenario does on the original ROM.

usage: scripts/scenario_cdl.py ROM SCENARIO.json OUT.cdl [--merge OLD.cdl] [--extra FRAMES]
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
    a = p.parse_args()
    scenario = json.load(open(a.scenario))
    with tempfile.TemporaryDirectory() as tmp:
        r = emucap_run.run(os.path.abspath(a.rom), scenario, tmp)
    inputs = ";".join(f"{i['frame']}:{','.join(i['buttons'])}:{i['press']}" for i in r.get("inputs", []))
    clear = [s["write"] for s in scenario["steps"] if "write" in s and s["write"]["memory_type"] == "gbCartRam"]
    cmd = [sys.executable, os.path.join(HERE, "mesen_cdl.py"), a.rom, a.out,
           "--frames", str(r["final_frame"] + a.extra), "--inputs", inputs]
    if clear:
        cmd += ["--clear-cart", str(clear[0].get("length", 0))]
    if a.merge:
        cmd += ["--merge", a.merge]
    subprocess.run(cmd, check=True)


if __name__ == "__main__":
    main()
