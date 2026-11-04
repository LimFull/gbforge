#!/usr/bin/env python3
"""Inspect a running port in emucap: ARM PC/LR as symbols, GB CPU state, trap info, screenshots.

usage: scripts/port_probe.py PORT.gba OUTDIR FRAMES[,FRAMES...] [--tap BUTTON@FRAME ...]
Reads PORT.sym / PORT.map.json written by `gb2gba port`.
"""
import bisect
import json
import os
import sys

from mcp_client import Mcp


def main(port, outdir, frames, taps):
    base = os.path.splitext(port)[0]
    funcs, all_syms = [], {}
    for line in open(base + ".sym"):
        p = line.split()
        if len(p) == 3:
            all_syms[p[2]] = int(p[0], 16)
            if p[1] in "tTwW":
                funcs.append((int(p[0], 16), p[2]))
    funcs.sort()
    starts = [a for a, _ in funcs]

    def sym(addr):
        i = bisect.bisect_right(starts, addr) - 1
        return f"{funcs[i][1]}+{addr - funcs[i][0]:#x}" if i >= 0 and addr - funcs[i][0] < 0x10000 else f"{addr:#x}"

    mem = json.load(open(base + ".map.json"))["memory"]
    cpu, trap = int(mem["cpu"], 16), int(mem["trap"], 16)
    os.makedirs(outdir, exist_ok=True)
    with Mcp([os.environ["EMUCAP_MCP"]]) as m:
        plan = m.call("launch_plan", {"content_path": os.path.abspath(port)})
        m.call("launch", dict(plan["preferred_launcher"]["args"], start_frozen=True))
        lid = m.call("status", {})["runtime_instance"]["launch_id"]
        rd = lambda a, n: bytes.fromhex(m.call("read_memory", {"memory_type": "gbaMemory", "address": a, "length": n})["hex"])
        frame = 0
        try:
            for target in frames:
                for button, at in sorted((t for t in taps if frame <= t[1] < target), key=lambda t: t[1]):
                    m.call("step", {"count": at - frame})
                    m.call("tap", {"buttons": [button], "press_frames": 2, "after_frames": 0})
                    frame = at + 2
                m.call("step", {"count": target - frame})
                frame = target
                st = m.call("get_state", {})["state"]
                c = rd(cpu, 22)
                t = rd(trap, 6)
                io = rd(all_syms["io"], 0x80) if "io" in all_syms else b""
                print(f"[frame {frame}] pc={sym(st['cpu.r15'])} lr={sym(st['cpu.r14'])} arm_sp={st['cpu.r13']:#x}")
                w = lambda o: c[o] | c[o + 1] << 8   # layout: runtime-gba/gb.h GbCpu
                print(f"  gb a={c[0]:02x} bc={w(8):04x} de={w(10):04x} hl={w(12):04x}"
                      f" z{c[1]}n{c[2]}h{c[3]}c{c[4]} ime={c[5]} irq={c[6]} sp={w(14):04x}"
                      f" ret={w(16):04x} bank={w(18)} frame_sp={w(20):04x}")
                if io:
                    print(f"  LCDC={io[0x40]:02x} STAT={io[0x41]:02x} SCY={io[0x42]:02x} SCX={io[0x43]:02x} IF={io[0x0f]:02x} KEY1={io[0x4d]:02x} VBK={io[0x4f]:02x} SVBK={io[0x70]:02x}")
                if t[5]:
                    print(f"  TRAP reason={t[4]} at {t[0] | t[1] << 8:02X}:{t[2] | t[3] << 8:04X}")
                m.call("screenshot", {"save_path": os.path.join(os.path.abspath(outdir), f"f{frame}.png")})
        finally:
            m.call("stop", {"launch_id": lid})


if __name__ == "__main__":
    args = sys.argv[1:]
    if len(args) < 3:
        raise SystemExit(__doc__)
    taps = []
    if "--tap" in args:
        i = args.index("--tap")
        taps = [(b, int(f)) for b, f in (x.split("@") for x in args[i + 1:])]
        args = args[:i]
    main(args[0], args[1], [int(x) for x in args[2].split(",")], taps)
