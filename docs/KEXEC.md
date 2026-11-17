# Booting SteamOS through kexec (locked headsets)

The normal install flashes `out/boot_steamos.img` to the boot partition with
fastboot, which needs an **unlocked** bootloader. Kexec is the path for a
**locked** headset: a kernel module in the running Quest OS starts the SteamOS
kernel directly, without the bootloader. The loader itself lives in the
separate [`quest-kexec`](../../quest-kexec) project and needs **root** on Quest
OS (kexec removes the need to unlock, not the need for root).

This repository's job for that path is to hand `quest-kexec` the three loose
pieces it wants, taken from the **same** boot image the flashed path validates,
so the two boots run identical bytes.

## 1. Build the payload

Build the boot image as usual, then extract the kexec payload:

```sh
sh image/build.sh                       # produces image/out/boot_steamos.img
python3 image/tools/make_kexec_payload.py image/out/boot_steamos.img -o image/out/kexec
# or in one step:
KEXEC=1 sh image/build.sh               # also writes image/out/kexec/
```

It writes:

| File                     | What it is                                             |
| ------------------------ | ------------------------------------------------------ |
| `kexec/Image`            | the raw ARM64 kernel (uncompressed, `text_offset` 0x80000) |
| `kexec/initramfs`        | the SteamOS ramdisk, byte-for-byte                     |
| `kexec/cmdline`          | the kernel command line for `/chosen/bootargs`         |
| `kexec/manifest.json`    | sizes, sha256/crc32, and whether the initramfs fits    |

### The command line

The flashed boot image's command line already carries every SteamOS argument
(display, GPU, root, loglevel). The tool reuses it and makes only the two
changes kexec needs:

- **drops `softdog.soft_panic=1`** — the soft watchdog would panic the target
  during the hand-off window (quest-kexec quiesces the real watchdog in the DTB).
- **appends `rdinit=/init nokaslr qkx_qmp_adopt=1`** — run the SteamOS init,
  keep the staged addresses fixed (no KASLR), and adopt the AOP/QMP link the
  loader leaves up.

A locked install keeps the SteamOS root in a pinned file rather than the
`userdata` partition, so override the root selector for it:

```sh
python3 image/tools/make_kexec_payload.py image/out/boot_steamos.img \
    -o image/out/kexec --rootdev <what the locked install expects>
```

`--extra "<args>"` appends further arguments; `--no-default-extra` omits the
kexec glue if quest-kexec adds it itself.

## 2. Hand off to quest-kexec

On the host, with the Quest 2 rooted over adb:

```sh
cd ../quest-kexec
tools/capture.sh out/captured            # runtime.dtb, iomem, cmdline, config (read-only)
python3 tools/prepare.py \
    --live-dtb out/captured/runtime.dtb --iomem out/captured/iomem \
    --kernel-image ../quest2-steamos/image/out/kexec/Image \
    --initramfs   ../quest2-steamos/image/out/kexec/initramfs \
    --cmdline "$(cat ../quest2-steamos/image/out/kexec/cmdline)" \
    --output out/payload
tools/run.sh out/payload
```

Use `prepare.py` directly, **not** `prep.sh`: `prep.sh` rebuilds the command
line from Android's own `/proc/cmdline` and would drop every SteamOS argument.
Pass this repo's `cmdline` through `--cmdline` instead.

Return to Quest OS is a normal reboot.

## Staged initrd window

A full SteamOS ramdisk (~19 MiB internal, ~39 MiB USB installer) is far bigger
than the 2 MiB initramfs window quest-kexec's original Quest Pro layout used.
The loader window has been grown to fit it:

| Region  | Address      | Size                      |
| ------- | ------------ | ------------------------- |
| kernel  | `0x90080000` | ≤ 47.5 MiB (`image_size`) |
| initrd  | `0x93000000` | **48 MiB**                |
| DTB     | `0x96000000` | 2 MiB                     |
| top     | `0x96200000` | —                         |

The window (`0x90000000`–`0x96200000`) sits in one clean System RAM band on the
Quest 2 — above the top firmware carveout (`pil_spss`, ends `0x8bf00000`) and
below the next one up (`ramoops`, `0x9ba00000`). Both the loader (`qkx_ram_cb`)
and `prepare.py` (`check_ram`) re-check the whole window against the live
`/proc/iomem`, so a window that is not clear fails staging instead of corrupting
a boot. The matching constants live in `quest-kexec` (`QKX_INITRD_MAX` /
`QKX_IMAGE_MAX` in `module/loader.c` and `prepare.py`) and in
`STAGING_INITRD_MAX` here; keep them in sync. A ramdisk over 48 MiB still needs
the window grown further.

**The loader module must be rebuilt** against the Quest 2's exact running Android
kernel after this change (`uname -r`, e.g. `4.19.325-cip128-st12-g…`). The
prebuilt `.ko` in quest-kexec is for Quest Pro and will not load here.

## Building the loader module (CI)

The `build-quest-kexec-module` workflow builds `quest_kexec.ko` (plus
`marker_read.ko`, `usb_log.ko`) against the **stock** Quest 2 kernel — the one
running on the headset that loads the module, not the SteamOS target kernel.

For the module to `insmod` on a locked, rooted Quest OS its symbol CRCs and
vermagic must match that kernel. This firmware has `CONFIG_MODVERSIONS=y` and no
module signing, so the build is pinned to reproduce it exactly:

- **source**: the stock commit in `uname -r` (the `-g<sha>` suffix) from
  `facebookincubator/oculus-linux-kernel`;
- **config**: `config/stock-hollywood.config`, captured from the device's
  `/proc/config.gz`;
- **vermagic**: `CONFIG_LOCALVERSION` is pinned so `kernel.release` equals the
  firmware's `uname -r` regardless of the CI checkout's git state.

The workflow verifies the built `.ko`'s vermagic against `target_release` and
warns on a mismatch. The artifact `quest-kexec-module` holds the `.ko` files,
`Module.symvers` and the built `kernel.release`.

**Two things to get right before running it:**

1. **Push the grown-window change.** The workflow fetches the module *source*
   from `kexec_repo`/`kexec_ref`. The `QKX_DTB`/`QKX_HI` edit lives in your local
   quest-kexec checkout — push it (to a branch) and point `kexec_ref` at it, or
   the module built here keeps the old 2 MiB window. The build log prints the
   `QKX_*` defines it compiled so you can confirm `QKX_HI 0x96200000`.
2. **Match the source commit.** `kernel_commit` defaults to the `-g<sha>` from
   `uname -r`. If your firmware differs, set it to your own `uname -r`'s sha (and
   recapture `config/stock-hollywood.config` from your headset's
   `/proc/config.gz`).

Then it's Step 1: push the `.ko` to the headset and `insmod` it with the default
`execute=0` (staging only, reversible) before any jump — **not** quest-kexec's
`run.sh`, which forces `execute=1`.
