"""Exercise the one-command build without downloading sources or touching disks."""
from contextlib import redirect_stdout, redirect_stderr
import hashlib
import io
from pathlib import Path
import shutil
import struct
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from lapis.build_progress import BuildProgress
from lapis.common import read_json, sha256, runtime_sources
from lapis.gpt import read_gpt
from lapis.slots import boot_names
from tools import build_fedora_usb as builder, fetch_sources


class BuildCommandTests(unittest.TestCase):
    boot_members = (('vmlinuz', 'boot/x86_64/loader/linux'), ('initrd.img', 'boot/x86_64/loader/initrd'))

    def iso(self, root, label='Fedora-WS-Live-45'):
        iso = root / 'fedora.iso'
        data = bytearray(1048576)
        data[32768:32775] = b'\x01CD001\x01'
        data[32808:32840] = label.encode().ljust(32, b' ')
        iso.write_bytes(data)
        return iso

    def test_one_command_builds_prerequisites_then_publishes_a_compact_image(self):
        for version, mode in [('44', []), ('45', ['--without-installer']), ('46-1-2', [])]:
            with self.subTest(version=version, mode=mode), tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                iso = self.iso(root, 'Fedora-WS-Live-' + version)
                image = root / 'installer.img'
                # An old failed build must be retained and must not block retries.
                old = root / 'installer.build'
                old.mkdir()
                (old / 'marker').write_text('keep me')
                events = []
                output = io.StringIO()

                def fetch(sources, runner):
                    events.append('fetch')

                def compile_loader(sources, destination, runner):
                    events.append('compile')

                def verify_runtime(vendor, repository):
                    events.append('runtime')
                    self.assertEqual(vendor, root / 'cache')
                    return {}

                def extract(progress, argv, **kwargs):
                    events.append('extract')
                    if argv[-1] == self.boot_members[0][1]:
                        kernel = bytearray(0x238)
                        kernel[0x202:0x206] = b'HdrS'
                        struct.pack_into('<H', kernel, 0x206, 0x20c)
                        struct.pack_into('<H', kernel, 0x236, 1)
                        kwargs['stdout'].write(kernel)
                    else:
                        kwargs['stdout'].write(b'initramfs')

                def sign(kernel, initrd, cmdline, boot, vendor, **kwargs):
                    events.append('sign')
                    self.assertEqual(vendor, root / 'cache')
                    self.assertIn('runner', kwargs)
                    self.assertEqual(kwargs['kernel_sha256'], sha256(kernel))
                    boot.mkdir()
                    images = []
                    for name in boot_names('a'):
                        (boot / (name + '.img')).write_bytes(bytes(1048576))
                        images.append(dict(name=name, size=1048576))
                    return dict(slot='a', images=images)

                def validate(image, work, loader):
                    events.append('validate')
                    with image.open('rb') as disk:
                        self.assertEqual(len(read_gpt(disk, image.stat().st_size).partitions()), 6)

                checksum = ['--iso-sha256', sha256(iso).upper()] if version == '45' else []
                with patch.object(sys, 'argv', ['build_fedora_usb.py', '--iso', str(iso), '--output', str(image),
                                              '--sources', str(root / 'cache'), *mode, *checksum]), \
                        patch.object(builder, 'live_boot_members', return_value=self.boot_members), \
                        patch.object(builder, 'check_tools'), \
                        patch.object(builder, 'fetch_sources', side_effect=fetch), \
                        patch.object(builder, 'vendor_payload', side_effect=verify_runtime), \
                        patch.object(builder, 'build_loader', side_effect=compile_loader), \
                        patch.object(builder, 'require_loader', return_value=root / 'loader'), \
                        patch.object(builder, 'kit_payload', return_value={}) as kit, \
                        patch.object(BuildProgress, 'run', extract), \
                        patch.object(builder, 'build_set', side_effect=sign), \
                        patch.object(builder, 'validate_live_image', side_effect=validate), \
                        redirect_stdout(output):
                    builder.main()
                self.assertEqual(events, ['fetch', 'runtime', 'compile', 'extract', 'extract', 'sign', 'validate'])
                self.assertEqual(kit.call_count, 0 if mode else 1)
                if not mode:
                    kit.assert_called_once_with(root / 'cache')
                for step in range(1, 9):
                    self.assertIn(f'[{step}/8]', output.getvalue())
                self.assertIn('100%', output.getvalue())
                manifest = read_json(image.with_suffix('.manifest.json'))
                self.assertEqual(manifest['sha256'], sha256(image))
                self.assertEqual(manifest['iso_sha256'], sha256(iso))
                self.assertEqual(manifest['iso_volume_label'], 'Fedora-WS-Live-' + version)
                self.assertIn('root=live:CDLABEL=Fedora-WS-Live-' + version, manifest['cmdline'])
                self.assertEqual(manifest['bundled_kit'], not bool(mode))
                self.assertTrue(manifest['backup_partition']['deferred'])
                self.assertLess(image.stat().st_size, 16 * 1024**2)
                self.assertTrue(image.with_suffix('.sha256').read_text().startswith(sha256(image)))
                self.assertEqual((old / 'marker').read_text(), 'keep me')
                self.assertEqual(len(list(root.glob('installer-*.build/build.log'))), 1)

    def test_wrong_iso_never_downloads_or_creates_a_build_directory(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            iso = self.iso(root, 'Fedora-S-Live-45')
            image = root / 'installer.img'
            with patch.object(sys, 'argv', ['build_fedora_usb.py', '--iso', str(iso), '--output', str(image)]), \
                    patch.object(builder, 'check_tools'), \
                    patch.object(builder, 'fetch_sources') as fetch, redirect_stdout(io.StringIO()):
                with self.assertRaisesRegex(RuntimeError, 'Workstation live ISO'):
                    builder.main()
            fetch.assert_not_called()
            self.assertEqual(list(root.iterdir()), [iso])

    def test_optional_iso_checksum_mismatch_stops_before_downloads(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            iso = self.iso(root)
            with patch.object(sys, 'argv', ['builder', '--iso', str(iso), '--output', str(root / 'output.img'),
                                            '--iso-sha256', '0' * 64]), \
                    patch.object(builder, 'check_tools'), patch.object(builder, 'fetch_sources') as fetch, \
                    redirect_stdout(io.StringIO()):
                with self.assertRaisesRegex(RuntimeError, 'ISO checksum differs'):
                    builder.main()
            fetch.assert_not_called()
            self.assertEqual(list(root.iterdir()), [iso])

    def test_fetch_failure_retains_log_and_does_not_compile_or_publish(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            iso = self.iso(root)
            image = root / 'installer.img'

            def fail(sources, runner):
                runner([sys.executable, '-c', 'import sys; print("download failed"); sys.exit(3)'])

            with patch.object(sys, 'argv', ['build_fedora_usb.py', '--iso', str(iso), '--output', str(image)]), \
                    patch.object(builder, 'live_boot_members', return_value=self.boot_members), \
                    patch.object(builder, 'check_tools'), \
                    patch.object(builder, 'fetch_sources', side_effect=fail), \
                    patch.object(builder, 'build_loader') as compile_loader, \
                    redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
                with self.assertRaises(subprocess.CalledProcessError):
                    builder.main()
            compile_loader.assert_not_called()
            self.assertFalse(image.exists())
            self.assertFalse(image.with_suffix('.manifest.json').exists())
            log = next(root.glob('installer-*.build/build.log'))
            self.assertIn('download failed', log.read_text())


class ProgressTests(unittest.TestCase):
    def test_slow_commands_emit_heartbeat_and_keep_details_in_log(self):
        with tempfile.TemporaryFile(mode='w+') as log:
            output = io.StringIO()
            progress = BuildProgress(log=log, stream=output, interval=0.01)
            with progress.stage(2, 8, 'Downloading sources'):
                progress.run([sys.executable, '-c', 'import time; print("verbose tool output"); time.sleep(.05)'])
            self.assertIn('working', output.getvalue())
            self.assertIn('done', output.getvalue())
            self.assertNotIn('verbose tool output', output.getvalue())
            log.seek(0)
            self.assertIn('verbose tool output', log.read())

    def test_hash_progress_measures_all_bytes_and_keeps_digest_correct(self):
        with tempfile.TemporaryDirectory() as tmp:
            source = Path(tmp) / 'data'
            data = b'x' * (1024**2 + 17)
            source.write_bytes(data)
            updates = []
            digest = sha256(source, progress=lambda done, total: updates.append((done, total)))
            self.assertEqual(digest, hashlib.sha256(data).hexdigest())
            self.assertEqual(updates[-1], (len(data), len(data)))
            self.assertGreater(len(updates), 1)


@unittest.skipUnless(shutil.which('git'), 'Git is required')
class SourceRetryTests(unittest.TestCase):
    def test_runtime_lookup_supports_checkout_and_offline_installed_kit(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self.assertEqual(runtime_sources(root), root / 'build/sources')
            (root / 'vendor').mkdir()
            self.assertEqual(runtime_sources(root), root / 'vendor')

    def test_retry_resumes_an_interrupted_fetch_without_touching_other_files(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            origin = root / 'origin'
            subprocess.run(['git', 'init', '-q', str(origin)], check=True)
            (origin / 'source').write_text('pinned source')
            subprocess.run(['git', '-C', str(origin), 'add', 'source'], check=True)
            subprocess.run(['git', '-C', str(origin), '-c', 'user.name=Test', '-c', 'user.email=test@example.invalid',
                            'commit', '-qm', 'source'], check=True)
            revision = subprocess.check_output(['git', '-C', str(origin), 'rev-parse', 'HEAD'], text=True).strip()
            cache = root / 'cache'

            def interrupted(argv):
                if 'fetch' in argv:
                    raise RuntimeError('network interrupted')
                return subprocess.run(list(map(str, argv)), check=True, capture_output=True)

            def quiet(argv):
                return subprocess.run(list(map(str, argv)), check=True, capture_output=True)

            with patch.object(fetch_sources, 'SOURCES', {'sample': (str(origin), revision)}):
                with self.assertRaisesRegex(RuntimeError, 'network interrupted'):
                    fetch_sources.fetch_sources(cache, runner=interrupted)
                fetch_sources.fetch_sources(cache, runner=quiet)
                self.assertEqual((cache / 'sample/source').read_text(), 'pinned source')
                # A populated, modified checkout must never be reset on retry.
                (cache / 'sample/source').write_text('local edits')
                with self.assertRaisesRegex(RuntimeError, 'modified source'):
                    fetch_sources.fetch_sources(cache, runner=quiet)
                self.assertEqual((cache / 'sample/source').read_text(), 'local edits')


if __name__ == '__main__':
    unittest.main()
