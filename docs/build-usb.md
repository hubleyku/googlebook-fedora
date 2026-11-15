# Build the installer USB

Build on Linux with Python 3.10+, OpenSSL, and `bsdtar` available. The image builder
does not need `mkfs.ext4`: it produces a compact boot image without an embedded
backup filesystem. The bundled loader harness is a Linux x86-64 executable.

The first build needs network access to fetch pinned public dependencies. Start
from a clone or extracted source archive of this project; no pre-populated
`vendor/` directory is required.

## Build with one command

On Fedora, install the build tools if needed:

```sh
sudo dnf install gcc make git openssl bsdtar
```

Supply a **Fedora Workstation x86-64 live ISO**. No particular Fedora release,
compose, or live kernel version is required. The builder checks the
`Fedora-WS-Live-*` volume label, locates the live kernel and initramfs, verifies
that the kernel is x86-64, and records the supplied ISO's SHA-256 in the output
manifest. Both `boot/x86_64/loader/` and `images/pxeboot/` boot-file layouts are
supported.

From the repository root:

```sh
python3 -B tools/build_fedora_usb.py \
    --iso /path/to/Fedora-Workstation-Live-x86_64.iso \
    --output build/lapis-fedora-installer.img
```

This single command fetches and verifies pinned sources, compiles the loader
harness, and builds the image. No separate `fetch_sources.py` or `build_loader.py`
commands are needed. Source checkouts are cached under `build/sources`; subsequent
builds verify them and use incremental compilation. `--sources /path/to/cache`
selects another cache. The older helper scripts remain available for development.
System packages are checked up front and are not installed automatically.

To check an expected ISO checksum as well, add `--iso-sha256 <SHA256>` using
the value for your chosen ISO. There is no built-in checksum tied to one release.
The resulting installation continues to target Fedora 45 and its configured
kernel package; the live ISO's release does not select the target release.
The live environment must provide the installer tools, including `dnf5`.

The terminal reports eight steps:

1. Check tools, inputs, Workstation media, and the ISO checksum if supplied.
2. Fetch and verify pinned public sources.
3. Compile the boot-validation harness.
4. Extract the live kernel and initramfs.
5. Bundle live configuration and the installer.
6. Build and sign boot images.
7. Assemble the compact USB image.
8. Validate bootability and save checksums.

Hashing and image assembly show percentages, byte counts, and transfer rates.
Downloads, compilation, and other operations show periodic elapsed-time updates
while they run. Detailed compiler and tool output is saved in `build.log` inside
the unique build directory printed at startup. On failure, that directory and
its log are preserved and printed again. A failed source download can be retried
with the same command; existing source edits are never reset.

Android and ChromiumOS sources are downloaded into `build/sources/`, which is
ignored by Git. The builder uses those pinned checkouts for both compilation
and boot-image tools. `--vendor /path/to/runtime` can select a separate verified
mkbootimg/AVB runtime directory when needed.

The installer kit is included by default. The builder adds the hardware startup
configuration, `install-fedora` launcher, USB identity configuration, and backup
automount service to the live initramfs. Public mkbootimg/AVB runtime files are
checked against `assets/vendor-runtime.json` and copied into the generated kit
with their license notices. The installed kit retains these files for offline
kernel boot-image updates. No Git metadata is needed in the resulting kit.

The completed image is checked with the loader harness, and the output includes
a `.manifest.json` and `.sha256`. The resulting USB layout is:

| Partition | Purpose |
| --- | --- |
| ISO9660 | Original Fedora live filesystem |
| `boot_a`, `init_boot_a`, `vendor_boot_a`, `vbmeta_a` | Signed live boot images |
| `misc` | Boot-control state |
| `LAPIS-BACKUP` / filesystem label `LapisBackups` | Writable ext4 storage created separately by the writer |

Only the ISO, boot images, and `misc` are stored in the `.img` file. The manifest
records the backup partition's requested size and UUIDs. The writer adds that
partition to the physical USB's GPT and formats it after copying and verifying
the boot image. Increasing `--backup-size-mib` does not enlarge the image file.

The backup partition defaults to **2048 MiB**. The minimal snapshot is about
112 MiB before metadata: four active-slot images and `misc`, plus GPT tables
and the disk plan. Zero-filled image padding is stored as sparse holes. A
completed install also saves a Linux bootstrap snapshot. Extra space
accommodates both snapshots and installation state; the builder accounts for
ext4 overhead and rejects a size below its calculated minimum. To reserve more
space, for example for repeated installations:

```sh
python3 -B tools/build_fedora_usb.py \
    --iso /path/to/Fedora-Workstation-Live-x86_64.iso \
    --output build/lapis-fedora-installer.img --backup-size-mib 4096
```

Choose a USB large enough for the compact image **plus** the requested backup
space and GPT metadata. The manifest records the minimum capacity. The writer
checks capacity and places the secondary GPT at the end of the physical USB.
The backup filesystem keeps the size selected at build time.

## Write the USB

Identify the external USB device. On Linux use its whole-disk `/dev/sdX` path;
the writer also supports whole USB disks on macOS through `/dev/diskN`.
The writer needs `mkfs.ext4` or `mke2fs` from e2fsprogs. On Fedora, install the
`e2fsprogs` package. On macOS, install e2fsprogs separately; the writer also
checks the standard Homebrew locations for `mkfs.ext4`. OS partition-node
refresh and formatting still need validation on physical macOS hardware.

First inspect the proposed write:

```sh
python3 -B tools/write_usb.py \
    --image build/lapis-fedora-installer.img --device /dev/sdX
```

Then write it:

```sh
sudo python3 -B tools/write_usb.py \
    --image build/lapis-fedora-installer.img --device /dev/sdX --apply
```

The writer shows the selected device, gives a red erase warning, and accepts
only `YES`. It reports progress while checking the source, copying boot data,
flushing writes, verifying boot data, and creating the backup filesystem. It
checks both GPT tables and the new filesystem's UUID, label, and capacity;
gigabytes of empty backup space are neither copied nor read back. Filesystem
creation avoids whole-partition discard and initializes only required metadata.
This USB creation acknowledgment is separate from the acknowledgment shown
later when installing Fedora on the Googlebook.

Use this writer rather than `dd` for compact images: `dd` alone will not create
the backup partition. The writer also accepts older images containing the
empty backup filesystem. It authenticates the old source file locally, skips
the trailing backup area during USB copy and read-back, and creates a fresh
empty filesystem with the expected UUID. Existing large images do not need to
be rebuilt to benefit from this faster write path.

Writing a fresh installer image erases existing USB contents, including any
backups already stored on that USB. Copy backups elsewhere before rebuilding
the USB if you need to retain them.

## Boot prerequisites

The Googlebook must already be in developer mode with its bootloader unlocked
and external boot permitted. Bootloader unlocking erases factory user data;
prepare personal backups and separate factory recovery media first. Follow the
[foundation project's device-specific instructions](https://github.com/jmklex/asus-googlebook-omarchy)
for that setup. This builder does not unlock or flash the machine's firmware.
Select external boot from the developer screen, check that the Fedora live
desktop works on your device, then follow [the installation guide](install.md).

## Optional build modes

`--without-installer` builds a live-only image. The older `--include-kit` flag
is still accepted, but is unnecessary because inclusion is now the default.

## Retry a failed build

Each attempt uses a unique `.build` directory, so an earlier failed work directory
does not block a retry. Existing output images, manifests, and checksum files
are preserved. If a failure left one of those output files, retry with a fresh
output name, such as `--output build/lapis-fedora-installer-retry.img`. Use that
same new image path in the writer command afterward.
