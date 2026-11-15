#!/usr/bin/env python3
"""Restore a minimal Linux GPT/boot bundle to a blank supported-size drive."""
import argparse
import fcntl
import hashlib
import json
import os
from pathlib import Path
import stat
import subprocess
import sys
import tempfile

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from lapis.backup import backup_parts
from lapis.common import ROOT, read_json, require, require_regular
from lapis.gpt import read_gpt
from lapis.slots import active_slot, boot_names
from tools.native_fedora import external, live_disk_size, write_all
from tools.prepare_fedora_boot import check_profile
from tools.restore_fedora_boot import validate_loader


def inspect_bootstrap(backup):
    require(backup.is_dir() and not backup.is_symlink(), 'Expected a real Linux bootstrap backup directory')
    require_regular(backup / 'manifest.json')
    manifest = read_json(backup / 'manifest.json')
    require(manifest.get('schema') == 1 and manifest.get('scope') == 'bootstrap' and manifest.get('purpose') == 'linux-bootstrap',
            'Select the completed install backup\'s linux-bootstrap directory')
    slot = manifest.get('slot')
    names = (*boot_names(slot), 'misc')
    profile = read_json(ROOT / 'assets/lapis-512gb.json')
    sizes = {p['name'] + '.img': p['size'] for p in profile['partitions'] if p['name'] in names}
    sizes.update({'gpt-primary.bin': 34 * 512, 'gpt-secondary.bin': 33 * 512})
    records = manifest['records']
    require(len(records) == len(sizes) + 1 and {r['file'] for r in records} == set(sizes) | {'plan.json'}, 'Incomplete bootstrap backup')
    verified = {}
    for record in records:
        name = record['file']
        require(record['size'] == sizes[name] if name in sizes else 0 < record['size'] <= 1024**2,
                'Unexpected bootstrap file size: ' + name)
        path = backup / name
        require_regular(path)
        require(path.stat().st_size == record['size'], 'Backup file size changed: ' + name)
        data = path.read_bytes()
        require(hashlib.sha256(data).hexdigest() == record['sha256'], 'Backup file hash failed: ' + name)
        verified[name] = data
    plan = json.loads(verified['plan.json'])
    size = plan['disk_bytes']
    require(size == profile['disk_bytes'], 'Snapshot size differs from the supported disk profile')
    with tempfile.TemporaryFile() as image:
        image.truncate(size)
        image.write(verified['gpt-primary.bin'])
        secondary = verified['gpt-secondary.bin']
        image.seek(size - len(secondary)); image.write(secondary)
        gpt = read_gpt(image, size)
    parts = check_profile(gpt)
    require('factory_pvmfw_a' in parts and 'factory_pvmfw_b' in parts, 'Snapshot is not a Linux bootstrap layout')
    slot = active_slot(parts)
    require(gpt.guid == plan['disk_guid'] == manifest['disk_guid'], 'Backup disk identities disagree')
    require(hashlib.sha256(gpt.primary + gpt.secondary).hexdigest() == plan['gpt_sha256'], 'Backup GPT hash differs')
    require(gpt.partitions() == plan['partitions'] and plan['slot'] == manifest['slot'] == slot, 'Backup layout or slot records disagree')
    regions = []
    for part in backup_parts(parts):
        name = part['name']
        contents = verified[name + '.img']
        if name == 'misc':
            require(contents[:32] == bytes(32), 'Snapshot has a pending Android recovery command')
        regions.append((name, part['start_lba'] * 512, contents))
    return gpt, regions


def restore_bootstrap(disk, gpt, regions):
    """Write only the GPT and the small boot regions; leave userdata untouched."""
    # Publish vbmeta after its images, then make the new layout visible last.
    ordered = [r for r in regions if not r[0].startswith('vbmeta_')]
    ordered.extend(r for r in regions if r[0].startswith('vbmeta_'))
    for name, offset, contents in ordered:
        write_all(disk.fileno(), offset, contents)
        require(os.pread(disk.fileno(), len(contents), offset) == contents, 'Bootstrap read-back failed: ' + name)
    for offset, contents in ((gpt.disk_bytes - len(gpt.secondary), gpt.secondary), (0, gpt.primary)):
        write_all(disk.fileno(), offset, contents)
        require(os.pread(disk.fileno(), len(contents), offset) == contents, 'GPT restore read-back failed')
    checked = read_gpt(disk, gpt.disk_bytes)
    require((checked.primary, checked.secondary) == (gpt.primary, gpt.secondary), 'Restored GPT differs')


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    choice = parser.add_mutually_exclusive_group(required=True)
    choice.add_argument('--device', type=Path)
    choice.add_argument('--disk-image', type=Path, help='disposable regular file for testing')
    parser.add_argument('--backup', required=True, type=Path)
    parser.add_argument('--loader', type=Path, default=ROOT / 'build/load_android_test')
    parser.add_argument('--apply', action='store_true')
    args = parser.parse_args(argv)
    path = args.device or args.disk_image
    gpt, regions = inspect_bootstrap(args.backup)
    validate_loader(gpt, regions, args.loader)
    if args.device:
        size = live_disk_size(path, ac=True)
        require(not subprocess.check_output(['lsblk', '-n', '-r', '-o', 'MOUNTPOINTS', str(path)], text=True).strip(), 'Unmount the target filesystems first')
        external(args.backup, path, writable=False)
    else:
        require_regular(path)
        require(not str(path.resolve()).startswith('/dev/'), 'Image mode cannot write device nodes')
        size = path.stat().st_size
    require(size == gpt.disk_bytes, 'Target size differs from the supported snapshot; automatic resizing is not supported')
    identity = path.stat()
    print('Verified GPT, boot-image hashes, and loader acceptance. Slot:', active_slot({p['name']: p for p in gpt.partitions()}).upper())
    print('Restores the partition layout and Linux boot images. A Linux root filesystem must be installed separately.')
    if not args.apply:
        print('Dry run passed; no target writes.')
        return 0
    if args.device:
        require(sys.stdin.isatty(), 'Run physical restoration from a terminal')
        print('\033[1;31mWARNING: This replaces the target partition table and boot images. This cannot be undone.\033[0m')
        while input('Type YES to restore, or press Ctrl-C to cancel: ').strip() != 'YES':
            print('Enter YES in capitals to proceed, or press Ctrl-C to cancel.')
        require(live_disk_size(path, ac=True) == size, 'Target size changed')
        require(not subprocess.check_output(['lsblk', '-n', '-r', '-o', 'MOUNTPOINTS', str(path)], text=True).strip(), 'Target became mounted')
    fd = os.open(path, os.O_RDWR | os.O_NOFOLLOW | os.O_SYNC)
    with os.fdopen(fd, 'r+b', buffering=0) as disk:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        actual = os.fstat(fd)
        require((actual.st_dev, actual.st_ino, actual.st_rdev) == (identity.st_dev, identity.st_ino, identity.st_rdev), 'Target identity changed')
        require(stat.S_ISBLK(actual.st_mode) if args.device else stat.S_ISREG(actual.st_mode), 'Unexpected target type')
        restore_bootstrap(disk, gpt, regions)
    print('Linux bootstrap layout and boot images restored and read back.')
    return 0


if __name__ == '__main__':
    try:
        raise SystemExit(main())
    except (KeyboardInterrupt, EOFError):
        print('\nRestoration cancelled.', file=sys.stderr)
        raise SystemExit(130)
    except (OSError, RuntimeError, ValueError, KeyError, TypeError, subprocess.SubprocessError) as error:
        print('Restoration stopped: ' + str(error), file=sys.stderr)
        raise SystemExit(1)
