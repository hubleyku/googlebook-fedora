"""Small GPT and active-slot boot snapshots, independent of a Linux filesystem."""
import hashlib
from pathlib import Path

from .common import require, read_json, save_json, sha256
from .gpt import read_gpt
from .slots import active_slot, boot_names

WORKING_RESERVE = 512 * 1024**2


def backup_parts(parts):
    slot = active_slot(parts)
    require('misc' in parts, 'Boot-control partition is missing')
    return [parts[name] for name in (*boot_names(slot), 'misc')]


def required_free_bytes(partitions):
    return sum(p['size'] for p in backup_parts({p['name']: p for p in partitions})) + WORKING_RESERVE


def save_boot_backup(source, size, destination, plan=None, *, purpose='pre-install'):
    """Read a disk or regular image; write only a new private backup directory."""
    gpt = read_gpt(source, size)
    require(plan is None or plan.get('disk_guid') == gpt.guid, 'Backup disk identity changed')
    parts = {p['name']: p for p in gpt.partitions()}
    slot = active_slot(parts)
    require(not destination.exists() and not destination.is_symlink(), 'Backup directory already exists')
    destination.mkdir(mode=0o700)
    records = []
    for part in backup_parts(parts):
        filename = part['name'] + '.img'
        source.seek(part['start_lba'] * 512)
        remaining = part['size']
        digest = hashlib.sha256()
        with (destination / filename).open('xb') as output:
            output.truncate(part['size'])
            while remaining:
                block = source.read(min(remaining, 1024**2))
                require(block, 'Short disk read during backup')
                if block == bytes(len(block)):
                    output.seek(len(block), 1)
                else:
                    output.write(block)
                digest.update(block)
                remaining -= len(block)
        (destination / filename).chmod(0o600)
        records.append(dict(file=filename, size=part['size'], sha256=digest.hexdigest()))
    for filename, contents in (('gpt-primary.bin', gpt.primary), ('gpt-secondary.bin', gpt.secondary)):
        (destination / filename).write_bytes(contents)
        (destination / filename).chmod(0o600)
        records.append(dict(file=filename, size=len(contents), sha256=hashlib.sha256(contents).hexdigest()))
    snapshot = dict(plan or {})
    snapshot.update(disk_bytes=size, disk_guid=gpt.guid, partitions=gpt.partitions(), slot=slot,
                    gpt_sha256=hashlib.sha256(gpt.primary + gpt.secondary).hexdigest())
    if plan is not None and 'slot' not in plan and purpose == 'pre-install':
        del snapshot['slot']  # Preserve older staged plans byte-for-byte in JSON.
    save_json(destination / 'plan.json', snapshot)
    records.append(dict(file='plan.json', size=(destination / 'plan.json').stat().st_size,
                        sha256=sha256(destination / 'plan.json')))
    save_json(destination / 'manifest.json', dict(schema=1, scope='bootstrap', purpose=purpose,
              slot=slot, disk_guid=gpt.guid, records=records))
    verify_backup(destination)
    return snapshot


def verify_backup(destination):
    manifest = read_json(destination / 'manifest.json')
    names = set()
    for record in manifest['records']:
        filename = record['file']
        require(Path(filename).name == filename and filename not in names, 'Invalid or duplicate backup filename')
        names.add(filename)
        path = destination / filename
        require(path.is_file() and not path.is_symlink() and path.stat().st_size == record['size']
                and sha256(path) == record['sha256'], 'Backup verification failed: ' + filename)
    return manifest
