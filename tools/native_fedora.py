#!/usr/bin/env python3
"""Experimental staged Fedora installation for the supported Lapis live USB."""
import argparse
import fcntl
import hashlib
import os
from pathlib import Path
import platform
import re
import shutil
import stat
import subprocess
import sys
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from lapis.common import ROOT, output, read_json, require, run, save_json, sha256, runtime_sources
from tools.prepare_fedora_boot import make_cmdline
from lapis.gpt import read_gpt
from lapis.boot import build_set
from lapis.backup import required_free_bytes, save_boot_backup, verify_backup as verify_boot_backup
from lapis.slots import active_slot, image_slot
from lapis.live import validate_live_environment

STATE = Path('/run/lapis-install')
TARGET = Path('/mnt')


def digest(data):
    return hashlib.sha256(data).hexdigest()


def validate_profile(gpt):
    """Check the GPT against the factory profile, tolerating a previous
    installation's pvmfw label renames. Returns True when the disk is in
    previously-installed (reinstall) state."""
    profile = read_json(ROOT / 'assets/lapis-512gb.json')
    require(gpt.disk_bytes == profile['disk_bytes'], 'Only the tested 512 GB layout is supported')
    renamed = {'factory_pvmfw_a': 'pvmfw_a', 'factory_pvmfw_b': 'pvmfw_b'}
    actual = []
    for p in gpt.partitions():
        name = renamed.get(p['name'], p['name'])
        actual.append({k: (name if k == 'name' else p[k]) for k in ('index', 'name', 'start_lba', 'size', 'type_guid')})
    if actual != profile['partitions']:
        by_name = {x['name']: x for x in actual}
        expected = {x['name']: x for x in profile['partitions']}
        diffs = []
        for name in sorted(set(by_name) | set(expected)):
            a, e = by_name.get(name), expected.get(name)
            if a is None:
                diffs.append(f"missing: {name} (factory index {e['index']}, {e['size']} bytes)")
            elif e is None:
                diffs.append(f"unexpected: {name} (index {a['index']}, {a['size']} bytes)")
            elif (a['index'], a['start_lba'], a['size'], a['type_guid']) != (e['index'], e['start_lba'], e['size'], e['type_guid']):
                diffs.append(f"changed: {name} index {a['index']}->{e['index']} start {a['start_lba']}->{e['start_lba']} size {a['size']}->{e['size']}")
        print('Factory partition layout differs. Sanitized geometry diff (names/offsets only, no identifiers):')
        for line in diffs:
            print(' ', line)
        raise RuntimeError('Factory partition layout differs; stop and report the sanitized geometry above')
    parts = {p['name']: p for p in gpt.partitions()}
    active_slot(parts)
    return any(p['name'] in renamed for p in gpt.partitions())


def live_disk_size(device, *, ac=False):
    require(os.geteuid() == 0 and sys.platform == 'linux', 'Run as root on the live Googlebook')
    require(Path('/sys/class/dmi/id/sys_vendor').read_text().strip() == 'Google', 'Wrong DMI vendor')
    require(Path('/sys/class/dmi/id/product_name').read_text().strip() == 'Lapis', 'Wrong board')
    require(Path('/sys/bus/pci/devices/0000:00:02.0/device').read_text().strip() == '0xb090', 'Wrong graphics device')
    require(Path('/sys/bus/pci/devices/0000:00:1f.3/subsystem_device').read_text().strip() == '0x15e4', 'Wrong audio subsystem')
    validate_live_environment(platform.freedesktop_os_release(), Path('/proc/cmdline').read_text(), platform.machine())
    require(re.fullmatch(r'/dev/nvme\d+n\d+', str(device)), 'Expected a whole internal NVMe disk')
    require(stat.S_ISBLK(device.stat().st_mode), 'Not a block device')
    node = Path('/sys/class/block') / device.name
    require((node / 'removable').read_text().strip() == '0', 'Disk must be internal')
    require(output(['blockdev', '--getss', device]) == '512', 'Only 512-byte logical sectors supported')
    if ac:
        supplies = Path('/sys/class/power_supply').glob('*/online')
        require(any(p.read_text().strip() == '1' for p in supplies), 'Connect AC power')
    return int(output(['blockdev', '--getsize64', device]))


