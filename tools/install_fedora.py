#!/usr/bin/env python3
"""Install Fedora on the supported Googlebook with one upfront preferences form."""
import argparse
from datetime import datetime, timezone
import getpass
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
from types import SimpleNamespace
from zoneinfo import available_timezones

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from lapis.accounts import hash_password
from lapis.backup import save_boot_backup
from lapis.common import ROOT, output, read_json, require, save_json
from lapis.fedora_target import validate_account
from lapis.installation import DESKTOPS, InstallApproval, Preferences, validate_timezone, validate_username
from lapis.installer_usb import ensure_mounted, required_backup_free_bytes
from lapis.network import read_wifi_profiles
from lapis.locales import DEFAULT_LOCALE, available_locales, locale_data_path, locale_packages, validate_locale
from lapis.slots import active_slot
from tools import configure_fedora, native_fedora


def available_keymaps():
    result = subprocess.run(['localectl', 'list-keymaps'], capture_output=True, text=True)
    keymaps = set(result.stdout.splitlines()) if result.returncode == 0 else set()
    if not keymaps:
        for directory in (Path('/usr/lib/kbd/keymaps'), Path('/usr/share/kbd/keymaps')):
            keymaps.update(p.name.split('.map', 1)[0] for p in directory.rglob('*.map*'))
    require(keymaps, 'The live environment has no keymap database.')
    return keymaps


def default_timezone():
    path = str(Path('/etc/localtime').resolve())
    candidate = path.split('/zoneinfo/', 1)[-1] if '/zoneinfo/' in path else 'UTC'
    try:
        return validate_timezone(candidate)
    except RuntimeError:
        return 'UTC'


def ask(label, validator, *, default=None, choices=None, input_fn=input):
    while True:
        value = input_fn(label + (f' [{default}]' if default else '') + ': ').strip()
        if value == '?' and choices is not None:
            print(', '.join(sorted(choices)))
            continue
        value = value or default or ''
        try:
            return validator(value)
        except (ValueError, RuntimeError) as error:
            print(str(error))


def collect_preferences(*, keymaps=None, input_fn=input, password_fn=getpass.getpass):
    """Read and validate every choice; perform no installation or backup work."""
    keymaps = available_keymaps() if keymaps is None else set(keymaps)
    print('Fedora installer — enter ? to list timezones, locale suggestions, or keymaps. Ctrl-C cancels.')
    zone = ask('Timezone', validate_timezone, default=default_timezone(),
               choices=available_timezones(), input_fn=input_fn)
    language = ask('Locale', validate_locale, default=DEFAULT_LOCALE,
                   choices=available_locales(), input_fn=input_fn)

    def keymap(value):
        require(value in keymaps, 'Unknown keymap. Enter ? to list available keymaps.')
        return value

    keyboard = ask('Keymap', keymap, default='us' if 'us' in keymaps else sorted(keymaps)[0],
                   choices=keymaps, input_fn=input_fn)
    user = ask('Username', validate_username, input_fn=input_fn)
    while True:
        password = password_fn('Password: ')
        try:
            validate_account(user, password)
            if password != password_fn('Repeat password: '):
                print('Passwords do not match. Please try again.')
                continue
            # Detect unsupported password inputs or hashing support before YES.
            hash_password(password)
            break
        except RuntimeError as error:
            print(str(error))
    keys = list(DESKTOPS)
    print('Desktop environment:')
    for index, name in enumerate(keys, 1):
        print(f'  {index}. {DESKTOPS[name].name}')

    def desktop(value):
        if value.isdecimal() and 1 <= int(value) <= len(keys):
            return keys[int(value) - 1]
        normalized = value.casefold()
        for key, choice in DESKTOPS.items():
            if normalized in (key, choice.name.casefold()):
                return key
        raise RuntimeError('Choose a listed desktop number or name.')

    selected = ask('Desktop', desktop, default='1', input_fn=input_fn)
    preferences = Preferences(zone, keyboard, user, password, selected, locale=language)
    preferences.validate(keymaps)
    return preferences


def find_target(explicit=None):
    candidates = [explicit] if explicit else [Path('/dev') / p.name for p in sorted(Path('/sys/class/block').glob('nvme*n*'))
                                             if re.fullmatch(r'nvme[0-9]+n[0-9]+', p.name)]
    supported = []
    for device in candidates:
        try:
            gpt = native_fedora.hardware(device, ac=True)
            require(not output(['lsblk', '-n', '-r', '-o', 'MOUNTPOINTS', device]), 'The target disk has mounted filesystems.')
            supported.append((device, gpt))
        except (RuntimeError, OSError, subprocess.CalledProcessError):
            if explicit:
                raise
    require(supported, 'No supported unmounted disk found. Boot the Lapis Fedora live USB and connect AC power.')
    require(len(supported) == 1, 'More than one supported disk found. Specify the intended disk with --device /dev/nvmeNnN.')
    return supported[0]


