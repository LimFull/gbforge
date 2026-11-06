#!/bin/sh
# Download public test ROMs that are too large to keep in git (mooneye-test-suite, MIT) into
# build/test-roms/ and print the directory. Pinned by version and SHA-256.
set -e
root=$(cd "$(dirname "$0")/.." && pwd)
out="$root/build/test-roms"
v=mts-20260714-0944-31510e1
sha=5f0694e4e9e58441db23ff057a5ab681870d4cfaed91267e99885936db8e6f1d
if [ ! -d "$out/$v" ]; then
  mkdir -p "$out"
  curl -sSL -o "$out/$v.tar.gz" "https://gekkio.fi/files/mooneye-test-suite/$v/$v.tar.gz"
  echo "$sha  $out/$v.tar.gz" | shasum -a 256 -c - >/dev/null
  tar -xzf "$out/$v.tar.gz" -C "$out"
fi
echo "$out/$v"
