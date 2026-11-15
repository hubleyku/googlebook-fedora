#!/usr/bin/env python3
"""Restore verified active-slot boot images from a live USB, or a disposable disk image.

Restores boot, init_boot, vendor_boot and vbmeta for the preferred slot only. Does not restore
userdata, GPT, Googlebook OS, or a removed kernel's modules.
"""
import argparse
import fcntl
import hashlib
import os
from pathlib import Path
import stat
import subprocess
import sys
import tempfile

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from lapis.common import ROOT, read_json, require, require_regular, sha256
from lapis.gpt import read_gpt
from lapis.slots import active_slot, boot_names, image_slot
from tools.native_fedora import hardware, external, typed, write_all
from tools.prepare_fedora_boot import check_profile


def inspect_backup(disk, size, backup):
    require(backup.is_dir() and not backup.is_symlink(), 'Expected a real backup directory')
    require_regular(backup / 'manifest.json')
    manifest = read_json(backup / 'manifest.json')
    gpt = read_gpt(disk, size)
    parts = check_profile(gpt)
    slot = active_slot(parts)
    require(manifest.get('slot', slot) == slot, 'Backup belongs to a different boot slot')
    require(gpt.guid == manifest['disk_guid'], 'Backup belongs to a different disk')
    disk.seek(parts['misc']['start_lba'] * 512)
    require(disk.read(32) == bytes(32), 'Pending Android recovery command; stop and inspect misc')
    if 'source_regions' in manifest:
        records = manifest['source_regions']
        require(image_slot(records, manifest.get('slot')) == slot, 'Backup belongs to a different boot slot')
    else:
        records_by_file = {}
        for r in manifest['records']:
            require(r['file'] not in records_by_file, 'Duplicate backup record')
            records_by_file[r['file']] = r
        plan_rec = records_by_file['plan.json']
        require_regular(backup / 'plan.json')
        require(sha256(backup / 'plan.json') == plan_rec['sha256'], 'Backup plan changed')
        plan = read_json(backup / 'plan.json')
        require(plan['disk_guid'] == gpt.guid and plan['disk_bytes'] == size, 'Backup disk identity differs')
        old_parts = {p['name']: p for p in plan['partitions']}
        require(active_slot(old_parts) == slot and plan.get('slot', slot) == slot, 'Backup plan belongs to a different boot slot')
        records = []
        for name in boot_names(slot):
            p = old_parts[name]; r = records_by_file[name + '.img']
            records.append(dict(name=name, offset=p['start_lba'] * 512, size=r['size'], sha256=r['sha256']))
    regions = []
    for r in records:
        p = parts[r['name']]
        require(r['offset'] == p['start_lba'] * 512 and r['size'] == p['size'], 'Backup geometry differs')
        path = backup / (r['name'] + '.img')
        require_regular(path)
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
        with os.fdopen(fd, 'rb') as source:
            require(os.fstat(fd).st_size == r['size'], 'Backup image size differs')
            data = source.read(r['size'])
        require(len(data) == r['size'] and hashlib.sha256(data).hexdigest() == r['sha256'], 'Backup image hash failed: ' + r['name'])
        regions.append((r['name'], r['offset'], data))
    return gpt, regions


def validate_loader(gpt, regions, loader):
    require_regular(loader)
    with tempfile.TemporaryDirectory(prefix='lapis-restore-') as tmp:
        image = Path(tmp) / 'loader.img'
        with image.open('wb') as out:
            out.truncate(gpt.disk_bytes)
            out.seek(0); out.write(gpt.primary)
            out.seek(gpt.disk_bytes - len(gpt.secondary)); out.write(gpt.secondary)
            for _, offset, data in regions:
                out.seek(offset); out.write(data)
        result = subprocess.run([str(loader.resolve()), '-b', '1', str(image)], cwd=tmp,
                                capture_output=True, text=True)
        require(result.returncode == 0 and 'vb2api_load_kernel() returned 0' in result.stdout,
                'Loader rejected the backup boot set; do not restore it')


def restore(disk, regions):
    # All bytes are already loaded and verified. The source backup is never changed.
    # vbmeta is last so descriptors are replaced after their images.
    for name, offset, data in regions:
        write_all(disk.fileno(), offset, data)
        require(os.pread(disk.fileno(), len(data), offset) == data, 'Restore read-back failed: ' + name)


def check_live(device, backup):
    gpt = hardware(device, ac=True)
    require(not subprocess.check_output(['lsblk', '-n', '-r', '-o', 'MOUNTPOINTS', str(device)], text=True).strip(),
            'Unmount all target-disk filesystems and close encrypted mappings before recovery')
    external(backup, device, writable=False)
    return gpt.disk_bytes


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    choice = parser.add_mutually_exclusive_group(required=True)
    choice.add_argument('--device', type=Path)
    choice.add_argument('--disk-image', type=Path)
    parser.add_argument('--backup', required=True, type=Path)
    parser.add_argument('--loader', type=Path, default=ROOT / 'build/load_android_test')
    parser.add_argument('--apply', action='store_true')
    args = parser.parse_args()
    path = args.device or args.disk_image
    image_mode = args.disk_image is not None
    if image_mode:
        require_regular(path)
        require(not str(path.resolve()).startswith('/dev/'), 'Device nodes are forbidden in image mode')
        size = path.stat().st_size
    else:
        size = check_live(path, args.backup)
    expected = path.stat()
    # Review is read-only, even with --apply. Physical confirmation comes afterward.
    with os.fdopen(os.open(path, os.O_RDONLY | os.O_NOFOLLOW), 'rb', buffering=0) as disk:
        opened = os.fstat(disk.fileno())
        require((opened.st_dev, opened.st_ino, opened.st_rdev) == (expected.st_dev, expected.st_ino, expected.st_rdev), 'Target identity changed')
        gpt, regions = inspect_backup(disk, size, args.backup)
        validate_loader(gpt, regions, args.loader)
    print('Backup hashes, disk identity, slot geometry, and Android loader verified.')
    slot = active_slot({p['name']: p for p in gpt.partitions()})
    print('Restores only the four slot-' + slot.upper() + ' boot images; keep the matching kernel modules installed.')
    if not args.apply:
        print('Dry run passed; no writes.')
        return
    if not image_mode:
        typed('RESTORE SLOT ' + slot.upper() + ' ON ' + str(path))
        require(check_live(path, args.backup) == size, 'Target size changed')
    fd = os.open(path, os.O_RDWR | os.O_NOFOLLOW | os.O_SYNC)
    with os.fdopen(fd, 'r+b', buffering=0) as disk:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        actual = os.fstat(fd)
        require((actual.st_dev, actual.st_ino, actual.st_rdev) == (expected.st_dev, expected.st_ino, expected.st_rdev), 'Target identity changed')
        require(stat.S_ISREG(actual.st_mode) if image_mode else stat.S_ISBLK(actual.st_mode), 'Unexpected target type')
        current, checked = inspect_backup(disk, size, args.backup)
        require(current.primary == gpt.primary and current.secondary == gpt.secondary and checked == regions,
                'Disk or backup changed after review')
        try:
            restore(disk, checked)
        except BaseException:
            print('Restore interrupted or failed. Keep the USB and backup; rerun recovery before booting internally.', file=sys.stderr)
            raise
    print('All four boot images restored and read back successfully.')


if __name__ == '__main__':
    main()
