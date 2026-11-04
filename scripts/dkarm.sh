#!/bin/sh
# Run a command with devkitARM: native if $DEVKITARM is set, else in the devkitpro/devkitarm image.
# usage: scripts/dkarm.sh make -C examples/hello-gba
set -e
if [ -n "$DEVKITARM" ]; then exec "$@"; fi
root=$(cd "$(dirname "$0")/.." && pwd)
exec docker run --rm -v "$root":"$root" -w "$PWD" devkitpro/devkitarm "$@"
