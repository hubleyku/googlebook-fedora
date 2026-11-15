#!/usr/bin/env python3
"""Staged Fedora target provisioning for the supported Lapis live environment.
"""
import argparse
from contextlib import contextmanager
import getpass
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from lapis.common import ROOT, read_json, require, run, save_json, sha256, runtime_sources
from lapis.fedora_target import _write, configure, validate_account
from lapis.accounts import create_account
from lapis.installation import DESKTOPS
from lapis.network import install_wifi_profiles
from lapis.locales import DEFAULT_LOCALE, locale_packages, validate_locale, verify_target_locale
from tools.native_fedora import STATE, TARGET, current, mounted, checked_backup

KERNEL_VERSION = '7.2.11-300.fc45.x86_64'
# Keep the machine usable before a desktop environment is installed. List
# essential hardware support explicitly: the Custom environment and optional
# dependencies of linux-firmware/NetworkManager do not guarantee these packages.
PACKAGES = [
    '@custom-environment',
    'kernel-7.2.11-300.fc45', 'btrfs-progs', 'dracut',

    # Lapis hardware: CPU, graphics, Wi-Fi, Bluetooth, and SoundWire audio.
    'linux-firmware', 'microcode_ctl', 'intel-gpu-firmware',
    'iwlwifi-mld-firmware', 'iwlbluetooth-firmware',
    'intel-audio-firmware', 'cirrus-audio-firmware', 'alsa-sof-firmware',

    # Wired/wireless networking, terminal configuration, DNS, and Bluetooth.
    'NetworkManager', 'NetworkManager-wifi', 'NetworkManager-tui',
    'NetworkManager-bluetooth', 'wpa_supplicant', 'systemd-resolved', 'bluez',

    # Desktop-independent audio services, routing profiles, and diagnostics.
    'alsa-ucm', 'alsa-utils', 'pipewire', 'pipewire-alsa',
    'pipewire-pulseaudio', 'wireplumber',

    # Removable PC media and services shared by desktop environments.
    'dosfstools', 'exfatprogs', 'ntfs-3g', 'udisks2', 'polkit', 'upower',

    # Local administration, security policy, TLS, and network time.
    'sudo', 'shadow-utils', 'authselect', 'selinux-policy-targeted', 'policycoreutils',
    'firewalld', 'ca-certificates', 'openssl', 'chrony',

    # Recovery and troubleshooting tools available without a graphical session.
    'python3', 'util-linux', 'procps-ng', 'psmisc', 'pciutils', 'usbutils',
    'iproute', 'iputils', 'iw', 'ethtool', 'curl', 'openssh-clients',
    'nano', 'less', 'bash-completion', 'man-db', 'man-pages', 'micro', 'glibc-langpack-en',

    # Common archive and compression formats.
    'tar', 'gzip', 'bzip2', 'xz', 'zstd', 'zip', 'unzip',

    # Misc
    'zsh'

]


def preserve_kernels(conf):
    lines = conf.splitlines(keepends=True)
    mains = [i for i, line in enumerate(lines) if re.fullmatch(r'\s*\[main\]\s*', line)]
    require(len(mains) == 1, 'Fedora DNF configuration must contain one main section')
    start = mains[0]
    end = next((i for i in range(start + 1, len(lines)) if re.match(r'\s*\[', lines[i])), len(lines))
    matches = [i for i in range(start + 1, end) if re.match(r'\s*installonly_limit\s*=', lines[i])]
    require(len(matches) <= 1, 'Duplicate kernel retention settings')
    if matches:
        lines[matches[0]] = 'installonly_limit=0\n'
    else:
        if not lines[start].endswith('\n'):
            lines[start] += '\n'
        lines.insert(start + 1, 'installonly_limit=0\n')
    return ''.join(lines)


@contextmanager
def target_mounts():
    mounted_paths = []
    try:
        for source, name in (('/dev', 'dev'), ('/sys', 'sys'), ('/proc', 'proc')):
            dest = TARGET / name
            require(not dest.is_symlink(), 'Refusing target mount through a symlink')
            dest.mkdir(exist_ok=True)
            require(subprocess.run(['mountpoint', '-q', str(dest)]).returncode != 0, 'Target mount already in use: ' + name)
            run(['mount', '--rbind', source, dest])
            mounted_paths.append(dest)
            run(['mount', '--make-rslave', dest])
        yield
    finally:
        for path in reversed(mounted_paths):
            run(['umount', '-R', path])


def temporary_dracut_masks():
    directory = TARGET / 'etc/kernel/install.d'
    directory.mkdir(parents=True, exist_ok=True)
    paths = [directory / name for name in ('20-grub.install', '50-dracut.install',
                                             '51-dracut-rescue.install', '95-set-boot-entry.install')]
    for path in paths:
        require(not path.exists() and not path.is_symlink(), 'Unexpected kernel-install override: ' + str(path))
    for path in paths:
        path.symlink_to('/dev/null')
    return paths


