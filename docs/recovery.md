# Lapis boot-image backup and recovery

The installer, updater, and restore tool detect the preferred bootable slot from
the GPT attributes on `vbmeta_a` and `vbmeta_b`. They support either A or B, require
the selected slot to be marked successful, and refuse tied priorities or a
preferred trial slot. They do not switch slots or change their priority flags.
On the installed system, updates also check the firmware's slot report in the
kernel command line or `/proc/bootconfig`, when available. A disagreement with
the GPT stops the update.

The tools write the selected slot's `boot`, `init_boot`, `vendor_boot`, and
`vbmeta` images together. The inactive slot's payloads remain unchanged. The
installer additionally renames the pvmfw entries and clears the pending recovery
command in `misc`, as before. A newly built live USB uses its own slot A; it is
independent of the internal drive's selected slot.

## What the backups contain

The installer automatically stores its backup on the writable partition of the
same USB used for live boot, under
`/run/lapis-usb/backups/<UTC timestamp>-<disk GUID prefix>/`. No second drive is
needed. The initial snapshot contains both GPT tables, a disk plan, the active
slot's four boot images, and `misc`: about 112 MiB of logical image data before
metadata. Zero padding is stored sparsely. This snapshot reflects the state
before installation. After success, `linux-bootstrap/` contains a second small
snapshot of the working Linux boot images and post-installation GPT, including
the pvmfw name changes. `installed-state/` and `installer-state/` retain the
installation records without duplicating those images. Neither snapshot
contains a Fedora root filesystem or personal files.

Kernel-update backups contain the previous four boot images for the selected
slot, hashes, the disk GUID, and kernel metadata. Automatic backups are stored
under `/var/lib/lapis-fedora/kernel-backups`. These subsequent kernel-update
backups are independent of the installation backup on the USB. Copy them to
the USB when you want them available independently of the installed filesystem.

New plans, manifests, and update records include the slot. Older A-only update
backups and prepared candidates remain readable: the image names identify A.
A backup targeting a different slot from the currently preferred slot is
rejected; images are never silently redirected from one slot to the other.

## Retry an update that failed during preparation

Check the recorded status and service journal:

```sh
sudo lapis-kernel-update --status
sudo journalctl -u 'lapis-kernel-update@*' -b
```

A preparation failure stops before the automatic updater commits new boot
images. Diagnose the recorded error and correct its cause before retrying.
Failures during commit need backup and write-status review before rebooting.

After correcting a preparation error, restart the service for the installed
kernel you intend to activate. Replace `KERNEL_VERSION` below with that exact
version from `/usr/lib/modules/`:

```sh
sudo systemctl restart "$(systemd-escape --template=lapis-kernel-update@.service KERNEL_VERSION)"
sudo lapis-kernel-update --status
```

The retry rebuilds and validates the candidate, then backs up, writes, and
verifies the boot images. Wait for a successful `updated` status before
rebooting into the new kernel. Keep the recovery USB available.

## Prepare a blank replacement drive without Googlebook OS

Use the **`linux-bootstrap/` directory from a completed installation**. The
pre-install snapshot may contain the previous OS's boot images and is not
accepted by the bootstrap restore command.

Boot the installer USB, connect AC power, and change to `/opt/lapis-kit`. The
replacement internal disk must have the same exact capacity as the supported
512 GB profile. Automatic layout resizing is not implemented. Unmount any
target-disk filesystems, then inspect the proposed restore:

```sh
sudo python3 -B tools/restore_bootstrap.py \
    --device /dev/nvme0n1 \
    --backup /run/lapis-usb/backups/INSTALLATION/linux-bootstrap
```

Replace `INSTALLATION` with the timestamped backup directory. After the dry run
passes, repeat with `--apply`. A red warning and one uppercase
`YES` acknowledgment authorize replacing the GPT and small boot regions. The
tool checks the backup hashes and loader acceptance, restores the boot images,
`misc`, and both GPT tables, and reads them back. It accepts a blank target and
does not depend on an existing Googlebook OS installation or valid target GPT.

This prepares the partition layout and Linux boot chain. A Linux root
filesystem must be installed separately; the restored installed-system kernel
still references the original root UUID until a new installer generates its
boot images. Run `install-fedora` from the USB to create that new installation.
Other partition payloads are left untouched. No machine firmware is flashed.

This path is tested with blank sparse disk images and the loader harness. A
physical blank-drive recovery test is still required to establish whether the
device firmware needs additional partition contents beyond the minimal set.

## Restore an existing supported disk

This procedure restores only the selected slot's four boot images. It does not
restore GPT tables, recreate partitions on a blank drive, restore a root
filesystem, or reinstall kernel modules. The matching Linux installation and
kernel modules must still be present. Use the bootstrap restore command above
when the partition table is missing or the target is blank.

1. Boot the project's supported Fedora Workstation Lapis live USB and connect AC power.
   Its backup partition mounts automatically at `/run/lapis-usb`. The tools are
   available in `/opt/lapis-kit`; change to that directory to use the commands
   below. Alternatively, use an extracted standalone recovery kit.
2. Unmount all filesystems on the internal target disk. The restore tool also
   checks the machine, live kernel, disk layout, selected slot, and backup disk
   identity. A changed slot, damaged GPT, or pending Android recovery command
   requires investigation before this tool can proceed.
3. From the repository or extracted recovery kit, run a dry run. Replace the
   device and backup path with the verified target and backup directory:

   ```sh
   sudo python3 -B tools/restore_fedora_boot.py \
       --device /dev/nvme0n1 --backup /path/to/backup
   ```

   This checks backup hashes and geometry, then validates the recovered boot
   set with the loader harness using a temporary sparse file. It does not write
   the target disk. Loader validation is not a physical boot test.

4. After reviewing the dry-run result, repeat the command with `--apply`. The
   tool asks for confirmation naming the detected slot and device, repeats its
   checks, restores the four images, and reads back every written image.

   ```sh
   sudo python3 -B tools/restore_fedora_boot.py \
       --device /dev/nvme0n1 --backup /path/to/backup --apply
   ```

If restoration fails or is interrupted, retain the USB and backup and complete
recovery before trying to boot internally. This tool does not automatically
switch to the other slot.

## Build a standalone recovery kit

```sh
python3 -B tools/build_recovery_kit.py --output /path/to/lapis-recovery-kit.tar.gz
```

The normal installer USB already includes these recovery tools. For a separate
copy, store the archive alongside the backup and extract it before use. Prepare the fetched sources and loader as described in
[the development guide](development.md#tests) before packaging from a source
checkout. The kit contains the restore tool, its dependencies, this guide, the tested partition
profile, and loader harness. It contains no partition backups.

## Test slot handling without touching hardware

```sh
PYTHONDONTWRITEBYTECODE=1 python3 -m unittest discover -s tests -v
```

The integration test uses disposable sparse regular files and real signed boot
images to exercise both slots, backup, restoration, interrupted-write rollback,
and refusal of a slot change after staging. It verifies that the other slot and
GPT remain unchanged. It needs OpenSSL, a built loader harness, and a readable
Linux kernel at `/usr/lib/modules/$(uname -r)/vmlinuz`; alternatively set
`LAPIS_TEST_KERNEL` to a kernel image. These tests do not boot Linux userspace or
prove that a physical machine will boot successfully.
