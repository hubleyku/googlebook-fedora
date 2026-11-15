"""Configure a freshly prepared Fedora target, never the running system."""
from pathlib import Path
import os
import tempfile
import re
import uuid
from .common import ROOT, require
from .locales import DEFAULT_LOCALE, validate_locale


def validate_account(user, password):
    require(re.fullmatch(r'[a-z][a-z0-9_-]{0,30}', user) is not None and user not in ('root', 'liveuser'), 'Invalid username')
    require(bool(password) and not any(c in password for c in ('\n', '\r', '\0')), 'Password must be nonempty and contain no line breaks')
    require(len(password.encode()) <= 512, 'Use a password no longer than 512 UTF-8 bytes')
    # Deliberately no minimum length: account creation will use the supplied value.


def render_fstab(root_uuid):
    value = str(uuid.UUID(root_uuid))
    options = 'noatime,compress=zstd:3'
    return (f'UUID={value} / btrfs {options},subvol=@ 0 0\n'
            f'UUID={value} /home btrfs {options},subvol=@home 0 0\n')


def _write(root, relative, data, mode=0o644):
    require(not relative.startswith('/') and '..' not in relative.split('/'), 'Unsafe target path')
    path = root
    for component in relative.split('/')[:-1]:
        path = path / component
        require(not path.is_symlink(), 'Refusing to write through a target symlink: ' + str(path))
        path.mkdir(exist_ok=True)
    path = root / relative
    require(not path.is_symlink(), 'Refusing to overwrite a target symlink: ' + str(path))
    tmp = None
    try:
        with tempfile.NamedTemporaryFile(mode='w', dir=path.parent, prefix='.lapis-', delete=False) as f:
            tmp = Path(f.name)
            f.write(data)
            f.flush()
            os.fsync(f.fileno())
        tmp.chmod(mode)
        os.replace(tmp, path)
    finally:
        if tmp is not None:
            tmp.unlink(missing_ok=True)


def configure(root, root_uuid, timezone, keymap='us', hostname='fedora', locale=DEFAULT_LOCALE):
    root = Path(root)
    require(not root.is_symlink() and root.is_dir() and root.resolve() != Path('/'), 'Expected a separate target root')
    require(re.fullmatch(r'[A-Za-z0-9_+-]+(?:/[A-Za-z0-9_+-]+)*', timezone) is not None, 'Invalid timezone')
    zone = root / 'usr/share/zoneinfo' / timezone
    require(zone.is_file() and zone.resolve().is_relative_to((root / 'usr/share/zoneinfo').resolve()), 'Timezone is unavailable in target')
    require(re.fullmatch(r'[A-Za-z0-9_-]+', keymap) is not None, 'Invalid keymap')
    require(re.fullmatch(r'[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?', hostname) is not None, 'Invalid hostname')
    locale = validate_locale(locale)
    _write(root, 'etc/fstab', render_fstab(root_uuid))
    _write(root, 'etc/hostname', hostname + '\n')
    _write(root, 'etc/locale.conf', 'LANG=' + locale + '\n')
    _write(root, 'etc/vconsole.conf', 'KEYMAP=' + keymap + '\n')
    _write(root, 'etc/sudoers.d/10-lapis-wheel', '%wheel ALL=(ALL:ALL) ALL\n', 0o440)
    _write(root, 'etc/modprobe.d/lapis-xe.conf', 'blacklist xe\nblacklist snd_sof_pci_intel_ptl\n')
    _write(root, 'etc/modprobe.d/lapis-audio.conf', 'options snd_soc_sof_sdw quirk=0x288000\noptions snd_sof_intel_hda_generic dmic_num=0\n')
    _write(root, 'usr/local/libexec/lapis-gpu-prepare', (ROOT / 'assets/installed/gpu-prepare.py').read_text(), 0o755)
    _write(root, 'etc/systemd/system/lapis-gpu-prepare.service', (ROOT / 'assets/installed/lapis-gpu-prepare.service').read_text())
    # SELinux must label files created outside the target's policy context.
    _write(root, '.autorelabel', '')
    generators = root / 'etc/systemd/system-generators'
    require(not generators.is_symlink(), 'Unexpected system-generators symlink')
    generators.mkdir(parents=True, exist_ok=True)
    generator = generators / 'systemd-gpt-auto-generator'
    if generator.is_symlink():
        require(generator.readlink() == Path('/dev/null'), 'Unexpected GPT auto-generator override')
    else:
        require(not generator.exists(), 'Unexpected GPT auto-generator override')
        generator.symlink_to('/dev/null')
    localtime = root / 'etc/localtime'
    if localtime.is_symlink() or localtime.is_file():
        localtime.unlink()
    require(not localtime.exists(), 'Unexpected localtime path')
    localtime.symlink_to('/usr/share/zoneinfo/' + timezone)
    return ['lapis-gpu-prepare.service', 'NetworkManager.service', 'systemd-resolved.service',
            'chronyd.service', 'firewalld.service', 'bluetooth.service']
