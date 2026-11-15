#!/usr/bin/env python3
"""Prepare (but do not install) a Fedora kernel update for ASUS Lapis.

Run on an installed Fedora Lapis system. Preparation does not write boot partitions; use commit_fedora_update.py after review.
"""
import argparse
import hashlib
import os
from pathlib import Path
import re
import shutil
import stat
import struct
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from lapis.common import ROOT, output, read_json, require, run, save_json, sha256
from lapis.gpt import read_gpt
from lapis.slots import active_slot, running_slot
from tools.prepare_fedora_boot import check_profile, disk_size


def boot_kernel_sha256(source, part):
    """Read the v4 boot image kernel bytes from a slot, without trusting its size."""
    offset = part['start_lba'] * 512
    source.seek(offset)
    header = source.read(44)
    require(len(header) == 44 and header[:8] == b'ANDROID!', 'Current boot partition is not an Android boot image')
    kernel_size = struct.unpack_from('<I', header, 8)[0]
    version = struct.unpack_from('<I', header, 40)[0]
    require(version == 4 and 0 < kernel_size <= part['size'] - 4096, 'Unexpected current boot image layout')
    source.seek(offset + 4096)
    digest = hashlib.sha256()
    remaining = kernel_size
    while remaining:
        block = source.read(min(1024 * 1024, remaining))
        require(block, 'Short read of current boot kernel')
        digest.update(block)
        remaining -= len(block)
    return digest.hexdigest()


def valid_kernel_version(value):
    return re.fullmatch(r'[A-Za-z0-9._+~-]+', value) is not None


def verify_system(device, kver, expected_boot=None):
    require(os.geteuid() == 0, 'Run as root on the installed Fedora system')
    require(Path('/sys/class/dmi/id/product_name').read_text().strip() == 'Lapis', 'Wrong device')
    require(re.fullmatch(r'/dev/nvme[0-9]+n[0-9]+', str(device)) is not None, 'Expected a whole NVMe disk')
    require(stat.S_ISBLK(device.stat().st_mode), 'Expected a block device')
    require((Path('/sys/class/block') / device.name / 'removable').read_text().strip() == '0', 'Disk must be internal')
    require(output(['blockdev', '--getss', device]) == '512', 'Expected 512-byte sectors')
    require(valid_kernel_version(kver), 'Invalid kernel version')
    release = Path('/etc/os-release').read_text()
    require(re.search(r'^ID=fedora$', release, re.M) and re.search(r'^VERSION_ID=45$', release, re.M), 'Expected Fedora 45')
    require(output(['findmnt', '-no', 'FSTYPE', '--mountpoint', '/']) == 'btrfs', 'Root is not Btrfs')
    require(output(['findmnt', '-no', 'FSROOT', '--mountpoint', '/']) == '/@', 'Root is not the expected @ subvolume')
    root_uuid = output(['findmnt', '-no', 'UUID', '--mountpoint', '/'])
    part = Path(str(device) + 'p1')
    require(output(['blkid', '-s', 'UUID', '-o', 'value', part]) == root_uuid, 'Mounted root is not this disk userdata')
    root_source = output(['findmnt', '-no', 'SOURCE', '--mountpoint', '/']).split('[', 1)[0]
    require(Path(root_source).resolve() == part.resolve(), 'Mounted root does not come from the specified partition')
    size = disk_size(device)
    with device.open('rb') as f:
        parts = check_profile(read_gpt(f, size))
        slot = active_slot(parts)
        reported = running_slot()
        require(reported is None or reported == slot, 'Firmware boot slot differs from the preferred GPT slot')
        boot_kernel_hash = boot_kernel_sha256(f, parts['boot_' + slot])
    require('factory_pvmfw_a' in parts and 'factory_pvmfw_b' in parts, 'Existing boot layout is not the expected installed layout')
    userdata = Path('/dev/disk/by-partuuid') / parts['userdata']['partuuid']
    require(userdata.resolve() == part.resolve(), 'userdata identity differs from the mounted root')
    installed = Path('/usr/lib/modules') / kver
    kernel = installed / 'vmlinuz'
    require(kernel.is_file() and not kernel.is_symlink(), 'Requested kernel image is unavailable')
    require((installed / 'modules.dep').is_file(), 'Requested kernel modules are incomplete')
    running = output(['uname', '-r'])
    running_modules = Path('/usr/lib/modules') / running
    require((running_modules / 'modules.dep').is_file(), 'Running kernel modules are unavailable')
    running_image = running_modules / 'vmlinuz'
    require(running_image.is_file() and not running_image.is_symlink(), 'Running kernel image is unavailable')
    require(sha256(running_image) == boot_kernel_hash or
            (expected_boot is not None and boot_kernel_hash == expected_boot),
            'Current slot ' + slot.upper() + ' kernel does not match the running kernel or recorded pending image')
    return root_uuid, running, kernel


def prepare(device, kernel_version, destination, expected_boot=None):
    for tool in ('openssl', 'dracut', 'lsinitrd'):
        require(shutil.which(tool) is not None, 'Required updater command is missing: ' + tool)
    root_uuid, running, kernel = verify_system(device, kernel_version, expected_boot)
    require(not destination.exists() and not destination.is_symlink(), 'Output already exists')
    require(not str(destination.resolve()).startswith('/dev/'), 'Output cannot be under /dev')
    destination.mkdir(mode=0o700, parents=False)
    initrd = destination.resolve() / 'initramfs.img'
    run([sys.executable, ROOT / 'tools/build_fedora_initramfs.py', '--kernel-version', kernel_version, '--output', initrd])
    run([sys.executable, ROOT / 'tools/prepare_fedora_boot.py', '--disk', device,
         '--kernel', kernel, '--initrd', initrd, '--root-uuid', root_uuid,
         '--output', destination.resolve() / 'bootset'])
    proof = destination / 'bootset/validation.json'
    save_json(destination / 'update.json', {'running_kernel': running, 'candidate_kernel': kernel_version,
              'slot': read_json(proof)['slot'],
              'root_uuid': root_uuid, 'kernel_sha256': sha256(kernel), 'initrd_sha256': sha256(initrd),
              'validation_sha256': sha256(proof), 'status': 'prepared only; not installed'})


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--device', type=Path, required=True)
    p.add_argument('--kernel-version', required=True)
    p.add_argument('--output', type=Path, required=True, help='new private staging directory')
    args = p.parse_args()
    prepare(args.device, args.kernel_version, args.output)
    print('Update candidate prepared and loader-checked; no boot partitions were written.')
    print('Use commit_fedora_update.py to review and explicitly install this candidate before rebooting.')


if __name__ == '__main__':
    main()
