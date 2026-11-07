#!/usr/bin/env python3
"""Build the release bundle for this OS: dist/gbforge-VERSION-OS-ARCH.zip

usage: scripts/package.py [--sign]
The bundle needs no Python, CMake or GBA SDK: gbforge (PyInstaller), core/gbforge-core, runtime-gba/,
toolchain/ (the Arm GNU Toolchain trimmed to what `port` runs, plus libtonc) and the licences.
--sign (macOS): Developer ID signature with hardened runtime on every binary, then notarization with
the notarytool keychain profile in $NOTARY_PROFILE (see local.env.example).
Needs PyInstaller (pip install pyinstaller), CMake and Ninja, plus a C++ compiler.
"""
import argparse
import glob
import json
import os
import platform
import shutil
import subprocess
import sys
import zipfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path[:0] = [os.path.join(ROOT, "tools"), os.path.join(ROOT, "scripts")]
import fetch_toolchain  # noqa: E402
import gb2gba  # noqa: E402

GCC_VER = "15.2.1"
KEEP = [  # Arm GNU Toolchain files `port` runs (gcc -mthumb -mcpu=arm7tdmi selects the thumb/nofp multilib)
    "bin/arm-none-eabi-gcc{exe}", "bin/arm-none-eabi-objcopy{exe}", "bin/arm-none-eabi-nm{exe}", "bin/*.dll",
    "arm-none-eabi/bin/as{exe}", "arm-none-eabi/bin/ld{exe}", "arm-none-eabi/bin/*.dll",
    "libexec/gcc/arm-none-eabi/{gcc}/cc1{exe}", "libexec/gcc/arm-none-eabi/{gcc}/collect2{exe}",
    "libexec/gcc/arm-none-eabi/{gcc}/*.dll",
    "lib/gcc/arm-none-eabi/{gcc}/include/**", "lib/gcc/arm-none-eabi/{gcc}/include-fixed/**",
    "arm-none-eabi/include/*.h", "arm-none-eabi/include/[!c]*/**", "license.txt",
]  # port links fetch_toolchain's lib/ (libgcc, libc) and tonc/
NOTICE = """gbforge bundles third-party software; each directory keeps its own licence.

toolchain/arm   Arm GNU Toolchain {arm} (GCC, binutils, newlib), unmodified binaries with unused files
toolchain/lib   removed; lib/ holds its libgcc and libc without debug info. GCC and binutils are GPL-3.0
                (libgcc: GCC Runtime Library Exception); newlib is under BSD-style licences. Source code:
                https://developer.arm.com/downloads/-/arm-gnu-toolchain-downloads
toolchain/tonc  libtonc {tonc} (MIT, toolchain/tonc/LICENSE), https://github.com/gbadev-org/libtonc
core            gbforge-core links gb-recompiled {gbr} (MIT, LICENSES/gb-recompiled.txt),
                https://github.com/arcanite24/gb-recompiled
gbforge         Python runtime packaged with PyInstaller (PSF licence; PyInstaller bootloader: GPL-2.0
                with an exception for bundled programs)
"""


def run(*cmd, **kw):
    print("+", " ".join(cmd), file=sys.stderr)
    subprocess.run(cmd, check=True, **kw)


def macho(path):
    with open(path, "rb") as f:
        return f.read(4) in (b"\xcf\xfa\xed\xfe", b"\xce\xfa\xed\xfe", b"\xca\xfe\xba\xbe")


def sign_and_notarize(stage, archive):
    ids = subprocess.run(["security", "find-identity", "-v", "-p", "codesigning"],
                         capture_output=True, text=True).stdout
    ident = next((l.split('"')[1] for l in ids.splitlines() if "Developer ID Application" in l), None)
    profile = os.environ.get("NOTARY_PROFILE")
    if not ident or not profile:
        raise SystemExit("--sign needs a Developer ID Application certificate and NOTARY_PROFILE")
    files = [p for p in glob.glob(os.path.join(stage, "**"), recursive=True)
             if os.path.isfile(p) and not os.path.islink(p) and macho(p)]
    main = os.path.join(stage, "gbforge")
    for p in sorted(files, key=lambda p: p == main):  # the launcher last
        run("codesign", "--force", "--options", "runtime", "--timestamp", "--sign", ident, p)
    zip_dir(stage, archive)
    print("+ notarytool submit", archive, file=sys.stderr)
    r = subprocess.run(["xcrun", "notarytool", "submit", archive, "--keychain-profile", profile, "--wait",
                        "--output-format", "json"], capture_output=True, text=True)
    if r.returncode or json.loads(r.stdout).get("status") != "Accepted":
        raise SystemExit(f"notarization failed: {r.stdout}{r.stderr}")  # details: xcrun notarytool log ID


