# Project structure and verification

The normal user entry point is `install-fedora`. Staged development commands
remain available for diagnosis and recovery; they are not the normal
installation procedure.

| Area | Main files |
| --- | --- |
| USB image and writer | `tools/build_fedora_usb.py`, `tools/write_usb.py` |
| Live overlay and launcher | `assets/live/` |
| Preferences and orchestration | `tools/install_fedora.py`, `lapis/installation.py` |
| Installer USB identity and mount | `lapis/installer_usb.py`, `tools/mount_installer_usb.py` |
| Disk plan, backup, Btrfs, boot commit | `tools/native_fedora.py` |
| Base packages, desktop, target setup | `tools/configure_fedora.py`, `lapis/fedora_target.py` |
| Password provisioning | `lapis/accounts.py` |
| Wi-Fi profile transfer | `lapis/network.py` |
| Slot selection and signed images | `lapis/slots.py`, `lapis/boot.py`, `lapis/gpt.py` |
| Installed kernel updates | `tools/auto_fedora_update.py`, `assets/installed/60-lapis.install` |
| Boot recovery | `tools/restore_fedora_boot.py`, `tools/restore_bootstrap.py`, `tools/build_recovery_kit.py` |
| Minimal boot snapshots | `lapis/backup.py` |

## Installation boundaries

Preferences are collected without mutating target or backup storage. After
preflight, one `YES` produces an in-process `InstallApproval` bound to the disk
path, disk GUID, and GPT hash. Both formatting and the final boot commit check
that approval against their current plan; they do not ask again. Disk identity,
geometry, backup integrity, Btrfs subvolume selection, candidate signatures,
and read-back verification remain automatic checks throughout the workflow.

The minimal boot backup finishes before internal formatting. After commit, a
second snapshot preserves the Linux boot set and post-install GPT for the
blank-drive bootstrap restore path. The physical
installer USB is accepted as backup storage through its separate writable
partition. The target disk itself and read-only filesystems remain invalid
backup destinations.

The target's `/dev`, `/sys`, and `/proc` trees are temporarily bound for package
installation. `/run` is not bound to the target. A temporary resolver file lets
chroot DNF use live DNS; the installed resolver configuration is restored
afterward. Stock kernel/GRUB hooks are masked during provisioning, then the
Lapis-specific kernel updater is installed during configuration.

Those pseudo-filesystems remain mounted while the local authselect profile
is generated and checked and the account is created. Password hashes are
supplied on stdin to the live host's `chpasswd --prefix <target> --encrypted`;
this avoids depending on the target's PAM or SELinux runtime for shadow updates.
The stored hash is checked against the supplied password, and target SELinux
labels are applied after the host trees are unmounted. Password-update errors
retain stderr diagnostics with passwords and hashes redacted.

Fedora desktop environment IDs are defined in `lapis/installation.py` and passed
to chroot DNF as `@<environment>`. The display manager is selected from units
actually installed by that environment. None excludes kmscon packages, masks
its units, overrides its Fedora presets, and enables the standard getty
template (including its `autovt@.service` alias). All installation choices use
`graphical.target`, which includes `multi-user.target` and starts the
`display-manager.service` alias when present. This keeps console-only installs
usable while allowing greetd or another display manager to be enabled later
without a separate boot-target change.

The locale defaults to `en_US.UTF-8` and is collected before preflight. The
chosen `glibc-langpack-<language>` package and its UTF-8 locale data file must
exist in Fedora metadata before confirmation. After installation, the target's
`locale -a` must include the selected locale before it is written to
`/etc/locale.conf`. `C.UTF-8` needs no additional language package.

The public boot tools included in kits are explicitly enumerated and hashed in
`assets/vendor-runtime.json`, with upstream source URLs and pinned revisions.
Extra local files and private keys are not copied into the runtime bundle.
Full source checkouts for the loader and Android boot tools are fetched to
`build/sources`; no upstream source trees are committed in `vendor/`. Generated
kits contain a minimal `vendor/` runtime instead. Tool lookup uses that bundled
runtime in a kit and `build/sources` in a development checkout.
`tools/build_fedora_usb.py` handles source fetching, loader compilation, and
image creation in one command. It checks prerequisites before any download,
reports eight numbered stages, and keeps verbose tool output in a per-attempt
`build.log`. Measured byte progress is used for checksums and image assembly;
stages without a measurable total emit elapsed-time updates. The standalone
fetch and loader scripts expose the same functions for development use.

Live media are identified by their Fedora Workstation volume label and x86-64
kernel rather than an exact ISO hash or running kernel version. The builder
records the actual ISO hash and supports an optional `--iso-sha256` check.
Live runtime checks require Fedora's Workstation variant, x86-64, and a live
Workstation root command line. Hardware, GPT, USB identity, and loader checks
still apply. Package preflight explicitly queries the Fedora 45 target
repositories even when the live environment is a different release.

## Compact USB images

`lapis-usb-v2` images contain the live ISO, signed boot set, `misc`, and their
own valid GPT. The backup partition is deferred metadata in the manifest, with
UUIDs already embedded in the live initramfs configuration. The writer checks
capacity, adds the backup entry to the destination GPT, transfers and verifies
only the boot image, then formats the backup partition directly. It verifies
the small filesystem superblock rather than reading the empty partition.
Older embedded-backup images use the same optimized path by transferring only
the prefix before the trailing backup partition. Source authentication still
covers the entire old image file, while USB I/O excludes its empty backup area.

Partition-node formatting requires matching kernel geometry and GUIDs on Linux,
or matching parent disk, size, and partition GUID on macOS. The formatter is
located before erasure is offered. No running write process is hot-reloaded
when these scripts are edited.

## Tests

Run from the repository root:

```sh
PYTHONDONTWRITEBYTECODE=1 python3 -m unittest discover -s tests -v
```

Tests use temporary directories, fake provisioning commands, real libcrypt
password checks and encrypted shadow updates in a temporary prefix, regular-file
ext4/GPT images, and signed boot sets. They do not
format, mount, or write physical disks. The signed-image tests need OpenSSL, the
loader harness, and a readable Linux kernel. Set `LAPIS_TEST_KERNEL` if the
running kernel's `/usr/lib/modules/<version>/vmlinuz` is unavailable.

For packaging and real boot-image checks, prepare dependencies first:

```sh
python3 -B tools/fetch_sources.py
python3 -B tools/build_loader.py
```

The one-command USB builder also prepares these dependencies automatically.

The suite covers preference retry and cancellation, locale normalization,
language data preflight and target configuration, pseudo-mount lifetime through
account creation and error cleanup, real systemctl getty alias replacement,
ordering of confirmation
and backup before formatting, identity-bound authorization, password hashing
and encrypted shadow updates, Wi-Fi secrets/permissions, USB partition sizing
and identity checks, archive packaging, and both A/B slots including rollback.
Updater tests also exercise preparation metadata and the automatic updater's
handoff to commit for both slots, including refusal to commit after a
preparation failure. Those tests stub image building and disk access while
using real staging files and metadata hashes.

Full DNF provisioning and GUI login are simulated in automated tests. They need
an on-device test with the chosen Workstation live ISO to validate actual package
transactions, USB discovery timing, SELinux behavior, Wi-Fi reconnection,
display-manager startup, and first boot.
