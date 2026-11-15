#!/usr/bin/env python3
"""Prepare and validate Fedora Android boot images. Never writes to a disk.

This is a development tool, not an installer or a complete kernel updater.
"""
import argparse
import hashlib
from pathlib import Path
import stat
import subprocess
import sys
import uuid

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from lapis.boot import build_set
from lapis.common import ROOT, read_json, require, require_regular, save_json, sha256, runtime_sources
from lapis.gpt import read_gpt
from lapis.slots import active_slot, boot_names


def disk_size(path):
    mode = path.stat().st_mode
    if stat.S_ISREG(mode):
        return path.stat().st_size
    require(stat.S_ISBLK(mode), 'Disk input must be a regular disk image or a block device')
    return int(subprocess.check_output(['blockdev', '--getsize64', str(path)], text=True).strip())


def check_profile(gpt):
    profile = read_json(ROOT / 'assets/lapis-512gb.json')
    require(gpt.disk_bytes == profile['disk_bytes'], 'Disk capacity does not match the tested 512 GB Lapis profile')
    rename = {'factory_pvmfw_a': 'pvmfw_a', 'factory_pvmfw_b': 'pvmfw_b'}
    actual = []
    for part in gpt.partitions():
        actual.append({k: rename.get(part[k], part[k]) if k == 'name' else part[k]
                       for k in ('index', 'name', 'start_lba', 'size', 'type_guid')})
    require(actual == profile['partitions'], 'Disk partition geometry differs from the tested Lapis profile')
    parts = {p['name']: p for p in gpt.partitions()}
    active_slot(parts)
    return parts


def hash_region(source, offset, length):
    source.seek(offset)
    h = hashlib.sha256()
    while length:
        block = source.read(min(length, 1024 * 1024))
        require(block, 'Short read while hashing source disk')
        h.update(block)
        length -= len(block)
    return h.hexdigest()


def make_cmdline(root_uuid):
    parsed = uuid.UUID(root_uuid)
    require(str(parsed) == root_uuid.lower(), 'Use a canonical Btrfs filesystem UUID')
    return (f'root=UUID={parsed} rootfstype=btrfs rootflags=subvol=@ rw '
            'modprobe.blacklist=xe,snd_sof_pci_intel_ptl console=tty0')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--disk', required=True, type=Path, help='read-only whole disk or sparse copy')
    parser.add_argument('--kernel', required=True, type=Path)
    parser.add_argument('--initrd', required=True, type=Path)
    parser.add_argument('--root-uuid', required=True, help='UUID of intended Btrfs filesystem')
    parser.add_argument('--output', required=True, type=Path, help='new private staging directory')
    parser.add_argument('--loader', type=Path, default=ROOT / 'build/load_android_test')
    parser.add_argument('--vendor', type=Path, default=runtime_sources())
    args = parser.parse_args()
    require_regular(args.kernel); require_regular(args.initrd); require_regular(args.loader)
    cmdline = make_cmdline(args.root_uuid)
    require(not args.output.exists() and not args.output.is_symlink(), 'Output already exists')
    require(not str(args.output.resolve()).startswith('/dev/'), 'Output must not be under /dev')
    size = disk_size(args.disk)
    with args.disk.open('rb') as source:
        gpt = read_gpt(source, size)
        parts = check_profile(gpt)
        slot = active_slot(parts)
        names = boot_names(slot)
        misc = parts['misc']
        source.seek(misc['start_lba'] * 512)
        sector = source.read(512)
        source_regions = [dict(name=name, offset=parts[name]['start_lba'] * 512, size=parts[name]['size'],
                               sha256=hash_region(source, parts[name]['start_lba'] * 512, parts[name]['size']))
                          for name in names]
    require(args.initrd.stat().st_size < 29 * 1024**2,
            'Initramfs is too large for the 32 MiB vendor_boot partition; build a smaller host-specific image')
    require(len(sector) == 512 and sector[:32].split(b'\0')[0] in (b'', b'boot-recovery'), 'Unexpected misc boot command')
    args.output.mkdir(mode=0o700, parents=False)
    candidate = args.output / 'candidate'
    manifest = build_set(args.kernel, args.initrd, cmdline, candidate, args.vendor,
                         sizes={n: parts[n]['size'] for n in names}, installed=True, slot=slot,
                         kernel_sha256=sha256(args.kernel))
    require([x['name'] for x in manifest['images']] == list(names), 'Unexpected image set')
    primary, secondary = gpt.rename_vm_partitions()
    fixture = args.output / 'loader-fixture.img'
    with fixture.open('xb') as f:
        f.truncate(size)
        f.seek(0); f.write(primary)
        f.seek(size - len(secondary)); f.write(secondary)
        f.seek(misc['start_lba'] * 512); f.write(bytes(32) + sector[32:])
        for rec in manifest['images']:
            source = candidate / (rec['name'] + '.img')
            require(source.stat().st_size == parts[rec['name']]['size'], 'Image size mismatch')
            f.seek(parts[rec['name']]['start_lba'] * 512)
            with source.open('rb') as image:
                while block := image.read(1024 * 1024):
                    f.write(block)
    result = subprocess.run([str(args.loader.resolve()), '-b', '1', str(fixture.resolve())],
                            cwd=args.output, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
    (args.output / 'loader.log').write_text(result.stdout)
    require(result.returncode == 0 and 'vb2api_load_kernel() returned 0' in result.stdout,
            'Loader validation failed; inspect loader.log')
    fixture.unlink()
    # The harness may write extracted ramdisk/kernel test files in its cwd.
    for name in ('loaded-ramdisk.img', 'loaded-kernel.img'):
        (args.output / name).unlink(missing_ok=True)
    save_json(args.output / 'validation.json', {
        'candidate_manifest_sha256': sha256(candidate / 'manifest.json'),
        'disk_guid': gpt.guid, 'disk_bytes': size,
        'gpt_sha256': hashlib.sha256(gpt.primary + gpt.secondary).hexdigest(),
        'source_regions': source_regions,
        'misc_sector_sha256': hashlib.sha256(sector).hexdigest(),
        'root_uuid': str(uuid.UUID(args.root_uuid)),
        'slot': slot,
        'loader_accepted': True,
        'status': 'Candidate only; not a kernel update or proof of physical boot',
    })
    print('Loader accepted staged boot images. No disk writes were performed.')
    print('This does not test initramfs bootability or update the machine.')


if __name__ == '__main__':
    main()