def remove_dracut_masks(paths):
    for path in paths:
        require(path.is_symlink() and path.readlink() == Path('/dev/null'), 'Kernel-install override changed')
    for path in paths:
        path.unlink()


@contextmanager
def target_resolver():
    """Provide working live DNS for chroot DNF, then restore resolved's link."""
    path = TARGET / 'etc/resolv.conf'
    link = path.readlink() if path.is_symlink() else None
    previous = path.read_bytes() if path.is_file() and link is None else None
    if path.exists() or path.is_symlink():
        path.unlink()
    path.write_bytes(Path('/etc/resolv.conf').read_bytes())
    path.chmod(0o644)
    try:
        yield
    finally:
        path.unlink(missing_ok=True)
        if link is not None:
            path.symlink_to(link)
        elif previous is not None:
            path.write_bytes(previous)
            path.chmod(0o644)
        else:
            path.symlink_to('../run/systemd/resolve/stub-resolv.conf')


def install_desktop(desktop):
    require(desktop in DESKTOPS, 'Unknown desktop environment')
    environment = DESKTOPS[desktop].environment
    if environment:
        with target_resolver():
            run(['chroot', TARGET, 'dnf5', '--assumeyes', '--repo=fedora,updates', 'install',
                 '@' + environment, *DESKTOPS[desktop].packages])


def configure_desktop(desktop):
    choice = DESKTOPS[desktop]
    if choice.environment is None:
        # Fedora presets select kmscon as autovt and disable agetty. Keep the
        # standard console even when package updates re-apply those presets.
        _write(TARGET, 'etc/systemd/system-preset/00-lapis-console.preset',
               'disable kmscon.service\ndisable kmsconvt@.service\nenable getty@.service\n')
        run(['systemctl', '--root=' + str(TARGET), 'mask', 'kmscon.service', 'kmsconvt@.service'])
        run(['systemctl', '--root=' + str(TARGET), 'enable', '--force', 'getty@.service'])
        # graphical.target includes multi-user.target and keeps console logins.
        # It also starts display-manager.service if a user later enables greetd
        # or another manager after choosing a console-only installation.
        run(['systemctl', '--root=' + str(TARGET), 'set-default', 'graphical.target'])
        return
    manager = next((name for name in choice.managers
                    if (TARGET / 'usr/lib/systemd/system' / (name + '.service')).is_file()), None)
    require(manager is not None, 'The selected desktop did not provide a supported display manager')
    run(['systemctl', '--root=' + str(TARGET), 'enable', '--force', manager + '.service'])
    run(['systemctl', '--root=' + str(TARGET), 'set-default', 'graphical.target'])


def install_kit():
    dest = TARGET / 'opt/lapis-kit'
    require(not dest.exists(), 'Target already contains a Lapis kit')
    for folder in ('lapis', 'tools', 'assets', 'docs'):
        shutil.copytree(ROOT / folder, dest / folder,
                        ignore=shutil.ignore_patterns('__pycache__', '*.pyc'))
    for name in ('README.md', 'LICENSE', 'NOTICE.md'):
        shutil.copy2(ROOT / name, dest / name)
    from tools.build_fedora_usb import vendor_payload
    for relative, data in vendor_payload(runtime_sources(ROOT), ROOT).items():
        path = dest / 'vendor' / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
        path.chmod(0o755 if path.suffix == '.py' else 0o644)
    loader = ROOT / 'build/load_android_test'
    require(loader.is_file() and not loader.is_symlink(), 'Loader harness is missing')
    (dest / 'build').mkdir(parents=True, exist_ok=True)
    shutil.copy2(loader, dest / 'build/load_android_test')
    return dest


def label_target():
    """Prototype: apply target SELinux labels before its first boot.

    Keep /.autorelabel until a clean end-to-end installation has verified the
    labeling process. Run only after target_mounts has unmounted host trees.
    """
    contexts = TARGET / 'etc/selinux/targeted/contexts/files/file_contexts'
    require(contexts.is_file() and not contexts.is_symlink(), 'Target SELinux file contexts are missing')
    for name in ('dev', 'proc', 'sys', 'run'):
        path = TARGET / name
        require(subprocess.run(['mountpoint', '-q', str(path)]).returncode != 0,
                'Refusing to relabel a mounted host tree: ' + name)
    exclusions = []
    for name in ('dev', 'proc', 'sys', 'run', 'mnt'):
        exclusions.extend(['-e', str(TARGET / name)])
    run(['setfiles', '-F', '-r', TARGET, *exclusions, contexts, TARGET, TARGET / 'home'])
    systemd = TARGET / 'usr/lib/systemd/systemd'
    context = os.getxattr(systemd, 'security.selinux').decode().rstrip('\0')
    require(context.split(':')[2:3] == ['init_exec_t'], 'Systemd executable has an unexpected SELinux label')


