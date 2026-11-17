#!/usr/bin/env python3
"""Turn a finished SteamOS boot image into a kexec payload for quest-kexec.

The flashed path (`fastboot flash boot out/boot_steamos.img`) bundles the
kernel, the ramdisk, Meta's device tree and the command line into one Android
boot image.  Kexec does not go through the bootloader, so quest-kexec's
`prepare.py` wants those pieces loose instead:

    out/kexec/Image        raw ARM64 kernel (uncompressed, text_offset 0x80000)
    out/kexec/initramfs    the SteamOS ramdisk, byte-for-byte
    out/kexec/cmdline      the command line to put in /chosen/bootargs

This reads them back out of the SAME image the flashed path validates, so the
kexec boot and the flashed boot run identical bytes.  The only change is to the
command line: the boot-time soft watchdog is dropped (quest-kexec quiesces the
real watchdog through the DTB, and softdog.soft_panic would panic the target
during the hand-off), and the kexec glue is appended (`rdinit=/init`, no KASLR
so the staged addresses hold, and `qkx_qmp_adopt=1` to adopt the AOP link the
loader leaves up).  Everything else - the display/GPU/root args - is kept
exactly as the flashed image carries it.

This host tool only reads and writes host files.  It does not touch the
headset and it does not boot anything.

Hand-off to quest-kexec (on the host, with the Quest 2 rooted over adb):

    tools/capture.sh out/captured          # runtime.dtb, iomem, cmdline, config
    python3 tools/prepare.py \
        --live-dtb out/captured/runtime.dtb --iomem out/captured/iomem \
        --kernel-image .../out/kexec/Image --initramfs .../out/kexec/initramfs \
        --cmdline "$(cat .../out/kexec/cmdline)" --output out/payload
    tools/run.sh out/payload

prep.sh is deliberately NOT used: it rebuilds the command line from Android's
own /proc/cmdline and would drop every SteamOS argument.  Pass our cmdline
straight to prepare.py instead.
"""
import argparse
import hashlib
import json
import struct
import sys
import zlib
from pathlib import Path

# quest-kexec's prepare.py staging limits.  The kernel is staged at 0x90080000
# (text_offset 0x80000 inside the 0x90000000 window); the initramfs at
# 0x93000000; the DTB at 0x96000000.  The initramfs window was 2 MiB (Quest Pro
# BusyBox) and was grown to 48 MiB to fit a full OS ramdisk - keep these in
# sync with QKX_INITRD_MAX / QKX_IMAGE_MAX in quest-kexec.
STAGING_TEXT_OFFSET = 0x80000
STAGING_IMAGE_MAX = 0x2F80000      # kernel image_size ceiling (47.5 MiB)
STAGING_INITRD_MAX = 0x3000000     # 48 MiB initramfs window
STAGING_DTB_MAX = 0x200000

# Kernel args the bootloader/Android set that must not survive into the kexec
# target.  softdog.soft_panic=1 makes the kernel panic when the soft watchdog
# bites, which fires during the hand-off window (prep.sh strips it too).
CMDLINE_STRIP = ("softdog.soft_panic=1",)
# The glue kexec needs that the flashed path gets from the bootloader/DTB.
DEFAULT_KEXEC_EXTRA = "rdinit=/init nokaslr qkx_qmp_adopt=1"


def unpack_boot(data):
    """Split an Android boot image (header v0-v2) into kernel, ramdisk, cmdline.

    Mirrors scripts/unpack_boot.py so the kexec path and the flashed path read
    the image the same way.
    """
    if data[:8] != b"ANDROID!":
        sys.exit("not an Android boot image (no ANDROID! magic)")
    ksz, _kaddr, rsz, _raddr, ssz, _saddr, _taddr, page, ver = struct.unpack_from("<9I", data, 8)
    if ver not in (0, 1, 2):
        sys.exit(f"unsupported boot header version {ver}")
    cmd = data[64:576].split(b"\0", 1)[0]
    extra = data[608:1632].split(b"\0", 1)[0]
    up = lambda n: (n + page - 1) // page * page
    off = page
    kernel = data[off:off + ksz]; off += up(ksz)
    ramdisk = data[off:off + rsz]; off += up(rsz)
    cmdline = (cmd + extra).decode("ascii", "replace")
    return kernel, ramdisk, cmdline


