#!/usr/bin/env python3
"""Automatically update Lapis boot images when Fedora installs a newer kernel."""
import argparse
from datetime import datetime, timezone
import fcntl
import os
from pathlib import Path
import re
import subprocess
import sys
import tempfile
import traceback
import uuid

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from lapis.common import ROOT, read_json, require, save_json, sha256, output
from lapis.gpt import read_gpt
from lapis.fedora_target import _write
from lapis.slots import active_slot
from tools.prepare_fedora_update import verify_system, valid_kernel_version, boot_kernel_sha256, prepare
from tools.prepare_fedora_boot import check_profile, disk_size
from tools.commit_fedora_update import commit_prepared, LOCAL_BACKUPS
from tools.simulate_boot_commit import sync_directory

BASE = Path('/var/lib/lapis-fedora')
STATE = BASE / 'automatic-updates'
MASKS = ('20-grub.install', '50-dracut.install', '51-dracut-rescue.install', '95-set-boot-entry.install')


def device_for_root():
    source = output(['findmnt', '-no', 'SOURCE', '--mountpoint', '/']).split('[', 1)[0]
    source = str(Path(source).resolve())
    require(re.fullmatch(r'/dev/nvme[0-9]+n[0-9]+p1', source) is not None, 'Unexpected root device')
    return Path(source.rsplit('p', 1)[0])


def private_directory(path):
    require(not path.is_symlink(), 'Refusing symlinked state directory')
    path.mkdir(mode=0o700, parents=True, exist_ok=True)
    info = path.stat()
    require(info.st_uid == 0 and info.st_mode & 0o077 == 0, 'State directory must be private and root-owned: ' + str(path))


def version_compare(a, b):
    require(valid_kernel_version(a) and valid_kernel_version(b), 'Invalid kernel version')
    # rpm's version comparison without opening its package database/transaction lock.
    return int(output(['rpm', '--eval', '%{lua:print(rpm.vercmp("' + a + '","' + b + '"))}']))


def boot_identity(device):
    with device.open('rb') as source:
        gpt = read_gpt(source, disk_size(device))
        parts = check_profile(gpt)
        slot = active_slot(parts)
        digest = boot_kernel_sha256(source, parts['boot_' + slot])
    return gpt.guid, slot, digest


def pending_expectation(device, record, root_uuid):
    if not record:
        return None
    guid, slot, digest = boot_identity(device)
    if (record.get('disk_guid') != guid or record.get('root_uuid') != root_uuid
            or record.get('kernel_sha256') != digest or record.get('slot', 'a') != slot):
        return None
    version = record.get('kernel_version', '')
    require(valid_kernel_version(version), 'Invalid pending kernel record')
    image = Path('/usr/lib/modules') / version / 'vmlinuz'
    require(image.is_file() and not image.is_symlink() and sha256(image) == digest, 'Pending kernel image is missing or changed')
    return digest


def installed_boot_version(device):
    _, slot, digest = boot_identity(device)
    versions = []
    for image in Path('/usr/lib/modules').glob('*/vmlinuz'):
        if image.is_file() and not image.is_symlink() and sha256(image) == digest:
            versions.append(image.parent.name)
    require(len(versions) == 1, 'Cannot identify installed kernel currently in slot ' + slot.upper())
    return versions[0]


def status(value):
    save_json(STATE / 'status.json', dict(value, time=datetime.now(timezone.utc).isoformat()))
    sync_directory(STATE)