def preview_plan(device, gpt):
    return dict(device=str(device), disk_guid=gpt.guid,
                gpt_sha256=native_fedora.digest(gpt.primary + gpt.secondary))


def verify_package_sources(desktop, locale=DEFAULT_LOCALE):
    """Check Fedora metadata before erasing anything; use no third-party repos."""
    require(shutil.which('chpasswd'), 'The live environment needs chpasswd from shadow-utils.')
    base = ['dnf5', '--quiet', '--releasever=45', '--repo=fedora,updates']
    specs = [p for p in configure_fedora.PACKAGES if not p.startswith('@')]
    specs.extend(DESKTOPS[desktop].packages)
    specs.extend(locale_packages(locale))
    result = subprocess.run([*base, 'repoquery', '--available', '--queryformat=%{name}\n', *specs],
                            capture_output=True, text=True, check=True)
    available = set(result.stdout.splitlines())
    expected = {p if p != 'kernel-7.2.11-300.fc45' else 'kernel' for p in specs}
    require(expected <= available, 'Required Fedora packages are unavailable: ' + ', '.join(sorted(expected - available)))
    if locale_packages(locale):
        files = subprocess.run([*base, 'repoquery', '--available', '--files', *locale_packages(locale)],
                               capture_output=True, text=True, check=True)
        require(locale_data_path(locale) in files.stdout.splitlines(),
                'Fedora does not provide the selected UTF-8 locale: ' + locale)
    for environment in filter(None, ('custom-environment', DESKTOPS[desktop].environment)):
        subprocess.run([*base, 'environment', 'info', environment], capture_output=True, text=True, check=True)


def confirm(preferences, device, slot, backup_root, profile_count, *, input_fn=input):
    print('\nReady to install:')
    print('  Target:', device, '— boot slot', slot.upper())
    print('  Timezone:', preferences.timezone, '  Keymap:', preferences.keymap)
    print('  Locale:', preferences.locale)
    print('  Username:', preferences.user, '  Password: set')
    print('  Desktop:', DESKTOPS[preferences.desktop].name)
    print('  Wi-Fi profiles to transfer:', profile_count)
    print('  Automatic backup:', backup_root)
    print('\033[1;31mWARNING: Existing user data on this target will be erased. This cannot be undone.\033[0m')
    while input_fn('Type YES to install, or press Ctrl-C to cancel: ').strip() != 'YES':
        print('Enter YES in capitals to proceed, or press Ctrl-C to cancel.')


def archive_state(backup, *, error=None, compact=False):
    if native_fedora.STATE.is_dir() and backup.is_dir():
        ignored = ['*.sparse.img', 'loaded-*.img']
        if compact:
            ignored.extend(('*.img', '*.pem'))
        shutil.copytree(native_fedora.STATE, backup / 'installer-state', dirs_exist_ok=True,
                        ignore=shutil.ignore_patterns(*ignored))
        if error is not None:
            save_json(backup / 'failure.json', dict(error=str(error), time=datetime.now(timezone.utc).isoformat()))
        os.sync()


def cleanup_target(device):
    result = subprocess.run(['findmnt', '-no', 'SOURCE', '--mountpoint', str(native_fedora.TARGET)],
                            capture_output=True, text=True)
    if result.returncode == 0:
        source = result.stdout.strip().split('[', 1)[0]
        if Path(source).resolve() == Path(str(device) + 'p1').resolve():
            subprocess.run(['umount', '-R', str(native_fedora.TARGET)], check=True)


def save_installed_bootstrap(device, destination):
    plan = read_json(native_fedora.STATE / 'plan.json')
    snapshot = destination / 'linux-bootstrap'
    with device.open('rb') as source:
        saved = save_boot_backup(source, plan['disk_bytes'], snapshot, plan, purpose='pending-linux-bootstrap')
    require(saved['slot'] == plan['slot'], 'Boot slot changed before the Linux bootstrap snapshot')
    manifest = read_json(snapshot / 'manifest.json')
    records = {r['file']: r for r in manifest['records']}
    candidate = read_json(native_fedora.STATE / 'candidate/manifest.json')
    for record in candidate['images']:
        require(records[record['name'] + '.img']['sha256'] == record['sha256'], 'Linux bootstrap image differs from the committed candidate')
    with (snapshot / 'misc.img').open('rb') as source:
        require(source.read(32) == bytes(32), 'A recovery command appeared before snapshot completion')
    manifest['purpose'] = 'linux-bootstrap'
    save_json(snapshot / 'manifest.json', manifest)


