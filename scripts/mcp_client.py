"""Minimal MCP stdio client (stdlib only) — drives emucap-mcp from scripts/CI.

    with Mcp([EMUCAP_MCP]) as m:
        m.call("status", {})
"""
import json
import subprocess


class McpError(RuntimeError):
    pass


class Mcp:
    def __init__(self, cmd, env=None):
        self.p = subprocess.Popen(cmd, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                  env=env, text=True, bufsize=1)
        self.next_id = 0
        self.request("initialize", {"protocolVersion": "2025-06-18", "capabilities": {},
                                    "clientInfo": {"name": "gbforge", "version": "0"}})
        self.send({"jsonrpc": "2.0", "method": "notifications/initialized"})

    def send(self, msg):
        self.p.stdin.write(json.dumps(msg) + "\n")
        self.p.stdin.flush()

    def request(self, method, params):
        self.next_id += 1
        self.send({"jsonrpc": "2.0", "id": self.next_id, "method": method, "params": params})
        while True:
            line = self.p.stdout.readline()
            if not line:
                raise McpError(f"server exited during {method}")
            msg = json.loads(line)
            if msg.get("id") != self.next_id:
                continue  # notifications / server requests are ignored
            if "error" in msg:
                raise McpError(f"{method}: {msg['error']}")
            return msg["result"]

    def tools(self):
        return self.request("tools/list", {})["tools"]

    def call(self, name, args=None):
        """Returns parsed JSON of the first text content (or raw text / content list)."""
        r = self.request("tools/call", {"name": name, "arguments": args or {}})
        content = r.get("content", [])
        texts = [c["text"] for c in content if c.get("type") == "text"]
        out = r.get("structuredContent")
        if out is None and texts:
            try:
                out = json.loads(texts[0])
            except ValueError:
                out = texts[0]
        if r.get("isError"):
            raise McpError(f"{name}: {out if out is not None else content}")
        return out if out is not None else content

    def close(self):
        self.p.stdin.close()
        self.p.wait(timeout=30)

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()
