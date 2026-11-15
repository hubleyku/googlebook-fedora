#!/usr/bin/env python3
"""Build a Fedora 45 installer USB image for ASUS Lapis Depthcharge."""
import argparse
import json
import os
from pathlib import Path
import re
import shutil
import struct
import subprocess
import sys
import tempfile
import uuid
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from lapis.common import ROOT, new_regular_output, read_json, require, require_regular, run, save_json, sha256
from lapis.boot import build_set
from lapis.build_progress import BuildProgress
from lapis.cpio import archive
from lapis.gpt import ENTRY, tables
from lapis.installer_usb import DEFAULT_BACKUP_BYTES, backup_partition_spec, minimum_backup_bytes, usb_config
from lapis.live import workstation_label
from tools.fetch_sources import fetch_sources
from tools.build_loader import build_loader

SECTOR = 512


def iso_label(iso):
    with iso.open('rb') as f:
        f.seek(16 * 2048)
        desc = f.read(2048)
    require(desc[:7] == b'\x01CD001\x01', 'Missing ISO9660 primary descriptor')
    label = desc[40:72].decode('ascii').rstrip()
    require(label and all(c.isalnum() or c in '._- ' for c in label), 'Unsupported ISO volume label')
    return label


def live_boot_members(iso):
    listing = subprocess.run(['bsdtar', '-tf', str(iso)], capture_output=True, text=True, check=True)
    members = {name.removeprefix('./').lstrip('/'): name for name in listing.stdout.splitlines()}
    for kernel, initrd in (('boot/x86_64/loader/linux', 'boot/x86_64/loader/initrd'),
                           ('images/pxeboot/vmlinuz', 'images/pxeboot/initrd.img')):
        if kernel in members and initrd in members:
            return (('vmlinuz', members[kernel]), ('initrd.img', members[initrd]))
    raise RuntimeError('Fedora Workstation ISO has no supported live kernel/initramfs layout')


def validate_live_kernel(kernel):
    # Linux x86 boot protocol: HdrS, version 2.12+, XLF_KERNEL_64.
    # https://docs.kernel.org/arch/x86/boot.html
    with kernel.open('rb') as source:
        header = source.read(0x238)
    require(len(header) == 0x238 and header[0x202:0x206] == b'HdrS'
            and struct.unpack_from('<H', header, 0x206)[0] >= 0x20c
            and struct.unpack_from('<H', header, 0x236)[0] & 1,
            'Fedora Workstation live ISO must contain an x86-64 Linux kernel')


def vendor_payload(vendor, root):
    """Package only hashed runtime files, including from Git-free source archives."""
    from tools.fetch_sources import SOURCES
    manifest = read_json(root / 'assets/vendor-runtime.json')
    require(manifest['schema'] == 1, 'Unsupported vendor runtime manifest')
    for name in ('mkbootimg', 'avb'):
        record = manifest['sources'][name]
        require((record['url'], record['revision']) == SOURCES[name], 'Vendor source pin differs: ' + name)
        src = vendor / name
        if (src / '.git').exists():
            revision = subprocess.check_output(['git', '-C', str(src), 'rev-parse', 'HEAD'], text=True).strip()
            require(revision == record['revision'], 'Unexpected source revision: ' + name)
    files = {}
    for relative, digest in manifest['files'].items():
        parts = Path(relative).parts
        require(parts and parts[0] in ('mkbootimg', 'avb') and '..' not in parts and not relative.startswith('/'), 'Invalid vendor runtime path')
        path = vendor / relative
        require(path.is_file() and not path.is_symlink() and sha256(path) == digest, 'Vendor runtime hash differs: ' + relative)
        files[relative] = path.read_bytes()
    return files


def require_loader(root=ROOT):
    loader = Path(root) / 'build/load_android_test'
    require(loader.is_file() and not loader.is_symlink() and os.access(loader, os.X_OK),
            'Missing or non-executable loader harness: ' + str(loader) + '\n'
            'Run tools/build_fedora_usb.py with --iso and --output; it fetches sources and builds the harness automatically.\n'
            'See docs/build-usb.md for build tools and usage.')
    return loader


