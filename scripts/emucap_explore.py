#!/usr/bin/env python3
"""Find an input route on the original in one live emulator session, recording a replayable scenario.

  emucap_explore.py serve ROM PREFIX.json OUTDIR     # play PREFIX, then run steps sent by `send`
  emucap_explore.py send OUTDIR OP...                # OP: btn[+btn]:press:after | wait:N | shot:NAME | quit | JSON step

Every executed step is appended to OUTDIR/steps.json, a scenario for emucap_run/verify. One session
instead of savestate checkpoints: an emucap savestate needs an instruction-step halt, which can run a
HALTed CPU up to the next interrupt and shift the frame phase of every later input.
"""
import json
import os
import socket
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import emucap_run  # noqa: E402


def parse(op):
    if op.startswith("{"):  # any emucap_run step as JSON, e.g. {"reset": true}
        return json.loads(op)
    k, *r = op.split(":")
    if k == "wait":
        return {"step": int(r[0])}
    if k == "shot":
        return {"shot": r[0]}
    if k == "quit":
        return {"quit": True}
    return {"tap": k.split("+"), "press": int(r[0]), "after": int(r[1])}


def serve(rom, prefix, outdir):
    os.makedirs(outdir, exist_ok=True)
    steps = list(prefix["steps"])
    path = os.path.join(outdir, "sock")
    if os.path.exists(path):
        os.unlink(path)

    def save():
        with open(os.path.join(outdir, "steps.json"), "w") as f:
            json.dump({"steps": steps}, f, indent=1)

    def loop(do):
        save()
        srv = socket.socket(socket.AF_UNIX)
        srv.bind(path)
        srv.listen()
        print("ready", flush=True)
        while True:
            conn, _ = srv.accept()
            with conn:
                req = json.loads(conn.makefile().readline())
                try:
                    for s in req:
                        if "quit" in s:
                            conn.sendall(b"bye\n")
                            return
                        do(s)
                        steps.append(s)
                    save()
                    conn.sendall(b"ok\n")
                except Exception as e:  # report and keep the session; the step was not recorded
                    conn.sendall(f"error: {e}\n".encode())

    emucap_run.run(os.path.abspath(rom), prefix, outdir, extra=loop)


def send(outdir, ops):
    c = socket.socket(socket.AF_UNIX)
    c.connect(os.path.join(outdir, "sock"))
    c.sendall((json.dumps([parse(o) for o in ops]) + "\n").encode())
    print(c.makefile().readline().strip())


if __name__ == "__main__":
    if len(sys.argv) >= 5 and sys.argv[1] == "serve":
        serve(sys.argv[2], json.load(open(sys.argv[3])), sys.argv[4])
    elif len(sys.argv) >= 3 and sys.argv[1] == "send":
        send(sys.argv[2], sys.argv[3:])
    else:
        raise SystemExit(__doc__)