def hardware(device, *, ac=False):
    size = live_disk_size(device, ac=ac)
    with device.open('rb') as f:
        gpt = read_gpt(f, size)
    validate_profile(gpt)
    return gpt


def plan(args):
    require(not STATE.exists(), 'Existing install state: do not restart or reformat blindly')
    gpt = hardware(args.device)
    reinstall = any(p['name'] in ('factory_pvmfw_a', 'factory_pvmfw_b') for p in gpt.partitions())
    STATE.mkdir(mode=0o700)
    p = dict(device=str(args.device), disk_bytes=gpt.disk_bytes, disk_guid=gpt.guid,
             model=(Path('/sys/class/block') / args.device.name / 'device/model').read_text().strip(),
             gpt_sha256=digest(gpt.primary + gpt.secondary), partitions=gpt.partitions(),
             reinstall=reinstall, slot=active_slot({p['name']: p for p in gpt.partitions()}))
    save_json(STATE / 'plan.json', p)
    if not getattr(args, 'quiet', False):
        print('Target:', args.device, 'slot', p['slot'].upper())
        print('Plan saved. Next: save the boot-support backup to the installer USB.')


def current():
    p = read_json(STATE / 'plan.json')
    device = Path(p['device'])
    gpt = hardware(device, ac=True)
    require(gpt.guid == p['disk_guid'] and digest(gpt.primary + gpt.secondary) == p['gpt_sha256'], 'Disk/GPT changed since plan')
    slot = active_slot({part['name']: part for part in gpt.partitions()})
    require(p.get('slot', slot) == slot, 'Boot slot changed since plan')
    require((Path('/sys/class/block') / device.name / 'device/model').read_text().strip() == p['model'], 'Disk identity changed')
    return p, device, gpt


def external(path, device, *, writable=True):
    require(path.is_dir() and not path.is_symlink(), 'Backup parent must already exist on an external mounted filesystem')
    source = output(['findmnt', '-no', 'SOURCE', '--target', path]).split('[')[0]
    require(source.startswith('/dev/'), 'Backup must be on a mounted block filesystem, not RAM or a network share')
    ancestors = output(['lsblk', '-s', '-n', '-r', '-o', 'PATH', source]).splitlines()
    require(str(device) not in ancestors, 'Backup is on the disk being installed')
    if writable:
        options = output(['findmnt', '-no', 'OPTIONS', '--target', path]).split(',')
        require('rw' in options, 'Backup filesystem must be writable')


def backup(args):
    p, device, gpt = current()
    dest = args.destination.resolve()
    external(dest.parent, device)
    require(not dest.exists(), 'Choose a new backup directory')
    required_bytes = required_free_bytes(p['partitions'])
    require(shutil.disk_usage(dest.parent).free > required_bytes, 'Insufficient external backup space')
    with device.open('rb') as f:
        save_boot_backup(f, gpt.disk_bytes, dest, p)
    os.sync()
    verify_backup(dest)
    save_json(STATE / 'backup.json', dict(path=str(dest), manifest_sha256=sha256(dest / 'manifest.json')))
    print('Boot-support backup saved and hashes verified:', dest)


def verify_backup(dest):
    manifest = verify_boot_backup(dest)
    print('All backup hashes verified')
    return manifest


def checked_backup(device):
    record = read_json(STATE / 'backup.json')
    dest = Path(record['path'])
    external(dest, device)
    require(sha256(dest / 'manifest.json') == record['manifest_sha256'], 'Backup manifest changed')
    verify_backup(dest)
    require(read_json(dest / 'plan.json') == read_json(STATE / 'plan.json'), 'Backup belongs to a different plan')
    return dest


def typed(phrase):
    require(sys.stdin.isatty(), 'Destructive stages require an interactive local terminal')
    require(input('Type exactly ' + phrase + ': ') == phrase, 'Cancelled')


