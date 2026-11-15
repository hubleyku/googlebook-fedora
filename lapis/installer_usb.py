"""Identity and mount handling for the writable partition on the installer USB."""
import os
from pathlib import Path
import stat
import subprocess
import uuid

from .common import ROOT, output, read_json, require, run
from .backup import required_free_bytes

BACKUP_LABEL = 'LapisBackups'
BACKUP_MOUNT = Path('/run/lapis-usb')
USB_CONFIG = Path('/etc/lapis/usb.json')
DEFAULT_BACKUP_BYTES = 2 * 1024**3


def required_backup_free_bytes():
    profile = read_json(ROOT / 'assets/lapis-512gb.json')
    # The public profile omits device-specific attributes; sizing is independent
    # of which slot is active because A and B have identical geometry.
    parts = [dict(p, attributes=(15 << 48 | 1 << 56) if p['name'] == 'vbmeta_a' else 0)
             for p in profile['partitions']]
    return required_free_bytes(parts)


def minimum_backup_bytes():
    # Include ext4 journal/metadata overhead as well as the free working space
    # required by the installer. Round up so a CLI MiB value can satisfy it.
    mib = 1024**2
    return ((required_backup_free_bytes() + 128 * mib + mib - 1) // mib) * mib


def usb_config():
    return dict(schema=1, disk_guid=str(uuid.uuid4()), iso_partuuid=str(uuid.uuid4()),
                backup_partuuid=str(uuid.uuid4()), backup_uuid=str(uuid.uuid4()))


def validate_config(config):
    require(config.get('schema') == 1, 'Unsupported installer USB metadata')
    for key in ('disk_guid', 'iso_partuuid', 'backup_partuuid', 'backup_uuid'):
        try:
            require(str(uuid.UUID(config[key])) == config[key], 'Invalid installer USB identifier')
        except (KeyError, ValueError, TypeError, AttributeError):
            raise RuntimeError('Invalid installer USB metadata: ' + key) from None
    require(config['iso_partuuid'] != config['backup_partuuid'], 'Installer partitions must be distinct')
    return config


def backup_partition_spec(image_bytes, config, size, index=8):
    """Describe storage created by the writer, outside the compact image."""
    validate_config(config)
    require(isinstance(size, int) and size > 0 and size % 1048576 == 0, 'Backup size must be a positive whole MiB')
    require(1 <= index <= 128, 'No GPT entry available for backup storage')
    return dict(name='LAPIS-BACKUP', offset=((image_bytes + 1048575) // 1048576) * 1048576,
                size=size, partuuid=config['backup_partuuid'], index=index, deferred=True)


def installer_partition(config):
    """Verify the backup partition belongs to the USB that supplied the live ISO."""
    validate_config(config)
    blocks = []
    for key in ('iso_partuuid', 'backup_partuuid'):
        block = (Path('/dev/disk/by-partuuid') / config[key]).resolve(strict=True)
        require(stat.S_ISBLK(block.stat().st_mode), 'Installer USB partition is not a block device')
        blocks.append(block)
    iso, backup = blocks
    parents = [output(['lsblk', '-dn', '-o', 'PKNAME', p]) for p in blocks]
    require(parents[0] and parents[0] == parents[1], 'Backup partition is not on the installer USB')
    device = Path('/dev') / parents[0]
    require(output(['lsblk', '-dn', '-o', 'TRAN', device]) == 'usb', 'Installer storage is not a USB drive')
    require(output(['blkid', '-s', 'PTUUID', '-o', 'value', device]) == config['disk_guid'], 'Installer USB disk identity differs')
    live = output(['findmnt', '-no', 'SOURCE', '--mountpoint', '/run/initramfs/live']).split('[', 1)[0]
    require(Path(live).resolve() == iso, 'This USB did not supply the running live environment')
    require(output(['blkid', '-s', 'TYPE', '-o', 'value', backup]) == 'ext4', 'Installer backup filesystem must be ext4')
    require(output(['blkid', '-s', 'UUID', '-o', 'value', backup]) == config['backup_uuid'], 'Installer backup filesystem identity differs')
    return backup


def ensure_mounted(config_path=USB_CONFIG, mountpoint=BACKUP_MOUNT):
    require(os.geteuid() == 0, 'Mounting installer storage requires root')
    config = validate_config(read_json(config_path))
    partition = installer_partition(config)
    require(not mountpoint.is_symlink(), 'Invalid installer backup mountpoint')
    mountpoint.mkdir(mode=0o700, parents=True, exist_ok=True)
    mounted = subprocess.run(['findmnt', '-no', 'SOURCE', '--mountpoint', str(mountpoint)],
                             capture_output=True, text=True)
    if mounted.returncode == 0:
        require(Path(mounted.stdout.strip().split('[', 1)[0]).resolve() == partition,
                'Another filesystem occupies the installer backup mountpoint')
    else:
        run(['mount', '-t', 'ext4', '-o', 'rw,nosuid,nodev', partition, mountpoint])
    require(output(['findmnt', '-no', 'UUID', '--mountpoint', mountpoint]) == config['backup_uuid'], 'Wrong installer backup mount')
    options = output(['findmnt', '-no', 'OPTIONS', '--mountpoint', mountpoint]).split(',')
    require('rw' in options and os.access(mountpoint, os.W_OK), 'Installer backup partition is not writable')
    return mountpoint
