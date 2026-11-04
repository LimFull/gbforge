#!/usr/bin/env python3
"""Run an input/observation scenario against a ROM through emucap (Mesen2) and save results.

usage: scripts/emucap_run.py ROM SCENARIO.json OUTDIR

Scenario: {"steps": [
  {"step": 300},                                   # advance frames
  {"until": {"memory_type": "gameboyMemory", "address": 49152}, "eq": "0a00", "max": 900},  # align on game state
                                                   # ("ge": n compares one byte; "eq": exact hex bytes)
  {"tap": ["right"], "press": 2, "after": 2},     # frame-exact input
  {"read": "label", "memory_type": "gameboyMemory", "address": 49152, "length": 5},
  {"shot": "label"},                               # OUTDIR/label.png
  {"write": {"memory_type": "gbCartRam", "address": 0, "fill": 0, "length": 8192}}  # or "hex": "..."
]}
Writes OUTDIR/result.json: {"rom_sha256", "system", "reads": {label: hex}, "shots": {label: sha256}}.
Env: EMUCAP_MCP (or emucap-mcp on PATH), MESEN_BIN, EMUCAP_GBA_BIOS — see local.env.example.
"""
import hashlib
import json
import os
import shutil
import sys

from mcp_client import Mcp

EMUCAP_MCP = os.environ.get("EMUCAP_MCP") or shutil.which("emucap-mcp")


def gb_to_gba(map_doc):
    """Translate GB-address reads to the port's GBA addresses using gb2gba's .map.json."""
    regions = [(int(r["gb"], 16), int(r["gba"], 16), r["size"]) for r in map_doc["memory"].values() if isinstance(r, dict) and "gb" in r]

    def tr(spec):
        if spec["memory_type"] == "gbCartRam":  # GB cart RAM is mapped 1:1 onto GBA SRAM
            return dict(spec, memory_type="gbaSaveRam")
        if spec["memory_type"] != "gameboyMemory":
            return spec
        for gb, gba, size in regions:
            if gb <= spec["address"] < gb + size:
                return dict(spec, memory_type="gbaMemory", address=gba + spec["address"] - gb)
        raise SystemExit(f"GB address {spec['address']:#x} is not mapped in the port")
    return tr


def until_eq(m, spec, want, max_frames):
    """Run until the bytes at spec equal `want`: a write breakpoint on the first byte (value filter,
    pause on hit) stops at the exact instruction that stores it — one emulator run, no per-frame polling."""
    bp = m.call("set_breakpoint", {"kind": "write", "memory_type": spec["memory_type"], "start": spec["address"],
                                   "end": spec["address"], "value": int(want[:2], 16), "pause_on_hit": True})["id"]
    try:
        left = max_frames
        while left > 0:
            if m.call("read_memory", spec)["hex"] == want:
                return
            r = m.call("step", {"count": left})
            left -= max(r.get("count", left), 1)
        if m.call("read_memory", spec)["hex"] != want:
            raise SystemExit(f"until: {want} not reached within {max_frames} frames")
    finally:
        m.call("clear_breakpoint", {"id": bp})


def run(rom, scenario, outdir, translate=lambda spec: spec):
    os.makedirs(outdir, exist_ok=True)
    result = {"rom": os.path.basename(rom),
              "rom_sha256": hashlib.sha256(open(rom, "rb").read()).hexdigest(),
              "reads": {}, "shots": {}}
    with Mcp([EMUCAP_MCP]) as m:
        plan = m.call("launch_plan", {"content_path": rom})
        if plan.get("launch_blockers"):
            raise SystemExit(f"launch blocked: {plan['launch_blockers']}")
        args = dict(plan["preferred_launcher"]["args"], start_frozen=True)
        host = m.call("launch", args).get("host_build", {})
        st = m.call("status", {})
        launch_id = st["runtime_instance"]["launch_id"]
        result["system"] = args.get("system")
        result["emulator"] = f"Mesen {host.get('tag')} ({host.get('commit', '')[:10]})"
        try:
            for s in scenario["steps"]:
                if "step" in s:
                    m.call("step", {"count": s["step"]})
                elif "tap" in s:
                    m.call("tap", {"buttons": s["tap"], "press_frames": s.get("press", 2),
                                   "after_frames": s.get("after", 1)})
                elif "until" in s and "eq" in s:
                    until_eq(m, translate(dict(s["until"], length=len(s["eq"]) // 2)), s["eq"], s.get("max", 1000))
                elif "until" in s:
                    spec = translate(dict(s["until"], length=1))
                    for _ in range(s.get("max", 1000)):
                        if int(m.call("read_memory", spec)["hex"], 16) >= s["ge"]:
                            break
                        m.call("step", {"count": 1})
                    else:
                        raise SystemExit(f"until: condition not reached within {s.get('max', 1000)} frames")
                elif "read" in s:
                    spec = translate({k: s[k] for k in ("memory_type", "address", "length")})
                    result["reads"][s["read"]] = m.call("read_memory", spec)["hex"]
                elif "write" in s:
                    w = dict(s["write"])
                    if "fill" in w:
                        w["hex"] = f"{w.pop('fill'):02x}" * w.pop("length")
                    m.call("write_memory", translate(w))
                elif "shot" in s:
                    r = m.call("screenshot", {"save_path": os.path.join(outdir, s["shot"] + ".png")})
                    result["shots"][s["shot"]] = r["provenance"]["sha256"]
                else:
                    raise SystemExit(f"unknown step: {s}")
        finally:
            m.call("stop", {"launch_id": launch_id})
    with open(os.path.join(outdir, "result.json"), "w") as f:
        json.dump(result, f, indent=2)
    return result


if __name__ == "__main__":
    if len(sys.argv) != 4:
        raise SystemExit(__doc__)
    if not EMUCAP_MCP:
        raise SystemExit("emucap-mcp not found: set EMUCAP_MCP (see local.env.example)")
    rom, scen, out = sys.argv[1:]
    map_path = os.path.splitext(rom)[0] + ".map.json"
    tr = gb_to_gba(json.load(open(map_path))) if rom.endswith(".gba") and os.path.exists(map_path) else (lambda s: s)
    print(json.dumps(run(os.path.abspath(rom), json.load(open(scen)), os.path.abspath(out), tr), indent=2))