def review_format():
    p, device, _ = current()
    checked_backup(device)
    require(not (STATE / 'format-started').exists(), 'Format already attempted; use recovery instructions')
    part = next(x for x in p['partitions'] if x['name'] == 'userdata')
    block = Path('/dev/disk/by-partuuid') / part['partuuid']
    require(block.resolve() == Path(str(device) + 'p1'), 'Unexpected userdata device')
    require(not list((Path('/sys/class/block') / block.resolve().name / 'holders').iterdir()), 'Userdata has active holders')
    require(not output(['lsblk', '-n', '-r', '-o', 'MOUNTPOINTS', device]), 'An internal partition is mounted')
    require(subprocess.run(['mountpoint', '-q', TARGET]).returncode != 0, '/mnt already mounted')
    return block, part


def format_root(args, *, approval=None):
    block, part = review_format()
    print('Will erase ONLY userdata:', block, part['size'], 'bytes; create unencrypted Btrfs.')
    if not args.apply:
        return
    if approval is None:
        print('\033[1;31mExisting user data will be erased. This cannot be undone.\033[0m')
        typed('YES')
    else:
        approval.check(read_json(STATE / 'plan.json'))
    checked_block, checked_part = review_format()
    require(checked_block == block and checked_part == part, 'Format target changed after confirmation')
    (STATE / 'format-started').touch(mode=0o600)
    run(['mkfs.btrfs', '-f', '-L', 'Fedora', block])
    TARGET.mkdir(exist_ok=True)
    run(['mount', block, TARGET])
    for name in ('@', '@home'):
        run(['btrfs', 'subvolume', 'create', TARGET / name])
    run(['umount', TARGET])
    run(['mount', '-o', 'noatime,compress=zstd:3,subvol=@', block, TARGET])
    (TARGET / 'home').mkdir(exist_ok=True)
    run(['mount', '-o', 'noatime,compress=zstd:3,subvol=@home', block, TARGET / 'home'])
    save_json(STATE / 'format.json', dict(btrfs_uuid=output(['blkid', '-s', 'UUID', '-o', 'value', block])))
    os.sync()
    print('Unencrypted Btrfs mounted on /mnt and /mnt/home.')


def mounted():
    require((STATE / 'format.json').is_file(), 'Complete the format stage first')
    p = read_json(STATE / 'plan.json')
    part = next(x for x in p['partitions'] if x['name'] == 'userdata')
    block = Path('/dev/disk/by-partuuid') / part['partuuid']
    expected_uuid = read_json(STATE / 'format.json')['btrfs_uuid']
    require(block.resolve() == Path(p['device'] + 'p1'), 'Userdata device changed')
    require(output(['blkid', '-s', 'UUID', '-o', 'value', block]) == expected_uuid, 'Btrfs identity changed')
    for mountpoint, subvol in ((TARGET, '/@'), (TARGET / 'home', '/@home')):
        require(output(['findmnt', '-no', 'FSTYPE', '--mountpoint', mountpoint]) == 'btrfs', 'Target mount is not Btrfs')
        require(output(['findmnt', '-no', 'UUID', '--mountpoint', mountpoint]) == expected_uuid, 'Wrong target filesystem')
        require(output(['findmnt', '-no', 'FSROOT', '--mountpoint', mountpoint]) == subvol, 'Wrong target subvolume')


def build(args):
    p, device, gpt = current(); checked_backup(device); mounted()
    config = read_json(STATE / 'configured.json')
    require(config['status'] == 'configured', 'Configure target first')
    kver = config['kernel_version']
    require(re.fullmatch(r'[A-Za-z0-9._+~-]+', kver), 'Invalid kernel version')
    kernel = TARGET / 'usr/lib/modules' / kver / 'vmlinuz'
    initrd = TARGET / 'boot' / ('lapis-initramfs-' + kver + '.img')
    require(sha256(kernel) == config['kernel_sha256'] and sha256(initrd) == config['initrd_sha256'], 'Target boot inputs changed')
    cmdline = make_cmdline(read_json(STATE / 'format.json')['btrfs_uuid'])
    build_set(kernel, initrd, cmdline, STATE / 'candidate', runtime_sources(),
              sizes={p['name']: p['size'] for p in gpt.partitions()}, installed=True,
              kernel_sha256=config['kernel_sha256'],
              slot=active_slot({part['name']: part for part in gpt.partitions()}))
    print('Candidate files built; no boot partition writes. Next: validate.')


