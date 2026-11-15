#!/usr/bin/env python3
"""Review and explicitly commit a prepared Fedora kernel update.

Use only after preparation succeeds; retain the external backup and live USB.
"""
import argparse
import fcntl
import os
from pathlib import Path
import shutil
import stat
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from lapis.common import read_json, require, sha256
from lapis.slots import image_slot
from tools.native_fedora import external, typed
from tools.prepare_fedora_update import verify_system
from tools.simulate_boot_commit import inspect, commit

LOCAL_BACKUPS = Path('/var/lib/lapis-fedora/kernel-backups')


def ac_online():
    return any(p.read_text().strip() == '1' for p in Path('/sys/class/power_supply').glob('*/online'))


def review(device, stage, backup, automatic=False, expected_boot=None):
    update = read_json(stage / 'update.json')
    require(update.get('status') == 'prepared only; not installed', 'Unexpected update stage')
    root_uuid, running, kernel = verify_system(device, update['candidate_kernel'], expected_boot)
    require(running == update['running_kernel'] and root_uuid == update['root_uuid'], 'Running system changed since preparation')
    require(sha256(kernel) == update['kernel_sha256'], 'Candidate kernel changed')
    initrd = stage / 'initramfs.img'
    require(initrd.is_file() and not initrd.is_symlink() and sha256(initrd) == update['initrd_sha256'], 'Candidate initramfs changed')
    proof = stage / 'bootset/validation.json'
    require(proof.is_file() and not proof.is_symlink() and sha256(proof) == update['validation_sha256'], 'Validation record changed')
    proof_record = read_json(proof)
    require(proof_record['root_uuid'] == root_uuid, 'Candidate root filesystem differs')
    slot = image_slot(proof_record['source_regions'], proof_record.get('slot'))
    require(update.get('slot', 'a') == slot, 'Update record targets a different boot slot')
    manifest = read_json(stage / 'bootset/candidate/manifest.json')
    require(manifest['kernel_sha256'] == update['kernel_sha256'] and manifest['initramfs_sha256'] == update['initrd_sha256'],
            'Boot images do not match the selected kernel and initramfs')
    if automatic:
        require(backup.parent == LOCAL_BACKUPS and not backup.parent.is_symlink(), 'Unexpected automatic backup location')
        owner = backup.parent.stat()
        require(owner.st_uid == 0 and owner.st_mode & 0o077 == 0, 'Automatic backup directory must be private and root-owned')
    else:
        external(backup.parent, device)
    require(not backup.exists() and not backup.is_symlink(), 'Choose a new external backup directory')
    require(shutil.disk_usage(backup.parent).free > 512 * 1024**2, 'Insufficient backup space')
    if not automatic:
        require(ac_online(), 'Connect AC power')
    fd = os.open(device, os.O_RDONLY | os.O_NOFOLLOW)
    with os.fdopen(fd, 'rb', buffering=0) as disk:
        require(stat.S_ISBLK(os.fstat(fd).st_mode), 'Expected a block device')
        proof_data, manifest = inspect(disk, stage / 'bootset')
    return proof_data, manifest


def commit_prepared(device, stage, backup, automatic=False, expected_boot=None, previous_boot_version=None):
    """Commit after repeated device, system, candidate and backup checks."""
    proof, manifest = review(device, stage, backup, automatic, expected_boot)
    slot = image_slot(proof['source_regions'], proof.get('slot'))
    identity = device.stat()
    require(stat.S_ISBLK(identity.st_mode), 'Expected a block device')
    if not automatic:
        typed('WRITE VALIDATED SLOT ' + slot.upper() + ' ON ' + str(device))
    fd = os.open(device, os.O_RDWR | os.O_NOFOLLOW | os.O_SYNC)
    with os.fdopen(fd, 'r+b', buffering=0) as disk:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        actual = os.fstat(fd)
        require(stat.S_ISBLK(actual.st_mode) and actual.st_rdev == identity.st_rdev,
                'Opened device identity changed')
        # Repeat the system, power and backup checks after confirmation.
        repeated, images = review(device, stage, backup, automatic, expected_boot)
        require(repeated == proof and images == manifest, 'Update changed after review')
        checked, images = inspect(disk, stage / 'bootset')
        require(checked == proof and images == manifest, 'Boot regions changed after review')
        try:
            update = read_json(stage / 'update.json')
            metadata = {'previous_boot_kernel': previous_boot_version or update['running_kernel'],
                        'candidate_kernel': update['candidate_kernel'], 'root_uuid': update['root_uuid'], 'slot': slot}
            commit(disk, stage / 'bootset', backup, checked, images, metadata=metadata)
        except BaseException:
            print('Update failed. Keep the live USB and backup at ' + str(backup) +
                  '; inspect rollback.json and use the recovery guide before rebooting.', file=sys.stderr)
            raise
    print('Slot ' + slot.upper() + ' updated and read back. Backup: ' + str(backup))


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--device', required=True, type=Path)
    p.add_argument('--stage', required=True, type=Path)
    p.add_argument('--backup', required=True, type=Path)
    p.add_argument('--apply', action='store_true')
    args = p.parse_args()
    proof, manifest = review(args.device, args.stage, args.backup)
    print('Candidate, current boot regions, root identity, backup destination, and AC power verified.')
    if not args.apply:
        print('Dry run only; no disk writes.')
        return
    commit_prepared(args.device, args.stage, args.backup)


if __name__ == '__main__':
    main()