def packages(args, *, password=None):
    current(); mounted(); checked_backup(Path(read_json(STATE / 'plan.json')['device']))
    require(not (STATE / 'packages-started').exists(), 'Package stage already attempted')
    require(re.fullmatch(r'[a-z][a-z0-9_-]{0,30}', args.user) is not None, 'Invalid username')
    require(args.user not in ('root', 'liveuser'), 'Invalid username')
    account = dict(user=args.user, timezone=args.timezone, keymap=args.keymap, hostname=args.hostname,
                   desktop=getattr(args, 'desktop', 'none'),
                   locale=validate_locale(getattr(args, 'locale', DEFAULT_LOCALE)))
    require(account['desktop'] in DESKTOPS, 'Unknown desktop environment')
    if not args.apply:
        print('Dry run: would install Fedora Custom and hardware/boot packages into /mnt.')
        print('Account settings:', account)
        return
    if password is None:
        require(sys.stdin.isatty(), 'Run account setup from a local terminal')
        password = getpass.getpass('New login/sudo password: ')
        require(password == getpass.getpass('Repeat login password: '), 'Passwords differ')
    validate_account(args.user, password)
    save_json(STATE / 'account.json', account)
    (STATE / 'packages-started').touch(mode=0o600)
    masks = temporary_dracut_masks()
    with target_mounts():
        exclusions = ['--exclude=kmscon*'] if account['desktop'] == 'none' else []
        run(['dnf5', '--installroot=' + str(TARGET), '--releasever=45', '--use-host-config',
             '--assumeyes', '--repo=fedora,updates', *exclusions, 'install',
             *PACKAGES, *locale_packages(account['locale'])])
        install_desktop(account['desktop'])
        verify_target_locale(TARGET, account['locale'])
        # RPM installation alone can leave PAM files waiting for first-boot
        # authselect setup. Generate and check the local login stack now.
        run(['chroot', TARGET, 'authselect', 'select', 'local', '--force'])
        run(['chroot', TARGET, 'authselect', 'check'])
        create_account(TARGET, args.user, password)
        remove_dracut_masks(masks)
    save_json(STATE / 'packages.json', {'status': 'installed'})
    del password


def setup(args, *, wifi_profiles=()):
    current(); mounted(); checked_backup(Path(read_json(STATE / 'plan.json')['device']))
    require(read_json(STATE / 'packages.json')['status'] == 'installed', 'Install Fedora packages first')
    require(not (STATE / 'configure-started').exists(), 'Configuration already attempted')
    account = read_json(STATE / 'account.json')
    zone = TARGET / 'usr/share/zoneinfo' / account['timezone']
    require(zone.is_file(), 'Timezone is missing')
    if not args.apply:
        print('Dry run: would configure the unencrypted Btrfs target and build a compact Fedora initramfs.')
        return
    (STATE / 'configure-started').touch(mode=0o600)
    services = configure(TARGET, read_json(STATE / 'format.json')['btrfs_uuid'], account['timezone'],
                         account['keymap'], account['hostname'], account.get('locale', DEFAULT_LOCALE))
    install_wifi_profiles(TARGET, wifi_profiles)
    dnfconf = TARGET / 'etc/dnf/dnf.conf'
    conf = dnfconf.read_text()
    conf = preserve_kernels(conf)
    dnfconf.write_text(conf)
    install_kit()
    from tools.auto_fedora_update import install_hooks
    install_hooks(TARGET)
    run(['systemctl', '--root=' + str(TARGET), 'enable', *services])
    configure_desktop(account.get('desktop', 'none'))
    run(['systemd-machine-id-setup', '--root=' + str(TARGET)])
    with target_mounts():
        run(['chroot', TARGET, '/usr/bin/python3', '/opt/lapis-kit/tools/build_fedora_initramfs.py',
             '--kernel-version', KERNEL_VERSION, '--output', '/boot/lapis-initramfs-' + KERNEL_VERSION + '.img'])
    label_target()
    kernel = TARGET / 'usr/lib/modules' / KERNEL_VERSION / 'vmlinuz'
    initrd = TARGET / 'boot' / ('lapis-initramfs-' + KERNEL_VERSION + '.img')
    save_json(STATE / 'configured.json', dict(status='configured', kernel_version=KERNEL_VERSION,
                                              kernel_sha256=sha256(kernel), initrd_sha256=sha256(initrd)))


def main():
    p = argparse.ArgumentParser(description=__doc__)
    subs = p.add_subparsers(required=True)
    q = subs.add_parser('packages'); q.add_argument('--user', required=True); q.add_argument('--timezone', required=True)
    q.add_argument('--keymap', default='us'); q.add_argument('--hostname', default='googlebook')
    q.add_argument('--locale', default=DEFAULT_LOCALE, type=validate_locale)
    q.add_argument('--desktop', choices=DESKTOPS, default='none'); q.add_argument('--apply', action='store_true'); q.set_defaults(fn=packages)
    q = subs.add_parser('configure'); q.add_argument('--apply', action='store_true'); q.set_defaults(fn=setup)
    args = p.parse_args(); args.fn(args)


if __name__ == '__main__':
    main()