def check_arm64_image(kernel):
    """Return (text_offset, image_size, flags) and validate the kexec layout."""
    if kernel[:2] == b"\x1f\x8b":
        sys.exit("kernel is gzip-compressed; quest-kexec needs a raw ARM64 Image "
                 "(build the kernel with a decompressed Image, not Image.gz)")
    if len(kernel) < 64 or kernel[56:60] != b"ARM\x64":
        sys.exit("kernel is not a raw ARM64 Image (no ARM\\x64 magic at offset 56)")
    text_offset, image_size, flags = struct.unpack_from("<QQQ", kernel, 8)
    problems = []
    if text_offset != STAGING_TEXT_OFFSET:
        problems.append(f"text_offset {text_offset:#x} != {STAGING_TEXT_OFFSET:#x}")
    if image_size > STAGING_IMAGE_MAX:
        problems.append(f"image_size {image_size:#x} > staging max {STAGING_IMAGE_MAX:#x}")
    if image_size < len(kernel):
        problems.append(f"image_size {image_size:#x} < file size {len(kernel):#x}")
    if flags & 1:
        problems.append("big-endian kernel flag set")
    if flags & 6 not in (0, 2):
        problems.append("kernel does not want a 4K-page target (flags page bits)")
    if not flags & 8:
        problems.append("kernel does not permit placement away from the base of RAM (flag bit 3)")
    if problems:
        sys.exit("kernel will not stage for kexec:\n  - " + "\n  - ".join(problems))
    return text_offset, image_size, flags


def build_cmdline(src, rootdev, extra, add_default_extra):
    toks = [t for t in src.split() if t not in CMDLINE_STRIP]
    if rootdev is not None:
        toks = [t for t in toks if not t.startswith("q2_rootdev=")]
        toks.append(f"q2_rootdev={rootdev}")
    if add_default_extra:
        toks += DEFAULT_KEXEC_EXTRA.split()
    if extra:
        toks += extra.split()
    # de-dup while preserving order (a later explicit arg wins its position)
    seen, out = set(), []
    for t in toks:
        key = t.split("=", 1)[0]
        if key in seen and "=" in t:
            out = [x for x in out if x.split("=", 1)[0] != key]
        seen.add(key)
        out.append(t)
    return " ".join(out)


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("image", nargs="?", default="out/boot_steamos.img",
                    help="finished SteamOS boot image (default: out/boot_steamos.img)")
    ap.add_argument("-o", "--output", default="out/kexec", type=Path,
                    help="output directory (default: out/kexec)")
    ap.add_argument("--rootdev",
                    help="override q2_rootdev (a locked/kexec install keeps the "
                         "SteamOS root in a pinned file, not the userdata partition)")
    ap.add_argument("--extra", default="",
                    help="extra kernel args to append after the kexec glue")
    ap.add_argument("--no-default-extra", action="store_true",
                    help="do not append '" + DEFAULT_KEXEC_EXTRA + "'")
    ap.add_argument("--initrd-max", type=lambda s: int(s, 0), default=STAGING_INITRD_MAX,
                    help="initramfs staging window to warn against "
                         f"(default {STAGING_INITRD_MAX:#x}; raise it only if the "
                         "quest-kexec loader window was grown to match)")
    a = ap.parse_args()

    img = Path(a.image)
    if not img.is_file():
        sys.exit(f"missing {img} - build the SteamOS boot image first (image/build.sh)")
    kernel, ramdisk, cmdline_src = unpack_boot(img.read_bytes())
    text_offset, image_size, flags = check_arm64_image(kernel)
    cmdline = build_cmdline(cmdline_src, a.rootdev, a.extra, not a.no_default_extra)

    a.output.mkdir(parents=True, exist_ok=True)
    (a.output / "Image").write_bytes(kernel)
    (a.output / "initramfs").write_bytes(ramdisk)
    (a.output / "cmdline").write_text(cmdline + "\n")

    manifest = dict(
        source_image=str(img),
        kernel=dict(size=len(kernel), image_size=image_size, text_offset=text_offset,
                    flags=flags, sha256=hashlib.sha256(kernel).hexdigest(),
                    crc32=f"{zlib.crc32(kernel):08x}"),
        initramfs=dict(size=len(ramdisk), sha256=hashlib.sha256(ramdisk).hexdigest(),
                       crc32=f"{zlib.crc32(ramdisk):08x}"),
        cmdline=cmdline,
        staging=dict(image_max=STAGING_IMAGE_MAX, initrd_max=a.initrd_max,
                     initrd_fits=len(ramdisk) <= a.initrd_max),
    )
    (a.output / "manifest.json").write_text(json.dumps(manifest, indent=2) + "\n")

    mib = lambda n: f"{n / 1048576:.1f} MiB"
    print(f"kexec payload written to {a.output}/")
    print(f"  Image        {mib(len(kernel))}  (image_size {image_size:#x}, fits {STAGING_IMAGE_MAX:#x})")
    print(f"  initramfs    {mib(len(ramdisk))}")
    print(f"  cmdline      {cmdline}")

    if len(ramdisk) > a.initrd_max:
        over = len(ramdisk) - a.initrd_max
        print(f"\nWARNING: initramfs is {mib(len(ramdisk))}, over the quest-kexec "
              f"staging window of {mib(a.initrd_max)} by {mib(over)}.", file=sys.stderr)
        print("prepare.py will reject it as-is.  Grow the kexec loader's initrd "
              "window further (QKX_DTB/QKX_HI in module/loader.c and the matching "
              "QKX_INITRD_MAX in prepare.py, plus STAGING_INITRD_MAX here), keeping "
              "the window inside one System RAM band, or load the initramfs from "
              "storage instead of staged RAM.  This is quest-kexec loader work, not "
              "a boot-image change.", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