def zip_dir(stage, archive):
    top = os.path.dirname(stage)
    with zipfile.ZipFile(archive, "w", zipfile.ZIP_DEFLATED) as z:
        for p in sorted(glob.glob(os.path.join(stage, "**"), recursive=True)):
            if os.path.isfile(p):
                z.write(p, os.path.relpath(p, top))


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--sign", action="store_true")
    a = p.parse_args()
    osname = {"Darwin": "macos", "Windows": "windows", "Linux": "linux"}[platform.system()]
    arch = {"arm64": "arm64", "aarch64": "arm64", "x86_64": "x64", "AMD64": "x64"}[platform.machine()]
    name = f"gbforge-{gb2gba.TOOL_VERSION}-{osname}-{arch}"
    dist, build = os.path.join(ROOT, "dist"), os.path.join(ROOT, "build", "package")
    stage = os.path.join(dist, name)
    shutil.rmtree(stage, ignore_errors=True)

    run(sys.executable, fetch_toolchain.__file__)
    core_build = os.path.join(build, "core")
    flags = {"macos": ["-DCMAKE_OSX_DEPLOYMENT_TARGET=11.0"],
             "windows": ["-DCMAKE_MSVC_RUNTIME_LIBRARY=MultiThreaded"],
             "linux": ["-DCMAKE_EXE_LINKER_FLAGS=-static-libstdc++ -static-libgcc"]}[osname]
    run("cmake", "-G", "Ninja", "-B", core_build, "-DCMAKE_BUILD_TYPE=Release", *flags, ROOT)
    run("cmake", "--build", core_build, "--target", "gbforge-core")
    run(sys.executable, "-m", "PyInstaller", "--noconfirm", "--onedir", "--name", "gbforge",
        "--distpath", build, "--workpath", os.path.join(build, "pyi"), "--specpath", build,
        os.path.join(ROOT, "tools", "gb2gba.py"))
    shutil.copytree(os.path.join(build, "gbforge"), stage)

    os.makedirs(os.path.join(stage, "core"))
    shutil.copy2(os.path.join(core_build, "gbforge-core" + gb2gba.EXE), os.path.join(stage, "core"))
    shutil.copytree(os.path.join(ROOT, "runtime-gba"), os.path.join(stage, "runtime-gba"))
    src = os.path.join(ROOT, "build", "toolchain")
    for d in ("tonc", "lib"):
        shutil.copytree(os.path.join(src, d), os.path.join(stage, "toolchain", d))
    for pattern in KEEP:
        for f in glob.glob(os.path.join(src, "arm", pattern.format(exe=gb2gba.EXE, gcc=GCC_VER)), recursive=True):
            if os.path.isfile(f):
                dst = os.path.join(stage, "toolchain", "arm", os.path.relpath(f, os.path.join(src, "arm")))
                os.makedirs(os.path.dirname(dst), exist_ok=True)
                shutil.copy2(f, dst)

    lic = os.path.join(stage, "LICENSES")
    os.makedirs(lic)
    shutil.copy2(os.path.join(ROOT, "LICENSE"), os.path.join(lic, "gbforge.txt"))
    gbr_src = glob.glob(os.path.join(core_build, "_deps", "gbrecompiled-src", "LICENSE"))
    if not gbr_src:  # a local clone given with -DFETCHCONTENT_SOURCE_DIR_GBRECOMPILED
        cache = open(os.path.join(core_build, "CMakeCache.txt")).read().splitlines()
        line = next(l for l in cache if l.startswith("FETCHCONTENT_SOURCE_DIR_GBRECOMPILED"))
        gbr_src = [os.path.join(line.split("=", 1)[1], "LICENSE")]
    shutil.copy2(gbr_src[0], os.path.join(lic, "gb-recompiled.txt"))
    with open(os.path.join(stage, "NOTICE.txt"), "w") as f:
        f.write(NOTICE.format(arm=fetch_toolchain.ARM_VER, tonc=fetch_toolchain.TONC_COMMIT[:12],
                              gbr=open(os.path.join(ROOT, "CMakeLists.txt")).read().split("GBRECOMPILED_COMMIT ")[1][:12]))
    shutil.copy2(os.path.join(ROOT, "README.md"), stage)

    # the bundle has to port on its own before it ships
    smoke = os.path.join(build, "smoke")
    shutil.rmtree(smoke, ignore_errors=True)
    run(os.path.join(stage, "gbforge" + gb2gba.EXE), "port", os.path.join(ROOT, "tests", "fixtures", "tiny", "tiny.gb"),
        "--output", os.path.join(smoke, "tiny.gba"), cwd=build)

    archive = stage + ".zip"
    if a.sign:
        sign_and_notarize(stage, archive)
    else:
        zip_dir(stage, archive)
    print(archive)


if __name__ == "__main__":
    main()
