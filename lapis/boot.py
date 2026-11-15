"""Build signed Android v4 boot sets, for files only."""
from pathlib import Path
import os
import sys

from .common import require, require_regular, run, save_json, sha256
from .cpio import archive
from .slots import boot_names


def make_key(path, runner=run):
    path = Path(path)
    if not path.exists():
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        os.close(fd)
        runner(['openssl', 'genrsa', '-out', path, '2048'])
    require_regular(path)
    require(path.stat().st_mode & 0o077 == 0, 'AVB private key must be mode 600')


def build_set(kernel, initrd, cmdline, destination, vendor, *, sizes=None, installed=False, kernel_sha256=None, slot='a', runner=run):
    """Installed initrd goes in vendor_boot (32 MiB); live initrd in init_boot."""
    boot_names(slot)  # Validate before creating output. New live USBs default to A.
    dest, vendor = Path(destination), Path(vendor)
    require(not dest.exists(), 'Boot output directory already exists')
    dest.mkdir(parents=True, mode=0o700)
    for path in (kernel, initrd):
        require_regular(path)
    require(kernel_sha256 is not None, 'A verified kernel hash is required for this boot set')
    require(sha256(kernel) == kernel_sha256, 'Kernel differs from the pinned version for this boot set')
    require(len(cmdline.encode()) < 1536 and '\n' not in cmdline, 'Invalid kernel command line')
    mk, avb = vendor / 'mkbootimg/mkbootimg.py', vendor / 'avb/avbtool.py'
    require(mk.is_file() and avb.is_file(), 'Run tools/fetch_sources.py first')
    key = dest / 'avb-private.pem'
    make_key(key, runner=runner)
    runner(['openssl', 'rsa', '-in', key, '-pubout', '-out', dest / 'avb-public.pem'])
    empty = dest / 'empty.cpio'
    empty.write_bytes(archive({}))
    ramdisk = Path(initrd).read_bytes()
    padded = dest / 'aligned-initramfs.img'
    padded.write_bytes(ramdisk + bytes((-len(ramdisk)) % 4))
    runner([sys.executable, mk, '--header_version', '4', '--kernel', kernel, '--cmdline', cmdline, '-o', dest / f'boot_{slot}.img'])
    runner([sys.executable, mk, '--header_version', '4', '--ramdisk', empty if installed else padded, '-o', dest / f'init_boot_{slot}.img'])
    runner([sys.executable, mk, '--header_version', '4', '--pagesize', '4096', '--vendor_cmdline', cmdline,
         '--ramdisk_type', 'platform', '--ramdisk_name', 'linux' if installed else 'empty',
         '--vendor_ramdisk_fragment', padded if installed else empty, '--vendor_boot', dest / f'vendor_boot_{slot}.img'])
    image_sizes = {}
    for name in ('boot', 'init_boot', 'vendor_boot'):
        image = dest / f'{name}_{slot}.img'
        size = sizes[f'{name}_{slot}'] if sizes else ((image.stat().st_size + 1048575) // 1048576 + 1) * 1048576
        runner([sys.executable, avb, 'add_hash_footer', '--image', image, '--partition_name', name,
             '--partition_size', str(size), '--algorithm', 'SHA256_RSA2048', '--key', key])
        (dest / f'{name}.img').symlink_to(image.name)
        image_sizes[f'{name}_{slot}'] = size
    argv = [sys.executable, avb, 'make_vbmeta_image', '--output', dest / f'vbmeta_{slot}.img',
            '--algorithm', 'SHA256_RSA2048', '--key', key, '--padding_size', '4096']
    for name in ('boot', 'init_boot', 'vendor_boot'):
        argv += ['--include_descriptors_from_image', dest / f'{name}_{slot}.img']
    runner(argv)
    size = sizes[f'vbmeta_{slot}'] if sizes else 1048576
    require((dest / f'vbmeta_{slot}.img').stat().st_size <= size, 'vbmeta partition too small')
    with (dest / f'vbmeta_{slot}.img').open('r+b') as f:
        f.truncate(size)
    image_sizes[f'vbmeta_{slot}'] = size
    runner([sys.executable, avb, 'verify_image', '--image', dest / f'vbmeta_{slot}.img', '--key', dest / 'avb-public.pem'])
    result = dict(slot=slot, kernel_sha256=sha256(kernel), initramfs_sha256=sha256(initrd), cmdline=cmdline,
                  vendor_alignment_padding=(-len(ramdisk)) % 4 if installed else 0,
                  images=[dict(name=n, size=s, sha256=sha256(dest / f'{n}.img')) for n, s in image_sizes.items()])
    save_json(dest / 'manifest.json', result)
    return result