def candidate_regions(p, gpt, backup):
    manifest = read_json(STATE / 'candidate/manifest.json')
    parts = {x['name']: x for x in p['partitions']}
    slot = active_slot({part['name']: part for part in gpt.partitions()})
    require(active_slot(parts) == slot and p.get('slot', slot) == slot, 'Planned boot slot differs')
    require(image_slot(manifest['images'], manifest.get('slot')) == slot, 'Candidate targets a different boot slot')
    regions = []
    for rec in manifest['images']:
        src = STATE / 'candidate' / (rec['name'] + '.img')
        part = parts[rec['name']]
        require(src.stat().st_size == part['size'] == rec['size'] and sha256(src) == rec['sha256'], 'Candidate changed')
        regions.append((part['start_lba'] * 512, src.read_bytes(), (backup / src.name).read_bytes()))
    primary, secondary = gpt.rename_vm_partitions()
    # Always include GPT in the sparse loader fixture; the commit skips unchanged regions.
    regions += [(p['disk_bytes'] - len(secondary), secondary, gpt.secondary), (0, primary, gpt.primary)]
    misc = (backup / 'misc.img').read_bytes()
    require(misc[:32].split(b'\0')[0] in (b'', b'boot-recovery'), 'Unexpected Android recovery command')
    regions.append((parts['misc']['start_lba'] * 512, bytes(32) + misc[32:512], misc[:512]))
    return regions


def validate(args):
    p, device, gpt = current()
    backup = checked_backup(device)
    regions = candidate_regions(p, gpt, backup)
    image = STATE / 'loader-test.sparse.img'
    require(not image.exists(), 'Existing loader fixture; remove only this file before retrying')
    with image.open('xb') as f:
        f.truncate(p['disk_bytes'])
        for offset, new, _ in regions:
            f.seek(offset)
            f.write(new)
    loader = args.loader.resolve()
    require(loader.is_file(), 'Build the loader harness first')
    result = subprocess.run([str(loader), '-b', '1', str(image)], cwd=STATE, capture_output=True, text=True)
    (STATE / 'loader-validation.log').write_text(result.stdout + result.stderr)
    require(result.returncode == 0 and 'vb2api_load_kernel() returned 0' in result.stdout, 'Loader failed; inspect private log')
    save_json(STATE / 'validated.json', dict(manifest_sha256=sha256(STATE / 'candidate/manifest.json'),
                                             regions=[dict(offset=o, sha256=digest(n)) for o, n, _ in regions]))
    image.unlink()
    print('ChromiumOS Android loader accepted the candidate. This is not a physical boot test.')


def write_all(fd, offset, data):
    view = memoryview(data)
    while view:
        n = os.pwrite(fd, view, offset)
        require(n > 0, 'Short disk write')
        view, offset = view[n:], offset + n
    os.fsync(fd)


