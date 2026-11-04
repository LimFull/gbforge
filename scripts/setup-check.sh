#!/bin/sh
# Report toolchain/emulator availability for gbforge. Exit 1 if a required tool is missing.
# Machine-specific paths come from env (see local.env.example): EMUCAP_MCP, MESEN_BIN, EMUCAP_GBA_BIOS, MGBA.
root=$(cd "$(dirname "$0")/.." && pwd)
[ -f "$root/local.env" ] && . "$root/local.env"
fail=0

row() { printf '%-14s %s\n' "$1" "$2"; }
need() { # name, version-command
  if v=$(sh -c "$2" 2>/dev/null | head -1) && [ -n "$v" ]; then row "$1" "$v"; else row "$1" "MISSING"; fail=1; fi
}
want() { # optional
  if v=$(sh -c "$2" 2>/dev/null | head -1) && [ -n "$v" ]; then row "$1" "$v"; else row "$1" "missing (optional)"; fi
}

need git      "git --version"
need cmake    "cmake --version"
need ninja    "ninja --version"
need python3  "python3 --version"
need rgbds    "rgbasm --version"
if [ -n "$DEVKITARM" ]; then
  need devkitARM "$DEVKITARM/bin/arm-none-eabi-gcc --version"
else
  need devkitARM "docker run --rm devkitpro/devkitarm sh -c '\$DEVKITARM/bin/arm-none-eabi-gcc --version' | sed 's/^/docker: /'"
fi
need mGBA     "\"${MGBA:-/Applications/mGBA.app/Contents/MacOS/mGBA}\" --version | sed 's/.*mGBA //'"
need emucap   "test -x \"${EMUCAP_MCP:-$(command -v emucap-mcp)}\" && echo \"${EMUCAP_MCP:-$(command -v emucap-mcp)}\""
need Mesen2   "test -x \"$MESEN_BIN\" && grep -o '\"tag\": *\"[^\"]*\"' \"$(dirname "$MESEN_BIN")/emucap-mesen-build.json\""
want GBA-BIOS "test -f \"$EMUCAP_GBA_BIOS\" && shasum -a 256 \"$EMUCAP_GBA_BIOS\" | cut -c1-16"

exit $fail
