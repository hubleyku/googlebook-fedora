#!/usr/bin/env python3
"""Exercise boot-image commit and rollback on a regular disk image only.

Block devices are refused. This is not a physical kernel updater.
"""
import argparse
import fcntl
import hashlib
import os
from pathlib import Path
import stat
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from lapis.common import read_json, require, require_regular, save_json, sha256
from lapis.gpt import read_gpt
from lapis.slots import active_slot, image_slot
from tools.prepare_fedora_boot import check_profile, hash_region

CHUNK = 1024 * 1024


def write_file(fd, offset, path):
    with path.open('rb') as src:
        while block := src.read(CHUNK):
            view = memoryview(block)
            while view:
                written = os.pwrite(fd, view, offset)
                require(written > 0, 'Short write')
                view = view[written:]
                offset += written
    os.fsync(fd)


def save_region(disk, rec, path):
    disk.seek(rec['offset'])
    left = rec['size']
    with path.open('xb') as out:
        while left:
            block = disk.read(min(CHUNK, left))
            require(block, 'Short disk read during backup')
            out.write(block)
            left -= len(block)
        out.flush(); os.fsync(out.fileno())
    require(sha256(path) == rec['sha256'], 'Backup verification failed')
    path.chmod(0o600)


def inspect(disk, stage):
    proof = read_json(stage / 'validation.json')
    manifest_file = stage / 'candidate/manifest.json'
    require_regular(manifest_file)
    require(proof.get('loader_accepted') is True and sha256(manifest_file) == proof['candidate_manifest_sha256'], 'Candidate validation is missing or stale')
    mode = os.fstat(disk.fileno()).st_mode
    size = os.fstat(disk.fileno()).st_size if stat.S_ISREG(mode) else proof['disk_bytes']
    gpt = read_gpt(disk, size)
    parts = check_profile(gpt)
    slot = active_slot(parts)
    require('factory_pvmfw_a' in parts and 'factory_pvmfw_b' in parts, 'Simulation requires already-renamed pvmfw labels')
    require(gpt.guid == proof['disk_guid'] and gpt.disk_bytes == proof['disk_bytes'], 'Disk identity differs')
    require(hashlib.sha256(gpt.primary + gpt.secondary).hexdigest() == proof['gpt_sha256'], 'Disk GPT changed')
    disk.seek(parts['misc']['start_lba'] * 512)
    misc = disk.read(512)
    require(len(misc) == 512 and hashlib.sha256(misc).hexdigest() == proof['misc_sector_sha256'], 'Misc boot command changed')
    require(misc[:32].split(b'\0')[0] == b'', 'Refuse to update while an Android boot command is pending')
    manifest = read_json(manifest_file)
    require(image_slot(manifest['images'], manifest.get('slot')) == slot, 'Candidate targets a different boot slot')
    require(image_slot(proof['source_regions'], proof.get('slot')) == slot, 'Validation targets a different boot slot')
    for new, old in zip(manifest['images'], proof['source_regions']):
        name = new['name']; part = parts[name]
        require(old['offset'] == part['start_lba'] * 512 and old['size'] == part['size'], 'Source region geometry differs')
        path = stage / 'candidate' / (name + '.img')
        require_regular(path)
        require(path.stat().st_size == new['size'] == part['size'] and sha256(path) == new['sha256'], 'Candidate image changed')
        require(hash_region(disk, old['offset'], old['size']) == old['sha256'], 'Current boot image changed')
    return proof, manifest


def sync_directory(path):
    """Persist directory entries before relying on a backup for recovery."""
    fd = os.open(path, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def commit(disk, stage, backup, proof, manifest, metadata=None):
    slot = image_slot(proof['source_regions'], proof.get('slot'))
    require(image_slot(manifest['images'], manifest.get('slot')) == slot, 'Candidate and backup slots differ')
    require(not backup.exists() and not backup.is_symlink(), 'Choose a new backup directory')
    backup.mkdir(mode=0o700)
    for rec in proof['source_regions']:
        save_region(disk, rec, backup / (rec['name'] + '.img'))
    save_json(backup / 'manifest.json', {'source_regions': proof['source_regions'], 'disk_guid': proof['disk_guid'],
              'slot': slot, 'metadata': metadata or {}})
    sync_directory(backup)
    sync_directory(backup.parent)
    attempted = []
    try:
        for old, new in zip(proof['source_regions'], manifest['images']):
            attempted.append(old)
            path = stage / 'candidate' / (new['name'] + '.img')
            write_file(disk.fileno(), old['offset'], path)
            require(hash_region(disk, old['offset'], old['size']) == new['sha256'], 'Written image failed read-back')
    except BaseException:
        restored = []
        for old in reversed(attempted):
            try:
                write_file(disk.fileno(), old['offset'], backup / (old['name'] + '.img'))
                require(hash_region(disk, old['offset'], old['size']) == old['sha256'], 'Rollback read-back failed')
                restored.append({'name': old['name'], 'restored': True})
            except BaseException as exc:
                restored.append({'name': old['name'], 'restored': False, 'error': str(exc)})
        save_json(backup / 'rollback.json', restored)
        raise
    save_json(backup / 'committed.json', {'disk_guid': proof['disk_guid'], 'slot': slot, 'status': 'boot images updated and read back'})


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--disk-image', type=Path, required=True)
    p.add_argument('--stage', type=Path, required=True)
    p.add_argument('--backup', type=Path, required=True)
    p.add_argument('--apply', action='store_true')
    args = p.parse_args()
    require_regular(args.disk_image)
    require(stat.S_ISREG(args.disk_image.stat().st_mode), 'Only regular disk images are supported')
    require(not str(args.disk_image.resolve()).startswith('/dev/'), 'Device nodes are forbidden')
    fd = os.open(args.disk_image, (os.O_RDWR if args.apply else os.O_RDONLY) | os.O_NOFOLLOW)
    if not stat.S_ISREG(os.fstat(fd).st_mode):
        os.close(fd)
        raise RuntimeError('Opened file is not a regular disk image')
    with os.fdopen(fd, 'r+b' if args.apply else 'rb', buffering=0) as disk:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        proof, manifest = inspect(disk, args.stage)
        print('Validated candidate, disk GPT, and source boot regions. This tool refuses physical block devices.')
        if args.apply:
            commit(disk, args.stage, args.backup, proof, manifest)
            print('Updated and verified the regular disk image; original regions were backed up.')
        else:
            print('Dry run only; no changes made.')


if __name__ == '__main__':
    main()
