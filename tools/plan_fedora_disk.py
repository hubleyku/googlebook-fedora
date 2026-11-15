#!/usr/bin/env python3
"""Read-only inspection of an ASUS Lapis disk for a future Fedora installer.

This tool does not format, mount, rename, or write any disk. It is not an
installer and does not guarantee that installation will succeed.
"""
import argparse
from pathlib import Path
import re
import stat
import subprocess
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from lapis.common import require
from lapis.gpt import read_gpt
from lapis.slots import active_slot
from tools.prepare_fedora_boot import check_profile, disk_size


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--device', type=Path, required=True, help='whole internal NVMe disk, e.g. /dev/nvme0n1')
    args = p.parse_args()
    device = args.device
    require(re.fullmatch(r'/dev/nvme[0-9]+n[0-9]+', str(device)), 'Specify a whole /dev/nvmeNnN disk')
    require(stat.S_ISBLK(device.stat().st_mode), 'Expected a block device')
    require(Path('/sys/class/dmi/id/product_name').read_text().strip() == 'Lapis', 'This is not an ASUS Lapis')
    require((Path('/sys/class/block') / device.name / 'removable').read_text().strip() == '0', 'Expected an internal disk')
    sector = subprocess.check_output(['blockdev', '--getss', str(device)], text=True).strip()
    require(sector == '512', 'Only 512-byte logical sectors are supported')
    size = disk_size(device)
    with device.open('rb') as f:
        gpt = read_gpt(f, size)
    parts = check_profile(gpt)
    slot = active_slot(parts)
    userdata = parts['userdata']
    print('Read-only inspection passed: supported 512 GB Lapis partition geometry; slot ' + slot.upper() + ' is preferred and successful.')
    print(f"userdata: partition index {userdata['index']}, start LBA {userdata['start_lba']}, size {userdata['size']} bytes")
    print('Expected future installation: reformat userdata as unencrypted Btrfs and replace slot ' + slot.upper() + ' boot images.')
    print('The exact partition positions and sizes will remain unchanged. This inspection wrote nothing.')
    mounts = subprocess.check_output(['lsblk', '-n', '-r', '-o', 'MOUNTPOINTS', str(device)], text=True).strip()
    if mounts:
        print('This disk has mounted filesystems. An eventual installer must run from the Fedora live USB with the internal disk unmounted.')
    print('Run install-fedora in the supported live environment to start the installer.')


if __name__ == '__main__':
    main()
