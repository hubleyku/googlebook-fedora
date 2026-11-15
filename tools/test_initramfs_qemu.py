#!/usr/bin/env python3
"""Test a Fedora initramfs using a disposable virtual NVMe/Btrfs disk.

Requires qemu-system-x86_64, mkfs.btrfs, and a statically linked test init built
from tests/fixtures/qemu_subvolume_init.c. No host disk device is opened.
"""
import argparse
from pathlib import Path
import subprocess
import tempfile
import uuid


def boot(qemu, kernel, initrd, disk, fsuuid, subvol):
    cmdline = f'root=UUID={fsuuid} rootfstype=btrfs rw console=ttyS0,115200 rd.retry=15 panic=10'
    if subvol:
        cmdline += ' rootflags=subvol=@'
    command = [qemu, '-machine', 'q35,accel=tcg', '-cpu', 'max', '-m', '1536',
               '-kernel', str(kernel), '-initrd', str(initrd), '-append', cmdline,
               '-drive', f'file={disk},format=raw,if=none,id=rootdisk',
               '-device', 'nvme,drive=rootdisk,serial=lapisfixture',
               '-display', 'none', '-serial', 'stdio', '-monitor', 'none', '-no-reboot', '-nic', 'none']
    return subprocess.run(command, text=True, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, timeout=120)


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--kernel', required=True, type=Path)
    p.add_argument('--initrd', required=True, type=Path)
    p.add_argument('--init-binary', required=True, type=Path)
    p.add_argument('--qemu', default='qemu-system-x86_64')
    args = p.parse_args()
    for path in (args.kernel, args.initrd, args.init_binary):
        if not path.is_file():
            p.error(f'Not a regular file: {path}')
    with tempfile.TemporaryDirectory(prefix='lapis-qemu-') as tmp:
        root = Path(tmp) / 'root'; (root / 'sbin').mkdir(parents=True)
        (root / 'etc').mkdir(); (root / 'proc').mkdir(); (root / 'usr').mkdir()
        (root / 'etc/os-release').write_text('NAME=Fedora\nID=fedora\n')
        import shutil
        shutil.copyfile(args.init_binary, root / 'sbin/init'); (root / 'sbin/init').chmod(0o755)
        disk = Path(tmp) / 'nvme.img'
        with disk.open('wb') as f: f.truncate(512 * 1024**2)
        fsuuid = str(uuid.uuid4())
        subprocess.run(['mkfs.btrfs', '-f', '-q', '-U', fsuuid, '-r', str(root), str(disk)], check=True)
        first = boot(args.qemu, args.kernel.resolve(), args.initrd.resolve(), disk, fsuuid, False)
        if first.returncode or 'LAPIS_SUBVOLUME_CREATED' not in first.stdout:
            raise RuntimeError('First virtual boot failed:\n' + first.stdout[-8000:])
        second = boot(args.qemu, args.kernel.resolve(), args.initrd.resolve(), disk, fsuuid, True)
        if second.returncode or 'LAPIS_BTRFS_SUBVOLUME_BOOTED' not in second.stdout:
            raise RuntimeError('Subvolume virtual boot failed:\n' + second.stdout[-8000:])
        print('Fedora kernel reached init on Btrfs @ over virtual NVMe in QEMU.')
        print('The test does not exercise Depthcharge or physical hardware.')


if __name__ == '__main__':
    main()
