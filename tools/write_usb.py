#!/usr/bin/env python3
"""Write a validated image to an explicitly chosen external USB. Dry run by default."""
import argparse
from dataclasses import dataclass
import hashlib
import json
import os
from pathlib import Path
import platform
import plistlib
import re
import shutil
import stat
import struct
import subprocess
import sys
import time
import uuid
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from lapis.common import require, require_regular
from lapis.gpt import ENTRY, read_gpt, tables
from lapis.installer_usb import BACKUP_LABEL, backup_partition_spec, minimum_backup_bytes, validate_config


def inspect(device):
    if platform.system() == 'Darwin':
        require(re.fullmatch(r'/dev/disk\d+', str(device)), 'Use a whole /dev/diskN device')
        info = plistlib.loads(subprocess.check_output(['diskutil', 'info', '-plist', str(device)]))
        require(info.get('Whole') is True and info.get('Internal') is False, 'Refuse internal disks and partitions')
        require(info.get('BusProtocol') == 'USB' and info.get('Writable') is True, 'Require a writable USB disk')
        require(info.get('DeviceBlockSize') == 512, 'Require 512-byte logical sectors')
        return dict(device=str(device), bytes=info['TotalSize'], model=info.get('MediaName', ''),
                    identity=info.get('DeviceTreePath', ''), raw='/dev/r' + device.name)
    require(platform.system() == 'Linux', 'Supported writers: macOS and Linux')
    require(re.fullmatch(r'/dev/sd[a-z]+', str(device)), 'Use a whole USB /dev/sdX disk')
    info = json.loads(subprocess.check_output(['lsblk', '--json', '--bytes', '--nodeps', '-o', 'PATH,TYPE,TRAN,RO,SIZE,LOG-SEC,MODEL,SERIAL', str(device)]))['blockdevices']
    require(len(info) == 1, 'Ambiguous disk')
    d = info[0]
    require(d['type'] == 'disk' and d['tran'] == 'usb' and not d['ro'], 'Require a writable whole USB disk')
    require(d['log-sec'] == 512, 'Require 512-byte logical sectors')
    root = subprocess.check_output(['findmnt', '-no', 'SOURCE', '/'], text=True).strip().split('[')[0]
    if root.startswith('/dev/'):
        parents = subprocess.check_output(['lsblk', '-s', '-n', '-r', '-o', 'PATH', root], text=True).splitlines()
        require(str(device) not in parents, 'Refuse the running system disk')
    return dict(device=str(device), bytes=d['size'], model=d.get('model'), identity=d.get('serial'), raw=str(device))


def resized_tables(gpt, disk_bytes, entries=None):
    require(disk_bytes >= gpt.disk_bytes and disk_bytes % 512 == 0, 'USB too small or not sector aligned')
    header = list(gpt.header)
    header[6] = disk_bytes // 512 - 1
    header[8] = header[6] - 33
    mbr = bytearray(gpt.primary[:512])
    struct.pack_into('<I', mbr, 458, min(header[6], 0xffffffff))
    return tables(header, gpt.entries if entries is None else entries, mbr)


@dataclass(frozen=True)
class WritePlan:
    primary: bytes
    secondary: bytes
    payload_bytes: int
    backup: dict | None


