#!/usr/bin/env python3
"""Fetch the GBA toolchain that `gb2gba port` builds with: the pinned Arm GNU Toolchain (DEST/arm),
its libgcc and libc for the GBA without debug info (DEST/lib), and libtonc built from pinned source
with it (DEST/tonc). Downloads are checksum-verified.

usage: scripts/fetch_toolchain.py [DEST]     (default build/toolchain)
"""
import glob
import hashlib
import os
import platform
import shutil
import subprocess
import sys
import tarfile
import urllib.request
import zipfile
from concurrent.futures import ThreadPoolExecutor

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
ARM_VER = "15.2.rel1"
ARM_PKG = {  # (system, machine) -> (archive suffix, sha256 from developer.arm.com)
    ("Darwin", "arm64"): ("darwin-arm64-arm-none-eabi.tar.xz",
                          "1938a84b7105c192e3fb4fa5e893ba25f425f7ddab40515ae608cd40f68669a8"),
    ("Linux", "x86_64"): ("x86_64-arm-none-eabi.tar.xz",
                          "597893282ac8c6ab1a4073977f2362990184599643b4c5ee34870a8215783a16"),
    ("Windows", "AMD64"): ("mingw-w64-x86_64-arm-none-eabi.zip",
                           "7936cac895611023ffb22a64b8e426098c7104cb689778c1894572ca840b9ece"),
}
TONC_COMMIT = "c5af1b2cb019dcde43216596390490bc07800b21"  # gbadev-org/libtonc (MIT)
TONC_SHA256 = "5f5703d4c3188ddcdd770c2dc43cae7bea0780b348b1cd9eb473ba777c0aa396"
TONC_SKIP = {"tte_iohook.c"}  # stdio hook on devkitARM's devoptab; gbforge does not use it
EXE = ".exe" if os.name == "nt" else ""


def fetch(url, sha256, path):
    if not os.path.exists(path):
        print(f"downloading {url}", file=sys.stderr)
        with urllib.request.urlopen(url) as r, open(path + ".part", "wb") as f:
            shutil.copyfileobj(r, f, 1 << 20)
        os.replace(path + ".part", path)
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    if h.hexdigest() != sha256:
        os.remove(path)
        raise SystemExit(f"{path}: sha256 {h.hexdigest()} != {sha256}")


def unpack(archive, dest):
    """Extract an archive with one top-level directory as dest."""
    tmp = dest + ".tmp"
    shutil.rmtree(tmp, ignore_errors=True)
    if archive.endswith(".zip"):
        with zipfile.ZipFile(archive) as z:
            z.extractall(tmp)
    else:
        with tarfile.open(archive) as t:
            t.extractall(tmp, filter="data")
    top = os.listdir(tmp)
    shutil.rmtree(dest, ignore_errors=True)
    os.replace(os.path.join(tmp, top[0]) if len(top) == 1 else tmp, dest)
    shutil.rmtree(tmp, ignore_errors=True)


def build_tonc(arm, src, out):
    gcc = os.path.join(arm, "bin", "arm-none-eabi-gcc" + EXE)
    inc = os.path.join(src, "include")
    objdir = os.path.join(out, "obj")
    os.makedirs(objdir, exist_ok=True)
    jobs = []
    for d in ("asm", "src", "src/font", "src/tte", "src/pre1.3"):
        for f in sorted(glob.glob(os.path.join(src, d, "*.[cs]"))):
            if os.path.basename(f) in TONC_SKIP:
                continue
            obj = os.path.join(objdir, os.path.basename(f) + ".o")
            lang = ["-x", "assembler-with-cpp"] if f.endswith(".s") else ["-O2", "-Wall", "-fno-strict-aliasing"]
            jobs.append((obj, [gcc, "-mthumb", "-mcpu=arm7tdmi", "-I", inc, *lang, "-c", f, "-o", obj]))
    with ThreadPoolExecutor(os.cpu_count()) as ex:
        failed = [r for r in ex.map(lambda j: subprocess.run(j[1]), jobs) if r.returncode]
    if failed:
        raise SystemExit("libtonc build failed")
    lib = os.path.join(out, "libtonc.a")
    if os.path.exists(lib):
        os.remove(lib)
    subprocess.run([os.path.join(arm, "bin", "arm-none-eabi-ar" + EXE), "rcs", lib, *(o for o, _ in jobs)], check=True)
    shutil.rmtree(objdir)
    shutil.rmtree(os.path.join(out, "include"), ignore_errors=True)
    shutil.copytree(inc, os.path.join(out, "include"))
    shutil.copy(os.path.join(src, "license.txt"), os.path.join(out, "LICENSE"))


def gba_libs(arm, out):
    """libgcc.a and libc.a of the multilib port links use, stripped of debug info. Arm builds them on each
    host and the copies differ only in debug sections; those shift ld's section ids, which name its
    interworking stubs and so set their order. Without them every host links the same bytes."""
    gcc = os.path.join(arm, "bin", "arm-none-eabi-gcc" + EXE)
    os.makedirs(out, exist_ok=True)
    for name in ("libgcc.a", "libc.a"):
        lib = subprocess.run([gcc, "-mthumb", "-mcpu=arm7tdmi", "-print-file-name=" + name],
                             capture_output=True, text=True, check=True).stdout.strip()
        subprocess.run([os.path.join(arm, "bin", "arm-none-eabi-objcopy" + EXE), "--strip-debug", lib,
                        os.path.join(out, name)], check=True)


def main():
    dest = os.path.abspath(sys.argv[1] if len(sys.argv) > 1 else os.path.join(ROOT, "build", "toolchain"))
    key = (platform.system(), platform.machine())
    if key not in ARM_PKG:
        raise SystemExit(f"no pinned Arm GNU Toolchain for {key}; supported: {sorted(ARM_PKG)}")
    suffix, sha = ARM_PKG[key]
    dl = os.path.join(dest, "downloads")
    os.makedirs(dl, exist_ok=True)
    name = f"arm-gnu-toolchain-{ARM_VER}-{suffix}"
    arm = os.path.join(dest, "arm")
    stamp = os.path.join(arm, "gbforge-version")
    if not (os.path.exists(stamp) and open(stamp).read() == name):
        fetch(f"https://developer.arm.com/-/media/Files/downloads/gnu/{ARM_VER}/binrel/{name}", sha,
              os.path.join(dl, name))
        unpack(os.path.join(dl, name), arm)
        with open(stamp, "w") as f:
            f.write(name)
    gba_libs(arm, os.path.join(dest, "lib"))
    tonc = os.path.join(dl, f"libtonc-{TONC_COMMIT}.tar.gz")
    fetch(f"https://github.com/gbadev-org/libtonc/archive/{TONC_COMMIT}.tar.gz", TONC_SHA256, tonc)
    src = os.path.join(dest, "tonc-src")
    unpack(tonc, src)
    build_tonc(arm, src, os.path.join(dest, "tonc"))
    shutil.rmtree(src)
    print(dest)


if __name__ == "__main__":
    main()
