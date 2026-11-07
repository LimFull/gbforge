#!/usr/bin/env python3
"""Sample where a --profile port spends GBA time, attributed to generated GB instructions.

usage: scripts/port_profile.py PORT.gba SCENARIO.json [TOP]
Runs the scenario (scripts/emucap_run.py format) on the port, then reads the profiler ring
buffer and maps each PC to the outermost source line (gen_*.c lines carry the GB instruction).
"""
import collections
import json
import os
import re
import subprocess
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from mcp_client import Mcp  # noqa: E402
import emucap_run  # noqa: E402

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "tools"))
import gb2gba  # noqa: E402


def main(port, scenario, top):
    base = os.path.splitext(port)[0]
    mp = json.load(open(base + ".map.json"))
    prof = mp["memory"].get("profile")
    if not prof:
        raise SystemExit("not a profiling build: gb2gba port --profile")
    tr = emucap_run.gb_to_gba(mp)
    steps = json.load(open(scenario))["steps"]
    with Mcp([os.environ["EMUCAP_MCP"]]) as m:
        plan = m.call("launch_plan", {"content_path": os.path.abspath(port)})
        m.call("launch", dict(plan["preferred_launcher"]["args"], start_frozen=True))
        lid = m.call("status", {})["runtime_instance"]["launch_id"]
        rd = lambda a, n: bytes.fromhex(m.call("read_memory", {"memory_type": "gbaMemory", "address": a, "length": n})["hex"])
        try:
            start = None
            for s in steps:
                if "write" in s:
                    w = dict(s["write"])
                    if "fill" in w:
                        w["hex"] = f"{w.pop('fill'):02x}" * w.pop("length")
                    m.call("write_memory", tr(w))
                elif "until" in s:
                    emucap_run.until_eq(m, tr(dict(s["until"], length=len(s["eq"]) // 2)), s["eq"], s.get("max", 1000))
                elif "mark" in s:   # profile only from here
                    start = int.from_bytes(rd(int(prof["count"], 16), 4), "little")
                elif "step" in s:
                    m.call("step", {"count": s["step"]})
                elif "tap" in s:
                    m.call("tap", {"buttons": s["tap"], "press_frames": s.get("press", 2), "after_frames": s.get("after", 1)})
            end = int.from_bytes(rd(int(prof["count"], 16), 4), "little")
            buf = rd(int(prof["pcs"], 16), 4 * prof["size"])
        finally:
            m.call("stop", {"launch_id": lid})
    start = end - min(end - (start or 0), prof["size"])
    pcs = [int.from_bytes(buf[4 * (i % prof["size"]):4 * (i % prof["size"]) + 4], "little") for i in range(start, end)]
    out = subprocess.run([gb2gba.arm_tool("addr2line"), "-i", "-a", "-e", base + ".elf", *(hex(p) for p in pcs)],
                         capture_output=True, text=True).stdout.split("\n")
    # -a prints the address line first; the last source line before the next address is the outermost
    outer, cur = [], None
    for line in out + ["0x"]:
        if line.startswith("0x"):
            if cur is not None:
                outer.append(cur)
            cur = "??"
        elif line.strip():
            cur = line.strip()
    c = collections.Counter()
    for loc in outer:
        mm = re.match(r"(.*):(\d+)", loc)
        if not mm:
            c["?? (BIOS: Halt/idle)"] += 1
            continue
        try:
            text = open(mm.group(1)).read().split("\n")[int(mm.group(2)) - 1].strip()
        except OSError:
            text = mm.group(1)
        c[f"{os.path.basename(mm.group(1))}: {text[:110]}"] += 1
    print(f"{len(outer)} samples")
    for k, v in c.most_common(top):
        print(f"{100 * v / len(outer):5.1f}%  {k}")


if __name__ == "__main__":
    if len(sys.argv) < 3:
        raise SystemExit(__doc__)
    main(sys.argv[1], sys.argv[2], int(sys.argv[3]) if len(sys.argv) > 3 else 20)