def make_write_plan(gpt, disk_bytes, manifest):
    """Keep empty backup storage out of the copy and read-back ranges."""
    require(manifest.get('image_format') in (None, 'lapis-usb-v2'), 'Unsupported installer image format')
    parts = {p['name']: p for p in gpt.partitions()}
    declared = manifest.get('backup_partition')
    if declared is None:
        require('LAPIS-BACKUP' not in parts and manifest.get('image_format') is None, 'Missing backup partition metadata')
        primary, secondary = resized_tables(gpt, disk_bytes)
        return WritePlan(primary, secondary, gpt.disk_bytes, None)
    config = validate_config(manifest['usb'])
    require(gpt.guid == config['disk_guid'] and parts['ISO9660']['partuuid'] == config['iso_partuuid'], 'Image USB identities differ')
    require(isinstance(declared['size'], int) and declared['size'] >= minimum_backup_bytes()
            and declared['size'] % 1048576 == 0, 'Backup partition is too small or not MiB aligned')
    entries = bytearray(gpt.entries)
    embedded = parts.get('LAPIS-BACKUP')
    if embedded:
        require(manifest.get('image_format') is None, 'Compact images must not embed a backup filesystem')
        backup = dict(name=embedded['name'], offset=embedded['start_lba'] * 512,
                      size=embedded['size'], partuuid=embedded['partuuid'], index=embedded['index'])
        require(all((p['end_lba'] + 1) * 512 <= backup['offset'] for p in parts.values()
                    if p['name'] != 'LAPIS-BACKUP'), 'Only a trailing backup partition can be omitted from copying')
        payload_bytes = backup['offset']
    else:
        require(manifest.get('image_format') == 'lapis-usb-v2', 'Missing embedded backup partition')
        backup = backup_partition_spec(gpt.disk_bytes, config, declared['size'],
                                       max(p['index'] for p in parts.values()) + 1)
        require(backup['partuuid'] not in {p['partuuid'] for p in parts.values()}, 'Duplicate backup partition GUID')
        offset = (backup['index'] - 1) * 128
        require(entries[offset:offset + 128] == bytes(128), 'Backup GPT entry is already occupied')
        entries[offset:offset + 128] = ENTRY.pack(
            uuid.UUID('0fc63daf-8483-4772-8e79-3d69d8477de4').bytes_le,
            uuid.UUID(backup['partuuid']).bytes_le, backup['offset'] // 512,
            (backup['offset'] + backup['size']) // 512 - 1, 0, backup['name'].encode('utf-16-le'))
        payload_bytes = gpt.disk_bytes
    for field in ('name', 'offset', 'size', 'partuuid'):
        require(declared[field] == backup[field], 'Backup partition metadata differs: ' + field)
    require(backup['partuuid'] == config['backup_partuuid'], 'Backup partition GUID differs from live configuration')
    require(backup['offset'] + backup['size'] + 33 * 512 <= disk_bytes,
            'USB is too small for the boot image and requested backup partition')
    primary, secondary = resized_tables(gpt, disk_bytes, entries)
    return WritePlan(primary, secondary, payload_bytes, backup)


class Progress:
    def __init__(self, label, total):
        self.label, self.total = label, total
        self.started = time.monotonic()
        self.last = self.started
        self.update(0, force=True)

    def update(self, done, *, force=False):
        now = time.monotonic()
        if not force and done != self.total and now - self.last < 2:
            return
        elapsed = max(now - self.started, 0.001)
        text = (f'{self.label}: {done / self.total:.0%} '
                f'({done / 1024**2:.0f}/{self.total / 1024**2:.0f} MiB, '
                f'{done / elapsed / 1024**2:.1f} MiB/s)')
        terminal = sys.stdout.isatty()
        print(('\r' if terminal else '') + text,
              end='\n' if not terminal or done == self.total else '', flush=True)
        self.last = now


def hash_source(image, payload_bytes, progress=None):
    require(payload_bytes > 0, 'Boot payload must not be empty')
    full, payload_digest = hashlib.sha256(), None
    position = 0
    with image.open('rb') as source:
        for block in iter(lambda: source.read(4 * 1024**2), b''):
            if position < payload_bytes < position + len(block):
                payload = full.copy()
                payload.update(block[:payload_bytes - position])
                payload_digest = payload.hexdigest()
            full.update(block)
            position += len(block)
            if position == payload_bytes:
                payload_digest = full.hexdigest()
            if progress:
                progress.update(position)
    require(position >= payload_bytes, 'Source image is shorter than the boot payload')
    return full.hexdigest(), payload_digest


def copy_payload(source, destination, size, progress=None):
    remaining = size
    while remaining:
        block = source.read(min(remaining, 4 * 1024**2))
        require(block, 'Short image read')
        write_full(destination, block)
        remaining -= len(block)
        if progress:
            progress.update(size - remaining)


def verify_payload(destination, size, expected, progress=None):
    destination.seek(0)
    digest, remaining = hashlib.sha256(), size
    while remaining:
        block = destination.read(min(remaining, 4 * 1024**2))
        require(block, 'Short USB read-back')
        digest.update(block)
        remaining -= len(block)
        if progress:
            progress.update(size - remaining)
    require(digest.hexdigest() == expected, 'USB boot image read-back failed')


def find_formatter():
    for name in ('mkfs.ext4', 'mke2fs'):
        command = shutil.which(name)
        if command:
            return command
    if platform.system() == 'Darwin':
        for prefix in ('/opt/homebrew/opt/e2fsprogs/sbin', '/usr/local/opt/e2fsprogs/sbin'):
            command = Path(prefix) / 'mkfs.ext4'
            if command.is_file() and os.access(command, os.X_OK):
                return str(command)
    raise RuntimeError('Install e2fsprogs (mkfs.ext4) before writing this installer USB')


def backup_partition_node(device, backup):
    unmount(device)
    if platform.system() == 'Linux':
        subprocess.run(['blockdev', '--rereadpt', str(device)], check=True)
        subprocess.run(['udevadm', 'settle'], check=True)
        node = Path(str(device) + str(backup['index']))
        require(stat.S_ISBLK(node.stat().st_mode), 'Backup partition node is not a block device')
        sysfs = Path('/sys/class/block') / node.name
        require(int((sysfs / 'start').read_text()) * 512 == backup['offset']
                and int((sysfs / 'size').read_text()) * 512 == backup['size'], 'Kernel backup partition geometry differs')
        require((Path('/dev/disk/by-partuuid') / backup['partuuid']).resolve(strict=True) == node.resolve(),
                'Kernel backup partition GUID differs')
    else:
        subprocess.run(['diskutil', 'list', str(device)], check=True, stdout=subprocess.DEVNULL)
        node = Path(str(device) + 's' + str(backup['index']))
        info = plistlib.loads(subprocess.check_output(['diskutil', 'info', '-plist', str(node)]))
        require(info.get('ParentWholeDisk') == device.name and info.get('TotalSize') == backup['size']
                and str(info.get('DiskUUID', '')).lower() == backup['partuuid'],
                'macOS has not refreshed the backup partition identity; reconnect the USB before retrying')
    unmount(device)  # A desktop automounter may have reacted to the new GPT.
    return node


def verify_backup_filesystem(node, config, size):
    with node.open('rb', buffering=0) as source:
        source.seek(1024)
        superblock = source.read(1024)
    require(len(superblock) == 1024 and struct.unpack_from('<H', superblock, 56)[0] == 0xEF53,
            'Backup filesystem superblock is invalid')
    require(str(uuid.UUID(bytes=superblock[104:120])) == config['backup_uuid'], 'Backup filesystem UUID differs')
    require(superblock[120:136].split(b'\0', 1)[0].decode('ascii') == BACKUP_LABEL, 'Backup filesystem label differs')
    log_block_size = struct.unpack_from('<I', superblock, 24)[0]
    require(log_block_size <= 6, 'Backup filesystem block size is invalid')
    block_size = 1024 << log_block_size
    blocks = struct.unpack_from('<I', superblock, 4)[0]
    if struct.unpack_from('<I', superblock, 96)[0] & 0x80:
        blocks |= struct.unpack_from('<I', superblock, 336)[0] << 32
    require(blocks * block_size == size, 'Backup filesystem capacity differs')


def format_backup_filesystem(node, config, size, formatter, *, allow_regular=False):
    require(not node.is_symlink(), 'Refusing a symlinked backup partition node')
    mode = node.stat().st_mode
    require(stat.S_ISBLK(mode) or (allow_regular and stat.S_ISREG(mode)), 'Expected a backup partition block device')
    subprocess.run([formatter, '-t', 'ext4', '-F', '-q', '-m', '0', '-L', BACKUP_LABEL,
                    '-U', config['backup_uuid'], '-E', 'nodiscard,lazy_itable_init=1,lazy_journal_init=0', str(node)], check=True)
    # Check only the freshly written metadata, not gigabytes of empty space.
    verify_backup_filesystem(node, config, size)


def unmount(device):
    if platform.system() == 'Darwin':
        subprocess.run(['diskutil', 'unmountDisk', str(device)], check=True)
    else:
        rows = subprocess.check_output(['lsblk', '-n', '-r', '-o', 'PATH,MOUNTPOINT', str(device)], text=True).splitlines()
        for row in reversed(rows):
            fields = row.split(maxsplit=1)
            if len(fields) == 2:
                require(fields[1] != '[SWAP]', 'Deactivate USB swap explicitly before writing')
                subprocess.run(['umount', fields[0]], check=True)


def write_full(f, data):
    view = memoryview(data)
    while view:
        n = f.write(view)
        require(n is not None and n > 0, 'Short USB write')
        view = view[n:]


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--image', required=True, type=Path)
    parser.add_argument('--device', required=True, type=Path)
    parser.add_argument('--apply', action='store_true')
    args = parser.parse_args()
    require_regular(args.image)
    image_identity = args.image.stat()
    require(image_identity.st_size > 1024**3, 'Image unexpectedly small')
    manifest = json.loads(args.image.with_suffix('.manifest.json').read_text())
    require(manifest['size'] == image_identity.st_size, 'Image size differs from builder manifest')
    with args.image.open('rb') as f:
        gpt = read_gpt(f, image_identity.st_size)
    info = inspect(args.device)
    plan = make_write_plan(gpt, info['bytes'], manifest)
    formatter = find_formatter() if plan.backup else None
    digest, payload_digest = hash_source(args.image, plan.payload_bytes,
                                         Progress('Checking source image', manifest['size']))
    require(digest == manifest['sha256'], 'Image hash differs from builder manifest')
    image_fs = subprocess.check_output(['df', '-P', str(args.image)], text=True).splitlines()[-1].split()[0]
    require(not re.fullmatch(re.escape(str(args.device)) + r'(?:s\d+|\d+)?', image_fs), 'Image file is on the USB about to be erased')
    print(json.dumps({k:v for k,v in info.items() if k != 'identity'}, indent=2))
    print('Image SHA256:', manifest['sha256'])
    print(f'Boot data to copy and verify: {plan.payload_bytes / 1024**3:.2f} GiB')
    if plan.backup:
        print(f'Backup partition: {plan.backup["size"] / 1024**3:.2f} GiB, formatted separately; empty space is not copied or read back.')
    if not args.apply:
        print('Dry run passed. No writes. Apply erases this entire USB.')
        return
    require(os.geteuid() == 0 and sys.stdin.isatty(), 'Run apply as root in an interactive terminal')
    print('\033[1;31mWARNING: All data on ' + str(args.device) + ' will be erased. This cannot be undone.\033[0m')
    while input('Type YES to write the installer USB, or press Ctrl-C to cancel: ').strip() != 'YES':
        print('Enter YES in capitals to proceed, or press Ctrl-C to cancel.')
    require(inspect(args.device) == info, 'USB identity changed')
    checked = args.image.stat()
    require((checked.st_dev, checked.st_ino, checked.st_size, checked.st_mtime_ns) ==
            (image_identity.st_dev, image_identity.st_ino, image_identity.st_size, image_identity.st_mtime_ns),
            'Source image changed after validation')
    unmount(args.device)
    require(inspect(args.device) == info, 'USB identity changed after unmount')
    with open(info['raw'], 'r+b', buffering=0) as dest, args.image.open('rb') as source:
        copy_payload(source, dest, plan.payload_bytes, Progress('Writing boot image', plan.payload_bytes))
        print('Flushing boot image writes…', flush=True)
        os.fsync(dest.fileno())
        verify_payload(dest, plan.payload_bytes, payload_digest,
                       Progress('Verifying boot image', plan.payload_bytes))
        print('Updating the USB partition table…', flush=True)
        dest.seek(info['bytes'] - len(plan.secondary)); write_full(dest, plan.secondary)
        dest.seek(0); write_full(dest, plan.primary)
        os.fsync(dest.fileno())
        checked = read_gpt(dest, info['bytes'])
        require(checked.primary == plan.primary and checked.secondary == plan.secondary, 'Resized USB GPT read-back failed')
    if plan.backup:
        require(inspect(args.device) == info, 'USB identity changed before filesystem creation')
        node = backup_partition_node(args.device, plan.backup)
        print('Creating the empty backup filesystem (metadata only)…', flush=True)
        format_backup_filesystem(node, manifest['usb'], plan.backup['size'], formatter)
        print('Backup filesystem UUID, label, and capacity verified.', flush=True)
    print('Image and capacity-adjusted GPT verified. Eject the USB before unplugging.')


if __name__ == '__main__':
    try:
        main()
    except (KeyboardInterrupt, EOFError):
        print('\nUSB write cancelled.', file=sys.stderr)
        raise SystemExit(130)
    except (OSError, RuntimeError, ValueError, subprocess.SubprocessError) as error:
        print('USB write stopped: ' + str(error), file=sys.stderr)
        raise SystemExit(1)