def perform_install(preferences, device, backup_root, wifi_profiles, approval):
    """Run the already-confirmed plan without asking any further questions."""
    stamp = datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')
    destination = backup_root / 'backups' / (stamp + '-' + approval.disk_guid[:8])
    require(not destination.parent.is_symlink(), 'Invalid USB backup directory')
    destination.parent.mkdir(mode=0o700, exist_ok=True)
    args = SimpleNamespace(**preferences.public(), device=device, destination=destination,
                           apply=True, quiet=True, loader=ROOT / 'build/load_android_test')
    stage = 'planning'
    try:
        native_fedora.plan(args)
        approval.check(read_json(native_fedora.STATE / 'plan.json'))
        stage = 'backup'
        print('\n[1/7] Saving and verifying the boot-support backup on the USB…')
        native_fedora.backup(args)
        archive_state(destination)
        stage = 'formatting'
        print('[2/7] Creating Fedora Btrfs subvolumes…')
        native_fedora.format_root(args, approval=approval)
        stage = 'packages and account'
        print('[3/7] Installing the base system, desktop, and login account…')
        configure_fedora.packages(args, password=preferences.password)
        stage = 'configuration'
        print('[4/7] Configuring hardware, networking, and kernel updates…')
        configure_fedora.setup(args, wifi_profiles=wifi_profiles)
        archive_state(destination)
        stage = 'boot image generation'
        print('[5/7] Building boot images…')
        native_fedora.build(args)
        stage = 'boot image validation'
        print('[6/7] Validating boot images…')
        native_fedora.validate(args)
        stage = 'boot image commit'
        print('[7/7] Installing and reading back boot images…')
        native_fedora.commit(args, approval=approval)
        stage = 'Linux bootstrap snapshot'
        print('Saving the Linux bootstrap recovery snapshot…')
        save_installed_bootstrap(device, destination)
        archive_state(destination, compact=True)
    except BaseException as error:
        try:
            archive_state(destination, error=RuntimeError(stage + ': ' + str(error)))
        except (OSError, RuntimeError):
            pass
        print('Installation stopped during ' + stage + '. Backup/state location: ' + str(destination), file=sys.stderr)
        raise
    finally:
        if (native_fedora.STATE / 'format-started').exists():
            cleanup_target(device)
    print('\nFedora installation complete. Backup:', destination)
    print('Shut down, remove the USB, and boot the internal drive.')
    return destination


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--device', type=Path, help='override automatic supported-disk selection')
    parser.add_argument('--dry-run', action='store_true', help='collect preferences and inspect the target without mounting, backing up, or installing')
    args = parser.parse_args(argv)
    try:
        require(os.geteuid() == 0 and sys.stdin.isatty(), 'Run install-fedora from a terminal in the Lapis live environment.')
        # Collect everything before mount fallback, package queries, plan files,
        # backups, formatting, or any target-system operation.
        preferences = collect_preferences()
        device, gpt = find_target(args.device)
        plan = preview_plan(device, gpt)
        slot = active_slot({p['name']: p for p in gpt.partitions()})
        require(not native_fedora.STATE.exists(), 'An earlier install attempt remains in /run/lapis-install. See docs/install.md before retrying.')
        if args.dry_run:
            print('Preferences valid. Supported target:', device, 'slot', slot.upper())
            print('Dry run complete. No backup or installation was started.')
            return 0
        backup_root = ensure_mounted()
        require(shutil.disk_usage(backup_root).free > required_backup_free_bytes(), 'The USB backup partition has insufficient free space.')
        native_fedora.external(backup_root, device)
        wifi_profiles = read_wifi_profiles()
        print('Checking Fedora package availability…')
        verify_package_sources(preferences.desktop, preferences.locale)
        confirm(preferences, device, slot, backup_root, len(wifi_profiles))
        perform_install(preferences, device, backup_root, wifi_profiles, InstallApproval.for_plan(plan))
        return 0
    except (KeyboardInterrupt, EOFError):
        print('\nInstaller cancelled.', file=sys.stderr)
        return 130
    except (OSError, RuntimeError, ValueError, subprocess.SubprocessError) as error:
        print('Installer stopped: ' + str(error), file=sys.stderr)
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
