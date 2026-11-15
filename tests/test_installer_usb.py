from pathlib import Path
import copy
import hashlib
import io
import re
import shutil
import stat
import subprocess
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from lapis.common import ROOT, save_json, sha256
from lapis.gpt import read_gpt
from lapis import installer_usb
from lapis.slots import boot_names
from tools import build_fedora_usb, build_loader
from tools.build_fedora_usb import create_usb_image, kit_payload, require_loader, vendor_payload
from tools.native_fedora import external
from tools.write_usb import (copy_payload, find_formatter, format_backup_filesystem, hash_source,
                             backup_partition_node, make_write_plan, resized_tables, verify_backup_filesystem, verify_payload)


class USBIdentityTests(unittest.TestCase):
    def test_default_size_has_room_for_boot_support_snapshot(self):
        self.assertGreater(installer_usb.DEFAULT_BACKUP_BYTES, installer_usb.minimum_backup_bytes())
        self.assertGreater(installer_usb.minimum_backup_bytes(), installer_usb.required_backup_free_bytes())
        self.assertEqual(installer_usb.required_backup_free_bytes(), (112 + 512) * 1024**2)

    def test_partition_must_be_on_the_usb_that_booted_the_live_environment(self):
        config = installer_usb.usb_config()
        def resolve(path, strict=False):
            if path.name == config['iso_partuuid']:
                return Path('/dev/sda2')
            if path.name == config['backup_partuuid']:
                return Path('/dev/sda8')
            return path
        def command(args):
            args = list(map(str, args))
            if 'PKNAME' in args:
                return 'sda'
            if 'TRAN' in args:
                return 'usb'
            if 'PTUUID' in args:
                return config['disk_guid']
            if args[0] == 'findmnt':
                return '/dev/sda2'
            if 'TYPE' in args:
                return 'ext4'
            if 'UUID' in args:
                return config['backup_uuid']
            self.fail(args)
        with patch.object(Path, 'resolve', resolve), \
                patch.object(Path, 'stat', return_value=SimpleNamespace(st_mode=stat.S_IFBLK)), \
                patch.object(installer_usb, 'output', side_effect=command):
            self.assertEqual(installer_usb.installer_partition(config), Path('/dev/sda8'))
            with patch.object(installer_usb, 'output', side_effect=lambda args: '/dev/sdb2' if args[0] == 'findmnt' else command(args)):
                with self.assertRaisesRegex(RuntimeError, 'did not supply'):
                    installer_usb.installer_partition(config)
            with patch.object(installer_usb, 'output', side_effect=['sda', 'sdb']):
                with self.assertRaisesRegex(RuntimeError, 'not on the installer USB'):
                    installer_usb.installer_partition(config)

    def test_mount_is_writable_and_does_not_replace_an_unrelated_mount(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            config = installer_usb.usb_config()
            config_path = root / 'usb.json'
            save_json(config_path, config)
            mountpoint = root / 'mount'
            def command(args):
                return config['backup_uuid'] if 'UUID' in args else 'rw,nosuid,nodev'
            with patch.object(installer_usb.os, 'geteuid', return_value=0), \
                    patch.object(installer_usb, 'installer_partition', return_value=Path('/dev/sda8')), \
                    patch.object(installer_usb.subprocess, 'run', return_value=subprocess.CompletedProcess([], 1, '', '')), \
                    patch.object(installer_usb, 'output', side_effect=command), \
                    patch.object(installer_usb, 'run') as run:
                self.assertEqual(installer_usb.ensure_mounted(config_path, mountpoint), mountpoint)
                self.assertEqual(run.call_args.args[0][:5], ['mount', '-t', 'ext4', '-o', 'rw,nosuid,nodev'])
                run.reset_mock()
                with patch.object(installer_usb.subprocess, 'run', return_value=subprocess.CompletedProcess([], 0, '/dev/sdb8\n', '')):
                    with self.assertRaisesRegex(RuntimeError, 'Another filesystem'):
                        installer_usb.ensure_mounted(config_path, mountpoint)
                run.assert_not_called()

    def test_installer_usb_is_an_allowed_backup_destination(self):
        with tempfile.TemporaryDirectory() as tmp:
            def command(args):
                if args[0] == 'lsblk':
                    return '/dev/sda8\n/dev/sda'
                return 'rw,nosuid,nodev' if 'OPTIONS' in args else '/dev/sda8'
            with patch('tools.native_fedora.output', side_effect=command):
                external(Path(tmp), Path('/dev/nvme0n1'))
                with self.assertRaisesRegex(RuntimeError, 'disk being installed'):
                    external(Path(tmp), Path('/dev/sda'))

    def test_linux_formatter_node_is_bound_to_expected_partition_geometry(self):
        config = installer_usb.usb_config()
        backup = installer_usb.backup_partition_spec(8 * 1024**2, config, 1024**3)
        node = Path('/dev/sda8')
        def resolve(path, strict=False):
            return node if path.name == backup['partuuid'] else path
        def read_text(path, *args, **kwargs):
            return str(backup['offset'] // 512) if path.name == 'start' else str(backup['size'] // 512)
        with patch('tools.write_usb.platform.system', return_value='Linux'), \
                patch('tools.write_usb.unmount'), patch('tools.write_usb.subprocess.run'), \
                patch.object(Path, 'stat', return_value=SimpleNamespace(st_mode=stat.S_IFBLK)), \
                patch.object(Path, 'resolve', resolve), patch.object(Path, 'read_text', read_text):
            self.assertEqual(backup_partition_node(Path('/dev/sda'), backup), node)
            with patch.object(Path, 'read_text', return_value='0'):
                with self.assertRaisesRegex(RuntimeError, 'geometry differs'):
                    backup_partition_node(Path('/dev/sda'), backup)

    def test_unexpected_regular_file_cannot_be_formatted_as_a_partition(self):
        with tempfile.TemporaryDirectory() as tmp:
            node = Path(tmp) / 'unexpected-file'
            node.touch()
            with patch('tools.write_usb.subprocess.run') as run:
                with self.assertRaisesRegex(RuntimeError, 'block device'):
                    format_backup_filesystem(node, installer_usb.usb_config(), 1024**3, '/usr/bin/mkfs.ext4')
                run.assert_not_called()


@unittest.skipUnless(shutil.which('mkfs.ext4') and shutil.which('debugfs'), 'e2fsprogs is required for regular-file filesystem tests')
class USBImageTests(unittest.TestCase):
    def test_minimum_partition_size_provides_the_required_free_space(self):
        with tempfile.TemporaryDirectory(prefix='lapis-usb-size-test-') as tmp:
            filesystem = Path(tmp) / 'minimum.ext4'
            with filesystem.open('xb') as stream:
                stream.truncate(installer_usb.minimum_backup_bytes())
            format_backup_filesystem(filesystem, installer_usb.usb_config(), filesystem.stat().st_size,
                                     find_formatter(), allow_regular=True)
            stats = subprocess.check_output(['debugfs', '-R', 'stats', str(filesystem)], text=True)
            blocks = int(re.search(r'^Free blocks:\s+(\d+)', stats, re.M).group(1))
            size = int(re.search(r'^Block size:\s+(\d+)', stats, re.M).group(1))
            self.assertGreater(blocks * size, installer_usb.required_backup_free_bytes())

    def compact_image(self, root):
        boot, work = root / 'boot', root / 'work'
        boot.mkdir(); work.mkdir()
        iso = root / 'live.iso'
        iso.write_bytes(b'fake ISO payload' + bytes(1048576 - 16))
        images = []
        for name in boot_names('a'):
            image = boot / (name + '.img')
            image.write_bytes(name.encode().ljust(1048576, b'\0'))
            images.append(dict(name=name, size=image.stat().st_size))
        config = installer_usb.usb_config()
        image = root / 'installer.img'
        layout = create_usb_image(image, iso, dict(slot='a', images=images), boot, work, config, 1024**3)
        manifest = dict(image_format='lapis-usb-v2', size=image.stat().st_size, sha256=sha256(image),
                        usb=config, backup_partition=next(p for p in layout if p['name'] == 'LAPIS-BACKUP'))
        with image.open('rb') as source:
            gpt = read_gpt(source, image.stat().st_size)
        self.assertFalse((work / 'backup.ext4').exists())
        return image, gpt, manifest

    def test_compact_image_and_separately_created_backup(self):
        with tempfile.TemporaryDirectory(prefix='lapis-usb-test-') as tmp:
            root = Path(tmp)
            image, gpt, manifest = self.compact_image(root)
            config = manifest['usb']
            self.assertNotIn('LAPIS-BACKUP', {p['name'] for p in gpt.partitions()})
            self.assertLess(image.stat().st_size, 16 * 1024**2)
            self.assertEqual(gpt.guid, config['disk_guid'])
            capacity = manifest['backup_partition']['offset'] + 1024**3 + 33 * 512
            plan = make_write_plan(gpt, capacity, manifest)
            self.assertEqual(plan.payload_bytes, image.stat().st_size)
            full, payload = hash_source(image, plan.payload_bytes)
            self.assertEqual(full, manifest['sha256'])
            self.assertEqual(full, payload)
            destination = root / 'usb.img'
            with destination.open('x+b') as disk, image.open('rb') as source:
                disk.truncate(capacity)
                copy_payload(source, disk, plan.payload_bytes)
                verify_payload(disk, plan.payload_bytes, payload)
                disk.seek(0); disk.write(plan.primary)
                disk.seek(capacity - len(plan.secondary)); disk.write(plan.secondary)
                final = read_gpt(disk, capacity)
            backup = next(p for p in final.partitions() if p['name'] == 'LAPIS-BACKUP')
            self.assertEqual(backup['partuuid'], config['backup_partuuid'])
            self.assertEqual(backup['size'], 1024**3)
            filesystem = root / 'partition.ext4'
            with filesystem.open('xb') as stream:
                stream.truncate(backup['size'])
            format_backup_filesystem(filesystem, config, backup['size'], find_formatter(), allow_regular=True)
            marker = root / 'marker.txt'
            marker.write_text('backup filesystem is writable')
            subprocess.run(['debugfs', '-w', '-R', 'write ' + str(marker) + ' /backup-proof.txt', str(filesystem)],
                           check=True, capture_output=True)
            result = subprocess.run(['debugfs', '-R', 'cat /backup-proof.txt', str(filesystem)],
                                    check=True, capture_output=True, text=True)
            self.assertIn('backup filesystem is writable', result.stdout)
            metadata = subprocess.check_output(['blkid', '-p', '-o', 'export', str(filesystem)], text=True)
            self.assertIn('UUID=' + config['backup_uuid'], metadata)
            self.assertIn('LABEL=' + installer_usb.BACKUP_LABEL, metadata)
            with self.assertRaisesRegex(RuntimeError, 'UUID differs'):
                verify_backup_filesystem(filesystem, installer_usb.usb_config(), backup['size'])
            larger = capacity + 32 * 1024**2
            primary, secondary = resized_tables(final, larger)
            with destination.open('r+b') as stream:
                stream.truncate(larger)
                stream.seek(0); stream.write(primary)
                stream.seek(larger - len(secondary)); stream.write(secondary)
                resized = read_gpt(stream, larger)
            self.assertEqual(resized.partitions(), final.partitions())
            self.assertEqual(resized.guid, config['disk_guid'])

    def test_legacy_embedded_backup_is_omitted_from_transfer(self):
        with tempfile.TemporaryDirectory(prefix='lapis-legacy-usb-test-') as tmp:
            root = Path(tmp)
            image, gpt, manifest = self.compact_image(root)
            capacity = manifest['backup_partition']['offset'] + 1024**3 + 33 * 512
            plan = make_write_plan(gpt, capacity, manifest)
            legacy = root / 'legacy.img'
            with legacy.open('x+b') as disk, image.open('rb') as source:
                disk.truncate(capacity)
                copy_payload(source, disk, plan.payload_bytes)
                disk.seek(0); disk.write(plan.primary)
                disk.seek(capacity - len(plan.secondary)); disk.write(plan.secondary)
                old_gpt = read_gpt(disk, capacity)
            legacy_manifest = dict(manifest)
            legacy_manifest.pop('image_format')
            legacy_manifest['size'] = capacity
            optimized = make_write_plan(old_gpt, capacity + 1024**2, legacy_manifest)
            self.assertEqual(optimized.payload_bytes, manifest['backup_partition']['offset'])
            self.assertLess(optimized.payload_bytes, 16 * 1024**2)
            self.assertGreater(capacity - optimized.payload_bytes, 1023 * 1024**2)
            self.assertEqual(optimized.backup['partuuid'], manifest['usb']['backup_partuuid'])

    def test_capacity_and_identity_errors_fail_before_writing(self):
        with tempfile.TemporaryDirectory(prefix='lapis-usb-plan-test-') as tmp:
            image, gpt, manifest = self.compact_image(Path(tmp))
            with self.assertRaisesRegex(RuntimeError, 'too small'):
                make_write_plan(gpt, manifest['backup_partition']['offset'] + 1024**3, manifest)
            changed = copy.deepcopy(manifest)
            changed['backup_partition']['partuuid'] = '00000000-0000-0000-0000-000000000000'
            with self.assertRaisesRegex(RuntimeError, 'metadata differs'):
                make_write_plan(gpt, 2 * 1024**3, changed)


class PayloadTests(unittest.TestCase):
    def test_hash_copy_and_readback_stop_before_empty_backup_space(self):
        with tempfile.TemporaryDirectory() as tmp:
            image = Path(tmp) / 'source.img'
            data = b'boot payload' + b'empty filesystem data'
            image.write_bytes(data)
            size = len(b'boot payload')
            full, payload = hash_source(image, size)
            self.assertEqual(full, hashlib.sha256(data).hexdigest())
            self.assertEqual(payload, hashlib.sha256(data[:size]).hexdigest())
            destination = io.BytesIO(b'X' * len(data))
            copy_payload(io.BytesIO(data), destination, size)
            self.assertEqual(destination.getvalue()[size:], b'X' * (len(data) - size))
            verify_payload(destination, size, payload)
            destination.seek(0); destination.write(b'corrupt')
            with self.assertRaisesRegex(RuntimeError, 'read-back failed'):
                verify_payload(destination, size, payload)


class KitPackagingTests(unittest.TestCase):
    def test_git_free_runtime_payload_and_installer_files_are_bundled(self):
        with tempfile.TemporaryDirectory() as tmp:
            loader = Path(tmp) / 'load_android_test'
            loader.write_bytes(b'test loader payload')
            with patch.object(build_fedora_usb, 'require_loader', return_value=loader):
                files = kit_payload(ROOT / 'build/sources')
        self.assertEqual(files['lapis-kit/build/load_android_test'], (0o100755, b'test loader payload'))
        for name in ('tools/install_fedora.py', 'tools/mount_installer_usb.py', 'lapis/accounts.py',
                     'assets/live/install-fedora', 'assets/live/lapis-usb-backup.service', 'README.md',
                     'LICENSE', 'NOTICE.md', 'tools/LICENSE.chromium', 'tools/LICENSE.apache-2.0',
                     'vendor/avb/LICENSE'):
            self.assertIn('lapis-kit/' + name, files)
        self.assertFalse(any(name.endswith(('.pem', '.img', '.pyc')) for name in files))

    def test_modified_runtime_is_refused_and_extra_private_files_are_ignored(self):
        with tempfile.TemporaryDirectory() as tmp:
            vendor = Path(tmp) / 'vendor'
            for relative, data in vendor_payload(ROOT / 'build/sources', ROOT).items():
                destination = vendor / relative
                destination.parent.mkdir(parents=True, exist_ok=True)
                destination.write_bytes(data)
            (vendor / 'avb/private.pem').write_text('must not be packaged')
            files = vendor_payload(vendor, ROOT)
            self.assertNotIn('avb/private.pem', files)
            with (vendor / 'avb/avbtool.py').open('a') as stream:
                stream.write('\n# modified\n')
            with self.assertRaisesRegex(RuntimeError, 'hash differs'):
                vendor_payload(vendor, ROOT)


class LoaderPrerequisiteTests(unittest.TestCase):
    def test_missing_or_non_executable_harness_has_setup_instructions(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            for present in (False, True):
                if present:
                    (root / 'build').mkdir()
                    (root / 'build/load_android_test').write_text('not executable')
                with self.assertRaisesRegex(RuntimeError, 'tools/build_fedora_usb.py') as error:
                    require_loader(root)
                self.assertIn('automatically', str(error.exception))
            loader = root / 'build/load_android_test'
            loader.chmod(0o755)
            self.assertEqual(require_loader(root), loader)

    def test_missing_tools_stop_both_modes_before_iso_or_output_work(self):
        for mode in ([], ['--without-installer']):
            with self.subTest(mode=mode), tempfile.TemporaryDirectory() as tmp:
                image = Path(tmp) / 'installer.img'
                with patch.object(sys, 'argv', ['build_fedora_usb.py', '--iso', 'missing.iso',
                                               '--output', str(image), *mode]), \
                        patch.object(build_fedora_usb, 'check_tools', side_effect=RuntimeError('missing tools')), \
                        patch.object(build_fedora_usb, 'sha256') as checksum, \
                        patch.object(build_fedora_usb, 'run') as command:
                    with self.assertRaisesRegex(RuntimeError, 'missing tools'):
                        build_fedora_usb.main()
                    checksum.assert_not_called()
                    command.assert_not_called()
                self.assertFalse(image.exists())
                self.assertFalse(image.with_suffix('.build').exists())

    def test_loader_build_explains_missing_toolchain_before_creating_output(self):
        with tempfile.TemporaryDirectory() as tmp:
            image = Path(tmp) / 'build/load_android_test'
            with patch.object(sys, 'argv', ['build_loader.py', '--output', str(image)]), \
                    patch.object(build_loader.shutil, 'which', return_value=None), \
                    patch.object(build_loader, 'run') as command:
                with self.assertRaisesRegex(RuntimeError, 'sudo dnf install gcc make'):
                    build_loader.main()
                command.assert_not_called()
            self.assertFalse(image.parent.exists())


if __name__ == '__main__':
    unittest.main()
