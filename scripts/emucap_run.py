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
  {"write": {"memory_type": "gbCartRam", "address": 0, "fill": 0, "length": 8192}},  # or "hex": "..."
  {"reset": true}                                  # soft reset (battery RAM survives)
]}
Writes OUTDIR/result.json: {"rom_sha256", "system", "reads": {label: hex}, "shots": {label: sha256}}.
Env: EMUCAP_MCP (or emucap-mcp on PATH), MESEN_BIN, EMUCAP_GBA_BIOS — see local.env.example.
"""
import hashlib
import json
import os
import shutil
import struct
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


def capture_state(m):
    """The original's whole GB state as runtime-gba's struct GbInject (armed=0). Capture at a
    compiled entry outside interrupt handlers so the port can restart there."""
    st = m.call("get_state", {"groups": ["cpu", "ppu", "cart", "memoryManager", "timer"]})["state"]

    sizes = {r["memory_type"]: r["size"] for r in m.call("status", {})["memory_regions"]}

    def rd(kind, addr, n):
        return bytes.fromhex(m.call("read_memory", {"memory_type": kind, "address": addr, "length": n})["hex"])

    def region(kind, n):  # the struct holds CGB sizes; DMG has 8 KiB of WRAM and VRAM
        have = min(n, sizes[kind])
        return b"".join(rd(kind, a, min(0x4000, have - a)) for a in range(0, have, 0x4000)).ljust(n, b"\0")
    io = bytearray(rd("gameboyMemory", 0xFF00, 0x80))
    io[0x0F] = st["memoryManager.irqRequests"] & 0x1F
    pal = lambda kind: b"".join((st[f"ppu.cgb{kind}Palettes{i}"] & 0xFFFF).to_bytes(2, "little") for i in range(32))
    lcd = st["ppu.lcdEnabled"]
    ram_bank = st.get("cart.ramBank", 0)
    rom_bank = st["cart.prgBank"]
    if "cart.mode" in st:                 # Mesen's MBC1 keeps the 5-bit and 2-bit registers apart
        rom_bank |= ram_bank << 5
        ram_bank = ram_bank if st["cart.mode"] else 0
    hdr = struct.pack("<IHH8B4B4H", 0, st["cpu.pc"], st["cpu.sp"],
                      st["cpu.a"], st["cpu.flags"], st["cpu.b"], st["cpu.c"], st["cpu.d"], st["cpu.e"], st["cpu.h"], st["cpu.l"],
                      int(st["cpu.ime"]), st["memoryManager.irqEnabled"], int(st["cart.ramEnabled"]), ram_bank,
                      rom_bank, st["ppu.scanline"] if lcd else 0, st["ppu.cycle"] if lcd else 0,
                      st["timer.divider"] & 0xFFFF)
    return (hdr + bytes(io) + rd("gbHighRam", 0, 0x7F) + b"\0" + pal("Bg") + pal("Obj") + rd("gbSpriteRam", 0, 0xA0)
            + region("gbWorkRam", 0x8000) + region("gbVideoRam", 0x4000))


def run_to_pc(m, pcs, max_frames):
    """Run until any of `pcs` (int or list) executes."""
    pcs = pcs if isinstance(pcs, list) else [pcs]
    bps = [m.call("set_breakpoint", {"kind": "exec", "memory_type": "gameboyMemory", "start": pc, "end": pc,
                                     "pause_on_hit": True})["id"] for pc in pcs]
    try:
        if m.call("step", {"count": max_frames}).get("reason") != "breakpoint":
            raise SystemExit(f"inject: none of {[hex(p) for p in pcs]} reached within {max_frames} frames")
    finally:
        for bp in bps:
            m.call("clear_breakpoint", {"id": bp})


GBA_KEYS = {"a": 0, "b": 1, "select": 2, "start": 3, "right": 4, "left": 5, "up": 6, "down": 7, "r": 8, "l": 9}


class GbClock:
    """Count scenario frames in GB time on a port (its GB VBlanks), so a port that lags in heavy
    scenes still receives each input at the same game moment as the original."""

    def __init__(self, m, addr, trap_addr=None):
        self.m, self.addr, self.trap_addr = m, addr, trap_addr

    def now(self):
        h = self.m.call("read_memory", {"memory_type": "gbaMemory", "address": self.addr, "length": 4})["hex"]
        return int.from_bytes(bytes.fromhex(h), "little")

    def step(self, n):
        """Run until the GB frame counter reads now + n. A write breakpoint on that value stops at the
        frame tick itself: one emulator run per step, and the port is always paused at the same GB
        moment however much it lags (stepping GBA frames could stop it mid-frame)."""
        target = self.now() + n
        bp = self.m.call("set_breakpoint", {"kind": "write", "memory_type": "gbaMemory", "start": self.addr,
                                            "end": self.addr, "value": target, "pause_on_hit": True})["id"]
        try:
            while (now := self.now()) < target:
                self.m.call("step", {"count": 600})
                if self.now() == now:  # 10 s of GBA frames without a GB frame (LCD-off loads are far shorter)
                    why = ""
                    if self.trap_addr:  # gb_trap_info: u16 bank, u16 addr, u8 reason, u8 hit
                        t = bytes.fromhex(self.m.call("read_memory", {"memory_type": "gbaMemory", "address": self.trap_addr, "length": 6})["hex"])
                        if t[5]:
                            why = f": trap {t[4]} at {int.from_bytes(t[0:2], 'little'):02X}:{int.from_bytes(t[2:4], 'little'):04X}"
                    raise RuntimeError(f"port stopped counting GB frames at {now}{why}")
        finally:
            self.m.call("clear_breakpoint", {"id": bp})


def port_options(rom):
    """run() keyword arguments for a gb2gba port (next to its .map.json); none for an original ROM."""
    map_path = os.path.splitext(rom)[0] + ".map.json"
    if not (rom.endswith(".gba") and os.path.exists(map_path)):
        return {}
    doc = json.load(open(map_path))
    mem = doc["memory"]
    return {"translate": gb_to_gba(doc),
            "gb_frames_addr": int(mem["gb_frames"], 16) if "gb_frames" in mem else None,
            "trap_addr": int(mem["trap"], 16) if "trap" in mem else None,
            "pace": {k: int(v, 16) for k, v in mem["pace"].items()} if "pace" in mem else None,
            "inject": int(mem["inject"], 16) if "inject" in mem else None,
            "keys": int(mem["keys"], 16) if "keys" in mem else None,
            "script": int(mem["script"], 16) if "script" in mem else None}


def run(rom, scenario, outdir, translate=lambda spec: spec, gb_frames_addr=None, extra=None, trap_addr=None,
        pace=None, inject=None, inject_states=None, keys=None, script=None):
    """Play the scenario; `extra(do)` (if given) may then execute more steps in the same session.

    {"inject": {"at": PC}}: on the original, run to PC (a compiled entry, e.g. the main loop's
    idle wait) and save the whole GB state; on a port, load the original's saved state
    (`inject_states`, in order) so both continue from identical state. With "every": N it also
    repeats before the first tap once N scenario frames have passed since the last one ("every": 0
    stops). Each checkpoint pauses the original, so keep N as large as the comparison allows."""
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
        clock = GbClock(m, gb_frames_addr, trap_addr) if gb_frames_addr else None

        periodic = {"at": None, "every": 0, "since": 0}
        # Port with an input schedule: steps and taps only advance a plan; the emulator runs it in
        # one go, keys applied by the port at each frame tick, when something must be observed.
        plan = {"base": None, "frames": 0, "script": []}

        def flush():
            if plan["base"] is None:
                return
            for f, k in plan["script"]:  # due now: the port is paused past this frame's tick (e.g. after "until")
                if f == plan["base"]:
                    m.call("write_memory", {"memory_type": "gbaMemory", "address": keys, "hex": k.to_bytes(2, "little").hex()})
            entries = plan["script"] + [(0, 0)] * (32 - len(plan["script"]))
            m.call("write_memory", {"memory_type": "gbaMemory", "address": script,
                                    "hex": b"".join(f.to_bytes(4, "little") + k.to_bytes(4, "little") for f, k in entries).hex()})
            target, plan["base"] = plan["base"] + plan["frames"], None
            clock.step(target - clock.now())

        def planned(frames, presses=()):
            if plan["base"] is not None and len(plan["script"]) + len(presses) > 32:
                flush()
            if plan["base"] is None:
                plan.update(base=clock.now(), frames=0, script=[])
            at = plan["base"] + plan["frames"]
            plan["script"] += [(at + offset, k) for offset, k in presses]
            plan["frames"] += frames
            return at

        def inject_now(at):
            flush()
            n = len(result.setdefault("inject", []))
            if inject is None:  # original
                run_to_pc(m, at, 120)
                name = f"inject-{n}.bin"
                with open(os.path.join(outdir, name), "wb") as f:
                    f.write(capture_state(m))
                result["inject"].append(name)
            else:  # port: fill struct GbInject, then arm it (applied at the next GB frame tick)
                data = open(inject_states[n], "rb").read()
                for i in range(4, len(data), 0x4000):
                    m.call("write_memory", {"memory_type": "gbaMemory", "address": inject + i, "hex": data[i:i + 0x4000].hex()})
                m.call("write_memory", {"memory_type": "gbaMemory", "address": inject, "hex": "01000000"})
                result["inject"].append(inject_states[n])
            periodic["since"] = 0

        def do(s):
            if "tap" in s and periodic["every"] and periodic["since"] >= periodic["every"]:
                inject_now(periodic["at"])
            periodic["since"] += s.get("step", 0) + (s.get("press", 2) + s.get("after", 1) if "tap" in s else 0)
            if script and clock and ("step" in s or "tap" in s):
                press, after = s.get("press", 2), s.get("after", 1)
                if "step" in s:
                    planned(s["step"])
                else:
                    down = sum(1 << GBA_KEYS[b] for b in s["tap"])
                    at = planned(press + after, [(0, 0x3FF & ~down), (press, 0x3FF)])
                    result.setdefault("inputs", []).append({"gb_frame": at, "buttons": s["tap"], "press": press})
                return
            flush()
            if "step" in s and clock:
                clock.step(s["step"])
            elif "tap" in s and clock:
                result.setdefault("inputs", []).append({"gb_frame": clock.now(), "buttons": s["tap"], "press": s.get("press", 2)})
                if keys:  # the port's own override: seen by the game's next joypad read, like the original
                    down = sum(1 << GBA_KEYS[b] for b in s["tap"])
                    set_keys = lambda v: m.call("write_memory", {"memory_type": "gbaMemory", "address": keys, "hex": v.to_bytes(2, "little").hex()})
                else:
                    rev = m.call("debug", {"operation": "describe"})["capability_revision"]
                    dbg = lambda args: m.call("debug", {"operation": "set_input", "arguments": args, "known_capability_revision": rev})
                    set_keys = lambda v: dbg({"buttons": s["tap"] if v != 0x3FF else []})
                    down = 1
                set_keys(0x3FF & ~down)
                clock.step(s.get("press", 2))
                set_keys(0x3FF)
                clock.step(s.get("after", 1))
            elif "step" in s:
                r = m.call("step", {"count": s["step"]})
                if r.get("status", "completed") != "completed":  # a silently short step desyncs everything after it
                    raise SystemExit(f"step {s['step']} did not complete: {r}")
            elif "tap" in s:
                result.setdefault("inputs", []).append(
                    {"frame": m.call("status", {})["frame"], "buttons": s["tap"], "press": s.get("press", 2)})
                m.call("tap", {"buttons": s["tap"], "press_frames": s.get("press", 2),
                               "after_frames": s.get("after", 1)})
            elif "until" in s and "eq" in s:
                until_eq(m, translate(dict(s["until"], length=len(s["eq"]) // 2)), s["eq"], s.get("max", 1000))
                if pace:  # count GB frames from this point, as the original steps whole frames from it
                    rd = lambda a, n=4: int.from_bytes(bytes.fromhex(m.call("read_memory", {"memory_type": "gbaMemory", "address": a, "length": n})["hex"]), "little")
                    t = rd(pace["ppu_acc"]) + ((rd(pace["cyc"]) - rd(pace["ppu_on_at"])) if rd(pace["lcdc"], 1) & 0x80 else 0)
                    m.call("write_memory", {"memory_type": "gbaMemory", "address": pace["pace_next"],
                                            "hex": ((t + 70224) & 0xFFFFFFFF).to_bytes(4, "little").hex()})
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
            elif "inject" in s:
                inject_now(s["inject"]["at"])
                if "every" in s["inject"]:
                    periodic.update(at=s["inject"]["at"], every=s["inject"]["every"])
            elif "reset" in s:  # soft reset; cart RAM (GBA SRAM on a port) survives it
                result.setdefault("inputs", []).append({"reset": m.call("status", {})["frame"]})  # frame count restarts
                m.call("reset", {})
                m.call("pause", {})  # reset resumes the guest; re-align with an "until" step after this
            else:
                raise SystemExit(f"unknown step: {s}")
        try:
            # Scenarios count frames, never wall time (ports pace on emulated GBA VBlanks), so the
            # emulator need not run at real speed.
            d = m.call("debug", {"operation": "describe"})
            m.call("debug", {"operation": "execution_speed", "arguments": {"mode": "unlimited"},
                             "known_capability_revision": d.get("capability_revision")})
            if inject is None and args.get("system") in ("gb", "gbc"):
                # Mesen fills RAM with random values at power-on, so two runs of the same scenario
                # could differ wherever a game reads memory it never wrote. Zero it, as a port powers on.
                sizes = {r["memory_type"]: r["size"] for r in st["memory_regions"]}   # DMG WRAM is 8 KiB
                for kind in ("gbWorkRam", "gbHighRam"):
                    for a in range(0, sizes[kind], 0x4000):
                        m.call("write_memory", {"memory_type": kind, "address": a,
                                                "hex": "00" * min(0x4000, sizes[kind] - a)})
                m.call("write_memory", {"memory_type": "gameboyMemory", "address": 0xFF30, "hex": "00" * 16})  # wave RAM
            for s in scenario["steps"]:
                do(s)
            if extra:
                extra(do)
            flush()
            result["final_frame"] = m.call("status", {})["frame"]
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
    print(json.dumps(run(os.path.abspath(rom), json.load(open(scen)), os.path.abspath(out), **port_options(rom)), indent=2))