def commit(args, *, approval=None):
    p, device, gpt = current()
    mounted()
    backup = checked_backup(device)
    require(not (STATE / 'commit-started').exists(), 'Commit already attempted; inspect result and recover manually')
    proof = read_json(STATE / 'validated.json')
    require(proof['manifest_sha256'] == sha256(STATE / 'candidate/manifest.json'), 'Candidate changed after validation')
    regions = candidate_regions(p, gpt, backup)
    require(proof['regions'] == [dict(offset=o, sha256=digest(n)) for o, n, _ in regions], 'Validated regions differ')
    with device.open('rb') as f:
        for offset, _, old in regions:
            f.seek(offset)
            require(f.read(len(old)) == old, 'Original boot region changed since backup')
    slot = active_slot({part['name']: part for part in gpt.partitions()})
    print('Will write four slot-' + slot.upper() + ' images, any necessary pvmfw label changes, and the 32-byte misc command. The other slot payloads remain unchanged.')
    if not args.apply:
        print('Dry run passed; no writes')
        return
    identity = device.stat()
    if approval is None:
        typed('WRITE VALIDATED SLOT ' + slot.upper())
    else:
        approval.check(p)
    repeated_plan, repeated_device, repeated_gpt = current()
    mounted()
    repeated_backup = checked_backup(repeated_device)
    require(repeated_plan == p and repeated_device == device and repeated_backup == backup,
            'Installation state changed after confirmation')
    require(candidate_regions(repeated_plan, repeated_gpt, repeated_backup) == regions,
            'Boot regions changed after confirmation')
    fd = os.open(device, os.O_RDWR | os.O_SYNC | os.O_NOFOLLOW)
    attempted = []
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        opened = os.fstat(fd)
        require(stat.S_ISBLK(opened.st_mode) and opened.st_rdev == identity.st_rdev, 'Opened disk identity changed')
        for offset, _, old in regions:
            require(os.pread(fd, len(old), offset) == old, 'Original boot region changed after confirmation')
        (STATE / 'commit-started').touch(mode=0o600)
        for offset, new, old in regions:
            if new == old:
                continue
            attempted.append((offset, old))
            write_all(fd, offset, new)
            require(os.pread(fd, len(new), offset) == new, 'Disk read-back failed')
        save_json(STATE / 'committed.json', dict(status='All boot regions passed read-back', disk_guid=p['disk_guid'], slot=slot))
    except BaseException:
        status = []
        for offset, old in reversed(attempted):
            try:
                write_all(fd, offset, old)
                require(os.pread(fd, len(old), offset) == old, 'Rollback read-back failed')
                status.append(dict(offset=offset, restored=True))
            except BaseException as error:
                status.append(dict(offset=offset, restored=False, error=str(error)))
        save_json(STATE / 'rollback.json', status)
        print('Commit failed. See /run/lapis-install/rollback.json. Do not reboot.')
        raise
    finally:
        os.close(fd)
    saved = TARGET / 'var/lib/lapis-fedora/installation'
    ignored = ['*.sparse.img', 'loaded-*.img']
    if approval is not None:
        # The wizard saves one verified linux-bootstrap bundle immediately
        # afterward. Keep these archives as records rather than duplicate images.
        ignored.extend(('*.img', '*.pem'))
    shutil.copytree(STATE, saved, ignore=shutil.ignore_patterns(*ignored), dirs_exist_ok=False)
    shutil.copytree(STATE, backup / 'installed-state', ignore=shutil.ignore_patterns(*ignored), dirs_exist_ok=False)
    os.sync()
    print('Boot committed and state archived. Follow the shutdown and first-boot checklist.')


def main():
    os.umask(0o022)
    parser = argparse.ArgumentParser(description=__doc__)
    subs = parser.add_subparsers(dest='command', required=True)
    q = subs.add_parser('plan'); q.add_argument('--device', required=True, type=Path); q.set_defaults(fn=plan)
    q = subs.add_parser('backup'); q.add_argument('--destination', required=True, type=Path); q.set_defaults(fn=backup)
    q = subs.add_parser('verify-backup'); q.add_argument('destination', type=Path); q.set_defaults(fn=lambda a: verify_backup(a.destination))
    for name, fn in [('format', format_root), ('commit', commit)]:
        q = subs.add_parser(name); q.add_argument('--apply', action='store_true'); q.set_defaults(fn=fn)
    q = subs.add_parser('build'); q.set_defaults(fn=build)
    q = subs.add_parser('validate'); q.add_argument('--loader', type=Path, default=ROOT / 'build/load_android_test'); q.set_defaults(fn=validate)
    args = parser.parse_args()
    args.fn(args)


if __name__ == '__main__':
    main()