def kit_payload(vendor, root=None):
    """Explicitly bundle development code and pinned public tools, never local secrets."""
    root = Path(root) if root is not None else Path(__file__).resolve().parents[1]
    loader = require_loader(root)
    files = {}
    def add(name, data, executable=False):
        parts = name.split('/')
        for i in range(1, len(parts)):
            files.setdefault('/'.join(parts[:i]), (0o040755, b''))
        files[name] = (0o100755 if executable else 0o100644, data)
    for folder in ('lapis', 'tools', 'assets', 'docs'):
        for path in sorted((root / folder).rglob('*')):
            if path.is_file() and not path.is_symlink() and '__pycache__' not in path.parts:
                require(path.suffix not in ('.pem', '.key', '.img', '.iso', '.bin', '.pyc'), 'Unexpected private/build file in kit')
                add('lapis-kit/' + str(path.relative_to(root)), path.read_bytes(), path.suffix in ('.py', '.sh'))
    for name in ('README.md', 'LICENSE', 'NOTICE.md', 'CONTRIBUTING.md'):
        add('lapis-kit/' + name, (root / name).read_bytes())
    for relative, data in vendor_payload(vendor, root).items():
        add('lapis-kit/vendor/' + relative, data, relative.endswith('.py'))
    add('lapis-kit/build/load_android_test', loader.read_bytes(), True)
    return files


def protective_mbr(last_lba):
    mbr = bytearray(SECTOR)
    mbr[450] = 0xee
    struct.pack_into('<II', mbr, 454, 1, min(last_lba, 0xffffffff))
    mbr[510:512] = b'\x55\xaa'
    return bytes(mbr)


def copy_sparse(source, destination, progress=None):
    """Preserve zero-filled holes while copying image components."""
    for block in iter(lambda: source.read(4 * 1024**2), b''):
        if block == bytes(len(block)):
            destination.seek(len(block), os.SEEK_CUR)
        else:
            destination.write(block)
        if progress:
            progress(len(block))