def update_kernel(version):
    require(os.geteuid() == 0, 'Run as root')
    require(valid_kernel_version(version), 'Invalid kernel version')
    private_directory(STATE)
    private_directory(LOCAL_BACKUPS)
    fd = os.open(STATE / 'update.lock', os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
    with os.fdopen(fd, 'r+') as lock:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        device = device_for_root()
        _, slot, _ = boot_identity(device)
        record = read_json(STATE / 'pending.json') if (STATE / 'pending.json').exists() else None
        root_uuid = output(['findmnt', '-no', 'UUID', '--mountpoint', '/'])
        expected = pending_expectation(device, record, root_uuid)
        root_uuid, running, kernel = verify_system(device, version, expected)
        boot_version = installed_boot_version(device)
        if version_compare(version, boot_version) <= 0:
            status({'status': 'unchanged', 'slot': slot, 'requested_kernel': version, 'boot_kernel': boot_version})
            print('Slot ' + slot.upper() + ' already contains this kernel or a newer one; no writes.')
            return
        backup = LOCAL_BACKUPS / (datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ') + '-' + version + '-' + uuid.uuid4().hex[:8])
        status({'status': 'preparing', 'slot': slot, 'requested_kernel': version, 'boot_kernel': boot_version})
        with tempfile.TemporaryDirectory(prefix='stage-', dir=STATE) as temp:
            stage = Path(temp) / 'candidate'
            prepare(device, version, stage, expected)
            require(read_json(stage / 'update.json')['slot'] == slot, 'Boot slot changed during preparation')
            commit_prepared(device, stage, backup, automatic=True, expected_boot=expected, previous_boot_version=boot_version)
        guid, committed_slot, digest = boot_identity(device)
        require(committed_slot == slot and digest == sha256(kernel), 'Boot slot or kernel differs after automatic commit')
        save_json(STATE / 'pending.json', dict(disk_guid=guid, root_uuid=root_uuid, slot=slot,
                  kernel_version=version, kernel_sha256=digest, backup=str(backup)))
        sync_directory(STATE)
        status({'status': 'updated', 'slot': slot, 'running_kernel': running, 'boot_kernel': version, 'backup': str(backup)})
        print('Lapis slot ' + slot.upper() + ' firmware boot images updated to ' + version + '. Reboot when ready.')


def install_hooks(root):
    root = Path(root)
    require(root.is_dir() and not root.is_symlink(), 'Invalid target root')
    directory = root / 'etc/kernel/install.d'
    directory.mkdir(parents=True, exist_ok=True)
    require(not directory.is_symlink(), 'Invalid kernel hook directory')
    for name in MASKS:
        path = directory / name
        if path.is_symlink():
            require(path.readlink() == Path('/dev/null'), 'Unexpected kernel hook override')
        else:
            require(not path.exists(), 'Existing kernel hook override: ' + name)
            path.symlink_to('/dev/null')
    _write(root, 'etc/kernel/install.d/60-lapis.install', (ROOT/'assets/installed/60-lapis.install').read_text(), 0o755)
    _write(root, 'etc/systemd/system/lapis-kernel-update@.service', (ROOT/'assets/installed/lapis-kernel-update@.service').read_text())
    _write(root, 'usr/local/bin/lapis-kernel-update', '#!/bin/sh\nexec /usr/bin/python3 /opt/lapis-kit/tools/auto_fedora_update.py "$@"\n', 0o755)
    for name in ('automatic-updates', 'kernel-backups'):
        path = root / 'var/lib/lapis-fedora' / name
        require(not path.is_symlink(), 'Invalid automatic update directory')
        path.mkdir(mode=0o700, parents=True, exist_ok=True)
        path.chmod(0o700)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    action = parser.add_mutually_exclusive_group(required=True)
    action.add_argument('--kernel-version')
    action.add_argument('--install', action='store_true')
    action.add_argument('--status', action='store_true')
    args = parser.parse_args()
    if args.status:
        print((STATE/'status.json').read_text() if (STATE/'status.json').is_file() else 'No automatic kernel update recorded.')
        return
    if args.install:
        verify_system(device_for_root(), output(['uname','-r']))
        install_hooks(Path('/'))
        subprocess.run(['restorecon', '-RF', '/etc/kernel/install.d', '/etc/systemd/system/lapis-kernel-update@.service', '/usr/local/bin/lapis-kernel-update', str(BASE)], check=True)
        subprocess.run(['systemctl', 'daemon-reload'], check=True)
        print('Automatic Fedora kernel boot-image updates installed.')
        return
    try:
        update_kernel(args.kernel_version)
    except BaseException as error:
        if STATE.is_dir() and not STATE.is_symlink() and os.geteuid() == 0:
            status({'status': 'failed', 'requested_kernel': args.kernel_version, 'error': str(error)})
        traceback.print_exc()
        print('Lapis automatic boot update failed; inspect lapis-kernel-update --status and the service journal before rebooting.', file=sys.stderr)
        raise SystemExit(1)


if __name__ == '__main__':
    main()
