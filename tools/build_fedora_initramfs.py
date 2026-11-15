#!/usr/bin/env python3
"""Build an experimental Fedora/Lapis initramfs; never writes boot partitions.

Run inside a Fedora 45 installation or Fedora target chroot on the Lapis host.
The result must be tested and signed before any use. This is not an installer.
"""
import argparse
import os
from pathlib import Path
import re
import subprocess
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from lapis.common import require
from lapis.cpio import members

MODULES = 'systemd systemd-udevd systemd-initrd systemd-modules-load dracut-systemd btrfs rootfs-block kernel-modules'
DRIVERS = 'nvme btrfs'
MAX_SIZE = 29 * 1024 * 1024
IGNORED = "No '/dev/log' or 'logger' included for syslog logging"


def arguments(kernel, output):
    return ['dracut', '--hostonly', '--hostonly-mode', 'strict',
            '--no-hostonly-default-device', '--no-hostonly-cmdline',
            '--modules', MODULES, '--force-drivers', DRIVERS,
            '--compress', 'zstd', '--kver', kernel, str(output)]


def validate(output, kernel, builtin):
    require(output.is_file() and not output.is_symlink(), 'dracut did not create a regular image')
    require(output.stat().st_size < MAX_SIZE, 'Initramfs is too large for the vendor_boot partition')
    with output.open('rb') as f:
        image = f.read()
    require(image[:6] in (b'070701', b'070702'), 'Intel early microcode archive missing or malformed')
    early, _ = members(image)
    ucode = early.get('kernel/x86/microcode/GenuineIntel.bin')
    require(ucode and len(ucode[1]) > 0, 'Intel early microcode missing')
    listing = subprocess.run(['lsinitrd', str(output)], capture_output=True, text=True, check=True).stdout
    for part in ('usr/lib/systemd/system/dracut-cmdline.service',
                 'usr/lib/systemd/system/dracut-mount.service',
                 'usr/lib/systemd/system/initrd-switch-root.service'):
        require(part in listing, 'Missing initramfs component: ' + part)
    for module, builtin_path in [('nvme', 'kernel/drivers/nvme/host/nvme.ko'),
                                 ('btrfs', 'kernel/fs/btrfs/btrfs.ko')]:
        present = bool(re.search(r'/' + module + r'\.ko(?:\.[\w]+)?(?:\s|$)', listing))
        require(present or builtin_path in builtin.splitlines(), 'Missing driver: ' + module)
    require(kernel in listing, 'Kernel version absent from initramfs inventory')
    return output.stat().st_size


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--kernel-version', required=True)
    p.add_argument('--output', required=True, type=Path)
    args = p.parse_args()
    require(os.geteuid() == 0, 'Run as root in a Fedora target environment')
    os_release = Path('/etc/os-release').read_text()
    require(re.search(r'^ID=fedora$', os_release, re.M) and re.search(r'^VERSION_ID=45$', os_release, re.M),
            'This prototype requires Fedora 45')
    require(re.fullmatch(r'[A-Za-z0-9._+~-]+', args.kernel_version), 'Invalid kernel version')
    modules = Path('/usr/lib/modules') / args.kernel_version
    require(modules.is_dir() and (modules / 'modules.builtin').is_file(), 'Kernel modules are not installed')
    require(not args.output.exists() and not args.output.is_symlink(), 'Output already exists')
    require(args.output.parent.is_dir() and not str(args.output.resolve()).startswith('/dev/'), 'Invalid output path')
    result = subprocess.run(arguments(args.kernel_version, args.output), capture_output=True, text=True)
    if result.stdout:
        print(result.stdout, end='')
    if result.stderr:
        print(result.stderr, end='', file=sys.stderr)
    errors = [line for line in result.stderr.splitlines() if 'dracut[E]' in line and IGNORED not in line]
    require(result.returncode == 0 and not errors, 'dracut reported an error; do not use this image')
    size = validate(args.output, args.kernel_version, (modules / 'modules.builtin').read_text())
    print(f'Experimental initramfs: {size} bytes; contents inspected.')
    print('The image is not installed or signed; boot and hardware validation are still required.')


if __name__ == '__main__':
    main()