def create_usb_image(image, iso, boot_manifest, boot, work, config, backup_bytes, progress=None):
    require(backup_bytes > 0 and backup_bytes % 1048576 == 0, 'Backup size must be a positive whole MiB')
    iso_size = iso.stat().st_size
    require(iso_size % SECTOR == 0, 'ISO is not sector aligned')
    entries = bytearray(128 * 128)
    cursor = ((34 * SECTOR + iso_size + 1048575) // 1048576) * 1048576
    layout = []
    items = [(x['name'], boot / (x['name'] + '.img'), x['size']) for x in boot_manifest['images']]
    misc = work / 'misc.img'
    misc.write_bytes(bytes(1048576))
    items.append(('misc', misc, 1048576))
    total = iso_size + sum(size for _, _, size in items)
    copied = 0

    def advance(size):
        nonlocal copied
        copied += size
        if progress:
            progress(copied, total)

    with image.open('xb') as destination:
        destination.write(protective_mbr(0))
        destination.write(bytes(SECTOR))
        destination.write(entries)
        destination.seek(34 * SECTOR)
        with iso.open('rb') as source:
            copy_sparse(source, destination, advance)
        for index, (name, source_path, size) in enumerate(items, 2):
            cursor = ((cursor + 1048575) // 1048576) * 1048576
            is_vbmeta = name == 'vbmeta_' + boot_manifest['slot']
            typ = '88434509-d9d1-487d-b82c-15ef964cbd4b' if is_vbmeta else '0fc63daf-8483-4772-8e79-3d69d8477de4'
            attrs = (15 << 48) | (1 << 56) if is_vbmeta else 0
            partuuid = str(uuid.uuid4())
            entries[index * 128:(index + 1) * 128] = ENTRY.pack(
                uuid.UUID(typ).bytes_le, uuid.UUID(partuuid).bytes_le, cursor // SECTOR,
                (cursor + size) // SECTOR - 1, attrs, name.encode('utf-16-le'))
            destination.seek(cursor)
            with source_path.open('rb') as source:
                copy_sparse(source, destination, advance)
            layout.append(dict(name=name, offset=cursor, size=size, partuuid=partuuid))
            cursor += size
        # Keep the established ISO partition number (2) for existing Lapis USBs.
        entries[128:256] = ENTRY.pack(uuid.UUID('ebd0a0a2-b9e5-4433-87c0-68b6b72699c7').bytes_le,
                                      uuid.UUID(config['iso_partuuid']).bytes_le, 34,
                                      34 + iso_size // SECTOR - 1, 0, 'ISO9660'.encode('utf-16-le'))
        header = [b'EFI PART', 0x10000, 92, 0, 0, 1, cursor // SECTOR + 32,
                  34, cursor // SECTOR - 1, uuid.UUID(config['disk_guid']).bytes_le, 2, 128, 128, 0]
        primary, secondary = tables(header, entries, protective_mbr(header[6]))
        destination.truncate((header[6] + 1) * SECTOR)
        destination.seek(0); destination.write(primary)
        destination.seek(cursor); destination.write(secondary)
        destination.flush(); os.fsync(destination.fileno())
    from lapis.gpt import read_gpt
    with image.open('rb') as source:
        gpt = read_gpt(source, image.stat().st_size)
    # Reserve the backup in the manifest, not in the image. The writer adds it
    # to the physical USB's GPT and formats it without copying empty gigabytes.
    layout.append(backup_partition_spec(image.stat().st_size, config, backup_bytes,
                                       max(p['index'] for p in gpt.partitions()) + 1))
    return layout


def validate_live_image(image, work, loader):
    require_regular(loader)
    result = subprocess.run([str(loader.resolve()), '-b', '1', str(image.resolve())], cwd=work,
                            capture_output=True, text=True)
    (work / 'usb-loader.log').write_text(result.stdout + result.stderr)
    require(result.returncode == 0 and 'vb2api_load_kernel() returned 0' in result.stdout,
            'Loader rejected the completed USB image; inspect usb-loader.log')


def check_tools():
    tools = ('git', 'make', os.environ.get('CC', 'cc'), 'openssl', 'bsdtar')
    missing = [name for name in tools if not shutil.which(name)]
    require(not missing, 'Missing build tools: ' + ', '.join(missing) +
            '. On Fedora, run: sudo dnf install gcc make git openssl bsdtar')


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--iso', required=True, type=Path)
    p.add_argument('--iso-sha256', help='optional expected SHA-256 of the supplied Fedora Workstation ISO')
    p.add_argument('--output', required=True, type=Path)
    p.add_argument('--vendor', type=Path,
                   help='optional pre-fetched mkbootimg/AVB runtime directory (default: --sources)')
    p.add_argument('--sources', type=Path, default=ROOT / 'build/sources',
                   help='cache for pinned source checkouts (default: build/sources)')
    p.add_argument('--include-kit', dest='include_kit', action='store_true', default=True,
                   help='bundle the installer (default; retained for older build commands)')
    p.add_argument('--without-installer', dest='include_kit', action='store_false', help='build a live-only USB')
    p.add_argument('--backup-size-mib', type=int, default=DEFAULT_BACKUP_BYTES // 1048576,
                   help='writable USB backup partition size in MiB (default: 2048)')
    args = p.parse_args()
    progress = BuildProgress()
    with progress.stage(1, 8, 'Checking tools, inputs, and Fedora Workstation ISO'):
        backup_bytes = args.backup_size_mib * 1048576
        require(backup_bytes >= minimum_backup_bytes(), 'Backup partition is too small for the supported disk backup and working space')
        check_tools()
        iso = args.iso.resolve()
        require_regular(iso)
        image = args.output.absolute()
        require(not str(image.resolve()).startswith('/dev/'), 'Image tools never write device nodes')
        for candidate in (image, image.with_suffix('.manifest.json'), image.with_suffix('.sha256')):
            require(not candidate.exists() and not candidate.is_symlink(), 'Output already exists: ' + str(candidate))
        label = workstation_label(iso_label(iso))
        if args.iso_sha256 is not None:
            require(re.fullmatch(r'[A-Fa-f0-9]{64}', args.iso_sha256), 'Expected a 64-digit ISO SHA-256')
        iso_digest = sha256(iso, progress=progress.bytes('ISO checked'))
        require(args.iso_sha256 is None or iso_digest == args.iso_sha256.lower(), 'ISO checksum differs from --iso-sha256')
        boot_members = live_boot_members(iso)
    image = new_regular_output(image).resolve()
    # Each attempt gets its own workspace. Failed work is retained without
    # blocking a retry or risking deletion of a user's previous build files.
    work = Path(tempfile.mkdtemp(prefix=image.stem + '-', suffix='.build', dir=image.parent))
    log_path = work / 'build.log'
    print('Build directory:', work, flush=True)
    print('Detailed command output:', log_path, flush=True)
    try:
        with log_path.open('w', buffering=1) as log:
            progress.log = log
            with progress.stage(2, 8, 'Fetching and verifying pinned sources'):
                fetch_sources(args.sources.resolve(), runner=progress.run)
                vendor = (args.vendor or args.sources).resolve()
                vendor_payload(vendor, ROOT)
            with progress.stage(3, 8, 'Compiling the boot-validation harness'):
                build_loader(args.sources.resolve(), ROOT / 'build/load_android_test', runner=progress.run)
                loader = require_loader()
            with progress.stage(4, 8, 'Extracting the live kernel and initramfs'):
                for name, member in boot_members:
                    with (work / name).open('wb') as f:
                        progress.run(['bsdtar', '-xOf', iso, member], stdout=f)
                    require((work / name).stat().st_size > 0, 'ISO boot component is empty: ' + name)
                validate_live_kernel(work / 'vmlinuz')
            # Spaces in filesystem labels are literal backslash-x20 in cmdlines.
            escaped_label = label.replace(' ', r'\x20')
            cmdline = (f'root=live:CDLABEL={escaped_label} rd.live.image console=tty0 '
                       'modprobe.blacklist=xe,snd_sof_pci_intel_ptl')
            config = usb_config()
            with progress.stage(5, 8, 'Bundling the live configuration and installer'):
                asset = ROOT / 'assets/live'
                files = {
                    'lapis-live': (0o040755, b''),
                    'lapis-live/gpu-prepare.py': (0o100755, (asset / 'gpu-prepare.py').read_bytes()),
                    'lapis-live/lapis-gpu-prepare.service': (0o100644, (asset / 'lapis-gpu-prepare.service').read_bytes()),
                    'lapis-live/lapis-live-labels.service': (0o100644, (asset / 'lapis-live-labels.service').read_bytes()),
                    'lapis-live/lapis-usb-backup.service': (0o100644, (asset / 'lapis-usb-backup.service').read_bytes()),
                    'lapis-live/install-fedora': (0o100755, (asset / 'install-fedora').read_bytes()),
                    'lapis-live/usb.json': (0o100644, (json.dumps(config) + '\n').encode()),
                    'var/lib/dracut/hooks/pre-pivot/99-lapis.sh': (0o100755, (asset / 'lapis-pre-pivot.sh').read_bytes()),
                }
                if args.include_kit:
                    files.update(kit_payload(vendor))
                initrd = work / 'initrd.img'
                with initrd.open('ab') as f:
                    f.write(bytes((-f.tell()) % 4))
                    f.write(archive(files))
            with progress.stage(6, 8, 'Building and signing boot images'):
                boot = work / 'boot'
                manifest = build_set(work / 'vmlinuz', initrd, cmdline, boot,
                                     vendor, kernel_sha256=sha256(work / 'vmlinuz'),
                                     runner=progress.run)
            with progress.stage(7, 8, 'Assembling the compact USB image'):
                layout = create_usb_image(image, iso, manifest, boot, work, config, backup_bytes,
                                          progress=progress.bytes('Image assembled'))
            with progress.stage(8, 8, 'Validating bootability and saving checksums'):
                validate_live_image(image, work, loader)
                backup = next(p for p in layout if p['name'] == 'LAPIS-BACKUP')
                report = dict(image_format='lapis-usb-v2', image=image.name,
                              sha256=sha256(image, progress=progress.bytes('Image checked')), size=image.stat().st_size,
                              iso_sha256=iso_digest, iso_volume_label=label, cmdline=cmdline,
                              android_partitions=[p for p in layout if p['name'] != 'LAPIS-BACKUP'],
                              backup_partition=backup, required_usb_bytes=backup['offset'] + backup['size'] + 33 * SECTOR,
                              usb=config, loader_accepted=True, internal_disk_writes=False, bundled_kit=args.include_kit,
                              status='Lapis Fedora installer USB' if args.include_kit else 'Lapis Fedora live-only USB')
                save_json(image.with_suffix('.manifest.json'), report, 0o644)
                image.with_suffix('.sha256').write_text(report['sha256'] + '  ' + image.name + '\n')
    except BaseException:
        print('Build files retained:', work, file=sys.stderr, flush=True)
        print('Detailed command output:', log_path, file=sys.stderr, flush=True)
        raise
    print('Created', image, '\nSHA256', report['sha256'], flush=True)
    print(f'Minimum USB capacity: {report["required_usb_bytes"] / 1024**3:.2f} GiB '
          '(including the separately created backup partition)', flush=True)


if __name__ == '__main__':
    try:
        main()
    except (KeyboardInterrupt, EOFError):
        print('\nUSB build cancelled.', file=sys.stderr)
        raise SystemExit(130)
    except (OSError, RuntimeError, ValueError, subprocess.SubprocessError) as error:
        print('USB build stopped: ' + str(error), file=sys.stderr)
        raise SystemExit(1)
