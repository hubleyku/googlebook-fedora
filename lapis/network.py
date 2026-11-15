"""Transfer live Wi-Fi profiles to the installed system without logging secrets."""
import configparser
from dataclasses import dataclass, field
import io
import os
from pathlib import Path
import subprocess
from urllib.parse import unquote, urlparse
import uuid

from .common import require

PROFILE_DIRS = (Path('/etc/NetworkManager/system-connections'),
                Path('/run/NetworkManager/system-connections'))


@dataclass(frozen=True)
class WifiProfile:
    uuid: str
    contents: str = field(repr=False)
    certificates: dict[str, bytes] = field(default_factory=dict, repr=False)


def nm_secret(connection, setting):
    result = subprocess.run(['nmcli', '--show-secrets', '--get-values', setting,
                             'connection', 'show', connection], capture_output=True, text=True, timeout=15)
    return result.stdout.rstrip('\n') if result.returncode == 0 else ''


def keyfile_secret(value):
    return value.replace('\\', '\\\\').replace('\n', '\\n').replace('\r', '\\r').replace('\t', '\\t').replace(' ', '\\s')


def active_wifi():
    result = subprocess.run(['nmcli', '--terse', '--fields', 'UUID,TYPE', 'connection', 'show', '--active'],
                            capture_output=True, text=True, check=True, timeout=15)
    return {line.partition(':')[0] for line in result.stdout.splitlines()
            if line.partition(':')[2] in ('wifi', '802-11-wireless')}


def read_wifi_profiles(directories=PROFILE_DIRS, active=None, secret_reader=nm_secret):
    active = active_wifi() if active is None else set(active)
    profiles = {}
    for directory in directories:
        if not directory.is_dir():
            continue
        for path in sorted(directory.iterdir()):
            if not path.is_file() or path.is_symlink():
                continue
            config = configparser.ConfigParser(interpolation=None, strict=False)
            try:
                config.read_string(path.read_text())
            except (UnicodeError, configparser.Error):
                raise RuntimeError('Cannot read NetworkManager profile: ' + path.name) from None
            if config.get('connection', 'type', fallback='') not in ('wifi', '802-11-wireless'):
                continue
            try:
                ident = str(uuid.UUID(config.get('connection', 'uuid')))
            except (ValueError, TypeError, configparser.Error):
                raise RuntimeError('Wi-Fi profile has an invalid UUID: ' + path.name) from None
            if ident in profiles:
                continue  # Persistent /etc profiles take precedence over /run.
            # A liveuser-only restriction would make the profile unusable by the
            # newly created user. Root-only keyfile permissions protect secrets.
            config.set('connection', 'permissions', '')
            for section, key in (('wifi-security', 'psk'), ('802-11-wireless-security', 'psk'),
                                 ('802-1x', 'password'), ('802-1x', 'private-key-password')):
                if not config.has_section(section):
                    continue
                if config.get(section, key, fallback=''):
                    config.set(section, key + '-flags', '0')
                    continue
                setting = ('802-11-wireless-security' if section == 'wifi-security' else section) + '.' + key
                secret = secret_reader(ident, setting)
                if secret and secret != '<hidden>':
                    config.set(section, key, keyfile_secret(secret))
                    config.set(section, key + '-flags', '0')
            security = next((s for s in ('wifi-security', '802-11-wireless-security') if config.has_section(s)), None)
            if ident in active and security:
                management = config.get(security, 'key-mgmt', fallback='')
                if management in ('wpa-psk', 'wpa-psk-sha256', 'sae'):
                    require(bool(config.get(security, 'psk', fallback='')),
                            'Save the current Wi-Fi password in the live network settings, then rerun install-fedora.')
                if management == 'wpa-eap' and config.get('802-1x', 'eap', fallback='').strip(';') in ('peap', 'ttls', 'pwd'):
                    require(bool(config.get('802-1x', 'password', fallback='')),
                            'Save the current enterprise Wi-Fi password in the live network settings, then rerun install-fedora.')
            certificates = {}
            for key in ('ca-cert', 'client-cert', 'private-key', 'phase2-ca-cert', 'phase2-client-cert', 'phase2-private-key'):
                value = config.get('802-1x', key, fallback='')
                if value.startswith('file://') or value.startswith('/'):
                    source = Path(unquote(urlparse(value).path) if value.startswith('file://') else value)
                    require(source.is_file(), 'A saved Wi-Fi certificate is unavailable: ' + source.name)
                    filename = ident + '-' + key
                    certificates[filename] = source.read_bytes()
                    config.set('802-1x', key, '/etc/NetworkManager/certs/' + filename)
            contents = io.StringIO()
            config.write(contents, space_around_delimiters=False)
            profiles[ident] = WifiProfile(ident, contents.getvalue(), certificates)
    require(active <= profiles.keys(), 'The active Wi-Fi connection has no transferable profile; save it in the live network settings first.')
    return list(profiles.values())


def install_wifi_profiles(root, profiles):
    root = Path(root)
    require(root.is_dir() and root.resolve() != Path('/'), 'Expected a mounted target root')
    for relative in ('etc', 'etc/NetworkManager', 'etc/NetworkManager/system-connections', 'etc/NetworkManager/certs'):
        directory = root / relative
        require(not directory.is_symlink(), 'Refusing a symlinked target network directory')
        directory.mkdir(mode=0o700, exist_ok=True)
    for profile in profiles:
        files = {'system-connections/lapis-wifi-' + profile.uuid + '.nmconnection': profile.contents.encode()}
        files.update({'certs/' + name: data for name, data in profile.certificates.items()})
        for relative, data in files.items():
            path = root / 'etc/NetworkManager' / relative
            require(not path.exists() and not path.is_symlink(), 'Target Wi-Fi file already exists')
            fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
            with os.fdopen(fd, 'wb') as stream:
                stream.write(data)
            if os.geteuid() == 0:
                os.chown(path, 0, 0)
